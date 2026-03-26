#!/usr/bin/env python3
"""
MusicRepo downloader — fetch YouTube videos/audio and catalog them in SQLite.
Usage:
    python downloader.py <URL>                    # single video
    python downloader.py <URL> --audio-only       # extract audio (mp3)
    python downloader.py <PLAYLIST_URL>           # full playlist
    python downloader.py --search "artist song"   # search + download
    python downloader.py --list                   # show library
    python downloader.py --tag ID genre=Rock      # tag a track
    python downloader.py --delete ID              # remove a track
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DB_PATH = BASE_DIR / "data" / "library.db"

DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            uploader    TEXT,
            duration    INTEGER,
            thumbnail   TEXT,
            file_path   TEXT,
            file_type   TEXT,
            genre       TEXT,
            album       TEXT,
            artist      TEXT,
            playlist    TEXT,
            tags        TEXT DEFAULT '[]',
            added_at    TEXT,
            play_count  INTEGER DEFAULT 0,
            last_played TEXT
        )
    """)
    conn.commit()
    return conn


def upsert_track(conn: sqlite3.Connection, info: dict):
    tags_json = json.dumps(info.get("tags", []))
    conn.execute("""
        INSERT INTO tracks
            (id, title, uploader, duration, thumbnail, file_path, file_type,
             genre, album, artist, playlist, tags, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            file_path   = excluded.file_path,
            file_type   = excluded.file_type,
            tags        = excluded.tags
    """, (
        info["id"], info["title"], info.get("uploader"),
        info.get("duration"), info.get("thumbnail"),
        info["file_path"], info["file_type"],
        info.get("genre"), info.get("album"), info.get("artist"),
        info.get("playlist"),
        tags_json, datetime.now().isoformat()
    ))
    conn.commit()


# ── Downloader ────────────────────────────────────────────────────────────────

