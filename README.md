# Library Dog

A self-hosted book discovery + download tool for the *arr stack.

Type an author, pick from the bibliography, and Library Dog fetches
ebooks (EPUB / MOBI / AZW3 / PDF, in your priority order) into a flat
folder your Calibre / Calibre-Web-Automated / Komga library can
ingest. Project Gutenberg is checked first; otherwise it aggregates
across every Prowlarr indexer you've enabled, routing NZB results to
SABnzbd and torrent / magnet results to qBittorrent. Torrent
downloads keep seeding after the book lands in the library — useful
if you're on a private tracker.

> **Heads up — this is largely Claude-assisted ("vibe-coded") code.**
> It works for the author's setup but should be treated as alpha
> quality if you're running it yourself. Read the diff before pointing
> it at anything sensitive, and expect rough edges. Bug reports
> welcome; PRs even more so.

## What it does

1. **Author search.** Resolves an author against OpenLibrary →
   Wikidata, with photo, bio, and a realistic work-count. The author
   whose name you actually typed in is ranked first; fuzzy neighbours
   from OpenLibrary fall behind.
2. **Bibliography.** Pulls a canonical work list from Wikidata SPARQL.
   Two modes, toggleable per-search:
     - **Strict** — only items typed as one of a known book class
       (literary work, book, written work, short story, diary, plus
       their P279 subclass closure). Cleaner output, but Wikidata's
       editor-by-editor inconsistency means some real books still
       leak through the cracks.
     - **Permissive** — any work the author is credited on (P50)
       that has either a publication date or an ISBN. Catches the
       strays at the cost of a bit more noise.
3. **Per-book search.** For each title, tried in order:
     1. **Project Gutenberg.** If found, done — public-domain works
        come straight from the source.
     2. **Aggregated Prowlarr search.** Every indexer Prowlarr knows
        about is fair game; you enable / disable / prioritise them in
        the UI. Results are filtered to the user's enabled formats
        (EPUB / MOBI / AZW3 / PDF) and sorted by format-priority then
        indexer-priority. NZBs route to SABnzbd; torrents / magnets
        route to qBittorrent.
     3. **Anna's Archive / Libgen mirrors** (opt-in, see below).
4. **Metadata enrichment.** When the downloaded file is an EPUB,
   title / author / year / ISBN / language / cover (from OpenLibrary's
   covers API) get embedded before it lands in the watch folder, so
   CWA's auto-import has clean data. MOBI / AZW3 / PDF are saved
   as-is — whatever metadata the source carried is what CWA sees.
5. **Seed-friendly torrent handling.** qBit saves into `/app/torrents`
   (a separate volume from the library). On completion Library Dog
   *hardlinks* the book up to `/app/downloads` for CWA to ingest;
   qBit keeps seeding the original file. MAM / Bibliotik users won't
   have their ratio tanked.

## Image variants

Two flavours, published to GHCR on every push to `main` and on `v*` tags:

- **Standard** (`...:latest`, `...:main`, `...:v1.2.3`) — ~150 MB.
  Project Gutenberg + Newznab indexers via Prowlarr. The right image
  for almost everyone.
- **Grey** (`...:latest-grey`, `...:main-grey`, `...:v1.2.3-grey`) —
  ~700 MB. Adds Playwright + Chromium for Anna's Archive / Libgen
  scraping when `ENABLE_GREY_SOURCES=true` is set.

## Quick start

```yaml
services:
  library-dog:
    image: ghcr.io/axolotl-industries/library-dog:latest
    ports:
      - "8080:80"
    environment:
      - PUID=1000
      - PGID=1000
      - AUTH_PASSWORD=change-me
      - SESSION_SECRET=$(openssl rand -base64 48)
      - PROWLARR_URL=http://prowlarr:9696        # Prowlarr API root, NOT a per-indexer URL
      - PROWLARR_KEY=...
      - SABNZBD_URL=http://sab:8080
      - SABNZBD_KEY=...
      # Optional torrent backend:
      - QBIT_URL=http://qbittorrent:8080
      - QBIT_USER=admin
      - QBIT_PASS=...
    volumes:
      - ./downloads:/app/downloads
      - ./torrents:/app/torrents                 # only needed when using qBittorrent;
                                                 # qBit container must mount the same path
    restart: unless-stopped
```

