#!/usr/bin/env python3
"""
Web UI for casting media to the FUDONI GC888 projector.

Browse local files, cast with one click, and control playback from any
browser on the network (phone/tablet/laptop). No client install needed.

Usage:
    ./castweb.py
    ./castweb.py --port 9090 --root /mnt/media

Requires: adb, ffmpeg, ffprobe
"""

import argparse
import html
import json
import mimetypes
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECTOR_IP = "192.168.86.248"
ADB_TARGET = f"{PROJECTOR_IP}:5555"

BT_INCOMPATIBLE_CODECS = {"ac3", "eac3", "dts", "dts_hd", "truehd", "mlp"}

MIME_FALLBACKS = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".ts": "video/mp2t",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
}

MEDIA_EXTENSIONS = set(MIME_FALLBACKS.keys())

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
state_lock = threading.Lock()
state = {
    "casting": False,
    "file": None,
    "transcoding": False,
    "transcode_progress": "",
    "tmp_file": None,
    "_serve_mode": None,
}

dsp_lock = threading.Lock()
dsp_settings = {"bass": 0, "mid": 0, "treble": 0, "loudnorm": False}

live_stream = {
    "proc": None,        # ffmpeg subprocess
    "file_path": None,   # source file being streamed
    "start_time": None,  # wall-clock time when playback started
    "seek_offset": 0,    # seconds seeked into the file at start
}

# Set by main() at startup
BROWSE_ROOT = ""
LOCAL_IP = ""
PORT = 0

# ---------------------------------------------------------------------------
# Audio probe & transcode
# ---------------------------------------------------------------------------
def get_audio_codec(file_path):
    """Return the audio codec name of the first audio stream, or None."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "json", file_path],
            capture_output=True, text=True, timeout=30,
        )
        info = json.loads(r.stdout)
        streams = info.get("streams", [])
        if streams:
            return streams[0].get("codec_name", "").lower()
    except Exception:
        pass
    return None


def build_audio_filters(dsp):
    """Build ffmpeg -af filter chain string from DSP settings. Returns None if flat."""
    filters = []
    bass, mid, treble = dsp["bass"], dsp["mid"], dsp["treble"]
    max_gain = max(bass, mid, treble, 0)

    # Headroom to prevent clipping when boosting
    if max_gain > 0:
        filters.append(f"volume=-{max_gain}dB")

    if bass != 0:
        filters.append(f"bass=g={bass}:f=100")
    if mid != 0:
        filters.append(f"equalizer=f=1000:t=o:w=1.0:g={mid}")
    if treble != 0:
        filters.append(f"treble=g={treble}:f=3000")

    if dsp["loudnorm"]:
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")

    return ",".join(filters) if filters else None


def needs_transcode(audio_codec, dsp):
    """Return True if codec is BT-incompatible OR any DSP setting is non-flat."""
    if audio_codec and audio_codec in BT_INCOMPATIBLE_CODECS:
        return True
    if dsp["bass"] != 0 or dsp["mid"] != 0 or dsp["treble"] != 0:
        return True
    if dsp["loudnorm"]:
        return True
    return False


def dsp_is_nonflat(dsp):
    """Return True if any EQ slider is non-zero or loudnorm is on."""
    return dsp["bass"] != 0 or dsp["mid"] != 0 or dsp["treble"] != 0 or dsp["loudnorm"]


def start_live_ffmpeg(file_path, dsp, seek_pos=0):
    """Start an ffmpeg process that streams MPEG-TS to stdout."""
    # Kill any existing live stream process
    if live_stream["proc"] is not None:
        try:
            live_stream["proc"].kill()
            live_stream["proc"].wait()
        except Exception:
            pass

    af = build_audio_filters(dsp)
    cmd = ["ffmpeg", "-ss", str(seek_pos), "-i", file_path]
    if af:
        cmd += ["-af", af]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-ac", "2", "-b:a", "192k",
            "-f", "mpegts", "pipe:1"]

    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    live_stream["proc"] = proc
    live_stream["file_path"] = file_path
    live_stream["start_time"] = time.time()
    live_stream["seek_offset"] = seek_pos

    return proc


def recast_with_new_dsp():
    """Restart ffmpeg with new DSP settings and tell the projector to reconnect."""
    file_path = live_stream["file_path"]
    if not file_path:
        return

    # Approximate current position in the file
    seek_pos = live_stream["seek_offset"] + (time.time() - live_stream["start_time"])

    with dsp_lock:
        dsp = dict(dsp_settings)

    start_live_ffmpeg(file_path, dsp, seek_pos=seek_pos)

    url = f"http://{LOCAL_IP}:{PORT}/media/live.ts"
    adb_open_url(url, "video/mp2t")


def transcode_audio(input_path, audio_filters=None):
    """Transcode audio to AAC stereo, copy video. Updates state with progress."""
    base = os.path.splitext(os.path.basename(input_path))[0]
    out_fd, out_path = tempfile.mkstemp(suffix=".mp4", prefix=f"{base}.")
    os.close(out_fd)

    with state_lock:
        state["transcoding"] = True
        state["transcode_progress"] = "starting..."

    cmd = ["ffmpeg", "-y", "-i", input_path]
    if audio_filters:
        cmd += ["-af", audio_filters]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-ac", "2", "-b:a", "192k", out_path]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    while proc.poll() is None:
        line = proc.stderr.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace")
        if "time=" in text:
            for part in text.split():
                if part.startswith("time="):
                    with state_lock:
                        state["transcode_progress"] = part

    proc.wait()

    with state_lock:
        state["transcoding"] = False

    if proc.returncode != 0:
        os.unlink(out_path)
        return None

    return out_path


# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------
def adb(*args):
    cmd = ["adb", "-s", ADB_TARGET] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


def adb_key(keycode):
    adb("shell", "input", "keyevent", keycode)


def adb_connect():
    r = subprocess.run(
        ["adb", "connect", ADB_TARGET],
        capture_output=True, text=True, timeout=10,
    )
    out = r.stdout.strip()
    if "connected" not in out and "already" not in out:
        sys.exit(f"adb connect failed: {out}")


def adb_open_url(url, mime):
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "settings", "put", "secure", "screensaver_enabled", "0")
    adb(
        "shell", "am", "start",
        "-a", "android.intent.action.VIEW",
        "-d", url,
        "-t", mime,
    )


def adb_stop():
    adb_key("KEYCODE_BACK")


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((PROJECTOR_IP, 80))
        return s.getsockname()[0]
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------
def safe_resolve(path):
    """Resolve path and verify it's under BROWSE_ROOT. Returns None if unsafe."""
    real = os.path.realpath(path)
    if real == BROWSE_ROOT or real.startswith(BROWSE_ROOT + os.sep):
        return real
    return None


