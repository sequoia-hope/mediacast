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
from urllib.request import Request, urlopen
from urllib.error import URLError

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
    "_serve_path": None,
    "track_info": None,
    "subtitle_files": [],
    "_subtitle_tmp_files": [],
    "_transcode_proc": None,
    "_input_size": 0,
}

dsp_lock = threading.Lock()
dsp_settings = {"bass": 0, "mid": 0, "treble": 0, "loudnorm": False}

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


TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}


def probe_tracks(file_path):
    """Probe all audio and subtitle streams + duration via ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries",
             "stream=index,codec_name,codec_type:stream_tags=language,title"
             ":format=duration",
             "-of", "json", file_path],
            capture_output=True, text=True, timeout=30,
        )
        info = json.loads(r.stdout)
    except Exception:
        return {"audio_tracks": [], "subtitle_tracks": [], "duration_ms": 0}

    audio_tracks = []
    subtitle_tracks = []
    audio_idx = 0
    sub_idx = 0
    for s in info.get("streams", []):
        tags = s.get("tags", {})
        lang = tags.get("language", "")
        title = tags.get("title", "")
        codec = s.get("codec_name", "")
        if s.get("codec_type") == "audio":
            audio_tracks.append({
                "index": s["index"], "stream_index": audio_idx,
                "codec": codec, "language": lang, "title": title,
            })
            audio_idx += 1
        elif s.get("codec_type") == "subtitle":
            subtitle_tracks.append({
                "index": s["index"], "stream_index": sub_idx,
                "codec": codec, "language": lang, "title": title,
            })
            sub_idx += 1

    duration_s = float(info.get("format", {}).get("duration", 0))
    duration_ms = int(duration_s * 1000)

    return {"audio_tracks": audio_tracks, "subtitle_tracks": subtitle_tracks,
            "duration_ms": duration_ms}


def extract_subtitles(file_path, subtitle_tracks):
    """Extract text-based subtitle tracks to temp .srt files. Returns list of dicts."""
    results = []
    for st in subtitle_tracks:
        codec = st["codec"].lower()
        if codec not in TEXT_SUB_CODECS:
            continue  # Skip bitmap formats (pgs, dvd_subtitle, etc.)
        lang = st["language"] or f"track{st['stream_index']}"
        label = st["title"] or lang
        fd, tmp_path = tempfile.mkstemp(suffix=".srt", prefix=f"sub_{lang}_")
        os.close(fd)
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", file_path,
                 "-map", f"0:{st['index']}", "-c:s", "srt", tmp_path],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0 and os.path.getsize(tmp_path) > 0:
                results.append({
                    "path": tmp_path, "language": lang, "label": label,
                    "source": "embedded", "stream_index": st["stream_index"],
                })
            else:
                os.unlink(tmp_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    return results


def find_companion_srt(file_path):
    """Find .srt files alongside the media file (e.g., movie.srt, movie.en.srt)."""
    directory = os.path.dirname(file_path)
    base = os.path.splitext(os.path.basename(file_path))[0]
    results = []
    try:
        for name in os.listdir(directory):
            if not name.lower().endswith(".srt"):
                continue
            srt_base = os.path.splitext(name)[0]
            if srt_base == base or srt_base.startswith(base + "."):
                suffix = srt_base[len(base):].lstrip(".")
                label = suffix if suffix else "external"
                full = os.path.join(directory, name)
                results.append({
                    "path": full, "language": suffix or "und",
                    "label": label, "source": "companion",
                })
    except OSError:
        pass
    return results


EQ_PLAYER_URL = f"http://{PROJECTOR_IP}:8081"


def send_eq_to_player(dsp):
    """Convert 3-slider dB values to 5-band millibel values and POST to the EQ player app."""
    bass_mb = int(dsp["bass"] * 100)
    mid_mb = int(dsp["mid"] * 100)
    treble_mb = int(dsp["treble"] * 100)
    # Map 3 sliders to 5 bands: bass→[0,1], mid→[2], treble→[3,4]
    bands = [bass_mb, bass_mb, mid_mb, treble_mb, treble_mb]
    try:
        data = json.dumps({"bands": bands}).encode()
        req = Request(f"{EQ_PLAYER_URL}/eq", data=data,
                      headers={"Content-Type": "application/json"})
        urlopen(req, timeout=2)
    except (URLError, OSError) as e:
        print(f"EQ send failed: {e}")

    # Send loudnorm setting
    try:
        data = json.dumps({"enabled": dsp["loudnorm"], "gain": 600}).encode()
        req = Request(f"{EQ_PLAYER_URL}/loudnorm", data=data,
                      headers={"Content-Type": "application/json"})
        urlopen(req, timeout=2)
    except (URLError, OSError) as e:
        print(f"Loudnorm send failed: {e}")


def transcode_audio(input_path, seek_seconds=0):
    """Start streaming transcode: AAC stereo, MPEG-TS for instant playback.

    Returns (out_path, proc) immediately after the first data is written,
    or (None, None) if ffmpeg fails to start.
    """
    base = os.path.splitext(os.path.basename(input_path))[0]
    out_fd, out_path = tempfile.mkstemp(suffix=".ts", prefix=f"{base}.")
    os.close(out_fd)

    with state_lock:
        state["transcoding"] = True
        state["transcode_progress"] = "starting..."

    cmd = ["ffmpeg", "-y", "-fflags", "+genpts"]
    if seek_seconds > 0:
        cmd += ["-ss", str(seek_seconds)]
    cmd += ["-i", input_path,
            "-c:v", "copy", "-c:a", "aac", "-ac", "2", "-b:a", "192k",
            "-f", "mpegts", out_path]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    # Monitor progress in background thread
    def monitor():
        while proc.poll() is None:
            line = proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            if "time=" in text:
                for part in text.split():
                    if part.startswith("time="):
                        with state_lock:
                            # Only update if we're still the active transcode
                            if state.get("_transcode_proc") is proc:
                                state["transcode_progress"] = part
        proc.wait()
        with state_lock:
            # Only clear state if we're still the active transcode —
            # a seek may have replaced us with a new proc
            if state.get("_transcode_proc") is proc:
                state["transcoding"] = False
                state["transcode_progress"] = ""
                state["_transcode_proc"] = None

    threading.Thread(target=monitor, daemon=True).start()

    # Wait for the first TS packets to be written
    for _ in range(300):  # up to 30s
        if proc.poll() is not None:
            break
        try:
            if os.path.getsize(out_path) > 256 * 1024:
                break
        except OSError:
            pass
        time.sleep(0.1)

    if proc.poll() is not None and proc.returncode != 0:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        with state_lock:
            state["transcoding"] = False
            state["_transcode_proc"] = None
        return None, None

    return out_path, proc


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


def adb_open_url(url, mime, subtitle_list=None, audio_count=0,
                 duration_ms=0, seek_offset_ms=0):
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "settings", "put", "secure", "screensaver_enabled", "0")
    # Build am start as a single shell command string to control quoting
    am_cmd = (f"am start -a android.intent.action.VIEW"
              f" -d '{url}' -t '{mime}'"
              f" -n com.mediacast.eqplayer/.MainActivity")
    if subtitle_list:
        subs_json = json.dumps(subtitle_list, separators=(",", ":"))
        # Single-quote for the remote shell; JSON never contains single quotes
        am_cmd += f" --es subtitles '{subs_json}'"
    if audio_count > 0:
        am_cmd += f" --ei audio_count {audio_count}"
    if duration_ms > 0:
        am_cmd += f" --el duration {duration_ms}"
    if seek_offset_ms > 0:
        am_cmd += f" --el seek_offset {seek_offset_ms}"
    adb("shell", am_cmd)


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
    """Transcode if codec is BT-incompatible, then tell projector to play. Runs in a thread."""
    serve_path = file_path
    tmp = None

    # Probe tracks
    tracks = probe_tracks(file_path)

    acodec = get_audio_codec(file_path)

    # Only transcode for BT-incompatible codecs (flat audio, no EQ filters)
    if acodec and acodec in BT_INCOMPATIBLE_CODECS:
        tmp, proc = transcode_audio(file_path)
        if tmp is None:
            with state_lock:
                state["casting"] = False
                state["file"] = None
                state["transcode_progress"] = "transcode failed"
            return
        serve_path = tmp
        with state_lock:
            state["_transcode_proc"] = proc
            state["_input_size"] = os.path.getsize(file_path)

    # Extract embedded subtitles and find companion .srt files
    extracted_subs = extract_subtitles(file_path, tracks["subtitle_tracks"])
    companion_subs = find_companion_srt(file_path)
    all_subs = extracted_subs + companion_subs

    # Build subtitle URL list
    subtitle_list = []
    sub_tmp_files = []
    for i, sub in enumerate(all_subs):
        fname_sub = urllib.parse.quote(f"sub_{i}_{sub['language']}.srt")
        sub_url = f"http://{LOCAL_IP}:{PORT}/subs/{i}/{fname_sub}"
        subtitle_list.append({
            "url": sub_url, "language": sub["language"],
            "label": sub["label"], "source": sub["source"],
        })
        if sub["source"] == "embedded":
            sub_tmp_files.append(sub["path"])

    with state_lock:
        state["tmp_file"] = tmp
        state["transcoding"] = False
        state["transcode_progress"] = ""
        state["track_info"] = tracks
        state["subtitle_files"] = all_subs
        state["_subtitle_tmp_files"] = sub_tmp_files

    ext = os.path.splitext(serve_path)[1].lower()
    mime = MIME_FALLBACKS.get(ext) or mimetypes.guess_type(serve_path)[0] or "video/mp4"

    fname = urllib.parse.quote(os.path.basename(serve_path))
    url = f"http://{LOCAL_IP}:{PORT}/media/{fname}"

    audio_count = len(tracks["audio_tracks"])
    duration_ms = tracks.get("duration_ms", 0)
    adb_open_url(url, mime, subtitle_list, audio_count, duration_ms=duration_ms)

    with state_lock:
        state["casting"] = True
        state["_serve_path"] = serve_path

    # Apply current EQ settings to the player app
    with dsp_lock:
        dsp = dict(dsp_settings)
    send_eq_to_player(dsp)


_seek_lock = threading.Lock()

def do_seek(position_ms):
    """Kill current transcode, start new one at the given position, relaunch player."""
    if not _seek_lock.acquire(blocking=False):
        return  # Another seek already in progress
    try:
        _do_seek_inner(position_ms)
    finally:
        _seek_lock.release()

def _do_seek_inner(position_ms):
    with state_lock:
        file_path = state.get("file")
        track_info = state.get("track_info")
        all_subs = list(state.get("subtitle_files", []))

    if not file_path:
        return

    acodec = get_audio_codec(file_path)
    if not (acodec and acodec in BT_INCOMPATIBLE_CODECS):
        return  # Non-transcoded file — player handles seeks locally

    seek_seconds = position_ms / 1000.0

    # Kill old transcode first to free RAM (512MB device)
    with state_lock:
        old_proc = state.get("_transcode_proc")
        old_tmp = state.get("tmp_file")
        state["_transcode_proc"] = None  # Prevent old monitor from clobbering

    if old_proc is not None:
        try:
            old_proc.kill()
            old_proc.wait(timeout=5)
        except Exception:
            pass

    # Start new transcode at seek position
    new_tmp, new_proc = transcode_audio(file_path, seek_seconds=seek_seconds)
    if new_tmp is None:
        with state_lock:
            state["transcode_progress"] = "seek failed"
        return

    with state_lock:
        state["tmp_file"] = new_tmp
        state["_serve_path"] = new_tmp
        state["_transcode_proc"] = new_proc
        state["_input_size"] = os.path.getsize(file_path)

    if old_tmp and old_tmp != new_tmp and os.path.exists(old_tmp):
        try:
            os.unlink(old_tmp)
        except OSError:
            pass

    ext = os.path.splitext(new_tmp)[1].lower()
    mime = MIME_FALLBACKS.get(ext) or mimetypes.guess_type(new_tmp)[0] or "video/mp4"
    fname = urllib.parse.quote(os.path.basename(new_tmp))
    url = f"http://{LOCAL_IP}:{PORT}/media/{fname}"

    duration_ms = (track_info or {}).get("duration_ms", 0)

    subtitle_list = []
    for i, sub in enumerate(all_subs):
        fname_sub = urllib.parse.quote(f"sub_{i}_{sub['language']}.srt")
        sub_url = f"http://{LOCAL_IP}:{PORT}/subs/{i}/{fname_sub}"
        subtitle_list.append({
            "url": sub_url, "language": sub["language"],
            "label": sub["label"], "source": sub["source"],
        })

    audio_count = len((track_info or {}).get("audio_tracks", []))

    adb_open_url(url, mime, subtitle_list, audio_count,
                 duration_ms=duration_ms, seek_offset_ms=position_ms)

    with state_lock:
        state["casting"] = True

    with dsp_lock:
        dsp = dict(dsp_settings)
    send_eq_to_player(dsp)


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
        elif path == "/api/tracks":
            self.handle_get_tracks()
        elif path.startswith("/media/"):
            self.handle_media(path, send_body=True)
        elif path.startswith("/subs/"):
            self.handle_subs(path)
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
        elif path == "/api/select_track":
            self.handle_select_track()
        elif path == "/api/seek":
            self.handle_seek()
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
                "audio_count": len((state.get("track_info") or {}).get("audio_tracks", [])),
                "subtitle_count": len(state.get("subtitle_files", [])),
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
            state["transcoding"] = True
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

        # Send EQ to projector app (instant, no re-transcode)
        threading.Thread(target=send_eq_to_player, args=(result,), daemon=True).start()

        self.send_json({"ok": True, "dsp": result, "live_update": True})

    def handle_media(self, path, send_body):
        """Serve the currently-casting media file with Range support.

        During streaming transcode, reports the input file size as
        Content-Length (close estimate since video is copied) and blocks
        on read when the player outruns ffmpeg.
        """
        with state_lock:
            serve_path = state.get("_serve_path")
            transcode_proc = state.get("_transcode_proc")

        if not serve_path or not os.path.isfile(serve_path):
            self.send_error(404)
            return

        streaming = transcode_proc is not None and transcode_proc.poll() is None
        size = os.path.getsize(serve_path)
        ext = os.path.splitext(serve_path)[1].lower()
        ctype = MIME_FALLBACKS.get(ext) or mimetypes.guess_type(serve_path)[0] or "video/mp4"

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
                    if chunk:
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                    else:
                        # No data yet — if transcode is still running, wait
                        with state_lock:
                            proc = state.get("_transcode_proc")
                            still_ours = state.get("_serve_path") == serve_path
                        if still_ours and proc is not None and proc.poll() is None:
                            time.sleep(0.2)
                        else:
                            break  # Transcode done, seek replaced us, or static file
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_subs(self, path):
        """Serve extracted/companion subtitle files: /subs/<index>/filename.srt"""
        parts = path.split("/")  # ['', 'subs', '<index>', 'filename.srt']
        if len(parts) < 4:
            self.send_error(404)
            return
        try:
            idx = int(parts[2])
        except ValueError:
            self.send_error(404)
            return
        with state_lock:
            subs = list(state.get("subtitle_files", []))
        if idx < 0 or idx >= len(subs):
            self.send_error(404)
            return
        sub_path = subs[idx]["path"]
        if not os.path.isfile(sub_path):
            self.send_error(404)
            return
        try:
            with open(sub_path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-subrip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_error(500)

    def handle_get_tracks(self):
        """Return track info for the current cast."""
        with state_lock:
            tracks = state.get("track_info")
            subs = state.get("subtitle_files", [])
        if tracks is None:
            self.send_json({"audio_tracks": [], "subtitle_tracks": []})
            return
        sub_list = [{"language": s["language"], "label": s["label"],
                     "source": s["source"]} for s in subs]
        result = dict(tracks)
        result["subtitle_files"] = sub_list
        self.send_json(result)

    def handle_select_track(self):
        """Forward track selection to the player's HTTP server."""
        body = json.loads(self.read_body())
        track_type = body.get("type", "")
        index = body.get("index", -1)
        if track_type not in ("audio", "subtitle"):
            self.send_json({"error": "type must be 'audio' or 'subtitle'"}, 400)
            return
        try:
            data = json.dumps({"type": track_type, "index": index}).encode()
            req = Request(f"{EQ_PLAYER_URL}/select_track", data=data,
                          headers={"Content-Type": "application/json"})
            resp = urlopen(req, timeout=5)
            result = json.loads(resp.read().decode())
            self.send_json(result)
        except (URLError, OSError) as e:
            self.send_json({"error": str(e)}, 502)

    def handle_seek(self):
        """Handle seek-ahead request from the player during streaming transcode."""
        body = json.loads(self.read_body())
        position_ms = body.get("position_ms", 0)
        if position_ms < 0:
            self.send_json({"error": "invalid position"}, 400)
            return
        threading.Thread(target=do_seek, args=(position_ms,), daemon=True).start()
        self.send_json({"ok": True, "seeking_to": position_ms})

    def handle(self):
        """Suppress ConnectionResetError from player dropping keep-alive connections."""
        try:
            super().handle()
        except ConnectionResetError:
            pass

    def log_message(self, fmt, *args):
        super().log_message(fmt, *args)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def cleanup_cast():
    with state_lock:
        state["casting"] = False
        state["file"] = None
        state["transcoding"] = False
        state["transcode_progress"] = ""
        state["_serve_path"] = None
        tmp = state.get("tmp_file")
        state["tmp_file"] = None
        state["track_info"] = None
        state["subtitle_files"] = []
        sub_tmps = state.get("_subtitle_tmp_files", [])
        state["_subtitle_tmp_files"] = []
        proc = state.get("_transcode_proc")
        state["_transcode_proc"] = None
        state["_input_size"] = 0
    if proc is not None:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    if tmp and os.path.exists(tmp):
        os.unlink(tmp)
    for f in sub_tmps:
        try:
            if os.path.exists(f):
                os.unlink(f)
        except OSError:
            pass


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
  <button class="eq-toggle" id="tracks-toggle" onclick="toggleTracks()" title="Tracks (t)">Tracks</button>
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
  <div class="dsp-note">EQ applied in real-time on the projector.</div>