`./downloads` is what you point Calibre-Web-Automated's ingest folder
at. Library Dog flattens everything to a single directory of book
files — no subfolders, no `.nfo`, no cruft.

`./torrents` is qBittorrent's save path. Both Library Dog and your
qBittorrent container must mount the same host path here so the
hardlink-up-to-the-library step has both ends of the link visible.

There's also a `docker-compose.yml` in the repo with every supported
env var documented inline.

## Configuration

| Env var                | Default        | What |
|------------------------|----------------|------|
| `PUID` / `PGID`        | `1000`         | Runtime UID/GID. Match your downloads dir owner. |
| `AUTH_PASSWORD`        | unset          | Shared password for the form login. |
| `AUTH_USERNAME`        | `user`         | Display name when nobody types one in. |
| `TRUSTED_PROXY_AUTH`   | `false`        | Honor `Remote-User` / `X-Forwarded-User` from a reverse proxy. |
| `SESSION_SECRET`       | random-per-run | Cookie signing secret. Set this or sessions die on restart. |
| `SESSION_COOKIE_SECURE`| `false`        | Flip to `true` once you're behind HTTPS. |
| `PROWLARR_URL`         | unset          | Prowlarr **API root** (`http://host:9696`), not a per-indexer URL. Legacy `/<id>` form is tolerated but limits you to one indexer. |
| `PROWLARR_KEY`         | unset          | API key from Prowlarr. |
| `SABNZBD_URL`          | unset          | SABnzbd base URL. |
| `SABNZBD_KEY`          | unset          | SABnzbd API key. |
| `QBIT_URL`             | unset          | qBittorrent Web UI base URL. |
| `QBIT_USER`/`QBIT_PASS`| unset          | qBittorrent credentials. |
| `QBIT_SAVE_PATH`       | `/app/torrents` | Where qBit writes finished torrents. Must be visible to both Library Dog and qBit (mount the same host path into both). |
| `QBIT_CATEGORY`        | `books`        | Category tag for Library Dog torrents. |
| `ENABLE_GREY_SOURCES`  | `false`        | Opt in to Anna's Archive / Libgen scraping (see below). |

**Per-user settings live in the browser**, not in env vars: which
indexers are enabled and in what priority order, which formats and in
what order, the strict/permissive bibliography toggle, and the theme
are all persisted to `localStorage`. Open the [ INDEXERS ] /
[ FORMATS ] panels in the UI to tweak them.

## Auth

Three modes, all derived from env. They can be combined:

- **Password.** `AUTH_PASSWORD=...` enables a form login with a
  single shared password. The username field is cosmetic — it's just
  what the UI greets you as. Comparison uses
  `secrets.compare_digest`.
- **Forward-auth.** `TRUSTED_PROXY_AUTH=true` makes Library Dog
  trust `Remote-User` / `X-Forwarded-User` /
  `X-Authentik-Username` headers. Authelia, Authentik, traefik
  forward-auth, etc. — all just work. **Library Dog must only be
  reachable via that proxy** when this is on; the header is
  trivially spoofable otherwise.
- **No auth.** Neither set. Fine for a private LAN; fatal on the
  public internet. A startup warning makes this obvious.

## Calibre-Web-Automated integration

Library Dog's value-add for CWA is that **EPUBs** land *already
metadata-tagged*. CWA's auto-import takes whatever's in the file,
so we make sure title / author / year / ISBN / language / cover are
all set from authoritative sources (Wikidata, OpenLibrary, Google
Books) before the file shows up. MOBI / AZW3 / PDF are passed
through unmodified — what the source carried is what CWA sees.

Setup:

1. Mount your CWA ingest folder into Library Dog at `/app/downloads`
   (or the same host path into both containers).
2. Run with `PUID`/`PGID` matching whoever owns that folder.
3. CWA picks up new files within ~30s.

## Sources

- **Project Gutenberg** — checked first via the Gutendex API.
  Public-domain books only, served straight from
  `gutenberg.org`. Always on.
- **Indexers via Prowlarr** — every indexer Prowlarr knows about is
  enumerated in the [ INDEXERS ] panel. Tick to enable, ↑/↓ to set
  priority. Searches are aggregated across enabled indexers via
  Prowlarr's `/api/v1/search`, restricted to category `7020`
  (Books > eBook), and results are sorted by your format-priority
  list and indexer-priority list before being tried. NZB results
  route to SABnzbd; torrent / magnet results route to qBittorrent
  (if configured). Available when `PROWLARR_URL` and `PROWLARR_KEY`
  are set.
