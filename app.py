import asyncio, html as htmllib, os, re, secrets, sys, time, uuid
from pathlib import Path
import uvicorn
from typing import Optional
from fastapi import FastAPI, Request, Body, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from core import MetadataFetcher, ScraperEngine, Downloader, NewznabScraper, SabnzbdClient, QbitClient, GutenbergClient, flatten_downloads

app = FastAPI()


# --- Session middleware ---

SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_urlsafe(32)
if not os.getenv("SESSION_SECRET"):
    print("[library-dog] WARN: SESSION_SECRET is unset; sessions will be invalidated on every "
          "container restart. Set a long random string in docker-compose.yml.", file=sys.stderr)

# Cookies are flagged Secure (HTTPS-only) when SESSION_COOKIE_SECURE is truthy. Default off so
# direct LAN access over http:// still works; turn it on once you're only reaching Library Dog
# through Cloudflare / a TLS reverse proxy.
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes")

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="library_dog_session",
    same_site="lax",
    https_only=SESSION_COOKIE_SECURE,
    max_age=7 * 24 * 3600,
)


# --- Auth backend ---
#
# Three modes, picked by env:
#   AUTH_PASSWORD set                       → form login, single shared password.
#   TRUSTED_PROXY_AUTH=true                 → trust Remote-User / X-Forwarded-User from a
#                                             reverse proxy (Authelia, Authentik, traefik
#                                             forward-auth). Library Dog MUST only be
#                                             reachable via that proxy or the header is
#                                             trivially spoofable.
#   neither                                 → fully open. Fine for a private LAN; fatal
#                                             on the public internet. We log a warning.
#
# The two are not mutually exclusive: you can set AUTH_PASSWORD as a fallback for direct
# access while letting proxy-authed users skip the login page entirely.

AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "")
AUTH_USERNAME_DEFAULT = os.getenv("AUTH_USERNAME", "user")
TRUSTED_PROXY_AUTH = os.getenv("TRUSTED_PROXY_AUTH", "").lower() in ("1", "true", "yes")

if not AUTH_PASSWORD and not TRUSTED_PROXY_AUTH:
    print("[library-dog] WARN: no auth configured (AUTH_PASSWORD unset, TRUSTED_PROXY_AUTH=false). "
          "Every route is open. Set AUTH_PASSWORD or run behind an auth-enforcing reverse proxy "
          "before exposing this beyond a trusted LAN.", file=sys.stderr)


def _proxy_user(request: Request) -> Optional[str]:
    """Username from a trusted reverse proxy's forward-auth header, or None."""
    if not TRUSTED_PROXY_AUTH:
        return None
    for h in ("Remote-User", "X-Forwarded-User", "X-Authentik-Username"):
        v = request.headers.get(h)
        if v:
            return v
    return None


def current_user(request: Request) -> str:
    proxy = _proxy_user(request)
    if proxy:
        # Cache in the session so EventSource / later requests still resolve a user even
        # if the proxy header is dropped on a particular request. Cheap, correct.
        request.session["user"] = proxy
        return proxy
    user = request.session.get("user")
    if user:
        return user
    if not AUTH_PASSWORD and not TRUSTED_PROXY_AUTH:
        return "anonymous"
    raise HTTPException(status_code=401, detail="Unauthorized")


# --- Job store ---

class JobStore:
    def __init__(self):
        self.jobs = {}
        self.tasks = {}

    def add_log(self, job_id, msg):
        if job_id not in self.jobs:
            self.jobs[job_id] = {'logs': [], 'status': 'running', 'created': time.time()}
        self.jobs[job_id]['logs'].append(msg)
        sys.stdout.write(f"[{job_id}] {msg}\n"); sys.stdout.flush()


JOBS = JobStore()


