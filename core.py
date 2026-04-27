import os
import re
import asyncio
import httpx
import ssl
import ebookmeta
import zipfile
import sys
import shutil
import unicodedata
from pathlib import Path

from typing import List, Dict, Optional, Tuple, Generator, Callable
from bs4 import BeautifulSoup
# Playwright is only imported lazily inside ScraperEngine — it's only present
# in the 'grey' image variant (built with INSTALL_PLAYWRIGHT=true). Importing
# it at module scope would crash the standard image on startup even when
# nobody's asked for grey sources.
from urllib.parse import quote, urljoin

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Supported book formats. Order in this dict is the canonical fallback priority
# when the user hasn't specified one (EPUB first because it's the lingua franca
# of the *arr / CWA ecosystem and metadata embedding works there).
FORMAT_MARKERS: Dict[str, Tuple[str, ...]] = {
    "epub": (" epub", ".epub"),
    "mobi": (" mobi", ".mobi"),
    "azw3": (" azw3", ".azw3", " azw", ".azw"),
    "pdf":  (" pdf", ".pdf"),
}
ALL_FORMATS = tuple(FORMAT_MARKERS.keys())
BOOK_EXTENSIONS = {f".{f}" for f in ALL_FORMATS}

# Things that, in a release title, mark it as definitely-not-a-book (a movie,
# audiobook, etc.) regardless of whether a book format is also mentioned.
NON_BOOK_MARKERS: Tuple[str, ...] = (
    " mp4", " avi", " mkv", " m4v", " wmv", ".mp4", ".avi", ".mkv",
    " hdtv", " 1080p", " 720p", " 2160p", " x264", " x265", " h264", " h265",
    " bluray", " blu-ray", " dvdrip", " bdrip", " webrip", " web-dl", " hdrip",
    " mp3", " m4a", " m4b", " flac", " wav", " ogg", ".mp3", ".m4b", ".flac",
    " audiobook", " audio book", " audiobk",
)


def detect_format(title: str) -> Optional[str]:
    """Return 'epub'/'mobi'/'azw3'/'pdf' if the release title declares one,
    else None."""
    t = f" {(title or '').lower()} "
    for fmt, markers in FORMAT_MARKERS.items():
        if any(m in t for m in markers):
            return fmt
    return None

def _ascii_fold(text: str) -> str:
    """Strip diacritical marks for indexer/library search queries (å→a, ø→o, ü→u, etc.)."""
    return ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')


_NUM_WORDS = {
    'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5',
    'six': '6', 'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10',
    'eleven': '11', 'twelve': '12',
}


def normalize_text(text: str) -> str:
    if not text: return ""
    # NFKC folds compatibility forms and replaces e.g. NBSP with regular space.
    t = unicodedata.normalize('NFKC', text)
    # Drop zero-width / format characters (Cf) — OpenLibrary data sometimes carries
    # U+200B between words, which would otherwise fuse them into a single token.
    t = ''.join(c for c in t if unicodedata.category(c) != 'Cf')
    t = t.lower()
    # Strip diacritics so accented names match ASCII variants in indexers (Knausgård → knausgard).
    t = unicodedata.normalize('NFD', t)
    t = ''.join(c for c in t if unicodedata.category(c) != 'Mn')
    # Replace dots, underscores, and common punctuation with spaces BEFORE stripping
    t = re.sub(r'[\._\-]', ' ', t)
    # Strip subtitles and parentheses
    t = t.split(':')[0].split('(')[0]
    # Clean up common prefixes
    t = re.sub(r'^the\s+|^a\s+|^an\s+', '', t)
    # Clean up common cruft suffixes
    t = re.sub(r'\[\d+/\d+\]|\(part \d+\)', '', t)
    # Make & and "and" equivalent for matching (so "Nightmares & Dreamscapes" == "Nightmares and Dreamscapes")
    t = t.replace('&', ' and ')
    # Remove remaining non-alphanumeric (except spaces)
    t = re.sub(r'[^\w\s]', '', t)
    # Normalise number words: "book two" → "book 2", "volume three" → "volume 3"
    t = ' '.join(_NUM_WORDS.get(w, w) for w in t.split())
    return " ".join(t.split())


def _query_title(title: str) -> str:
    """Build an indexer/mirror search query from a book title.

    Preserves the subtitle (colon kept as space) and normalises number words so
    'My Struggle: Book Two' becomes 'My Struggle Book 2' — matching NZB titles
    like 'My.Struggle.Book.2' without collapsing into the bare 'My Struggle'
    that would match every volume in the series.
    """
    t = _ascii_fold(title).lower()
    t = re.sub(r'[\._\-:]', ' ', t)
    t = re.sub(r'[^\w\s]', '', t)
    t = ' '.join(_NUM_WORDS.get(w, w) for w in t.split())
    return " ".join(t.split())


def hardlink_books_to_root(src_dir: str, dest_dir: str, log: Callable = print) -> None:
    """Surface every book file under src_dir as a hardlink in dest_dir.

    Used after a torrent completes: qBittorrent keeps seeding from src_dir,
    Calibre-Web-Automated picks up the hardlinked copy from dest_dir. No files
    are moved or deleted — same inode, two names.

    Falls back to a regular copy when hardlinking fails (cross-filesystem mount,
    fs without hardlink support). The seed survives either way.
    """
    src = Path(src_dir)
    dest = Path(dest_dir)
    if not src.is_dir():
        return
    dest.mkdir(parents=True, exist_ok=True)
    for path in src.rglob('*'):
        if not path.is_file():
            continue
        if path.suffix.lower() not in BOOK_EXTENSIONS:
            continue
        target = dest / path.name
        if target.exists():
            # Already surfaced from a prior pass. Don't disambiguate; that just
            # accumulates duplicate-but-suffixed entries in the library.
            continue
        try:
            os.link(str(path), str(target))
            log(f"Hardlinked {path.name} into the library root")
        except OSError as e:
            try:
                shutil.copy2(str(path), str(target))
                log(f"Copied {path.name} into the library root (hardlink unavailable: {e})")
            except Exception as e2:
                log(f"Failed to surface {path.name}: {e2}")