</div>
<div class="dsp-panel" id="tracks-panel">
  <div style="font-size:0.85rem;color:#aaa;margin-bottom:8px;">Audio Tracks</div>
  <div class="dsp-presets" id="audio-track-btns"></div>
  <div style="font-size:0.85rem;color:#aaa;margin:10px 0 8px;">Subtitles</div>
  <div class="dsp-presets" id="sub-track-btns"></div>
  <div class="dsp-status" id="track-status"></div>
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
  tracksLoaded = false;
  currentAudioTrack = 0;
  currentSubTrack = -1;
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

  // POST immediately — EQ is applied in real-time on the projector, no debounce needed
  const statusEl = document.getElementById("dsp-status");
  statusEl.className = "dsp-status applying";
  statusEl.textContent = "Applying\u2026";
  fetch("/api/dsp", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({bass, mid, treble, loudnorm}),
  }).then(r => r.json()).then(data => {
    statusEl.className = "dsp-status applied";
    statusEl.textContent = "Applied";
    setTimeout(() => { statusEl.classList.add("fade"); }, 2000);
  }).catch(e => {
    statusEl.className = "dsp-status applying";
    statusEl.textContent = "Error applying EQ";
  });
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

// --- Tracks ---
let tracksLoaded = false;
let currentAudioTrack = 0;
let currentSubTrack = -1;