# --- Login / logout ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None):
    # No password configured → no login page to show.
    if not AUTH_PASSWORD:
        return RedirectResponse("/", status_code=303)
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    with open("static/login.html") as f:
        html = f.read()
    err_html = f'<div class="error">{htmllib.escape(error)}</div>' if error else ""
    return html.replace("<!-- ERROR -->", err_html)


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...), username: str = Form("")):
    if not AUTH_PASSWORD:
        return RedirectResponse("/", status_code=303)
    if secrets.compare_digest(password, AUTH_PASSWORD):
        request.session["user"] = username.strip() or AUTH_USERNAME_DEFAULT
        return RedirectResponse("/", status_code=303)
    print("[library-dog] login failed", file=sys.stderr)
    await asyncio.sleep(0.5)  # minor speed bump for credential stuffing
    return RedirectResponse("/login?error=Invalid+password", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    if AUTH_PASSWORD:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/", status_code=303)


# --- App routes ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    proxy = _proxy_user(request)
    if proxy:
        request.session["user"] = proxy
    elif AUTH_PASSWORD and not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    # else: existing session OR fully open (no AUTH_PASSWORD, no TRUSTED_PROXY_AUTH).
    with open("static/index.html") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/whoami")
async def whoami(u: str = Depends(current_user)):
    return {"user": u}


@app.get("/search")
async def search(author: str, query: str = None, u: str = Depends(current_user)):
    fetcher = MetadataFetcher()
    try:
        authors = await fetcher.search_author(author)
        if not authors:
            return {"error": "Author not found"}
        return {"authors": authors}
    finally:
        await fetcher.aclose()


@app.get("/author_books")
async def author_books(author_id: str, author_name: str, query: str = None, u: str = Depends(current_user)):
    fetcher = MetadataFetcher()
    try:
        books = await fetcher.get_author_books(author_id, author_name, query)
        return {"author": author_name, "books": books}
    finally:
        await fetcher.aclose()


@app.get("/candidates")
async def candidates(author: str, title: str, u: str = Depends(current_user)):
    usenet = NewznabScraper(os.getenv('PROWLARR_URL'), os.getenv('PROWLARR_KEY'), lambda _: None)
    results = await usenet.search(author, title)
    return {"candidates": results}


@app.post("/start_job")
async def start_job(data: dict = Body(...), u: str = Depends(current_user)):
    job_id = str(uuid.uuid4())
    JOBS.jobs[job_id] = {'logs': ["Initializing job..."], 'status': 'running', 'created': time.time()}
    task = asyncio.create_task(run_background_download(job_id, data))
    JOBS.tasks[job_id] = task
    return {"job_id": job_id}


@app.post("/stop_job/{job_id}")
async def stop_job(job_id: str, u: str = Depends(current_user)):
    task = JOBS.tasks.get(job_id)
    if task:
        task.cancel()
        JOBS.add_log(job_id, "JOB_CANCELLED_BY_USER")
        if job_id in JOBS.jobs:
            JOBS.jobs[job_id]['status'] = 'cancelled'
        return {"status": "ok"}
    return {"error": "Job not found"}


DOWNLOAD_DIR = "/app/downloads"
MAX_INDEXER_TRIES = 3
QBIT_CATEGORY = os.getenv("QBIT_CATEGORY", "books")

# Anna's Archive and Libgen scraping. Off by default — these sources are legally grey
# in many jurisdictions and the scraping path drags in Playwright/Chromium. Project
# Gutenberg + Newznab indexers stay always-on regardless.
ENABLE_GREY_SOURCES = os.getenv("ENABLE_GREY_SOURCES", "").lower() in ("1", "true", "yes")


def _library_epubs() -> set:
    base = Path(DOWNLOAD_DIR)
    return {p.name for p in base.glob('*.epub')} if base.is_dir() else set()


async def run_background_download(job_id, data):
    def log(m): JOBS.add_log(job_id, m)

    usenet = NewznabScraper(os.getenv('PROWLARR_URL'), os.getenv('PROWLARR_KEY'), log)
    sab = SabnzbdClient(os.getenv('SABNZBD_URL'), os.getenv('SABNZBD_KEY'), log)
    qbit = QbitClient(
        os.getenv('QBIT_URL'),
        os.getenv('QBIT_USER', ''),
        os.getenv('QBIT_PASS', ''),
        os.getenv('QBIT_SAVE_PATH', DOWNLOAD_DIR),
        QBIT_CATEGORY,
        log,
    )
    scraper = ScraperEngine(log) if ENABLE_GREY_SOURCES else None
    downloader = Downloader(DOWNLOAD_DIR, log)
    gutenberg = GutenbergClient()

    if scraper:
        await scraper.start()
    try:
        for b in data['books']:
            log(f"Searching for '{b['title']}'...")
            before = _library_epubs()

            # 0. Project Gutenberg — public domain books come straight from the source;
            #    if found, skip Usenet and mirrors entirely.
            gut_url = await gutenberg.find_epub(data['author'], b['title'], log)
            if gut_url:
                await downloader.download("Project Gutenberg", gut_url, data['author'], b['title'], b)

            # 1. Indexer (Newznab) — try Prowlarr if configured; route NZB→SAB, torrent→qBit.
            #    If the user pre-selected a specific candidate from /candidates, use it directly.
            if not (_library_epubs() - before):
                if os.getenv('PROWLARR_URL') and os.getenv('PROWLARR_KEY'):
                    if b.get('nzb_url'):
                        candidates = [{'link': b['nzb_url'], 'kind': b.get('kind', 'nzb')}]
                    else:
                        candidates = await usenet.search(data['author'], b['title'])
                    for cand in candidates[:MAX_INDEXER_TRIES]:
                        kind = cand.get('kind', 'nzb')
                        title = f"{data['author']} - {b['title']}"
                        if kind == 'nzb':
                            job = await sab.add_url(cand['link'], title)
                            poll, cleanup = sab.check_status, sab.delete_from_history
                        else:
                            if not qbit.configured():
                                log("Skipping torrent result — qBittorrent not configured.")
                                continue
                            job = await qbit.add(cand['link'], title)
                            poll, cleanup = qbit.check_status, qbit.delete
                        if not job:
                            continue
                        while True:
                            status = await poll(job)
                            if status in ("completed", "failed", "unknown"):
                                break
                            await asyncio.sleep(5)
                        await cleanup(job)
                        flatten_downloads(DOWNLOAD_DIR, lambda _: None)
                        if _library_epubs() - before:
                            break

            # 2. Mirrors (Anna's Archive / Libgen) — opt-in via ENABLE_GREY_SOURCES.
            if scraper and not (_library_epubs() - before):
                mirrors = await scraper.get_mirrors(data['author'], b['title'], b['isbns'])
                for name, url in mirrors:
                    if await downloader.download(name, url, data['author'], b['title'], b):
                        break

            # Check if the specific book we wanted (or at least something new) exists
            safe_t = re.sub(r'[\\/*?:"<>|]', "", b['title']).lower()
            current_files = _library_epubs()
            new_files = current_files - before
            
            success = False
            for f in new_files:
                if safe_t in f.lower():
                    success = True
                    break
            
            if success:
                log(f"SUCCESS: {b['title']}")
            else:
                # Fallback: if we can't be sure it's OUR file, but the downloader returned True
                # or SABnzbd finished, we still trust the process.
                if new_files:
                    log(f"SUCCESS: {b['title']}")
                else:
                    log(f"FAILED: {b['title']}")
    except asyncio.CancelledError:
        log("STOPPING: Job was cancelled.")
        raise
    finally:
        flatten_downloads(DOWNLOAD_DIR, lambda _: None)
        if scraper:
            await scraper.stop()
        if job_id in JOBS.jobs:
            if JOBS.jobs[job_id]['status'] == 'running':
                JOBS.jobs[job_id]['status'] = 'complete'
        log("JOB_COMPLETE")
        if job_id in JOBS.tasks: del JOBS.tasks[job_id]


@app.get("/stream/{job_id}")
async def stream(request: Request, job_id: str, last_idx: int = 0, u: str = Depends(current_user)):
    # EventSource can't set custom Authorization headers, but it sends cookies and any
    # headers a reverse proxy injects (Remote-User, X-Forwarded-User), so current_user
    # resolves correctly in both password and forward-auth modes.

    async def generator():
        idx = last_idx
        while True:
            job = JOBS.jobs.get(job_id)
            if not job: break
            while idx < len(job['logs']):
                yield f"data: {job['logs'][idx]}\n\n"
                idx += 1
            if job['status'] in ['complete', 'cancelled']: break
            yield ": heartbeat\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(generator(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=80)