def flatten_downloads(base_dir: str, log: Callable = print) -> None:
    """Make base_dir a flat directory containing only book files.

    Moves every nested book file (any extension in BOOK_EXTENSIONS) up to
    base_dir (disambiguating on collision), then deletes everything else —
    subfolders, .nfo, .mp4, cover images, whatever SABnzbd or a multi-file
    NZB left behind.
    """
    base = Path(base_dir)
    if not base.is_dir():
        return

    for book in base.rglob('*'):
        if not book.is_file():
            continue
        if book.suffix.lower() not in BOOK_EXTENSIONS:
            continue
        if book.parent == base:
            continue
        dest = base / book.name
        if dest.exists():
            stem, suffix = book.stem, book.suffix
            i = 1
            while True:
                candidate = base / f"{stem} ({i}){suffix}"
                if not candidate.exists():
                    dest = candidate
                    break
                i += 1
        try:
            shutil.move(str(book), str(dest))
            log(f"Moved {book.name} to downloads root")
        except Exception as e:
            log(f"Failed to move {book.name}: {e}")

    for item in base.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item)
                log(f"Removed folder: {item.name}")
            elif item.suffix.lower() not in BOOK_EXTENSIONS:
                item.unlink()
                log(f"Removed non-book file: {item.name}")
        except Exception as e:
            log(f"Failed to clean {item.name}: {e}")

def create_robust_ssl_context():
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    except:
        return ssl._create_unverified_context()

async def resolve_annas_domain(log_func: Callable) -> str:
    mirrors = ["https://annas-archive.se", "https://annas-archive.li", "https://annas-archive.gs"]
    for m in mirrors:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                if (await client.head(m)).status_code < 400: return m
        except: continue
    return "https://annas-archive.gl"

MAX_EPUB_BYTES = 50 * 1024 * 1024  # 50MB — EPUBs are small; anything bigger is a movie/audiobook/rar pack.


class ProwlarrClient:
    """Talks to Prowlarr's v1 API across all configured indexers.

    Two endpoints we care about:
      GET /api/v1/indexer   — list of indexers (id, name, protocol, enable).
      GET /api/v1/search    — aggregated search; takes indexerIds= to scope
                              and categories= for the Newznab cat code.

    Search results come back as JSON ReleaseResource objects with an
    explicit `protocol` field (`usenet` | `torrent`), so we can route
    cleanly without the magnet:/.torrent URL sniffing the old Newznab
    passthrough required.
    """

    BOOK_CATEGORY = "7020"  # Newznab Books > EBook.

    def __init__(self, base_url: str, api_key: str, log_func: Callable):
        # Tolerate the legacy per-indexer URL form (PROWLARR_URL=http://host:9696/15)
        # by stripping a trailing /<digits> segment so we land on the API root.
        url = (base_url or "").strip().rstrip('/')
        m = re.match(r"^(.+?)/\d+$", url)
        if m:
            url = m.group(1)
        self.base_url = url
        self.api_key = (api_key or "").strip()
        self.log = log_func

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Api-Key": self.api_key,
            "User-Agent": UA,
            "Accept": "application/json",
        }

    async def list_indexers(self) -> List[Dict]:
        """Return Prowlarr's indexer list as [{id, name, protocol, enable, priority}]."""
        if not self.configured():
            return []
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=False, follow_redirects=True,
                                         headers=self._headers()) as client:
                r = await client.get(f"{self.base_url}/api/v1/indexer")
                if r.status_code != 200:
                    self.log(f"Prowlarr indexer list HTTP {r.status_code}: {r.text[:300]}")
                    return []
                data = r.json()
                out = []
                for i in data:
                    iid = i.get("id")
                    if iid is None:
                        continue
                    out.append({
                        "id": int(iid),
                        "name": i.get("name", "?"),
                        "protocol": (i.get("protocol") or "unknown").lower(),
                        "enable": bool(i.get("enable", True)),
                        "priority": int(i.get("priority", 25)),
                    })
                return out
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log(f"Prowlarr indexer list error: {e}")
            return []

    async def search(self, author: str, title: str,
                     indexer_ids: Optional[List[int]] = None,
                     formats: Optional[List[str]] = None) -> List[Dict]:
        """Aggregated search across (optionally a subset of) Prowlarr's indexers.
        Returns release dicts already filtered by author/title/format/size,
        sorted by user's format priority then indexer priority."""
        if not self.configured():
            return []
        self.log(f"Querying indexers for '{title}'...")

        # Prowlarr's /api/v1/search expects array params as repeated query keys
        # (indexerIds=10&indexerIds=15), not a CSV — passing a list lets httpx
        # generate that form. Sending a CSV gets you a 400 with
        # "The value '10,15,20' is not valid."
        params: List[Tuple[str, object]] = [
            ("query", _query_title(title)),
            ("type", "search"),
            ("categories", int(self.BOOK_CATEGORY)),
            ("limit", 100),
        ]
        if indexer_ids:
            for iid in indexer_ids:
                params.append(("indexerIds", int(iid)))

        try:
            async with httpx.AsyncClient(timeout=20.0, verify=False, follow_redirects=True,
                                         headers=self._headers()) as client:
                r = await client.get(f"{self.base_url}/api/v1/search", params=params)
                if r.status_code != 200:
                    self.log(f"Prowlarr search HTTP {r.status_code} for url={r.request.url}: {r.text[:400]}")
                    return []
                releases = r.json()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log(f"Prowlarr search error: {e}")
            return []

        items: List[Dict] = []
        for rel in releases:
            protocol = (rel.get("protocol") or "").lower()
            magnet = rel.get("magnetUrl") or ""
            download = rel.get("downloadUrl") or ""
            # Prefer magnet for torrents (no proxy round-trip needed); else downloadUrl.
            link = magnet if (protocol == "torrent" and magnet) else download
            if not link:
                continue
            kind = "torrent" if (protocol == "torrent" or link.startswith("magnet:")) else "nzb"
            items.append({
                "title": rel.get("title", "") or "",
                "link": link,
                "size": int(rel.get("size") or 0),
                "kind": kind,
                "indexer_id": int(rel["indexerId"]) if rel.get("indexerId") is not None else 0,
                "indexer_name": rel.get("indexer", "?"),
            })
        return self._filter(items, author, title, indexer_ids, formats)

    def _filter(self, items: List[Dict], author: str, title: str,
                indexer_ids: Optional[List[int]],
                formats: Optional[List[str]]) -> List[Dict]:
        # Default to EPUB-only when caller didn't specify (backwards compatible).
        allowed = list(formats) if formats else ["epub"]
        allowed_set = set(allowed)
        results = []
        # Two-pass title match: subtitle-kept first, subtitle-stripped fallback. Stops
        # "My Struggle: Book 2" collapsing to "my struggle" and matching every volume.
        norm_title_full = normalize_text(title.replace(':', ' '))
        norm_title = normalize_text(title)
        author_parts = [p for p in normalize_text(author).split() if len(p) > 2]
        for item in items:
            res_title = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', item.get("title", ""))
            norm_res_title = normalize_text(res_title)
            title_match = norm_title_full in norm_res_title or norm_title in norm_res_title
            author_match = any(p in norm_res_title for p in author_parts) if author_parts else True
            if not (title_match and author_match):
                continue
            t = f" {res_title.lower()} "
            if any(m in t for m in NON_BOOK_MARKERS):
                continue
            declared = detect_format(res_title)
            if declared and declared not in allowed_set:
                continue
            # Pin the result's format: declared if known, else assume the user's
            # top preference (this is how the no-format-declared 'ambiguous'
            # case behaved historically — it was implicitly EPUB).
            fmt = declared or allowed[0]
            size = int(item.get("size") or 0)
            if size and size > MAX_EPUB_BYTES:
                continue
            results.append({
                "title": res_title,
                "link": item["link"],
                "size": size,
                "kind": item["kind"],
                "format": fmt,
                "indexer_id": item.get("indexer_id", 0),
                "indexer_name": item.get("indexer_name", "?"),
            })
        # Sort by format priority first, then indexer priority. Both fall back
        # to "end of list" for unknown values.
        fmt_order = {f: i for i, f in enumerate(allowed)}
        idx_order = {iid: pos for pos, iid in enumerate(indexer_ids or [])}
        results.sort(key=lambda r: (
            fmt_order.get(r["format"], len(fmt_order)),
            idx_order.get(r.get("indexer_id", 0), len(idx_order)),
        ))
        return results