function toggleTracks() {
  const panel = document.getElementById("tracks-panel");
  const btn = document.getElementById("tracks-toggle");
  panel.classList.toggle("open");
  btn.classList.toggle("active", panel.classList.contains("open"));
  if (panel.classList.contains("open") && !tracksLoaded) loadTracks();
}

async function loadTracks() {
  try {
    const resp = await fetch("/api/tracks");
    const data = await resp.json();
    renderTrackButtons(data);
    tracksLoaded = true;
  } catch(e) {}
}

function renderTrackButtons(data) {
  const audioEl = document.getElementById("audio-track-btns");
  const subEl = document.getElementById("sub-track-btns");
  let audioHtml = "";
  for (let i = 0; i < (data.audio_tracks || []).length; i++) {
    const t = data.audio_tracks[i];
    const label = t.title || t.language || ("Track " + (i + 1));
    const cls = i === currentAudioTrack ? " active" : "";
    audioHtml += `<button class="${cls}" onclick="selectTrack('audio',${i})">${escHtml(label)}</button>`;
  }
  audioEl.innerHTML = audioHtml || '<span style="color:#555;font-size:0.8rem;">None</span>';

  let subHtml = `<button class="${currentSubTrack < 0 ? " active" : ""}" onclick="selectTrack('subtitle',-1)">Off</button>`;
  for (let i = 0; i < (data.subtitle_files || []).length; i++) {
    const s = data.subtitle_files[i];
    const label = s.label || s.language || ("Sub " + (i + 1));
    const cls = i === currentSubTrack ? " active" : "";
    subHtml += `<button class="${cls}" onclick="selectTrack('subtitle',${i})">${escHtml(label)}</button>`;
  }
  subEl.innerHTML = subHtml;
}

async function selectTrack(type, index) {
  const statusEl = document.getElementById("track-status");
  statusEl.className = "dsp-status applying";
  statusEl.textContent = "Switching\u2026";
  try {
    const resp = await fetch("/api/select_track", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({type, index}),
    });
    const data = await resp.json();
    if (data.ok) {
      if (type === "audio") currentAudioTrack = index;
      else currentSubTrack = index;
      statusEl.className = "dsp-status applied";
      statusEl.textContent = "Switched";
      setTimeout(() => { statusEl.classList.add("fade"); }, 2000);
      loadTracks();  // refresh highlight
    } else {
      statusEl.className = "dsp-status applying";
      statusEl.textContent = data.error || "Error";
    }
  } catch(e) {
    statusEl.className = "dsp-status applying";
    statusEl.textContent = "Error";
  }
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
    case "t": toggleTracks(); break;
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
