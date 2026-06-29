#!/usr/bin/env python3
"""
MusicRepo local web server — offline player at http://localhost:8765
"""

import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import enricher as _enricher

BASE_DIR = Path(__file__).parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DB_PATH = BASE_DIR / "data" / "library.db"
STATIC_DIR = BASE_DIR / "static"
PORT = 8765

# ── Enrich job state ─────────────────────────────────────────────────────────
enrich_state: dict = {
    "status": "idle",   # idle | running | done | error
    "current": 0, "total": 0, "current_title": "",
    "updated": 0, "failed": 0, "error": None,
}
enrich_lock = threading.Lock()


def _run_enrich(force: bool):
    with enrich_lock:
        enrich_state.update({"status": "running", "current": 0, "total": 0,
                              "updated": 0, "failed": 0, "error": None})

    def progress(i, total, title):
        with enrich_lock:
            enrich_state["current"] = i
            enrich_state["total"] = total
            enrich_state["current_title"] = title

    try:
        result = _enricher.enrich_all(force=force, progress_cb=progress)
        with enrich_lock:
            enrich_state.update({
                "status": "done",
                "current": result["total"],
                "total": result["total"],
                "updated": result["updated"],
                "failed": result["failed"],
            })
    except Exception as e:
        with enrich_lock:
            enrich_state.update({"status": "error", "error": str(e)})


# ── Download job queue ────────────────────────────────────────────────────────
# jobs[id] = {status, progress, log, error, tracks, url, started_at}
jobs: dict = {}
jobs_lock = threading.Lock()


def _upsert_track(conn, info: dict):
    conn.execute("""
        INSERT INTO tracks
            (id, title, uploader, duration, thumbnail, file_path, file_type,
             genre, album, artist, playlist, tags, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            file_path = excluded.file_path,
            file_type = excluded.file_type
    """, (
        info["id"], info["title"], info.get("uploader"),
        info.get("duration"), info.get("thumbnail"),
        info["file_path"], info["file_type"],
        info.get("genre"), info.get("album"),
        info.get("artist") or info.get("uploader"),
        info.get("playlist"), "[]", datetime.now().isoformat()
    ))
    conn.commit()


def _run_download(job_id: str, url: str, audio_only: bool, playlist_name: str):
    def log(msg):
        with jobs_lock:
            jobs[job_id]["log"].append(msg)

    with jobs_lock:
        jobs[job_id]["status"] = "running"

    try:
        fmt = "bestaudio/best" if audio_only else "bestvideo+bestaudio/best"
        # Track which .info.json files exist before download so we can find new ones after
        before = set(DOWNLOADS_DIR.glob("*.info.json"))

        cmd = [
            "yt-dlp",
            "--format", fmt,
            "--output", str(DOWNLOADS_DIR / "%(id)s.%(ext)s"),
            "--write-thumbnail", "--convert-thumbnails", "jpg",
            "--embed-metadata",
            "--write-info-json",   # saves <id>.info.json with full metadata
            "--progress",
            "--newline",
        ]
        if audio_only:
            cmd += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]
        else:
            cmd += ["--merge-output-format", "mp4"]
        if "list=" not in url and "playlist" not in url.lower():
            cmd += ["--no-playlist"]
        cmd.append(url)

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        current_title = ""
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            m = re.search(r'\[download\] Destination: .+?/([^/]+)\.(mp4|mp3|webm|mkv|m4a)', line)
            if m:
                current_title = m.group(1)
            m = re.search(r'\[download\]\s+([\d.]+)%', line)
            if m:
                pct = float(m.group(1))
                with jobs_lock:
                    jobs[job_id]["progress"] = pct
                    jobs[job_id]["current_title"] = current_title
            log(line)

        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("yt-dlp exited with error — check log for details.")

        # Read metadata from newly written .info.json files
        after = set(DOWNLOADS_DIR.glob("*.info.json"))
        new_info_files = after - before
        # If no new ones detected, fall back to all info.json files that have a matching media file
        if not new_info_files:
            new_info_files = after

        conn = get_db()
        saved = []
        for info_path in sorted(new_info_files):
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            vid_id = info.get("id", "")
            if not vid_id:
                continue
            # Find the actual media file
            file_path = None
            ext = None
            for e in (["mp3"] if audio_only else ["mp4", "mkv", "webm", "m4a", "mp3"]):
                fp = DOWNLOADS_DIR / f"{vid_id}.{e}"
                if fp.exists():
                    file_path = str(fp)
                    ext = e
                    break
            if not file_path:
                continue
            # Skip if already cataloged (when falling back to all info files)
            if not (after - before):
                existing = conn.execute("SELECT id FROM tracks WHERE id=?", (vid_id,)).fetchone()
                if existing:
                    continue
            thumb = DOWNLOADS_DIR / f"{vid_id}.jpg"
            track = {
                "id": vid_id,
                "title": info.get("title", vid_id),
                "uploader": info.get("uploader") or info.get("channel", ""),
                "duration": info.get("duration"),
                "thumbnail": str(thumb) if thumb.exists() else "",
                "file_path": file_path,
                "file_type": ext,
                "genre": info.get("genre"),
                "album": info.get("album"),
                "artist": info.get("artist") or info.get("uploader") or info.get("channel"),
                "playlist": playlist_name or info.get("playlist_title"),
            }
            _upsert_track(conn, track)
            saved.append(track)
        conn.close()

        with jobs_lock:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100
            jobs[job_id]["tracks"] = saved
            jobs[job_id]["track_count"] = len(saved)

        # Auto-enrich new tracks in the background
        threading.Thread(target=_run_enrich, args=(False,), daemon=True).start()

    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)


