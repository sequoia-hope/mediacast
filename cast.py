#!/usr/bin/env python3
"""
Stream a local media file to the FUDONI GC888 projector.

Starts an HTTP server with range-request support, then uses ADB to open
the URL in the projector's built-in media player. The projector remote
handles play/pause/seek natively; this script also provides keyboard
controls via ADB key events.

If the audio track uses a codec incompatible with Bluetooth output
(AC-3, DTS, EAC-3, TrueHD), the audio is automatically transcoded
to AAC stereo via ffmpeg.

Usage:
    ./cast.py <file>
    ./cast.py <file> --port 9090

Example:
    ./cast.py '/home/sequoia/Videos/Drunken Master (1978) 1080p.mp4'

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
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECTOR_IP = "192.168.86.248"
ADB_TARGET = f"{PROJECTOR_IP}:5555"

# Audio codecs that won't play over Bluetooth A2DP on the projector.
# These are surround / passthrough codecs that need transcoding to stereo AAC.
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


def transcode_audio(input_path):
    """Transcode audio to AAC stereo, copy video. Returns path to new file."""
    base = os.path.splitext(os.path.basename(input_path))[0]
    out_fd, out_path = tempfile.mkstemp(suffix=".mp4", prefix=f"{base}.")

    os.close(out_fd)
    print(f"transcoding audio to AAC stereo...")
    print(f"  tmp: {out_path}")

    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-i", input_path,
         "-c:v", "copy", "-c:a", "aac", "-ac", "2", "-b:a", "192k",
         out_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    # Show progress dots
    def progress():
        while proc.poll() is None:
            # Read stderr in chunks to avoid blocking
            line = proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            if "time=" in text:
                # Extract the time= portion for a progress indicator
                for part in text.split():
                    if part.startswith("time="):
                        print(f"\r  {part}  ", end="", flush=True)

    t = threading.Thread(target=progress, daemon=True)
    t.start()
    proc.wait()
    t.join(timeout=2)
    print()  # newline after progress

    if proc.returncode != 0:
        stderr = proc.stderr.read().decode("utf-8", errors="replace")
        os.unlink(out_path)
        sys.exit(f"ffmpeg failed (exit {proc.returncode}):\n{stderr[-500:]}")

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"  done ({size_mb:.0f} MB)")
    return out_path


# ---------------------------------------------------------------------------
# HTTP server with Range support
# ---------------------------------------------------------------------------
class RangeHTTPHandler(BaseHTTPRequestHandler):
    """Serves a single file with Range request support (needed for seeking)."""

    file_path: str = ""
    file_size: int = 0
    content_type: str = "application/octet-stream"

    def _serve(self, send_body):
        size = self.file_size
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

        self.send_header("Content-Type", self.content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        if not send_body:
            return

        try:
            with open(self.file_path, "rb") as f:
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

    def do_GET(self):
        self._serve(send_body=True)

    def do_HEAD(self):
        self._serve(send_body=False)

    def log_message(self, fmt, *args):
        if os.environ.get("DEBUG"):
            super().log_message(fmt, *args)


def start_http_server(file_path, port):
    size = os.path.getsize(file_path)
    ext = os.path.splitext(file_path)[1].lower()
    ctype = mimetypes.guess_type(file_path)[0] or MIME_FALLBACKS.get(ext, "video/mp4")

    RangeHTTPHandler.file_path = file_path
    RangeHTTPHandler.file_size = size
    RangeHTTPHandler.content_type = ctype

    server = HTTPServer(("0.0.0.0", port), RangeHTTPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return ctype, server


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
    """Open a URL in the projector's default media player via intent."""
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
# Local IP detection
# ---------------------------------------------------------------------------
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((PROJECTOR_IP, 80))
        return s.getsockname()[0]
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Interactive control loop
# ---------------------------------------------------------------------------
def control_loop():
    print("\n--- controls (projector remote also works) ---")
    print("  [p]   play/pause")
    print("  [s]   stop playback")
    print("  [f]   seek forward  (right arrow)")
    print("  [b]   seek backward (left arrow)")
    print("  [F]   seek forward  (fast-forward)")
    print("  [B]   seek backward (rewind)")
    print("  [+]   volume up")
    print("  [-]   volume down")
    print("  [m]   mute toggle")
    print("  [q]   stop & quit\n")

    while True:
        try:
            cmd = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            cmd = "q"

        if not cmd:
            continue
        elif cmd == "p":
            adb_key("KEYCODE_MEDIA_PLAY_PAUSE")
            print("play/pause toggled")
        elif cmd == "s":
            adb_key("KEYCODE_MEDIA_STOP")
            print("stopped")
        elif cmd == "f":
            adb_key("KEYCODE_DPAD_RIGHT")
            print(">> seek forward")
        elif cmd == "b":
            adb_key("KEYCODE_DPAD_LEFT")
            print("<< seek backward")
        elif cmd == "F":
            adb_key("KEYCODE_MEDIA_FAST_FORWARD")
            print(">>> fast forward")
        elif cmd == "B":
            adb_key("KEYCODE_MEDIA_REWIND")
            print("<<< rewind")
        elif cmd == "+":
            adb_key("KEYCODE_VOLUME_UP")
            print("volume up")
        elif cmd == "-":
            adb_key("KEYCODE_VOLUME_DOWN")
            print("volume down")
        elif cmd == "m":
            adb_key("KEYCODE_VOLUME_MUTE")
            print("mute toggled")
        elif cmd == "q":
            print("stopping playback...")
            adb_stop()
            break
        else:
            print(f"unknown: {cmd!r}  (p/s/f/b/F/B/+/-/m/q)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Stream a local media file to the projector.",
    )
    parser.add_argument("file", help="path to media file")
    parser.add_argument("--port", type=int, default=8080,
                        help="HTTP server port (default: 8080)")
    parser.add_argument("--no-transcode", action="store_true",
                        help="skip audio transcoding even if codec is incompatible")
    args = parser.parse_args()

    for tool in ("adb", "ffprobe", "ffmpeg"):
        if not shutil.which(tool):
            sys.exit(f"error: {tool} not found in PATH")

    file_path = os.path.abspath(args.file)
    if not os.path.isfile(file_path):
        sys.exit(f"file not found: {file_path}")

    local_ip = get_local_ip()
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    tmp_file = None

    print(f"file:       {os.path.basename(file_path)} ({size_mb:.0f} MB)")

    # Check audio codec and transcode if needed
    if not args.no_transcode:
        acodec = get_audio_codec(file_path)
        print(f"audio:      {acodec or 'none detected'}")
        if acodec and acodec in BT_INCOMPATIBLE_CODECS:
            print(f"\n'{acodec}' is incompatible with Bluetooth A2DP output.")
            tmp_file = transcode_audio(file_path)
            file_path = tmp_file
            size_mb = os.path.getsize(file_path) / (1024 * 1024)

    print(f"server:     http://{local_ip}:{args.port}/")
    print(f"projector:  {PROJECTOR_IP} (via adb)")

    # Connect ADB
    print("\nconnecting adb...")
    adb_connect()
    print("connected.")

    # Start HTTP server
    ctype, server = start_http_server(file_path, args.port)
    url = f"http://{local_ip}:{args.port}/{urllib.parse.quote(os.path.basename(file_path))}"
    print(f"url:        {url}")
    print(f"mime:       {ctype}")

    # Launch on projector
    print("\nlaunching on projector...")
    adb_open_url(url, ctype)
    print("playback started.")

    try:
        control_loop()
    finally:
        server.shutdown()
        if tmp_file and os.path.exists(tmp_file):
            os.unlink(tmp_file)
            print(f"cleaned up temp file.")
        print("done.")


if __name__ == "__main__":
    main()
