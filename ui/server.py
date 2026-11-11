#!/usr/bin/env python3
"""pianokb web UI: arrange the board, add songs, split a mix into stems.

Serves a small static app on 127.0.0.1 and a JSON API:

    GET  /api/state                 board, songs, benched songs, engine and splitter status
    GET  /api/now                   what the engine is playing
    POST /api/layout                {"layout": {"Song|stem": "key"}}
    POST /api/song                  {"name", "replace"} - empty folder for stems
    PUT  /api/stem/<song>/<file>    raw audio body
    PUT  /api/temp/<file>           raw audio body, returns an upload token
    POST /api/split                 {"song", "token", "replace"}
    POST /api/split-url             {"song", "url", "replace"} - download, then split
    GET  /api/jobs/<id>             split progress and log
    POST /api/restart               restart the pianokb service
    POST /api/bench                 {"song"} - take a song off the board
    POST /api/unbench               {"song"} - put it back
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
ROOT = os.path.dirname(PKG)
sys.path.insert(0, PKG)

import stems  # noqa: E402

STATIC = os.path.join(HERE, "static")
STEMS_DIR = stems.STEMS_DIR
INCOMING = os.path.join(STEMS_DIR, ".incoming")
BENCH_DIR = os.path.join(STEMS_DIR, ".bench")
NOW_FILE = os.path.join(PKG, "now_playing.json")
VENV_PY = os.path.join(PKG, ".venv", "bin", "python")
MODEL = os.environ.get("PIANOKB_MODEL", "htdemucs_6s")
HOST = "127.0.0.1"
PORT = int(os.environ.get("PIANOKB_UI_PORT", "8765"))
MAX_UPLOAD = 800 << 20
SIX_STEMS = ("vocals", "drums", "bass", "other", "piano", "guitar")

_restart_lock = threading.Lock()
_restart_timer: threading.Timer | None = None
_layout_lock = threading.Lock()
_splitter_lock = threading.Lock()
_splitter = {"ready": False, "detail": "checking…"}
_splitter_at = 0.0

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


# -- small helpers -----------------------------------------------------------

def sanitize_song(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", str(name))
    return name.strip(" .")[:80]


def sanitize_file(name: str) -> str:
    base = os.path.basename(str(name).replace("\\", "/"))
    base = re.sub(r"[\x00-\x1f]+", "", base)
    return base.strip(" .")[:120]


def engine_state() -> dict:
    try:
        done = subprocess.run(
            ["systemctl", "--user", "is-active", "pianokb"],
            capture_output=True, text=True, timeout=5,
        )
        text = done.stdout.strip() or "unknown"
        return {"active": text == "active", "text": text}
    except (OSError, subprocess.SubprocessError):
        return {"active": False, "text": "unavailable"}


def restart_engine() -> None:
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", "pianokb"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def schedule_restart(delay: float = 1.5) -> None:
    global _restart_timer
    with _restart_lock:
        if _restart_timer is not None:
            _restart_timer.cancel()
        _restart_timer = threading.Timer(delay, restart_engine)
        _restart_timer.daemon = True
        _restart_timer.start()


def splitter_state(force: bool = False) -> dict:
    """Whether the Demucs venv can be imported (cached, cheap to poll)."""
    global _splitter_at
    with _splitter_lock:
        if not force and _splitter_at and time.time() - _splitter_at < 300:
            return dict(_splitter)
        _splitter_at = time.time()
        if not os.path.exists(VENV_PY):
            _splitter.update(ready=False, detail="splitter venv missing — run pianokb/ui/setup-venv.sh")
            return dict(_splitter)
        try:
            done = subprocess.run(
                [VENV_PY, "-c",
                 "import demucs, torch, yt_dlp; "
                 "print(torch.__version__, yt_dlp.version.__version__)"],
                capture_output=True, text=True, timeout=90,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _splitter.update(ready=False, detail=f"splitter unavailable: {exc}")
            return dict(_splitter)
        if done.returncode == 0:
            parts = done.stdout.split()
            detail = f"demucs · torch {parts[0]}" + (f" · yt-dlp {parts[1]}" if len(parts) > 1 else "")
            _splitter.update(ready=True, detail=detail)
        else:
            tail = (done.stderr or "").strip().splitlines()
            _splitter.update(ready=False, detail=tail[-1] if tail else "demucs not importable")
        return dict(_splitter)


def list_songs() -> list[dict]:
    songs = []
    if not os.path.isdir(STEMS_DIR):
        return songs
    for entry in sorted(os.listdir(STEMS_DIR)):
        if entry.startswith("."):
            continue
        folder = os.path.join(STEMS_DIR, entry)
        if not os.path.isdir(folder):
            continue
        try:
            names = [name for name, _paths in stems._stem_parts(folder)]
        except OSError:
            continue
        if names:
            songs.append({"name": entry, "stems": names})
    return songs


def list_benched() -> list[dict]:
    """Songs parked in stems/.bench: off the board, still on disk."""
    out = []
    if not os.path.isdir(BENCH_DIR):
        return out
    for entry in sorted(os.listdir(BENCH_DIR)):
        folder = os.path.join(BENCH_DIR, entry)
        if entry.startswith(".") or not os.path.isdir(folder):
            continue
        try:
            names = [name for name, _paths in stems._stem_parts(folder)]
        except OSError:
            continue
        if names:
            out.append({"name": entry, "stems": names})
    return out


def now_state() -> dict:
    try:
        with open(NOW_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"active": False}


def board_state() -> dict:
    songs = list_songs()
    with _layout_lock:
        layout = stems.load_keymap()
    rows = [(song["name"], song["stems"]) for song in songs]
    _chosen, by_char = stems.assign_keys(rows, layout)
    by_key = {
        ch: {"song": songs[d]["name"], "stem": songs[d]["stems"][s]}
        for ch, (d, s) in by_char.items()
    }
    total = sum(len(song["stems"]) for song in songs)
    return {
        "rows": stems.KEY_ROWS,
        "layout": layout,
        "songs": songs,
        "benched": list_benched(),
        "byKey": by_key,
        "capacity": {"keys": sum(len(row) for row in stems.KEY_ROWS),
                     "stems": total, "hidden": max(0, total - len(by_key))},
        "engine": engine_state(),
        "splitter": splitter_state(),
        "model": MODEL,
        "stemsDir": STEMS_DIR,
    }


def clean_layout(layout: dict) -> dict:
    flat = {k for row in stems.KEY_ROWS for k in row}
    clean: dict[str, str] = {}
    for key, ch in layout.items():
        if not isinstance(key, str) or "|" not in key:
            continue
        if isinstance(ch, str) and ch.lower() in flat:
            clean[key] = ch.lower()
    return clean


# -- jobs --------------------------------------------------------------------

def job_log(job: dict, text: str) -> None:
    line = text.strip()
    if not line:
        return
    job["log"].append(line[:400])
    if len(job["log"]) > 250:
        del job["log"][:-250]


def start_split(song: str, src: str, replace: bool) -> dict:
    job = {
        "id": uuid.uuid4().hex[:10], "song": song, "state": "running",
        "stage": "preparing", "progress": 0.0, "log": [], "error": "",
        "started": time.time(),
    }
    with JOBS_LOCK:
        JOBS[job["id"]] = job
    threading.Thread(target=split_job, args=(job, song, src, replace), daemon=True).start()
    return job


def split_job(job: dict, song: str, src: str, replace: bool, work: str | None = None) -> None:
    work = work or os.path.join(INCOMING, job["id"])
    out = os.path.join(work, "separated")
    try:
        os.makedirs(out, exist_ok=True)
        if not os.path.exists(VENV_PY):
            raise RuntimeError("splitter not installed — run pianokb/ui/setup-venv.sh")
        cmd = [VENV_PY, "-m", "demucs", "-n", MODEL, "-o", out, src]
        job["stage"] = f"splitting with {MODEL}"
        job_log(job, " ".join(cmd))
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
        )
        assert proc.stdout is not None
        buf = b""
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                match = re.search(rb"[\r\n]", buf)
                if not match:
                    break
                line, buf = buf[:match.start()], buf[match.end():]
                text = line.decode("utf-8", "replace")
                job_log(job, text)
                pct = re.search(r"(\d{1,3})%", text)
                if pct:
                    job["progress"] = min(0.99, int(pct.group(1)) / 100)
        code = proc.wait()
        if code != 0:
            raise RuntimeError(f"demucs exited with status {code}")

        job["stage"] = "collecting stems"
        made: dict[str, str] = {}
        for path in glob.glob(os.path.join(out, "**", "*.wav"), recursive=True):
            made[os.path.splitext(os.path.basename(path))[0].lower()] = path
        wanted = [name for name in SIX_STEMS if name in made]
        if not wanted:
            raise RuntimeError("demucs produced no stems")

        job["stage"] = "installing stems"
        target = os.path.join(STEMS_DIR, song)
        if os.path.isdir(target) and not replace:
            raise FileExistsError(f"“{song}” already exists")
        staging = os.path.join(work, "final")
        os.makedirs(staging, exist_ok=True)
        for name in wanted:
            shutil.copyfile(made[name], os.path.join(staging, f"{name}.wav"))
        if os.path.isdir(target):
            shutil.rmtree(target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.rename(staging, target)

        job.update(state="done", progress=1.0, stage=f"added {len(wanted)} stems")
        schedule_restart(2.0)
    except BaseException as exc:                    # report anything to the UI
        job.update(state="error", error=str(exc) or type(exc).__name__)
        job_log(job, f"error: {exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def start_url_split(song: str, url: str, replace: bool) -> dict:
    job = {
        "id": uuid.uuid4().hex[:10], "song": song, "state": "running",
        "stage": "queued", "progress": 0.0, "log": [], "error": "",
        "started": time.time(),
    }
    with JOBS_LOCK:
        JOBS[job["id"]] = job
    threading.Thread(target=url_split_job, args=(job, song, url, replace), daemon=True).start()
    return job


def url_split_job(job: dict, song: str, url: str, replace: bool) -> None:
    """Fetch a link with yt-dlp, then run the normal Demucs split on it."""
    work = os.path.join(INCOMING, job["id"])
    try:
        os.makedirs(work, exist_ok=True)
        if not os.path.exists(VENV_PY):
            raise RuntimeError("splitter not installed — run pianokb/ui/setup-venv.sh")
        cmd = [
            VENV_PY, "-m", "yt_dlp", "--no-playlist", "--newline",
            "--js-runtimes", "node", "-x", "--audio-format", "wav",
            "-o", os.path.join(work, "source.%(ext)s"), url,
        ]
        job["stage"] = "downloading audio"
        job_log(job, " ".join(cmd))
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
        )
        assert proc.stdout is not None
        buf = b""
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                match = re.search(rb"[\r\n]", buf)
                if not match:
                    break
                line, buf = buf[:match.start()], buf[match.end():]
                text = line.decode("utf-8", "replace")
                job_log(job, text)
                pct = re.search(r"(\d{1,3}(?:\.\d+)?)%", text)
                if pct:
                    job["progress"] = min(0.3, 0.3 * float(pct.group(1)) / 100)
        code = proc.wait()
        if code != 0:
            raise RuntimeError(f"yt-dlp exited with status {code}")

        found = [
            path for path in glob.glob(os.path.join(work, "source.*"))
            if os.path.splitext(path)[1].lower() in stems.AUDIO_EXT
        ]
        if not found:
            raise RuntimeError("yt-dlp produced no audio file")
        job["progress"] = 0.3
        split_job(job, song, found[0], replace, work=work)
    except BaseException as exc:                    # report anything to the UI
        job.update(state="error", error=str(exc) or type(exc).__name__)
        job_log(job, f"error: {exc}")
        shutil.rmtree(work, ignore_errors=True)


# -- http --------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "pianokb-ui"
    protocol_version = "HTTP/1.1"

    # responses -------------------------------------------------------------
    def send_bytes(self, data: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, obj, code: int = 200) -> None:
        self.send_bytes(json.dumps(obj).encode(), "application/json", code)

    def fail(self, exc: Exception) -> None:
        if isinstance(exc, FileExistsError):
            code = 409
        elif isinstance(exc, (ValueError, KeyError)):
            code = 400
        else:
            code = 500
        self.send_json({"ok": False, "error": str(exc) or type(exc).__name__}, code)

    def log_message(self, fmt, *args):              # quiet, journal-friendly
        sys.stderr.write("ui: " + (fmt % args) + "\n")

    # body ------------------------------------------------------------------
    def json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > (1 << 20):
            raise ValueError("missing or oversized JSON body")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def save_body(self, dest: str, limit: int = MAX_UPLOAD) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("empty upload")
        if length > limit:
            raise ValueError("upload too large")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        left = length
        with open(part, "wb") as fh:
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                fh.write(chunk)
                left -= len(chunk)
        if left:
            os.remove(part)
            raise ValueError("upload cut short")
        os.replace(part, dest)

    # routes ----------------------------------------------------------------
    def do_GET(self) -> None:
        try:
            self.route_get(urlparse(self.path).path)
        except Exception as exc:
            self.fail(exc)

    def do_POST(self) -> None:
        try:
            self.route_post(urlparse(self.path).path)
        except Exception as exc:
            self.fail(exc)

    def do_PUT(self) -> None:
        try:
            self.route_put(urlparse(self.path).path)
        except Exception as exc:
            self.fail(exc)

    def route_get(self, path: str) -> None:
        if path == "/api/state":
            return self.send_json(board_state())
        if path == "/api/now":
            return self.send_json(now_state())
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if job is None:
                raise ValueError("no such job")
            return self.send_json(job)
        return self.serve_static(path)

    def route_post(self, path: str) -> None:
        if path == "/api/layout":
            body = self.json_body()
            layout = body.get("layout")
            if not isinstance(layout, dict):
                raise ValueError("layout must be an object")
            clean = clean_layout(layout)
            with _layout_lock:
                stems.save_keymap(clean)
            schedule_restart()
            return self.send_json({"ok": True, "layout": clean})

        if path == "/api/restart":
            restart_engine()
            return self.send_json({"ok": True})

        if path in ("/api/bench", "/api/unbench"):
            body = self.json_body()
            name = sanitize_song(body.get("song", ""))
            if not name:
                raise ValueError("name the song")
            benching = path == "/api/bench"
            src = os.path.join(STEMS_DIR if benching else BENCH_DIR, name)
            dst = os.path.join(BENCH_DIR if benching else STEMS_DIR, name)
            if not os.path.isdir(src):
                raise ValueError(f"“{name}” is not on the board" if benching
                                 else f"“{name}” is not benched")
            if os.path.exists(dst):
                raise FileExistsError(f"“{name}” already exists")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
            schedule_restart(1.0)
            return self.send_json({"ok": True, "name": name})

        if path == "/api/song":
            body = self.json_body()
            name = sanitize_song(body.get("name", ""))
            if not name:
                raise ValueError("give the song a name")
            folder = os.path.join(STEMS_DIR, name)
            if os.path.isdir(folder):
                if not body.get("replace"):
                    raise FileExistsError(f"“{name}” already exists")
                shutil.rmtree(folder)
            os.makedirs(folder, exist_ok=True)
            return self.send_json({"ok": True, "name": name})

        if path == "/api/split-url":
            body = self.json_body()
            name = sanitize_song(body.get("song", ""))
            if not name:
                raise ValueError("give the song a name")
            url = str(body.get("url", "")).strip()
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("paste an http(s) link")
            if len(url) > 2048:
                raise ValueError("link too long")
            job = start_url_split(name, url, bool(body.get("replace")))
            return self.send_json({"ok": True, "job": job["id"]})

        if path == "/api/split":
            body = self.json_body()
            name = sanitize_song(body.get("song", ""))
            if not name:
                raise ValueError("give the song a name")
            token = str(body.get("token", ""))
            if not re.fullmatch(r"[0-9a-f]{10}", token):
                raise ValueError("bad upload token")
            files = [p for p in glob.glob(os.path.join(INCOMING, token, "*"))
                     if os.path.isfile(p)]
            if not files:
                raise ValueError("uploaded file not found")
            job = start_split(name, files[0], bool(body.get("replace")))
            return self.send_json({"ok": True, "job": job["id"]})

        raise ValueError("not found")

    def route_put(self, path: str) -> None:
        if path.startswith("/api/temp/"):
            filename = sanitize_file(unquote(path[len("/api/temp/"):]))
            if not filename or os.path.splitext(filename)[1].lower() not in stems.AUDIO_EXT:
                raise ValueError("not an audio file")
            token = uuid.uuid4().hex[:10]
            self.save_body(os.path.join(INCOMING, token, filename))
            return self.send_json({"ok": True, "token": token, "file": filename})

        if path.startswith("/api/stem/"):
            rest = unquote(path[len("/api/stem/"):])
            if "/" not in rest:
                raise ValueError("expected song/file")
            song, filename = rest.split("/", 1)
            song, filename = sanitize_song(song), sanitize_file(filename)
            if not song or not filename:
                raise ValueError("bad song or file name")
            if os.path.splitext(filename)[1].lower() not in stems.AUDIO_EXT:
                raise ValueError(f"not an audio file: {filename}")
            self.save_body(os.path.join(STEMS_DIR, song, filename))
            schedule_restart(2.0)
            return self.send_json({"ok": True})

        raise ValueError("not found")

    def serve_static(self, path: str) -> None:
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        full = os.path.abspath(os.path.join(STATIC, name))
        if not full.startswith(STATIC + os.sep) or not os.path.isfile(full):
            return self.send_json({"ok": False, "error": "not found"}, 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(ext, "application/octet-stream")
        with open(full, "rb") as fh:
            self.send_bytes(fh.read(), ctype)


def main() -> None:
    port = PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    os.makedirs(INCOMING, exist_ok=True)
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    print(f"  pianokb ui: http://{HOST}:{port}/")
    print(f"  stems: {STEMS_DIR}")
    print(f"  splitter: {splitter_state(force=True)['detail']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
