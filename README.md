# Library Dog

A self-hosted book discovery + download tool for the *arr stack.

Type an author, pick from the bibliography, and Library Dog finds and
grabs EPUBs into a flat folder your Calibre / Calibre-Web-Automated /
Komga library can ingest. NZB results go to SABnzbd; torrent results
go to qBittorrent. Project Gutenberg is checked first so public-domain
works come straight from the source.

> **Heads up — this is largely Claude-assisted ("vibe-coded") code.**
> It works for the author's setup but should be treated as alpha
> quality if you're running it yourself. Read the diff before pointing
> it at anything sensitive, and expect rough edges. Bug reports
> welcome; PRs even more so.

## What it does

1. **Author search.** Resolves an author against OpenLibrary →
   Wikidata, with photo, bio, and a realistic work-count.
2. **Bibliography.** Pulls a canonical work list from Wikidata SPARQL
   (Q7725634 / Q571 / Q49084 / Q1144673 — written works, novels,
   short stories, diaries) so you don't end up with the OpenLibrary
   "garbage" of every reprint and study guide.
3. **Per-book search.** For each title:
   1. Project Gutenberg first. If found, done.
   2. Newznab indexer (Prowlarr). NZB → SABnzbd, torrent/magnet →
      qBittorrent, picked from the `<enclosure type=...>` attribute
      with magnet:/.torrent fallback detection.
   3. Anna's Archive / Libgen mirrors (opt-in, see below).
4. **EPUB enrichment.** Title, author, year, ISBN, language, and
   cover (via OpenLibrary covers API) get embedded into the EPUB
   before it lands in the watch folder, so Calibre-Web-Automated's
   auto-import has clean data to work with.

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
      - PROWLARR_URL=http://prowlarr:9696/<indexer-id>
      - PROWLARR_KEY=...
      - SABNZBD_URL=http://sab:8080
      - SABNZBD_KEY=...
      # Optional torrent backend:
      - QBIT_URL=http://qbittorrent:8080
      - QBIT_USER=admin
      - QBIT_PASS=...
    volumes:
      - ./downloads:/app/downloads
    restart: unless-stopped
```

`./downloads` is what you point Calibre-Web-Automated's ingest folder
at. Library Dog flattens everything to a single directory of book
files — no subfolders, no `.nfo`, no cruft.

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
| `PROWLARR_URL`         | unset          | Prowlarr **per-indexer** URL (`/<id>`), not the root. |
| `PROWLARR_KEY`         | unset          | API key from Prowlarr. |
| `SABNZBD_URL`          | unset          | SABnzbd base URL. |
| `SABNZBD_KEY`          | unset          | SABnzbd API key. |
| `QBIT_URL`             | unset          | qBittorrent Web UI base URL. |
| `QBIT_USER`/`QBIT_PASS`| unset          | qBittorrent credentials. |
| `QBIT_SAVE_PATH`       | `/app/downloads` | Where qBit writes finished torrents. Must be visible to Library Dog. |
| `QBIT_CATEGORY`        | `books`        | Category tag for Library Dog torrents. |
| `ENABLE_GREY_SOURCES`  | `false`        | Opt in to Anna's Archive / Libgen scraping (see below). |

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

Library Dog's value-add for CWA is that EPUBs land *already
metadata-tagged*. CWA's auto-import takes whatever's in the file,
so we make sure title/author/year/ISBN/language/cover are all set
from authoritative sources (Wikidata, OpenLibrary, Google Books)
before the file shows up.

Setup:

1. Mount your CWA ingest folder into Library Dog at `/app/downloads`
   (or the same host path into both containers).
2. Run with `PUID`/`PGID` matching whoever owns that folder.
3. CWA picks up new files within ~30s.

## Sources

- **Project Gutenberg** — checked first via the Gutendex API.
  Public-domain books only, served straight from
  `gutenberg.org`. Always on.
- **Newznab indexers via Prowlarr** — anything Prowlarr can proxy
  shows up here, restricted to category `7020` (Books > eBook).
  NZB results route to SABnzbd; torrent / magnet results route to
  qBittorrent (if configured). Always on when `PROWLARR_URL` is set.
- **Anna's Archive / Libgen mirrors** — opt-in via
  `ENABLE_GREY_SOURCES=true`. Drags in a Playwright/Chromium
  runtime to deal with their JS-resolved download links, and is
  legally grey in many jurisdictions. Off by default for both
  reasons.

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
- Image is large (~700MB) when `ENABLE_GREY_SOURCES=true` keeps
  Playwright/Chromium in scope.

PRs welcome on any of these.

## License

MIT — see [LICENSE](./LICENSE).
