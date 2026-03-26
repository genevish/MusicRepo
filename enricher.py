"""
Metadata enricher — looks up artist, album, genre via iTunes Search API.
Can be imported by server.py or run standalone:
    python enricher.py            # enrich all tracks missing genre/album
    python enricher.py --all      # re-enrich everything
"""

import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "library.db"

ITUNES_SEARCH = "https://itunes.apple.com/search"
# Be polite — iTunes asks for no more than 20 req/min
REQUEST_DELAY = 0.35  # seconds between requests

# Patterns to strip from YouTube video titles before searching
_STRIP = re.compile(
    r'\s*[\(\[]'
    r'(?:official\s*(?:music\s*)?(?:lyric\s*)?(?:hd\s*)?(?:4k\s*)?video|'
    r'official\s*audio|lyrics?|hq|hd|4k|remaster(?:ed)?|'
    r'visuali[sz]er|full\s*album|extended|live|acoustic|'
    r'feat\.?.*?|ft\.?.*?|with\s+.*?)'
    r'[\)\]]\s*',
    re.IGNORECASE
)
_FEAT = re.compile(r'\s+(?:feat\.?|ft\.?|with)\s+.+$', re.IGNORECASE)


def clean_title(raw: str) -> str:
    """Strip YouTube cruft from a title."""
    t = _STRIP.sub(' ', raw)
    t = _FEAT.sub('', t)
    return t.strip(' -–—|')


def parse_artist_track(title: str) -> tuple[str, str]:
    """Split 'Artist - Track Name' into (artist, track). Returns (None, title) if no separator."""
    for sep in (' - ', ' – ', ' — ', ' | '):
        if sep in title:
            artist, track = title.split(sep, 1)
            return clean_title(artist), clean_title(track)
    return None, clean_title(title)


def itunes_lookup(artist: str, track: str) -> dict | None:
    """Query iTunes Search API, return best match or None."""
    query = f"{artist} {track}" if artist else track
    params = urllib.parse.urlencode({
        "term": query,
        "media": "music",
        "entity": "song",
        "limit": 5,
    })
    url = f"{ITUNES_SEARCH}?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MusicRepo/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"    iTunes error: {e}")
        return None

    results = data.get("results", [])
    if not results:
        return None

    # Prefer result where artistName matches closely
    if artist:
        artist_lower = artist.lower()
        for r in results:
            if artist_lower in r.get("artistName", "").lower():
                return r

    return results[0]


def enrich_track(conn: sqlite3.Connection, row: sqlite3.Row, force: bool = False) -> bool:
    """Enrich a single track row. Returns True if updated."""
    # Skip if already complete (unless force)
    if not force and row["genre"] and row["album"]:
        return False

    title = row["title"] or ""
    stored_artist = row["artist"] or row["uploader"] or ""
    parsed_artist, track_name = parse_artist_track(title)

    # Use stored artist if we couldn't parse one from title, or if stored is more specific
    search_artist = parsed_artist or stored_artist

    if not track_name:
        return False

    result = itunes_lookup(search_artist, track_name)
    if not result:
        print(f"    No match: {title[:60]}")
        return False

    updates = {}

    # Artist — use canonical iTunes name if we don't have one
    if not row["artist"] or force:
        canon = result.get("artistName", "")
        if canon:
            updates["artist"] = canon

    # Album
    if (not row["album"] or force):
        album = result.get("collectionName", "")
        # Strip " - Single", " - EP" etc.
        album = re.sub(r'\s*-\s*(?:Single|EP|Deluxe.*|Remaster.*)$', '', album, flags=re.IGNORECASE).strip()
        if album:
            updates["album"] = album

    # Genre
    if (not row["genre"] or force):
        genre = result.get("primaryGenreName", "")
        if genre and genre.lower() not in ("music", ""):
            updates["genre"] = genre

    if not updates:
        return False

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE tracks SET {set_clause} WHERE id = ?",
                 list(updates.values()) + [row["id"]])
    conn.commit()
    print(f"    ✓ {title[:50]:<50}  {updates.get('artist','')[:20]} | {updates.get('album','')[:25]} | {updates.get('genre','')}")
    return True


def enrich_all(force: bool = False, progress_cb=None) -> dict:
    """Enrich all tracks. progress_cb(current, total, title) for live updates."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if force:
        rows = conn.execute("SELECT * FROM tracks ORDER BY title").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tracks WHERE genre IS NULL OR album IS NULL ORDER BY title"
        ).fetchall()

    total = len(rows)
    updated = 0
    failed = 0

    print(f"Enriching {total} track(s)…\n")

    for i, row in enumerate(rows):
        if progress_cb:
            progress_cb(i, total, row["title"])
        ok = enrich_track(conn, row, force=force)
        if ok:
            updated += 1
        else:
            failed += 1
        time.sleep(REQUEST_DELAY)

    conn.close()
    print(f"\nDone — {updated} updated, {failed} unchanged/not found.")
    return {"updated": updated, "failed": failed, "total": total}


if __name__ == "__main__":
    import sys
    force = "--all" in sys.argv
    enrich_all(force=force)