def _fmt_size(n: int) -> str:
    if not n: return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}GB"

class SabnzbdClient:
    def __init__(self, url: str, api_key: str, log_func: Callable):
        self.url = url.strip().rstrip('/')
        self.api_key = api_key.strip()
        self.log = log_func

    async def add_url(self, nzb_url: str, title: str) -> Optional[str]:
        if not self.url or not self.api_key: return None
        params = {
            "mode": "addurl",
            "name": nzb_url,
            "nzbname": title,
            "cat": "books",
            "apikey": self.api_key,
            "output": "json"
        }
        try:
            async with httpx.AsyncClient(timeout=15.0, verify=False, follow_redirects=True, headers={"User-Agent": UA}) as client:
                resp = await client.get(f"{self.url}/api", params=params)
                data = resp.json()
                if data.get("status") and data.get("nzo_ids"):
                    nzo_id = data["nzo_ids"][0]
                    self.log(f"Added to download queue.")
                    return nzo_id
        except asyncio.CancelledError: raise
        except Exception as e:
            self.log(f"SABnzbd error: {e}")
        return None

    async def delete_from_history(self, nzo_id: str) -> None:
        if not self.url or not self.api_key: return
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=False, follow_redirects=True, headers={"User-Agent": UA}) as client:
                await client.get(f"{self.url}/api", params={
                    "mode": "history", "name": "delete",
                    "del_files": 0, "value": nzo_id,
                    "apikey": self.api_key, "output": "json",
                })
        except asyncio.CancelledError: raise
        except Exception: pass

    async def check_status(self, nzo_id: str) -> str:
        """Returns 'downloading', 'completed', 'failed', or 'unknown'"""
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=False, follow_redirects=True, headers={"User-Agent": UA}) as client:
                # 1. Check Queue
                resp = await client.get(f"{self.url}/api", params={"mode": "queue", "nzo_id": nzo_id, "apikey": self.api_key, "output": "json"})
                q_data = resp.json()
                slots = q_data.get("queue", {}).get("slots", [])
                for s in slots:
                    if s.get("nzo_id") == nzo_id: return "downloading"

                # 2. Check History
                resp = await client.get(f"{self.url}/api", params={"mode": "history", "nzo_id": nzo_id, "apikey": self.api_key, "output": "json"})
                h_data = resp.json()
                slots = h_data.get("history", {}).get("slots", [])
                for s in slots:
                    if s.get("nzo_id") == nzo_id:
                        status = s.get("status", "").lower()
                        if status == "completed": return "completed"
                        if "failed" in status: return "failed"
                        # In history with an unrecognised status (e.g. "Extracting") —
                        # keep polling rather than treating as terminal.
                        return "downloading"
        except asyncio.CancelledError: raise
        except Exception as e:
            self.log(f"SABnzbd status check error: {e}")
        return "unknown"