- **Anna's Archive / Libgen mirrors** — opt-in via
  `ENABLE_GREY_SOURCES=true` *and* the **`-grey` image variant**.
  Setting the env var on the standard image will be downgraded to
  off at startup with a warning, since the standard image doesn't
  ship Playwright/Chromium. Use
  `ghcr.io/axolotl-industries/library-dog:latest-grey` (or pin to a
  semver-grey tag) when you actually want this. Legally grey in many
  jurisdictions; off by default.

### A note on indie authors

Library Dog leans on Wikidata, OpenLibrary, and major indexers, all of
which skew toward authors who've already broken through. That means
Library Dog will fail to find a lot of indie / small-press / self-published
work. **This is a feature, not a bug.**

If an author isn't notable enough to merit a Wikipedia article, then
they would also feel a hit if people were to pirate their work. So:

> **If you can't find an author with this app, then they need your
> money. Fuck you. Buy their work.**

## Themes

Seven themes, persisted in `localStorage`. Pick one from the dropdown
in the top-right corner.

- **Dark** (default) — flat dark, system font, *arr-stack-ish.
- **Light** — flat light variant of Dark, same layout.
- **AT** — green-phosphor BBS. Monospace, uppercase, scanlines.
- **XT** — amber-phosphor BBS. Same chrome as AT, different colour.
- **386** — full ANSI 16-colour BBS on black. Each viewbox in a
  different ANSI hue (cyan / yellow / green / magenta).
- **95** — Windows 95. Teal desktop, gray windows with bevels, MS Sans
  Serif, blue title bars on every card, beveled buttons that invert on
  click.
- **XP** — Windows XP "Luna". Bliss-blue gradient desktop, white
  rounded cards, Tahoma, gradient buttons, XP-style blue title bars.

The three BBS themes (AT / XT / 386) share monospace, uppercase,
scanlines, and square corners; they differ only in palette. 95 and XP
are full chrome refits — different fonts, borders, button styles, the
whole thing.

## How is this different from Readarr?

Readarr was the de facto *arr book solution — past tense. The original
project was **retired by its developers in late 2024**, and as of this
writing no fork has clearly emerged as the successor; what's out there
is a scattered ecosystem of half-maintained forks, none with the
momentum or feature parity to be called "the new Readarr." So the
honest comparison is more "what Readarr used to be, and what Library
Dog isn't trying to be" than a head-to-head.

Library Dog is narrower than Readarr ever was:

- Library Dog is **author-driven** and stateless — you punch in an
  author, pick books from a list, files land in a folder. There's
  no monitored library, no quality profiles, no notion of
  "missing." Calibre-Web-Automated handles ongoing library
  management; Library Dog just feeds it.
- Library Dog supports **EPUB / MOBI / AZW3 / PDF**, with a
  user-ranked priority list. Audiobooks aren't in scope.
- Library Dog finds **public-domain works correctly**. The
  Gutenberg-first pass means you get a clean source for the
  half-dozen authors most worth reading.
- Library Dog is **simpler to operate**: one container, one volume,
  one env file. No SQLite to corrupt, no monitored-list to drift.

If you want Readarr's continuous-watch model, run Readarr. If you
want "fill out an author's bibliography and call it done," try
this.

## Known limitations / roadmap

- Job state is in-memory; restart loses history.
- No library-awareness: if a book is already in your Calibre
  library, Library Dog will happily download it again.
- No notifications (Apprise / Discord / etc.).
- Single shared password; no multi-user accounts. Use forward-auth
  if you need real users.
- `verify=False` on outbound HTTPS for the indexer/SAB/qBit clients
  (legacy from running against self-signed Prowlarr behind a
  reverse proxy). To be made env-configurable.
- Metadata enrichment is EPUB-only. MOBI / AZW3 / PDF are saved
  with whatever the source carried.
- Multi-format support reaches Prowlarr indexer results only —
  Project Gutenberg + Anna's / Libgen mirrors still only fetch EPUB.
- The `-grey` image carries Playwright + Chromium and is ~700MB,
  vs ~150MB for the standard image. There's no in-between today.

PRs welcome on any of these.

## License

MIT — see [LICENSE](./LICENSE).
