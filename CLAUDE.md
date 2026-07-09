# MusicRepo

## Project summary
Offline-first personal YouTube music/video downloader and player, meant to run as a
kiosk on a Raspberry Pi (and locally on macOS for dev). Single-user, LAN-only, no
auth. Paste YouTube URL(s) into the web UI → `yt-dlp` downloads audio (mp3) or video
(mp4) into `downloads/` → metadata is cataloged into SQLite (`data/library.db`) →
missing genre/album is auto-enriched via the iTunes Search API right after each
download job finishes.

## Active features
- **Web UI** (`static/index.html`, single file, no build step) served at
  `http://localhost:8765`:
  - Download modal — one or more YouTube URLs (one per line), optional
    playlist/collection name, audio-only toggle, live progress bar + collapsible
    log, job history with retry.
  - Grid/list library views, search, filter by genre/artist/playlist, sort by
    newest/title/artist/plays/duration.
  - Player side panel — art, title/artist, editable tags (artist/genre/album/
    playlist), prev/next, delete.
  - True fullscreen video modal (native Fullscreen API, edge-to-edge overlay
    fallback), auto-closes when the queue ends.
  - Multi-select (checkboxes) → floating action bar: play selected / delete
    selected / clear.
  - Play-all / queue system (`playQueue`) with auto-advance on `ended`.
  - Auto-enrich — no manual "Enrich" button (removed in `e08b965`); server kicks
    off enrichment in the background after every download, UI polls
    `/api/enrich` every 600ms and shows a status bar.
- **`downloader.py`** — standalone CLI alternative to the web UI (download /
  `--search` / `--list` / `--tag` / `--delete`). Not kept in sync with
  `server.py`'s resilience improvements — see Known bugs #4–5.
- **`enricher.py`** — iTunes Search API lookup (artist/genre/album) with
  title-cleaning heuristics (strips "(Official Video)" etc., splits
  "Artist - Track"). Rate-limited to ~1 request / 0.35s.
- **Pi deployment** — `musicrepo.service` (systemd, runs as user `pi` from
  `/home/pi/MusicRepo`), `musicrepo-kiosk.desktop` (autostarts Chromium in kiosk
  mode once the server responds to a health-check curl loop).

## Tech stack
- **Backend**: Python 3 stdlib only (`http.server`, `sqlite3`, `subprocess`,
  `threading`) — no framework, only pip dep is `yt-dlp` (`requirements.txt`).
  Server is single-threaded (`HTTPServer`, not `ThreadingHTTPServer`).
- **Frontend**: one `static/index.html` — vanilla JS + inline CSS with custom
  properties for theming, no framework, no build step.
- **Storage**: SQLite at `data/library.db` (WAL mode), one `tracks` table.
  Media + `.jpg` thumbnails + `.info.json` sidecars live in `downloads/`.
  `data/` and `downloads/` are gitignored — local state, never committed.
- **Download engine**: `yt-dlp` binary, must be on `PATH` (Homebrew locally;
  presumably pip/apt on the Pi).
- **Enrichment**: iTunes Search API — public, unauthenticated, no API key.

## Code style & naming conventions
- snake_case in Python, camelCase in JS. DB columns are snake_case and map 1:1
  to the Python dict keys built from `yt-dlp` info (`_upsert_track` passes the
  dict straight through to parameterized SQL).
- The YouTube video `id` is the track primary key everywhere: filenames
  (`<id>.<ext>`, `<id>.jpg`, `<id>.info.json`) and the `tracks.id` column all
  use it.
- No ORM — hand-written SQL with `?` placeholders (parameterized, no injection
  observed). User-editable columns are allowlisted (`genre`, `album`, `artist`,
  `playlist`) and the `order` query param is validated against a fixed set
  (`safe_order`) before being interpolated into SQL.
- Track IDs from URL paths are validated with
  `re.fullmatch(r"[A-Za-z0-9_-]{1,32}", track_id)` before any DELETE or
  filesystem glob — path-traversal guard.
- `get_db()` / `upsert_track()` are duplicated (not shared) between
  `server.py` and `downloader.py` — schema is kept in sync by hand, no
  migrations file.
- No linter/type-checker configured.

## Known bugs / limitations
1. **Single-threaded server** — `HTTPServer`, not `ThreadingHTTPServer`. A slow
   request (e.g. streaming a large video file) blocks every other request,
   including the enrich-status poll and library reloads, until it finishes.
2. **`send_file` / `send_partial_file` load the whole requested range into
   memory** (`f.read()`) instead of streaming in chunks — large video files
   could spike memory on a Pi.
3. **Concurrent download jobs can misattribute new tracks.** `_run_download`
   detects "new" tracks by diffing `*.info.json` files present before/after
   the job. Two jobs running at once (two tabs, overlapping retries) can each
   pick up the other's newly-written info.json. Not exercised by the current
   UI, which runs jobs one at a time.
4. **`downloader.py` (CLI) has drifted from `server.py`.** The web path added
   `--ignore-errors` and `--download-archive` for resilient playlist
   downloads (`dd5f5d8`); the standalone CLI was never updated to match.
5. **`downloader.py` is broken under Python 3.9** — confirmed by running
   `python3 -c "import downloader"` locally:
   `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`.
   It uses PEP 604 syntax (`str | None`) without
   `from __future__ import annotations`, which only works natively on 3.10+.
   `server.py` doesn't import this module, so the web UI is unaffected — but
   running the CLI directly currently fails on any 3.9 host.
6. **No content-type sniffing fallback** — `mimetypes.guess_type` can return
   `None` for unusual extensions; falls back to `application/octet-stream`,
   which some browsers won't play inline.
7. **Job history is in-memory only** (`jobs: dict` in `server.py`) — a server
   restart loses all download-job history (tracks/files already saved to
   DB/disk are unaffected).

## Untested scenarios / next TODOs
- Concurrent downloads (two tabs, or overlapping retries) — see bug #3; real
  concurrent behavior hasn't been verified.
- Very large playlists (100+ items) — progress bar/log UI unverified against a
  long-running multi-hour job; the in-memory job log grows unbounded.
- Full Raspberry Pi kiosk boot flow end-to-end (systemd unit → desktop
  autostart → Chromium kiosk) — components were validated in isolated
  commits; no confirmed reboot-to-kiosk test.
- Non-YouTube sources — `yt-dlp` supports many sites, but only YouTube
  (video / playlist / search) has been exercised; UI copy assumes YouTube.
- Deleting a track that's mid-download, or actively playing in another
  browser tab.
- iTunes enrichment for non-English/non-Latin titles — `parse_artist_track`'s
  separator-based split and the iTunes lookup haven't been checked against
  K-pop, J-pop, Cyrillic, etc.

## Ports & paths
- Server: `http://localhost:8765` (hardcoded `PORT` in `server.py`)
- DB: `data/library.db`; download archive: `data/downloads.archive`;
  media: `downloads/`
- Pi deploy path: `/home/pi/MusicRepo`, systemd unit runs as `User=pi`