class QbitClient:
    """qBittorrent Web UI client.

    Configured to write into the same DOWNLOAD_DIR Library Dog watches, via the
    `savepath` param on /torrents/add. That requires the qBittorrent container
    to share the downloads volume with us — without that, qBit happily accepts
    the torrent and saves it somewhere we'll never see.

    qBit's API has a quirk: /torrents/add doesn't return the new infohash. We
    work around that by snapshotting hashes-in-category before the add and
    diffing afterwards.
    """

    # qBit state strings split into terminal/non-terminal buckets:
    # https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)#get-torrent-list
    _DONE_STATES = {"uploading", "stalledUP", "pausedUP", "queuedUP", "checkingUP", "forcedUP"}
    _FAIL_STATES = {"error", "missingFiles"}

    def __init__(self, url: str, username: str, password: str, save_path: str,
                 category: str, log_func: Callable):
        self.url = (url or "").strip().rstrip('/')
        self.username = username or ""
        self.password = password or ""
        self.save_path = save_path
        self.category = category or "books"
        self.log = log_func
        self._cookies = None

    def configured(self) -> bool:
        return bool(self.url)

    async def _login(self, client: httpx.AsyncClient) -> bool:
        if self._cookies:
            client.cookies = self._cookies
            return True
        try:
            r = await client.post(
                f"{self.url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                headers={"Referer": self.url},
            )
            if r.status_code == 200 and r.text.strip() == "Ok.":
                self._cookies = r.cookies
                return True
            self.log(f"qBittorrent login failed: HTTP {r.status_code}")
        except Exception as e:
            self.log(f"qBittorrent login error: {e}")
        return False

    async def add(self, url_or_magnet: str, title: str) -> Optional[str]:
        """Add a torrent or magnet. Returns the infohash, or None on failure."""
        if not self.configured():
            return None
        async with httpx.AsyncClient(timeout=20.0, verify=False, follow_redirects=True) as client:
            if not await self._login(client):
                return None

            try:
                r = await client.get(f"{self.url}/api/v2/torrents/info",
                                     params={"category": self.category})
                before = {t.get("hash") for t in r.json()} if r.status_code == 200 else set()
            except Exception:
                before = set()

            try:
                data = {
                    "urls": url_or_magnet,
                    "category": self.category,
                    "paused": "false",
                    "rename": title,
                }
                if self.save_path:
                    data["savepath"] = self.save_path
                r = await client.post(f"{self.url}/api/v2/torrents/add", data=data)
                if r.status_code != 200:
                    self.log(f"qBittorrent add error: HTTP {r.status_code}")
                    return None
                self.log("Added to torrent queue.")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"qBittorrent add error: {e}")
                return None

            # qBit can take a moment to register the new torrent; poll briefly.
            for _ in range(20):
                await asyncio.sleep(0.5)
                try:
                    r = await client.get(f"{self.url}/api/v2/torrents/info",
                                         params={"category": self.category})
                    if r.status_code == 200:
                        for t in r.json():
                            h = t.get("hash")
                            if h and h not in before:
                                return h
                except Exception:
                    pass
            self.log("qBittorrent add: torrent didn't appear in category — check qBit logs.")
            return None

    async def check_status(self, infohash: str) -> str:
        """Returns 'downloading', 'completed', 'failed', or 'unknown'."""
        if not self.configured() or not infohash:
            return "unknown"
        async with httpx.AsyncClient(timeout=10.0, verify=False, follow_redirects=True) as client:
            if not await self._login(client):
                return "unknown"
            try:
                r = await client.get(f"{self.url}/api/v2/torrents/info",
                                     params={"hashes": infohash})
                if r.status_code != 200:
                    return "unknown"
                items = r.json()
                if not items:
                    return "unknown"
                state = items[0].get("state", "")
                if state in self._DONE_STATES:
                    return "completed"
                if state in self._FAIL_STATES:
                    return "failed"
                return "downloading"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"qBittorrent status check error: {e}")
        return "unknown"

    async def delete(self, infohash: str, delete_files: bool = False) -> None:
        """Remove the torrent from qBit. We keep the files on disk by default —
        Library Dog has already moved the EPUB to the flat root by this point."""
        if not self.configured() or not infohash:
            return
        async with httpx.AsyncClient(timeout=10.0, verify=False, follow_redirects=True) as client:
            if not await self._login(client):
                return
            try:
                await client.post(
                    f"{self.url}/api/v2/torrents/delete",
                    data={"hashes": infohash, "deleteFiles": "true" if delete_files else "false"},
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                pass


# --- Bibliography fetchers ---

import random

class WikidataBibliography:
    """Uses Wikidata's SPARQL endpoint to get a canonical list of an author's works.
    This is highly authoritative and avoids the 'garbage' found in catch-all databases.
    """
    SEARCH_URL = "https://www.wikidata.org/w/api.php"
    SPARQL_URL = "https://query.wikidata.org/sparql"
    USER_AGENT = "LibraryDog/1.0 (https://github.com/axolotl-industries/library-dog)"

    def __init__(self, client: httpx.AsyncClient, log: Callable = lambda _: None):
        self.client = client
        self.log = log

    async def _get_author_id(self, author_name: str) -> Optional[str]:
        # Refined search to prefer authors/writers
        params = {
            "action": "wbsearchentities",
            "format": "json",
            "language": "en",
            "type": "item",
            "search": author_name
        }
        try:
            resp = await self.client.get(self.SEARCH_URL, params=params, headers={"User-Agent": self.USER_AGENT})
            results = resp.json().get("search", [])
            for res in results[:5]:
                desc = res.get("description", "").lower()
                # If the description mentions writer, author, novelist, or the person has a clear birth date
                if any(w in desc for w in ["author", "writer", "novelist", "philosopher", "poet", "creator"]):
                    return res["id"]
            
            if results:
                return results[0]["id"]
        except Exception as e:
            self.log(f"Wikidata search error: {e}")
        return None

    async def get_work_count(self, author_id: str) -> int:
        """Get a realistic work count from Wikidata."""
        query = f"""
        SELECT (COUNT(DISTINCT ?item) AS ?count) WHERE {{
          {{ ?item wdt:P50 wd:{author_id} }} UNION {{ ?item wdt:P98 wd:{author_id} }}
          ?item wdt:P31/wdt:P279* ?type .
          VALUES ?type {{ wd:Q7725634 wd:Q571 wd:Q47461344 wd:Q49084 wd:Q1144673 }}
        }}
        """
        try:
            resp = await self.client.get(self.SPARQL_URL, params={"query": query}, headers={"User-Agent": self.USER_AGENT, "Accept": "application/sparql-results+json"}, timeout=10.0)
            if resp.status_code == 200:
                return int(resp.json()["results"]["bindings"][0]["count"]["value"])
        except: pass
        return 0

    async def fetch(self, author_name: str, mode: str = "strict") -> List[Dict]:
        """Pull an author's bibliography from Wikidata.

        Two modes:
          'strict'     — only items typed as one of our known book-ish classes
                         (literary work / book / written work / short story /
                         diary, plus their P279 subclass closure). Cleaner
                         results, but Wikidata's class hierarchy is
                         inconsistent enough that some real books leak through
                         the cracks.
          'permissive' — drop the type filter, trust the credit relationship
                         (P50 author or P98 editor), and noise-filter
                         app-side: keep only items that have a publication
                         date (P577) or an ISBN (P212/P957). That's the
                         "this thing was published as a discrete book"
                         proxy.

        Both modes match anthology editors via P98 alongside straight
        authors via P50, so people like Ellen Datlow / Gardner Dozois
        whose careers are mostly anthology editing show up properly.
        """
        author_id = await self._get_author_id(author_name)
        if not author_id:
            return []

        self.log(f"Fetching bibliography for {author_name} ({mode} mode)...")

        if mode == "permissive":
            query = f"""
            SELECT DISTINCT ?item ?itemLabel ?date ?isbn WHERE {{
              {{ ?item wdt:P50 wd:{author_id} }} UNION {{ ?item wdt:P98 wd:{author_id} }}
              OPTIONAL {{ ?item wdt:P577 ?date . }}
              OPTIONAL {{ ?item wdt:P212 ?isbn . }}
              OPTIONAL {{ ?item wdt:P957 ?isbn . }}
              SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
            }}
            ORDER BY ?date
            """
        else:
            query = f"""
            SELECT DISTINCT ?item ?itemLabel ?date ?isbn WHERE {{
              {{ ?item wdt:P50 wd:{author_id} }} UNION {{ ?item wdt:P98 wd:{author_id} }}
              ?item wdt:P31/wdt:P279* ?type .
              VALUES ?type {{ wd:Q7725634 wd:Q571 wd:Q47461344 wd:Q49084 wd:Q1144673 }}
              OPTIONAL {{ ?item wdt:P577 ?date . }}
              OPTIONAL {{ ?item wdt:P212 ?isbn . }}
              OPTIONAL {{ ?item wdt:P957 ?isbn . }}
              SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
            }}
            ORDER BY ?date
            """

        headers = {"User-Agent": self.USER_AGENT, "Accept": "application/sparql-results+json"}
        try:
            resp = await self.client.get(self.SPARQL_URL, params={"query": query}, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                self.log(f"Wikidata SPARQL error: {resp.status_code}")
                return []

            data = resp.json()
            bindings = data.get("results", {}).get("bindings", [])

            books = {}
            for b in bindings:
                title = b.get("itemLabel", {}).get("value")
                if not title or re.match(r"^Q\d+$", title):
                    continue

                year_str = b.get("date", {}).get("value", "")
                year = int(year_str[:4]) if re.match(r"^\d{4}", year_str) else None
                isbn = b.get("isbn", {}).get("value", "").replace("-", "").strip() or None

                # Permissive-mode noise filter: skip anything without a publication
                # date OR an ISBN. That weeds out individual letters, blog posts,
                # forewords-to-other-people's-books, and the like.
                if mode == "permissive" and not year and not isbn:
                    continue

                norm = normalize_text(title)
                existing = books.get(norm)
                if not existing:
                    books[norm] = {"title": title, "year": year, "isbns": [isbn] if isbn else []}
                else:
                    if year and (existing["year"] is None or year < existing["year"]):
                        existing["year"] = year
                    if isbn and isbn not in existing["isbns"]:
                        existing["isbns"].append(isbn)

            return sorted(books.values(), key=lambda x: (x.get("year") or 9999, x.get("title", "")))
        except Exception as e:
            self.log(f"Wikidata SPARQL error: {e}")
            return []


class GoogleBooksBibliography:
    API = "https://www.googleapis.com/books/v1/volumes"
    
    def __init__(self, client: httpx.AsyncClient, log: Callable = lambda _: None):
        self.client = client
        self.log = log

    async def fetch(self, author_name: str) -> List[Dict]:
        """Simple fallback fetcher."""
        try:
            resp = await self.client.get(self.API, params={
                "q": f'inauthor:"{author_name}"',
                "maxResults": 40,
                "langRestrict": "en",
                "printType": "books",
            })
            if resp.status_code != 200: return []
            items = resp.json().get("items") or []
            books = {}
            for item in items:
                vol = item.get("volumeInfo") or {}
                title = vol.get("title")
                if not title: continue
                year = int(vol.get("publishedDate")[:4]) if re.match(r"^\d{4}", vol.get("publishedDate") or "") else None
                norm = normalize_text(title)
                if norm not in books:
                    books[norm] = {"title": title, "year": year, "isbns": []}
            return list(books.values())
        except: return []

    async def get_isbns(self, author: str, title: str) -> List[str]:
        """Try to find ISBNs for a specific canonical title."""
        try:
            resp = await self.client.get(self.API, params={"q": f'intitle:"{title}" inauthor:"{author}"', "maxResults": 5})
            if resp.status_code != 200: return []
            isbns = []
            for item in (resp.json().get("items") or []):
                for ident in (item.get("volumeInfo", {}).get("industryIdentifiers") or []):
                    if ident.get("type") in ("ISBN_13", "ISBN_10"):
                        isbns.append(ident["identifier"])
            return list(set(isbns))
        except: return []


class MetadataFetcher:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0, verify=False, headers={"User-Agent": UA})

    async def aclose(self):
        await self.client.aclose()

    async def search_author(self, name: str) -> List[Dict]:
        try:
            clean_name = name.replace(".", " ").strip().lower()
            
            # 1. Search OpenLibrary for name resolution (fuzzy match)
            docs = []
            try:
                resp = await self.client.get("https://openlibrary.org/search/authors.json", params={"q": clean_name}, timeout=5.0)
                docs = resp.json().get("docs", [])
            except: pass

            if not docs:
                # Direct Wikidata fallback
                try:
                    wd = WikidataBibliography(self.client)
                    wd_id = await wd._get_author_id(name)
                    if wd_id:
                        return [await self._enrich_wikidata_author(wd_id, name)]
                except: pass
                return []
            
            # Deduplicate by normalized name
            seen_names = {}
            for doc in docs[:15]:
                author_name = doc.get("name")
                if not author_name: continue
                norm_name = normalize_text(author_name)
                if norm_name not in seen_names or doc.get("work_count", 0) > seen_names[norm_name].get("work_count", 0):
                    seen_names[norm_name] = doc

            # Sort by name-match relevance first (exact > prefix > contains > token-coverage),
            # then by work_count as tiebreaker. Searching "Michael Crichton" used to surface
            # Harlan Ellison and Maeve Binchy ahead of him because OpenLibrary's fuzzy search
            # returned them and we re-sorted purely by work_count, which their long backlists
            # won.
            q = normalize_text(name)
            q_tokens = q.split()

            def relevance(doc):
                n = normalize_text(doc.get("name", ""))
                if n == q: return 3
                if n.startswith(q): return 2
                if q in n: return 1
                if q_tokens and all(t in n for t in q_tokens): return 1
                return 0

            candidates = sorted(
                seen_names.values(),
                key=lambda x: (-relevance(x), -(x.get("work_count") or 0)),
            )[:5]
            
            # 2. Enrich in parallel
            tasks = [self._fetch_author_details(doc) for doc in candidates]
            return await asyncio.gather(*tasks)
        except: return []

    async def _enrich_wikidata_author(self, qid: str, name: str) -> Dict:
        """Minimal enrichment for direct Wikidata results."""
        wd = WikidataBibliography(self.client)
        count = await wd.get_work_count(qid)
        return {
            "id": qid,
            "name": name,
            "work_count": count,
            "bio": "Authoritative record found via Wikidata.",
            "photo_url": ""
        }

    async def _fetch_author_details(self, doc: Dict) -> Dict:
        key = doc["key"]
        author_data = {
            "id": key, 
            "name": doc.get("name"), 
            "top_work": doc.get("top_work"), 
            "work_count": doc.get("work_count"), 
            "birth_date": doc.get("birth_date"), 
            "bio": "", 
            "photo_url": ""
        }
        try:
            resp = await self.client.get(f"https://openlibrary.org/authors/{key}.json", timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                
                # Check for Wikidata ID to get a realistic work count
                wd_id = data.get("remote_ids", {}).get("wikidata")
                if wd_id:
                    wd = WikidataBibliography(self.client)
                    author_data["work_count"] = await wd.get_work_count(wd_id)
                    author_data["id"] = wd_id # Prefer Wikidata ID for the main record
                
                bio = data.get("bio", "")
                author_data["bio"] = bio.get("value", bio) if isinstance(bio, dict) else bio
                if data.get("photos"): 
                    author_data["photo_url"] = f"https://covers.openlibrary.org/a/id/{data['photos'][0]}-M.jpg"
        except: pass
        return author_data

    async def get_author_books(self, author_id: str, author_name: str = "",
                                query: Optional[str] = None, mode: str = "strict") -> List[Dict]:
        """Return the author's bibliography. mode is forwarded to Wikidata."""
        books: List[Dict] = []
        if author_name:
            books = await WikidataBibliography(self.client).fetch(author_name, mode=mode)

        if not books and author_name:
            books = await GoogleBooksBibliography(self.client).fetch(author_name)

        if query:
            q = normalize_text(query)
            books = [b for b in books if q in normalize_text(b["title"])]

        # Limited enrichment for the UI: try to get ISBNs for the first 25 books
        # to help the downloader find high-quality mirrors.
        gb = GoogleBooksBibliography(self.client)
        enrichment_tasks = []
        for b in books[:25]:
            if not b["isbns"]:
                enrichment_tasks.append(self._enrich_book(gb, author_name, b))
        
        if enrichment_tasks:
            await asyncio.gather(*enrichment_tasks)

        return sorted(books, key=lambda x: (x.get("year") or 9999, x.get("title", "")))

    async def _enrich_book(self, gb: GoogleBooksBibliography, author: str, book: Dict):
        try:
            book["isbns"] = await gb.get_isbns(author, book["title"])
        except:
            pass

    async def _fetch_ol_works(self, author_id: str) -> List[Dict]:
        """Fetch raw works from OpenLibrary."""
        key = author_id.split('/')[-1]
        offset, limit = 0, 100
        books: List[Dict] = []
        try:
            resp = await self.client.get(f"https://openlibrary.org/authors/{key}/works.json", params={"limit": limit})
            if resp.status_code == 200:
                for work in resp.json().get("entries") or []:
                    books.append({
                        "title": work.get("title"),
                        "year": self._extract_year(work.get("first_publish_date")),
                        "isbns": []
                    })
        except: pass
        return books

    @staticmethod
    def _extract_year(date_value) -> Optional[int]:
        if not date_value: return None
        m = re.search(r'\b(1[89]\d{2}|20\d{2})\b', str(date_value))
        return int(m.group()) if m else None

def has_playwright() -> bool:
    """Probe for Playwright. Only the 'grey' image variant ships with it."""
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


class ScraperEngine:
    def __init__(self, log_func: Callable):
        self.log = log_func
        self.browser, self.playwright, self.annas_base = None, None, ""
        self.client = httpx.AsyncClient(verify=False, timeout=20.0, follow_redirects=True, headers={"User-Agent": UA})

    async def start(self):
        # Lazy import — Playwright is only in the -grey image. If we get here on
        # the standard image, raise with a clear message rather than letting an
        # ImportError surface from the import line itself.
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "ENABLE_GREY_SOURCES=true but Playwright is not installed in this image. "
                "Pull the -grey image variant (e.g. ghcr.io/axolotl-industries/library-dog:latest-grey) "
                "or set ENABLE_GREY_SOURCES=false."
            )
        self.annas_base = await resolve_annas_domain(self.log)
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )

    async def stop(self):
        try:
            if self.browser: await self.browser.close()
            if self.playwright: await self.playwright.stop()
        except: pass
        await self.client.aclose()

    async def _resolve_mirror(self, url: str, page) -> Optional[str]:
        try:
            await page.goto(url, timeout=15000)
            link = BeautifulSoup(await page.content(), 'html.parser').find('a', href=re.compile(r"get\.php|/get/|download", re.I))
            if link: return urljoin(url, link['href'])
        except asyncio.CancelledError: raise
        except Exception: pass
        return None

    async def get_mirrors(self, author: str, title: str, isbns: List[str]) -> List[Tuple[str, str]]:
        mirrors = []
        page = await self.browser.new_page()
        norm_title_full = normalize_text(title.replace(':', ' '))
        norm_title = normalize_text(title)
        author_parts = [p for p in normalize_text(author).split() if len(p) > 2]

        def _title_match(s: str) -> bool:
            n = normalize_text(s)
            return norm_title_full in n or norm_title in n

        queries = [isbns[0]] if isbns else []
        queries.append(_query_title(title))
        try:
            for q in queries:
                self.log(f"Checking mirrors for '{title}'...")
                try:
                    await page.goto(f"https://libgen.li/index.php?req={quote(q)}&res=25&filesuns=all", timeout=30000)
                    soup = BeautifulSoup(await page.content(), 'html.parser')
                    rows = soup.select('table[id="table-libgen"] tr') or soup.find_all('tr')[1:]
                    for r in rows:
                        cols = r.find_all('td')
                        if len(cols) < 8: continue
                        raw_t, raw_a, raw_l, raw_e = cols[0].get_text(strip=True).lower(), cols[1].get_text(strip=True).lower(), cols[4].get_text(strip=True).lower(), cols[7].get_text(strip=True).lower()
                        if 'epub' in raw_e and (any(l in raw_l for l in ['english', 'eng']) or not raw_l.strip()) and _title_match(raw_t) and (any(p in raw_a for p in author_parts) if author_parts else True):
                            ads = cols[-1].find('a', href=re.compile(r"ads\.php"))
                            if ads:
                                direct = await self._resolve_mirror(urljoin("https://libgen.li", ads['href']), page)
                                if direct: self.log(f"Found mirror match..."); mirrors.append(("Libgen", direct)); break
                    if mirrors: break
                except asyncio.CancelledError: raise
                except: pass

                try:
                    await page.goto(f"{self.annas_base}/search?q={quote(q)}&ext=epub&lang=en", timeout=30000)
                    results = BeautifulSoup(await page.content(), 'html.parser').select('a[href*="/md5/"]')
                    for cand in results[:3]:
                        cand_t = normalize_text(cand.get_text())
                        if _title_match(cand.get_text()) and (any(p in cand_t for p in author_parts) if author_parts else True):
                            await page.goto(urljoin(self.annas_base, cand['href']), timeout=30000)
                            msoup = BeautifulSoup(await page.content(), 'html.parser')
                            lg = msoup.find('a', href=re.compile(r"libgen\.li/ads\.php"))
                            if lg:
                                direct = await self._resolve_mirror(lg['href'], page)
                                if direct: self.log(f"Found mirror match..."); mirrors.append(("Anna Libgen", direct))
                            ipfs = msoup.find('a', href=re.compile(r"ipfs"))
                            if ipfs and 'ipfs://' in ipfs['href']: mirrors.append(("IPFS", f"https://ipfs.io/ipfs/{ipfs['href'].split('ipfs://')[1]}"))
                            if len(mirrors) >= 3: break
                    if mirrors: break
                except asyncio.CancelledError: raise
                except: pass
        finally: await page.close()
        return mirrors

