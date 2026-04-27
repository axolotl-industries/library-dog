import asyncio, html as htmllib, os, re, secrets, sys, time, uuid
from pathlib import Path
import uvicorn
from typing import Optional
from fastapi import FastAPI, Request, Body, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from core import (
    MetadataFetcher, ScraperEngine, Downloader, ProwlarrClient, SabnzbdClient, QbitClient,
    GutenbergClient, flatten_downloads, hardlink_books_to_root,
)

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
async def author_books(author_id: str, author_name: str, query: str = None,
                       mode: str = "strict", u: str = Depends(current_user)):
    fetcher = MetadataFetcher()
    try:
        books = await fetcher.get_author_books(author_id, author_name, query, mode=mode)
        return {"author": author_name, "books": books}
    finally:
        await fetcher.aclose()


@app.get("/indexers")
async def indexers(u: str = Depends(current_user)):
    """List Prowlarr indexers so the UI can show enable/priority controls."""
    client = ProwlarrClient(os.getenv('PROWLARR_URL'), os.getenv('PROWLARR_KEY'), lambda _: None)
    if not client.configured():
        return {"indexers": []}
    return {"indexers": await client.list_indexers()}


def _parse_indexer_ids(raw) -> Optional[list]:
    """Accept either a CSV string or a list of ints; return list[int] or None."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, list):
        out = []
        for v in raw:
            try: out.append(int(v))
            except (TypeError, ValueError): pass
        return out or None
    if isinstance(raw, str):
        out = []
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk: continue
            try: out.append(int(chunk))
            except ValueError: pass
        return out or None
    return None


_VALID_FORMATS = {"epub", "mobi", "azw3", "pdf"}

def _parse_formats(raw) -> Optional[list]:
    """CSV string or list of strings → ordered list of valid format names, or None."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        chunks = [c.strip().lower() for c in raw.split(",") if c.strip()]
    elif isinstance(raw, list):
        chunks = [str(c).strip().lower() for c in raw if str(c).strip()]
    else:
        return None
    out = [c for c in chunks if c in _VALID_FORMATS]
    return out or None


@app.get("/candidates")
async def candidates(author: str, title: str, indexer_ids: str = "", formats: str = "",
                     u: str = Depends(current_user)):
    client = ProwlarrClient(os.getenv('PROWLARR_URL'), os.getenv('PROWLARR_KEY'), lambda _: None)
    results = await client.search(
        author, title,
        indexer_ids=_parse_indexer_ids(indexer_ids),
        formats=_parse_formats(formats),
    )
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
TORRENT_DIR = os.getenv("QBIT_SAVE_PATH", "/app/torrents")
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

    indexer_ids = _parse_indexer_ids(data.get('indexer_ids'))
    formats = _parse_formats(data.get('formats'))
    prowlarr = ProwlarrClient(os.getenv('PROWLARR_URL'), os.getenv('PROWLARR_KEY'), log)
    sab = SabnzbdClient(os.getenv('SABNZBD_URL'), os.getenv('SABNZBD_KEY'), log)
    qbit = QbitClient(
        os.getenv('QBIT_URL'),
        os.getenv('QBIT_USER', ''),
        os.getenv('QBIT_PASS', ''),
        TORRENT_DIR,
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

            # 1. Indexers (Prowlarr aggregated) — route NZB→SAB, torrent→qBit.
            #    If the user pre-selected a specific candidate from /candidates, use it directly.
            if not (_library_epubs() - before):
                if prowlarr.configured():
                    if b.get('nzb_url'):
                        candidates = [{'link': b['nzb_url'], 'kind': b.get('kind', 'nzb')}]
                    else:
                        candidates = await prowlarr.search(data['author'], b['title'],
                                                            indexer_ids=indexer_ids,
                                                            formats=formats)
                    for cand in candidates[:MAX_INDEXER_TRIES]:
                        kind = cand.get('kind', 'nzb')
                        title = f"{data['author']} - {b['title']}"
                        if kind == 'nzb':
                            job = await sab.add_url(cand['link'], title)
                            poll = sab.check_status
                        else:
                            if not qbit.configured():
                                log("Skipping torrent result — qBittorrent not configured.")
                                continue
                            job = await qbit.add(cand['link'], title)
                            poll = qbit.check_status
                        if not job:
                            continue
                        final_status = "unknown"
                        while True:
                            final_status = await poll(job)
                            if final_status in ("completed", "failed", "unknown"):
                                break
                            await asyncio.sleep(5)

                        if kind == 'nzb':
                            # SAB: clear the history entry (non-destructive — files stay)
                            # and flatten everything that landed in DOWNLOAD_DIR up to root.
                            await sab.delete_from_history(job)
                            flatten_downloads(DOWNLOAD_DIR, log)
                        else:
                            # qBit: hardlink the book up to the library root so CWA picks
                            # it up; leave the torrent in qBit to keep seeding (private
                            # tracker users would not thank us for ratio-tanking them).
                            # On confirmed failure, evict the torrent + files so qBit
                            # isn't left with dead entries; on 'unknown', leave it alone
                            # in case it's still progressing.
                            if final_status == "completed":
                                hardlink_books_to_root(TORRENT_DIR, DOWNLOAD_DIR, log)
                            elif final_status == "failed":
                                await qbit.delete(job, delete_files=True)
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
        # Final pass for anything dropped into DOWNLOAD_DIR by the SAB / Gutenberg /
        # mirrors paths. Skipped for torrents — those stay where qBit put them,
        # surfaced via hardlink already.
        flatten_downloads(DOWNLOAD_DIR, log)
        hardlink_books_to_root(TORRENT_DIR, DOWNLOAD_DIR, log)
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