def get_db():
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default access log

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, status=200):
        mime, _ = mimetypes.guess_type(str(path))
        mime = mime or "application/octet-stream"
        size = path.stat().st_size
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", size)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(path, "rb") as f:
            self.wfile.write(f.read())

    def send_partial_file(self, path: Path, range_header: str):
        size = path.stat().st_size
        mime, _ = mimetypes.guess_type(str(path))
        mime = mime or "application/octet-stream"
        range_val = range_header.replace("bytes=", "")
        start_str, end_str = range_val.split("-")
        start = int(start_str)
        end = int(end_str) if end_str else size - 1
        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", length)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            self.wfile.write(f.read(length))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = dict(urllib.parse.parse_qsl(parsed.query))

        # ── API ──────────────────────────────────────────────────────────────
        if path == "/api/tracks":
            conn = get_db()
            q = params.get("q", "")
            genre = params.get("genre", "")
            artist = params.get("artist", "")
            playlist = params.get("playlist", "")
            order = params.get("order", "added_at")
            safe_order = order if order in ("added_at", "title", "artist", "play_count", "duration") else "added_at"

            where, vals = ["1=1"], []
            if q:
                where.append("(title LIKE ? OR artist LIKE ? OR uploader LIKE ?)")
                vals += [f"%{q}%", f"%{q}%", f"%{q}%"]
            if genre:
                where.append("genre = ?"); vals.append(genre)
            if artist:
                where.append("(artist = ? OR uploader = ?)"); vals += [artist, artist]
            if playlist:
                where.append("playlist = ?"); vals.append(playlist)

            rows = conn.execute(
                f"SELECT * FROM tracks WHERE {' AND '.join(where)} ORDER BY {safe_order} DESC",
                vals
            ).fetchall()
            conn.close()
            self.send_json([dict(r) for r in rows])

        elif path == "/api/filters":
            conn = get_db()
            genres   = [r[0] for r in conn.execute("SELECT DISTINCT genre   FROM tracks WHERE genre IS NOT NULL ORDER BY genre").fetchall()]
            artists  = [r[0] for r in conn.execute("SELECT DISTINCT artist  FROM tracks WHERE artist IS NOT NULL ORDER BY artist").fetchall()]
            playlists= [r[0] for r in conn.execute("SELECT DISTINCT playlist FROM tracks WHERE playlist IS NOT NULL ORDER BY playlist").fetchall()]
            conn.close()
            self.send_json({"genres": genres, "artists": artists, "playlists": playlists})

        elif path == "/api/stats":
            conn = get_db()
            total   = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            videos  = conn.execute("SELECT COUNT(*) FROM tracks WHERE file_type = 'mp4'").fetchone()[0]
            audio   = conn.execute("SELECT COUNT(*) FROM tracks WHERE file_type = 'mp3'").fetchone()[0]
            mins    = conn.execute("SELECT SUM(duration) FROM tracks").fetchone()[0] or 0
            conn.close()
            self.send_json({"total": total, "videos": videos, "audio": audio, "total_minutes": int(mins // 60)})

        elif path.startswith("/api/play/"):
            track_id = path.split("/api/play/")[1]
            conn = get_db()
            conn.execute("UPDATE tracks SET play_count = play_count + 1, last_played = datetime('now') WHERE id = ?", (track_id,))
            conn.commit()
            conn.close()
            self.send_json({"ok": True})

        elif path == "/api/enrich":
            with enrich_lock:
                self.send_json(dict(enrich_state))

        elif path == "/api/jobs":
            with jobs_lock:
                # return all jobs, newest first, omit verbose log unless requested
                include_log = params.get("log") == "1"
                result = []
                for j in sorted(jobs.values(), key=lambda x: x["started_at"], reverse=True):
                    entry = {k: v for k, v in j.items() if k != "log"}
                    if include_log and params.get("id") == j["id"]:
                        entry["log"] = j["log"]
                    result.append(entry)
            self.send_json(result)

        elif path.startswith("/api/jobs/"):
            job_id = path.split("/api/jobs/")[1]
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                self.send_json({"error": "not found"}, 404)
                return
            include_log = params.get("log") == "1"
            entry = {k: v for k, v in job.items() if k != "log" or include_log}
            self.send_json(entry)

        elif path.startswith("/api/tag/"):
            track_id = path.split("/api/tag/")[1]
            # GET /api/tag/<id>?genre=Rock&artist=X
            conn = get_db()
            allowed = ("genre", "album", "artist", "playlist")
            updates = {k: v for k, v in params.items() if k in allowed}
            if updates:
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(f"UPDATE tracks SET {set_clause} WHERE id = ?",
                             list(updates.values()) + [track_id])
                conn.commit()
            conn.close()
            self.send_json({"ok": True})

        # ── Media files ───────────────────────────────────────────────────────
        elif path.startswith("/media/"):
            filename = urllib.parse.unquote(path.split("/media/")[1])
            file_path = DOWNLOADS_DIR / filename
            if not file_path.exists():
                self.send_json({"error": "not found"}, 404)
                return
            range_header = self.headers.get("Range")
            if range_header:
                self.send_partial_file(file_path, range_header)
            else:
                self.send_file(file_path)

        # ── Frontend ──────────────────────────────────────────────────────────
        elif path == "/" or path == "/index.html":
            html_path = STATIC_DIR / "index.html"
            self.send_file(html_path)

        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if path == "/api/enrich":
            with enrich_lock:
                if enrich_state["status"] == "running":
                    self.send_json({"error": "already running"}, 409)
                    return
            force = bool(body.get("force", False))
            t = threading.Thread(target=_run_enrich, args=(force,), daemon=True)
            t.start()
            self.send_json({"ok": True})

        elif path == "/api/download":
            url = (body.get("url") or "").strip()
            if not url:
                self.send_json({"error": "url required"}, 400)
                return
            audio_only = bool(body.get("audio_only", False))
            playlist_name = (body.get("playlist") or "").strip() or None
            job_id = str(uuid.uuid4())[:8]
            with jobs_lock:
                jobs[job_id] = {
                    "id": job_id, "status": "queued", "progress": 0,
                    "log": [], "error": None, "tracks": [],
                    "track_count": 0, "current_title": "",
                    "url": url, "audio_only": audio_only,
                    "started_at": datetime.now().isoformat(),
                }
            t = threading.Thread(target=_run_download, args=(job_id, url, audio_only, playlist_name), daemon=True)
            t.start()
            self.send_json({"job_id": job_id})

        else:
            self.send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()


def main():
    import webbrowser, threading
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    httpd = HTTPServer(("", PORT), Handler)
    print(f"[MusicRepo] Server running at http://localhost:{PORT}")
    print(f"[MusicRepo] Library: {DB_PATH}")
    print(f"[MusicRepo] Press Ctrl+C to stop.\n")
    threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[MusicRepo] Stopped.")


if __name__ == "__main__":
    main()