class GutenbergClient:
    """Search Project Gutenberg via the Gutendex API and return a direct EPUB URL.

    If a match is found, the book is public domain and Gutenberg should be used
    exclusively — no need to touch Usenet, Libgen, or Anna's Archive.
    """
    _API = "https://gutendex.com/books/"

    async def find_epub(self, author: str, title: str, log: Callable) -> Optional[str]:
        query = _query_title(f"{title} {author}")
        norm_title_full = normalize_text(title.replace(':', ' '))
        norm_title = normalize_text(title)
        author_parts = [p for p in normalize_text(author).split() if len(p) > 2]

        def _title_match(s: str) -> bool:
            n = normalize_text(s)
            return norm_title_full in n or norm_title in n

        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers={"User-Agent": UA}) as client:
                resp = await client.get(self._API, params={"search": query})
                if resp.status_code != 200:
                    log(f"Gutenberg returned HTTP {resp.status_code}; skipping")
                    return None
                for book in resp.json().get("results", []):
                    if not _title_match(book.get("title", "")):
                        continue
                    raw_authors = " ".join(a.get("name", "") for a in book.get("authors", []))
                    if author_parts and not any(p in normalize_text(raw_authors) for p in author_parts):
                        continue
                    epub_url = book.get("formats", {}).get("application/epub+zip")
                    if epub_url:
                        log(f"Found on Project Gutenberg: {book['title']}")
                        return epub_url
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # gutendex.com goes down periodically; httpx.ReadTimeout in particular
            # renders with an empty __str__, which is why we include the type.
            log(f"Gutenberg unreachable: {type(e).__name__}: {e or '(no message)'} — falling through")
        return None