# ---------------------------------------------------------------------------
# Cast logic (runs in background thread)
# ---------------------------------------------------------------------------
def do_cast(file_path):
    """Transcode if needed, then tell projector to play. Runs in a thread."""
    serve_path = file_path
    tmp = None

    with dsp_lock:
        dsp = dict(dsp_settings)

    acodec = get_audio_codec(file_path)

    if needs_transcode(acodec, dsp) and dsp_is_nonflat(dsp):
        # Live streaming mode — pipe through ffmpeg in real-time
        start_live_ffmpeg(file_path, dsp)

        with state_lock:
            state["tmp_file"] = None
            state["transcode_progress"] = ""
            state["casting"] = True
            state["_serve_mode"] = "live"
            state["_serve_path"] = None

        url = f"http://{LOCAL_IP}:{PORT}/media/live.ts"
        adb_open_url(url, "video/mp2t")
        return

    if needs_transcode(acodec, dsp):
        # Codec is BT-incompatible but DSP is flat — pre-transcode (preserves Range/seek)
        af = build_audio_filters(dsp)
        tmp = transcode_audio(file_path, audio_filters=af)
        if tmp is None:
            with state_lock:
                state["casting"] = False
                state["file"] = None
                state["transcode_progress"] = "transcode failed"
            return
        serve_path = tmp

    with state_lock:
        state["tmp_file"] = tmp
        state["transcode_progress"] = ""
        state["_serve_mode"] = "file"

    ext = os.path.splitext(serve_path)[1].lower()
    mime = mimetypes.guess_type(serve_path)[0] or MIME_FALLBACKS.get(ext, "video/mp4")

    # Build the media URL — encode the served filename
    fname = urllib.parse.quote(os.path.basename(serve_path))
    url = f"http://{LOCAL_IP}:{PORT}/media/{fname}"

    adb_open_url(url, mime)

    with state_lock:
        state["casting"] = True
        # Store the actual path being served so the media handler can find it
        state["_serve_path"] = serve_path


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
class WebHandler(BaseHTTPRequestHandler):

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, content_type="text/html", status=200):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    # --- Routing -----------------------------------------------------------

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/":
            self.handle_index()
        elif path == "/api/browse":
            self.handle_browse(parsed.query)
        elif path == "/api/status":
            self.handle_status()
        elif path.startswith("/media/"):
            self.handle_media(path, send_body=True)
        else:
            self.send_error(404)

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/media/"):
            self.handle_media(parsed.path, send_body=False)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/cast":
            self.handle_cast()
        elif path == "/api/stop":
            self.handle_stop()
        elif path == "/api/control":
            self.handle_control()
        elif path == "/api/dsp":
            self.handle_dsp()
        else:
            self.send_error(404)

    # --- Handlers ----------------------------------------------------------

    def handle_index(self):
        self.send_text(HTML_PAGE)

    def handle_browse(self, query):
        params = urllib.parse.parse_qs(query)
        raw_path = params.get("path", [BROWSE_ROOT])[0]
        resolved = safe_resolve(raw_path)
        if resolved is None or not os.path.isdir(resolved):
            self.send_json({"error": "invalid path"}, 400)
            return

        entries = []
        try:
            names = sorted(os.listdir(resolved), key=str.lower)
        except PermissionError:
            self.send_json({"error": "permission denied"}, 403)
            return

        for name in names:
            if name.startswith("."):
                continue
            full = os.path.join(resolved, name)
            if os.path.isdir(full):
                entries.append({"name": name, "type": "dir", "path": full})
            else:
                ext = os.path.splitext(name)[1].lower()
                if ext in MEDIA_EXTENSIONS:
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        size = 0
                    entries.append({
                        "name": name, "type": "file", "path": full,
                        "size": size,
                    })

        self.send_json({"path": resolved, "entries": entries})

    def handle_status(self):
        with state_lock:
            s = {
                "casting": state["casting"],
                "file": os.path.basename(state["file"]) if state["file"] else None,
                "transcoding": state["transcoding"],
                "transcode_progress": state["transcode_progress"],
            }
        with dsp_lock:
            s["dsp"] = dict(dsp_settings)
        self.send_json(s)

    def handle_cast(self):
        body = json.loads(self.read_body())
        file_path = body.get("path", "")
        resolved = safe_resolve(file_path)
        if resolved is None or not os.path.isfile(resolved):
            self.send_json({"error": "invalid file"}, 400)
            return

        # Stop any current cast first
        cleanup_cast()

        with state_lock:
            state["casting"] = False
            state["file"] = resolved
            state["transcoding"] = False
            state["transcode_progress"] = "checking audio..."

        threading.Thread(target=do_cast, args=(resolved,), daemon=True).start()
        self.send_json({"ok": True, "file": os.path.basename(resolved)})

    def handle_stop(self):
        adb_stop()
        cleanup_cast()
        self.send_json({"ok": True})

    def handle_control(self):
        body = json.loads(self.read_body())
        action = body.get("action", "")
        keymap = {
            "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE",
            "stop": "KEYCODE_MEDIA_STOP",
            "seek_fwd": "KEYCODE_DPAD_RIGHT",
            "seek_back": "KEYCODE_DPAD_LEFT",
            "ff": "KEYCODE_MEDIA_FAST_FORWARD",
            "rw": "KEYCODE_MEDIA_REWIND",
            "vol_up": "KEYCODE_VOLUME_UP",
            "vol_down": "KEYCODE_VOLUME_DOWN",
            "mute": "KEYCODE_VOLUME_MUTE",
        }
        keycode = keymap.get(action)
        if not keycode:
            self.send_json({"error": f"unknown action: {action}"}, 400)
            return
        threading.Thread(target=adb_key, args=(keycode,), daemon=True).start()
        self.send_json({"ok": True})

    def handle_dsp(self):
        body = json.loads(self.read_body())
        errors = []
        for key in ("bass", "mid", "treble"):
            if key in body:
                val = body[key]
                if not isinstance(val, (int, float)) or val < -12 or val > 12:
                    errors.append(f"{key} must be a number between -12 and 12")
        if "loudnorm" in body and not isinstance(body["loudnorm"], bool):
            errors.append("loudnorm must be a boolean")
        if errors:
            self.send_json({"error": "; ".join(errors)}, 400)
            return

        with dsp_lock:
            for key in ("bass", "mid", "treble"):
                if key in body:
                    dsp_settings[key] = int(body[key])
            if "loudnorm" in body:
                dsp_settings["loudnorm"] = body["loudnorm"]
            result = dict(dsp_settings)

        # Trigger live re-cast if currently casting
        live_update = False
        with state_lock:
            casting = state["casting"]
            serve_mode = state.get("_serve_mode")
            cast_file = state.get("file")

        if casting and serve_mode == "live":
            # Already in live mode — restart ffmpeg with new DSP
            threading.Thread(target=recast_with_new_dsp, daemon=True).start()
            live_update = True
        elif casting and serve_mode == "file" and dsp_is_nonflat(result) and cast_file:
            # Transition from file mode to live mode
            def switch_to_live():
                start_live_ffmpeg(cast_file, result)
                with state_lock:
                    state["_serve_mode"] = "live"
                    state["_serve_path"] = None
                url = f"http://{LOCAL_IP}:{PORT}/media/live.ts"
                adb_open_url(url, "video/mp2t")
            threading.Thread(target=switch_to_live, daemon=True).start()
            live_update = True

        self.send_json({"ok": True, "dsp": result, "live_update": live_update})

    def handle_media(self, path, send_body):
        """Serve the currently-casting media file with Range support."""
        with state_lock:
            serve_mode = state.get("_serve_mode")

        # Live streaming mode — pipe from ffmpeg stdout
        if serve_mode == "live":
            file_path = live_stream.get("file_path")
            if not file_path:
                self.send_error(503)
                return

            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Connection", "close")
            self.end_headers()
            if not send_body:
                return

            # Restart ffmpeg so this handler gets a dedicated pipe.
            # The previous handler (if any) will get EOF on its old
            # pipe reference and exit cleanly.
            with dsp_lock:
                dsp = dict(dsp_settings)
            seek_pos = live_stream["seek_offset"] + (time.time() - live_stream["start_time"])
            proc = start_live_ffmpeg(file_path, dsp, seek_pos=seek_pos)

            try:
                while True:
                    chunk = proc.stdout.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        with state_lock:
            serve_path = state.get("_serve_path")

        if not serve_path or not os.path.isfile(serve_path):
            self.send_error(404)
            return

        size = os.path.getsize(serve_path)
        ext = os.path.splitext(serve_path)[1].lower()
        ctype = mimetypes.guess_type(serve_path)[0] or MIME_FALLBACKS.get(ext, "video/mp4")

        range_hdr = self.headers.get("Range")
        if range_hdr:
            try:
                spec = range_hdr.strip().split("=")[1]
                parts = spec.split("-")
                start = int(parts[0])
                end = int(parts[1]) if parts[1] else size - 1
            except (IndexError, ValueError):
                self.send_error(416)
                return
            end = min(end, size - 1)
            if start >= size:
                self.send_error(416)
                return
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
        else:
            start = 0
            length = size
            self.send_response(200)
            self.send_header("Content-Length", str(size))

        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        if not send_body:
            return

        try:
            with open(serve_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(remaining, 256 * 1024))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        if os.environ.get("DEBUG"):
            super().log_message(fmt, *args)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def cleanup_cast():
    # Kill live ffmpeg process if running
    if live_stream["proc"] is not None:
        try:
            live_stream["proc"].kill()
            live_stream["proc"].wait()
        except Exception:
            pass
    live_stream["proc"] = None
    live_stream["file_path"] = None
    live_stream["start_time"] = None
    live_stream["seek_offset"] = 0

    with state_lock:
        state["casting"] = False
        state["file"] = None
        state["transcoding"] = False
        state["transcode_progress"] = ""
        state["_serve_path"] = None
        state["_serve_mode"] = None
        tmp = state.get("tmp_file")
        state["tmp_file"] = None
    if tmp and os.path.exists(tmp):
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# Inline HTML/CSS/JS
# ---------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>castweb</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #1a1a2e; color: #e0e0e0; min-height: 100vh;
  display: flex; flex-direction: column;
}
header {
  background: #16213e; padding: 12px 16px;
  display: flex; align-items: center; gap: 12px;
  border-bottom: 1px solid #0f3460;
}
header h1 { font-size: 1.2rem; color: #e94560; }
.breadcrumb {
  display: flex; flex-wrap: wrap; gap: 4px; font-size: 0.85rem;
  padding: 8px 16px; background: #16213e; border-bottom: 1px solid #0f3460;
}
.breadcrumb span { color: #555; }
.breadcrumb a {
  color: #53a8b6; text-decoration: none; cursor: pointer;
}
.breadcrumb a:hover { text-decoration: underline; }
.file-list {
  flex: 1; overflow-y: auto; padding: 4px 0;
}
.file-item {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 16px; cursor: pointer; border-bottom: 1px solid #1a1a2e;
}
.file-item:hover { background: #16213e; }
.file-item.casting { background: #0f3460; }
.file-icon { font-size: 1.3rem; flex-shrink: 0; width: 28px; text-align: center; }
.file-name { flex: 1; word-break: break-word; }
.file-size { color: #777; font-size: 0.8rem; flex-shrink: 0; }
.status-bar {
  background: #0f3460; padding: 10px 16px; font-size: 0.85rem;
  border-top: 1px solid #16213e;
  min-height: 40px; display: flex; align-items: center; gap: 8px;
}
.status-bar .label { color: #e94560; font-weight: 600; }
.status-bar .info { color: #ccc; }
.controls {
  background: #16213e; padding: 12px 16px;
  display: flex; flex-wrap: wrap; justify-content: center; gap: 8px;
  border-top: 1px solid #0f3460;
}
.controls button {
  background: #0f3460; color: #e0e0e0; border: 1px solid #1a3a6e;
  border-radius: 8px; padding: 10px 14px; font-size: 0.95rem;
  cursor: pointer; min-width: 48px; transition: background 0.15s;
}
.controls button:hover { background: #1a4a80; }
.controls button:active { background: #e94560; }
.controls button.stop-btn { background: #7a1a1a; border-color: #a33; }
.controls button.stop-btn:hover { background: #a33; }
.eq-toggle {
  margin-left: auto; background: #0f3460; color: #e0e0e0; border: 1px solid #1a3a6e;
  border-radius: 6px; padding: 6px 12px; font-size: 0.85rem; cursor: pointer;
  transition: background 0.15s;
}
.eq-toggle:hover { background: #1a4a80; }
.eq-toggle.active { background: #e94560; border-color: #e94560; }
.dsp-panel {
  display: none; background: #16213e; border-top: 1px solid #0f3460;
  border-bottom: 1px solid #0f3460; padding: 12px 16px;
}
.dsp-panel.open { display: block; }
.dsp-presets {
  display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px;
}
.dsp-presets button {
  background: #0f3460; color: #e0e0e0; border: 1px solid #1a3a6e;
  border-radius: 6px; padding: 5px 10px; font-size: 0.8rem; cursor: pointer;
  transition: background 0.15s;
}
.dsp-presets button:hover { background: #1a4a80; }
.dsp-presets button.active { background: #53a8b6; border-color: #53a8b6; color: #111; }
.dsp-slider-row {
  display: flex; align-items: center; gap: 10px; margin-bottom: 6px;
}
.dsp-slider-row label {
  width: 50px; font-size: 0.85rem; color: #aaa; text-align: right;
}
.dsp-slider-row input[type="range"] {
  flex: 1; accent-color: #e94560; cursor: pointer;
}
.dsp-slider-row .dsp-val {
  width: 40px; font-size: 0.8rem; color: #e94560; text-align: right;
  font-variant-numeric: tabular-nums;
}
.dsp-options {
  display: flex; align-items: center; gap: 12px; margin-top: 8px;
}
.dsp-options label {
  font-size: 0.85rem; color: #aaa; display: flex; align-items: center; gap: 6px;
  cursor: pointer;
}
.dsp-options input[type="checkbox"] { accent-color: #e94560; cursor: pointer; }
.dsp-status {
  font-size: 0.8rem; margin-top: 8px; min-height: 1.2em;
  transition: opacity 0.3s;
}
.dsp-status.applying { color: #e9a845; }
.dsp-status.applied { color: #53a8b6; }
.dsp-status.fade { opacity: 0; }
.dsp-note {
  font-size: 0.75rem; color: #666; margin-top: 4px;
}
</style>
</head>
<body>

<header>
  <h1>castweb</h1>
  <button class="eq-toggle" id="eq-toggle" onclick="toggleDSP()" title="Audio EQ (e)">EQ</button>
</header>

<div class="breadcrumb" id="breadcrumb"></div>
<div class="file-list" id="file-list"></div>
<div class="dsp-panel" id="dsp-panel">
  <div class="dsp-presets">
    <button onclick="dspPreset('flat')">Flat</button>
    <button onclick="dspPreset('warm')">Warm</button>
    <button onclick="dspPreset('bass_boost')">Bass Boost</button>
    <button onclick="dspPreset('vocal')">Vocal Clarity</button>
  </div>
  <div class="dsp-slider-row">
    <label>Bass</label>
    <input type="range" id="dsp-bass" min="-12" max="12" value="0" oninput="dspChanged()">
    <span class="dsp-val" id="dsp-bass-val">0 dB</span>
  </div>
  <div class="dsp-slider-row">
    <label>Mid</label>
    <input type="range" id="dsp-mid" min="-12" max="12" value="0" oninput="dspChanged()">
    <span class="dsp-val" id="dsp-mid-val">0 dB</span>
  </div>
  <div class="dsp-slider-row">
    <label>Treble</label>
    <input type="range" id="dsp-treble" min="-12" max="12" value="0" oninput="dspChanged()">
    <span class="dsp-val" id="dsp-treble-val">0 dB</span>
  </div>
  <div class="dsp-options">
    <label><input type="checkbox" id="dsp-loudnorm" onchange="dspChanged()"> Loudness normalization</label>
  </div>
  <div class="dsp-status" id="dsp-status"></div>
  <div class="dsp-note">EQ adjusts live during casting (brief interruption).</div>
</div>
<div class="status-bar" id="status-bar">
  <span class="label">Ready</span>
</div>
<div class="controls" id="controls">
  <button onclick="ctrl('rw')" title="Rewind">&#x23EA;</button>
  <button onclick="ctrl('seek_back')" title="Seek back">&#x23F4;</button>
  <button onclick="ctrl('play_pause')" title="Play / Pause">&#x23EF;</button>
  <button onclick="ctrl('seek_fwd')" title="Seek forward">&#x23F5;</button>
  <button onclick="ctrl('ff')" title="Fast forward">&#x23E9;</button>
  <button onclick="ctrl('vol_down')" title="Volume down">&#x1F509;</button>
  <button onclick="ctrl('vol_up')" title="Volume up">&#x1F50A;</button>
  <button onclick="ctrl('mute')" title="Mute">&#x1F507;</button>
  <button onclick="doStop()" class="stop-btn" title="Stop">&#x23F9; Stop</button>
</div>

<script>
let currentPath = "";
let polling = null;

function escHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(0) + " KB";
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(0) + " MB";
  return (bytes / 1073741824).toFixed(1) + " GB";
}

async function browse(path) {
  const params = path ? "?path=" + encodeURIComponent(path) : "";
  const resp = await fetch("/api/browse" + params);
  const data = await resp.json();
  if (data.error) { alert(data.error); return; }

  currentPath = data.path;
  renderBreadcrumb(data.path);
  renderFiles(data.entries);
}

function renderBreadcrumb(fullPath) {
  const el = document.getElementById("breadcrumb");
  const parts = fullPath.split("/").filter(Boolean);
  let html = "";
  let accum = "";
  for (let i = 0; i < parts.length; i++) {
    accum += "/" + parts[i];
    const p = accum;
    if (i < parts.length - 1) {
      html += `<a onclick="browse('${escHtml(p)}')">${escHtml(parts[i])}</a><span>/</span>`;
    } else {
      html += `<a onclick="browse('${escHtml(p)}')">${escHtml(parts[i])}</a>`;
    }
  }
  el.innerHTML = html;
}

function renderFiles(entries) {
  const el = document.getElementById("file-list");
  if (entries.length === 0) {
    el.innerHTML = '<div style="padding:16px;color:#777;">No media files</div>';
    return;
  }
  let html = "";
  // Parent directory link
  const parent = currentPath.substring(0, currentPath.lastIndexOf("/")) || "/";
  if (currentPath !== "/") {
    html += `<div class="file-item" onclick="browse('${escHtml(parent)}')">
      <div class="file-icon">&#x1F519;</div>
      <div class="file-name">..</div>
    </div>`;
  }
  for (const e of entries) {
    if (e.type === "dir") {
      html += `<div class="file-item" onclick="browse('${escHtml(e.path)}')">
        <div class="file-icon">&#x1F4C1;</div>
        <div class="file-name">${escHtml(e.name)}</div>
      </div>`;
    } else {
      html += `<div class="file-item" onclick="cast('${escHtml(e.path)}')">
        <div class="file-icon">&#x1F3AC;</div>
        <div class="file-name">${escHtml(e.name)}</div>
        <div class="file-size">${formatSize(e.size)}</div>
      </div>`;
    }
  }
  el.innerHTML = html;
}

async function cast(path) {
  const resp = await fetch("/api/cast", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({path: path}),
  });
  const data = await resp.json();
  if (data.error) { alert(data.error); return; }
  startPolling();
}

async function doStop() {
  await fetch("/api/stop", {method: "POST"});
}

async function ctrl(action) {
  await fetch("/api/control", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: action}),
  });
}

function startPolling() {
  if (polling) clearInterval(polling);
  polling = setInterval(pollStatus, 2000);
  pollStatus();
}

async function pollStatus() {
  try {
    const resp = await fetch("/api/status");
    const s = await resp.json();
    const bar = document.getElementById("status-bar");

    if (s.dsp) syncDSPFromStatus(s.dsp);

    if (s.transcoding) {
      bar.innerHTML = `<span class="label">Transcoding</span><span class="info">${escHtml(s.file || "")} &mdash; ${escHtml(s.transcode_progress)}</span>`;
    } else if (s.casting) {
      bar.innerHTML = `<span class="label">Casting</span><span class="info">${escHtml(s.file || "")}</span>`;
    } else if (s.transcode_progress === "transcode failed") {
      bar.innerHTML = `<span class="label" style="color:#f55">Error</span><span class="info">Transcode failed</span>`;
      stopPolling();
    } else {
      bar.innerHTML = `<span class="label">Ready</span>`;
      if (!s.casting && !s.transcoding) stopPolling();
    }
  } catch(e) {}
}

function stopPolling() {
  if (polling) { clearInterval(polling); polling = null; }
}

// --- DSP / EQ ---
let dspInited = false;

function toggleDSP() {
  const panel = document.getElementById("dsp-panel");
  const btn = document.getElementById("eq-toggle");
  panel.classList.toggle("open");
  btn.classList.toggle("active", panel.classList.contains("open"));
}

const DSP_PRESETS = {
  flat:       {bass: 0,  mid: 0,  treble: 0},
  warm:       {bass: 6,  mid: 2,  treble: -2},
  bass_boost: {bass: 9,  mid: 0,  treble: 0},
  vocal:      {bass: -2, mid: 4,  treble: 2},
};

function dspPreset(name) {
  const p = DSP_PRESETS[name];
  if (!p) return;
  document.getElementById("dsp-bass").value = p.bass;
  document.getElementById("dsp-mid").value = p.mid;
  document.getElementById("dsp-treble").value = p.treble;
  dspChanged();
}

let dspDebounceTimer = null;

function dspChanged() {
  const bass = parseInt(document.getElementById("dsp-bass").value);
  const mid = parseInt(document.getElementById("dsp-mid").value);
  const treble = parseInt(document.getElementById("dsp-treble").value);
  const loudnorm = document.getElementById("dsp-loudnorm").checked;

  // Update display labels immediately
  document.getElementById("dsp-bass-val").textContent = (bass > 0 ? "+" : "") + bass + " dB";
  document.getElementById("dsp-mid-val").textContent = (mid > 0 ? "+" : "") + mid + " dB";
  document.getElementById("dsp-treble-val").textContent = (treble > 0 ? "+" : "") + treble + " dB";

  // Highlight matching preset button
  const presetBtns = document.querySelectorAll(".dsp-presets button");
  presetBtns.forEach(btn => btn.classList.remove("active"));
  for (const [name, p] of Object.entries(DSP_PRESETS)) {
    if (p.bass === bass && p.mid === mid && p.treble === treble) {
      const idx = Object.keys(DSP_PRESETS).indexOf(name);
      presetBtns[idx].classList.add("active");
    }
  }

  // Debounce the POST to avoid rapid re-casts while dragging sliders
  if (dspDebounceTimer) clearTimeout(dspDebounceTimer);
  dspDebounceTimer = setTimeout(async () => {
    const statusEl = document.getElementById("dsp-status");
    statusEl.className = "dsp-status applying";
    statusEl.textContent = "Applying\u2026";
    try {
      const resp = await fetch("/api/dsp", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({bass, mid, treble, loudnorm}),
      });
      const data = await resp.json();
      if (data.live_update) {
        statusEl.className = "dsp-status applied";
        statusEl.textContent = "EQ updated live";
      } else {
        statusEl.className = "dsp-status applied";
        statusEl.textContent = "EQ saved";
      }
    } catch(e) {
      statusEl.className = "dsp-status applying";
      statusEl.textContent = "Error applying EQ";
    }
    setTimeout(() => { statusEl.classList.add("fade"); }, 2000);
  }, 600);
}

function syncDSPFromStatus(dsp) {
  if (!dsp || dspInited) return;
  dspInited = true;
  document.getElementById("dsp-bass").value = dsp.bass;
  document.getElementById("dsp-mid").value = dsp.mid;
  document.getElementById("dsp-treble").value = dsp.treble;
  document.getElementById("dsp-loudnorm").checked = dsp.loudnorm;
  // Update display labels and preset highlight
  dspChanged();
}

// Keyboard shortcuts
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  switch (e.key) {
    case " ": e.preventDefault(); ctrl("play_pause"); break;
    case "ArrowRight": ctrl("seek_fwd"); break;
    case "ArrowLeft": ctrl("seek_back"); break;
    case "ArrowUp": e.preventDefault(); ctrl("vol_up"); break;
    case "ArrowDown": e.preventDefault(); ctrl("vol_down"); break;
    case "m": ctrl("mute"); break;
    case "s": doStop(); break;
    case "e": toggleDSP(); break;
  }
});

// Start
browse("");
startPolling();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global BROWSE_ROOT, LOCAL_IP, PORT

    parser = argparse.ArgumentParser(
        description="Web UI for casting media to the projector.",
    )
    parser.add_argument("--port", type=int, default=8080,
                        help="HTTP server port (default: 8080)")
    parser.add_argument("--root", default=os.path.expanduser("~"),
                        help="root directory for browsing (default: home)")
    args = parser.parse_args()

    for tool in ("adb", "ffprobe", "ffmpeg"):
        if not shutil.which(tool):
            sys.exit(f"error: {tool} not found in PATH")

    BROWSE_ROOT = os.path.realpath(args.root)
    if not os.path.isdir(BROWSE_ROOT):
        sys.exit(f"error: root is not a directory: {BROWSE_ROOT}")

    LOCAL_IP = get_local_ip()
    PORT = args.port

    print(f"connecting adb to {PROJECTOR_IP}...")
    adb_connect()
    print("connected.")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), WebHandler)
    server.daemon_threads = True
    print(f"serving on http://{LOCAL_IP}:{PORT}/")
    print(f"browse root: {BROWSE_ROOT}")

    def shutdown_handler(sig, frame):
        print("\nshutting down...")
        cleanup_cast()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    server.serve_forever()


if __name__ == "__main__":
    main()