def run_yt_dlp(args: list[str]) -> dict:
    """Run yt-dlp, return parsed JSON info."""
    cmd = ["yt-dlp", "--no-playlist", "--print-json"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    lines = [l for l in result.stdout.splitlines() if l.startswith("{")]
    if not lines:
        raise RuntimeError("No JSON output from yt-dlp")
    return json.loads(lines[-1])


def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', name)[:120]


def download_video(url: str, audio_only: bool = False, playlist_name: str | None = None) -> list[dict]:
    """Download one URL (video or playlist). Returns list of track info dicts."""
    output_dir = DOWNLOADS_DIR
    fmt_flag = "bestaudio/best" if audio_only else "bestvideo+bestaudio/best"
    ext = "mp3" if audio_only else "mp4"

    base_cmd = [
        "yt-dlp",
        "--format", fmt_flag,
        "--output", str(output_dir / "%(id)s.%(ext)s"),
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        "--embed-metadata",
        "--print-json",
    ]
    if audio_only:
        base_cmd += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        base_cmd += ["--merge-output-format", "mp4"]

    if "playlist" in url or "list=" in url:
        base_cmd.remove("--no-playlist") if "--no-playlist" in base_cmd else None
    else:
        base_cmd += ["--no-playlist"]

    base_cmd.append(url)

    print(f"[+] Running yt-dlp ...")
    result = subprocess.run(base_cmd, capture_output=False, text=True)

    # Collect info via --dump-json for all entries
    info_cmd = ["yt-dlp", "--dump-json", "--flat-playlist", url]
    info_result = subprocess.run(info_cmd, capture_output=True, text=True)

    tracks = []
    for line in info_result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            continue

        vid_id = info.get("id", "")
        title = info.get("title", "Unknown")
        uploader = info.get("uploader") or info.get("channel", "")

        # Determine actual saved file
        for candidate_ext in (["mp3"] if audio_only else ["mp4", "mkv", "webm"]):
            fp = output_dir / f"{vid_id}.{candidate_ext}"
            if fp.exists():
                file_path = str(fp)
                file_type = candidate_ext
                break
        else:
            file_path = str(output_dir / f"{vid_id}.{ext}")
            file_type = ext

        thumb = str(output_dir / f"{vid_id}.jpg")
        if not Path(thumb).exists():
            thumb = ""

        track_info = {
            "id": vid_id,
            "title": title,
            "uploader": uploader,
            "duration": info.get("duration"),
            "thumbnail": thumb if Path(thumb).exists() else "",
            "file_path": file_path,
            "file_type": file_type,
            "genre": info.get("genre"),
            "album": info.get("album"),
            "artist": info.get("artist") or uploader,
            "playlist": playlist_name or info.get("playlist_title"),
            "tags": [],
        }
        tracks.append(track_info)

    return tracks


def search_and_download(query: str, audio_only: bool = False) -> list[dict]:
    url = f"ytsearch1:{query}"
    return download_video(url, audio_only=audio_only)


# ── CLI ───────────────────────────────────────────────────────────────────────

def fmt_duration(secs):
    if not secs:
        return "?"
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def list_library(conn: sqlite3.Connection, query: str = ""):
    if query:
        rows = conn.execute(
            "SELECT * FROM tracks WHERE title LIKE ? OR artist LIKE ? OR genre LIKE ? ORDER BY added_at DESC",
            (f"%{query}%", f"%{query}%", f"%{query}%")
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tracks ORDER BY added_at DESC").fetchall()

    if not rows:
        print("Library is empty.")
        return

    print(f"\n{'ID':<12} {'Title':<45} {'Artist':<25} {'Genre':<15} {'Dur':>7} {'Type':<5}")
    print("-" * 115)
    for r in rows:
        print(f"{r['id']:<12} {r['title'][:44]:<45} {(r['artist'] or '')[:24]:<25} "
              f"{(r['genre'] or '')[:14]:<15} {fmt_duration(r['duration']):>7} {r['file_type']:<5}")
    print(f"\n{len(rows)} track(s)")


def tag_track(conn: sqlite3.Connection, track_id: str, assignments: list[str]):
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if not row:
        print(f"Track {track_id} not found.")
        return
    updates = {}
    for a in assignments:
        if "=" not in a:
            continue
        k, v = a.split("=", 1)
        k = k.strip().lower()
        if k in ("genre", "album", "artist", "playlist"):
            updates[k] = v.strip()
        elif k == "tag":
            tags = json.loads(row["tags"] or "[]")
            tags.append(v.strip())
            updates["tags"] = json.dumps(tags)
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE tracks SET {set_clause} WHERE id = ?",
                     list(updates.values()) + [track_id])
        conn.commit()
        print(f"Updated {track_id}: {updates}")


def delete_track(conn: sqlite3.Connection, track_id: str, keep_file: bool = False):
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if not row:
        print(f"Track {track_id} not found.")
        return
    if not keep_file and row["file_path"] and Path(row["file_path"]).exists():
        Path(row["file_path"]).unlink()
        thumb = Path(row["file_path"]).with_suffix(".jpg")
        if thumb.exists():
            thumb.unlink()
        print(f"Deleted file: {row['file_path']}")
    conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
    conn.commit()
    print(f"Removed {track_id} from library.")


def main():
    parser = argparse.ArgumentParser(description="MusicRepo downloader")
    parser.add_argument("url", nargs="?", help="YouTube URL or search query")
    parser.add_argument("--audio-only", action="store_true", help="Download audio only (mp3)")
    parser.add_argument("--search", metavar="QUERY", help="Search YouTube and download first result")
    parser.add_argument("--playlist", metavar="NAME", help="Label for playlist downloads")
    parser.add_argument("--list", nargs="?", const="", metavar="QUERY", help="List library (optional filter)")
    parser.add_argument("--tag", nargs="+", metavar=("ID", "KEY=VAL"), help="Tag a track: ID genre=Rock")
    parser.add_argument("--delete", metavar="ID", help="Remove a track from the library")
    parser.add_argument("--keep-file", action="store_true", help="Keep file when deleting from library")
    args = parser.parse_args()

    conn = get_db()

    if args.list is not None:
        list_library(conn, args.list)
        return

    if args.tag:
        tag_track(conn, args.tag[0], args.tag[1:])
        return

    if args.delete:
        delete_track(conn, args.delete, keep_file=args.keep_file)
        return

    if args.search:
        tracks = search_and_download(args.search, audio_only=args.audio_only)
    elif args.url:
        tracks = download_video(args.url, audio_only=args.audio_only, playlist_name=args.playlist)
    else:
        parser.print_help()
        return

    for t in tracks:
        upsert_track(conn, t)
        print(f"[✓] Saved: {t['title']} ({t['id']}) → {t['file_path']}")

    print(f"\nAdded {len(tracks)} track(s) to library.")


if __name__ == "__main__":
    main()