class Downloader:
    def __init__(self, base_dir: str, log_func: Callable):
        self.base_dir = os.path.abspath(base_dir)
        self.log = log_func
        self.ssl_ctx = create_robust_ssl_context()
        os.makedirs(self.base_dir, exist_ok=True)

    async def download(self, mirror: str, url: str, author: str, title: str, book_data: Dict) -> bool:
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
        path = os.path.join(self.base_dir, f"{safe_title}.epub")
        for cfg in [{"verify": self.ssl_ctx}, {"verify": False}]:
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=60.0, headers={"User-Agent": UA}, **cfg) as client:
                    async with client.stream("GET", url) as resp:
                        if resp.status_code != 200 or "text/html" in resp.headers.get("Content-Type", "").lower(): continue
                        size = int(resp.headers.get("Content-Length", 0))
                        if size > 0 and (size < 10000 or size > 40*1024*1024): continue
                        self.log(f"Downloading from {mirror}...")
                        with open(path, "wb") as f:
                            async for chunk in resp.aiter_bytes(): f.write(chunk)
                            f.flush(); os.fsync(f.fileno())
                if zipfile.is_zipfile(path):
                    with zipfile.ZipFile(path) as z:
                        if 'mimetype' in z.namelist():
                            await self._enrich_epub(path, author, title, book_data, source=mirror)
                            self.log(f"Saved to: {path}"); return True
                if os.path.exists(path): os.remove(path)
            except asyncio.CancelledError: raise
            except Exception:
                if os.path.exists(path): os.remove(path)
        return False

    # EPUBs we got from a non-canonical source (Anna's Archive / Libgen / IPFS
    # mirrors) deserve a "needs review" tag so the user knows to double-check
    # them in Calibre before pushing to CWA / their reading library. Project
    # Gutenberg is canonical and exempt.
    _CANONICAL_SOURCES = {"Project Gutenberg"}
    _REVIEW_TAG = "Library Dog: needs review"

    async def _enrich_epub(self, path: str, author: str, title: str,
                            book_data: Dict, source: Optional[str] = None) -> None:
        """Embed authoritative metadata into the EPUB before it lands in the watched
        folder Calibre-Web-Automated picks up. Each step is best-effort so a single
        ebookmeta API quirk doesn't lose the whole download.
        """
        try:
            meta = ebookmeta.get_metadata(path)
        except Exception as e:
            self.log(f"Metadata read failed: {e}")
            return

        try: meta.title = title
        except Exception: pass

        # Author. ebookmeta exposes both an Author-list setter and (in older versions)
        # a string-form setter; try the structured one first.
        try:
            from ebookmeta.myzipfile import Author  # noqa: F401  (some versions)
            from ebookmeta import Author as _Author
            meta.author_list = [_Author(name=author)]
        except Exception:
            try: meta.author_list_to_string = author
            except Exception: pass

        if book_data.get("year"):
            year_str = str(book_data["year"])
            try: meta.publish_info.year = year_str
            except Exception:
                try: meta.publish_year = year_str
                except Exception: pass

        isbns = book_data.get("isbns") or []
        if isbns:
            try: meta.identifier = isbns[0]
            except Exception: pass

        try: meta.lang = "en"
        except Exception: pass

        # Tag for grey-source review. Calibre Desktop reads dc:subject as tags,
        # so the user can filter on this in their library audit workflow.
        if source and source not in self._CANONICAL_SOURCES:
            try:
                existing = list(getattr(meta, 'tag_list', None) or [])
                if self._REVIEW_TAG not in existing:
                    existing.append(self._REVIEW_TAG)
                meta.tag_list = existing
            except Exception:
                pass

        # Cover. OpenLibrary's covers API serves a 404 for ?default=false when no
        # cover is on file, so we don't get a 1×1 placeholder embedded in the EPUB.
        if isbns:
            cover_bytes = await self._fetch_cover(isbns[0])
            if cover_bytes:
                try:
                    meta.cover_image_data = cover_bytes
                    meta.cover_file_name = "cover.jpg"
                except Exception: pass

        try:
            ebookmeta.set_metadata(path, meta)
        except Exception as e:
            self.log(f"Metadata write failed: {e}")

    async def _fetch_cover(self, isbn: str) -> Optional[bytes]:
        """Pull a cover from OpenLibrary by ISBN, or None if no cover exists."""
        if not isbn:
            return None
        url = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false"
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                          headers={"User-Agent": UA}) as client:
                r = await client.get(url)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                    return r.content
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        return None
