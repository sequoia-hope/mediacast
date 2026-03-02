#!/usr/bin/env python3
"""
ABANDONED — Superseded by cast.py (ADB-based approach).

This was the first attempt at streaming media to the FUDONI GC888 projector,
using UPnP/DLNA SOAP commands to the projector's CoolDlna renderer service
(port 1198). It never worked: the DLNA SetAVTransportURI and Play SOAP
commands returned success responses, but the projector stayed in
NO_MEDIA_PRESENT state and never made HTTP requests to fetch the media.
Neither video nor audio ever played via this method.

The issue appears to be on the projector side — no firewall was blocking
(UFW disabled), and the CoolDlna service accepted every SOAP command without
error but simply never acted on them. Possibly the CoolDlna app needs to be
in the foreground or requires some undocumented activation step.

cast.py replaced this with a working approach: serve the file over HTTP and
use ADB to send an android.intent.action.VIEW intent to the projector's
built-in media player, which works reliably.

---

Original description:
Stream a local media file to a DLNA renderer (e.g. FUDONI GC888 projector).

Starts an HTTP server with range-request support, then tells the renderer
to play via UPnP/DLNA.  The projector's own remote handles play/pause/stop
once playback begins.

Usage:
    ./dlna-cast.py <file>                        # auto-discover renderer
    ./dlna-cast.py <file> --renderer-url <url>   # explicit renderer

Example:
    ./dlna-cast.py '/home/sequoia/Videos/Drunken Master (1978) 1080p.mp4'
"""

import argparse
import html
import mimetypes
import os
import socket
import sys
import textwrap
import threading
import time
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECTOR_IP = "192.168.86.248"
DLNA_PORT = 1198
DLNA_INSTANCE = "40:e7:93:41:1f:a1"

AVT_CONTROL = (
    f"http://{PROJECTOR_IP}:{DLNA_PORT}"
    f"/AVTransport/{DLNA_INSTANCE}/control.xml"
)
RC_CONTROL = (
    f"http://{PROJECTOR_IP}:{DLNA_PORT}"
    f"/RenderingControl/{DLNA_INSTANCE}/control.xml"
)

MIME_FALLBACKS = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".ts":  "video/mp2t",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".jpg": "image/jpeg",
    ".png": "image/png",
}

# ---------------------------------------------------------------------------
# Tiny HTTP server with Range support (needed for seeking)
# ---------------------------------------------------------------------------
class RangeHTTPHandler(BaseHTTPRequestHandler):
    """Serves a single file, supporting Range requests."""

    file_path: str = ""   # set before server starts
    file_size: int = 0
    content_type: str = "application/octet-stream"

    def do_GET(self):
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
            if start >= size or end >= size:
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
            pass  # renderer closed connection, that's fine

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(self.file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def log_message(self, fmt, *args):
        # quiet unless debug
        if os.environ.get("DEBUG"):
            super().log_message(fmt, *args)


def start_http_server(file_path, host, port):
    """Start the file server in a daemon thread and return the URL."""
    size = os.path.getsize(file_path)
    ext = os.path.splitext(file_path)[1].lower()
    ctype = mimetypes.guess_type(file_path)[0] or MIME_FALLBACKS.get(ext, "video/mp4")

    RangeHTTPHandler.file_path = file_path
    RangeHTTPHandler.file_size = size
    RangeHTTPHandler.content_type = ctype

    server = HTTPServer((host, port), RangeHTTPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    filename = urllib.parse.quote(os.path.basename(file_path))
    url = f"http://{host}:{port}/{filename}"
    return url, ctype, server


# ---------------------------------------------------------------------------
# UPnP SOAP helpers
# ---------------------------------------------------------------------------
SOAP_ENVELOPE = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
                s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
      <s:Body>{body}</s:Body>
    </s:Envelope>""")


def soap_call(control_url, service, action, args=""):
    body = f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}">{args}</u:{action}>'
    envelope = SOAP_ENVELOPE.format(body=body)

    req = urllib.request.Request(
        control_url,
        data=envelope.encode("utf-8"),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"urn:schemas-upnp-org:service:{service}#{action}"',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return str(e)


def avt(action, args=""):
    return soap_call(AVT_CONTROL, "AVTransport:1", action, args)


def rc(action, args=""):
    return soap_call(RC_CONTROL, "RenderingControl:1", action, args)


# ---------------------------------------------------------------------------
# DLNA transport commands
# ---------------------------------------------------------------------------
def dlna_set_uri(media_url, title=""):
    meta = (
        '&lt;DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"&gt;'
        '&lt;item id="0" parentID="-1" restricted="1"&gt;'
        f'&lt;dc:title&gt;{html.escape(title)}&lt;/dc:title&gt;'
        '&lt;upnp:class&gt;object.item.videoItem.movie&lt;/upnp:class&gt;'
        '&lt;/item&gt;&lt;/DIDL-Lite&gt;'
    )
    return avt("SetAVTransportURI", (
        "<InstanceID>0</InstanceID>"
        f"<CurrentURI>{html.escape(media_url)}</CurrentURI>"
        f"<CurrentURIMetaData>{meta}</CurrentURIMetaData>"
    ))


def dlna_play():
    return avt("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")


def dlna_pause():
    return avt("Pause", "<InstanceID>0</InstanceID>")


def dlna_stop():
    return avt("Stop", "<InstanceID>0</InstanceID>")


def dlna_seek(target):
    """Seek to HH:MM:SS position."""
    return avt("Seek", (
        "<InstanceID>0</InstanceID>"
        "<Unit>REL_TIME</Unit>"
        f"<Target>{target}</Target>"
    ))


def dlna_get_position():
    resp = avt("GetPositionInfo", "<InstanceID>0</InstanceID>")
    def extract(tag):
        try:
            return resp.split(f"<{tag}>")[1].split(f"</{tag}>")[0]
        except IndexError:
            return "?"
    return extract("RelTime"), extract("TrackDuration")


def dlna_get_state():
    resp = avt("GetTransportInfo", "<InstanceID>0</InstanceID>")
    try:
        return resp.split("<CurrentTransportState>")[1].split("</CurrentTransportState>")[0]
    except IndexError:
        return "UNKNOWN"


def dlna_get_volume():
    resp = rc("GetVolume", "<InstanceID>0</InstanceID><Channel>Master</Channel>")
    try:
        return resp.split("<CurrentVolume>")[1].split("</CurrentVolume>")[0]
    except IndexError:
        return "?"


def dlna_set_volume(level):
    return rc("SetVolume", (
        "<InstanceID>0</InstanceID>"
        "<Channel>Master</Channel>"
        f"<DesiredVolume>{level}</DesiredVolume>"
    ))


# ---------------------------------------------------------------------------
# Local IP detection
# ---------------------------------------------------------------------------
def get_local_ip():
    """Get our IP address on the same network as the projector."""
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
    print("  [space/p] play/pause toggle")
    print("  [s]       stop")
    print("  [f]       seek forward 30s")
    print("  [b]       seek back 30s")
    print("  [+/-]     volume up/down")
    print("  [i]       show position/state")
    print("  [q]       stop & quit\n")

    while True:
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            cmd = "q"

        if not cmd:
            continue
        elif cmd in (" ", "p"):
            state = dlna_get_state()
            if state == "PLAYING":
                dlna_pause()
                print("paused")
            else:
                dlna_play()
                print("playing")
        elif cmd == "s":
            dlna_stop()
            print("stopped")
        elif cmd in ("f", "b"):
            pos, dur = dlna_get_position()
            try:
                parts = pos.split(":")
                secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2].split(".")[0])
                secs += 30 if cmd == "f" else -30
                secs = max(0, secs)
                h, m, s_ = secs // 3600, (secs % 3600) // 60, secs % 60
                target = f"{h:02d}:{m:02d}:{s_:02d}"
                dlna_seek(target)
                print(f"seek -> {target}")
            except Exception as e:
                print(f"seek failed: {e}")
        elif cmd == "+":
            vol = dlna_get_volume()
            try:
                new = min(100, int(vol) + 5)
                dlna_set_volume(new)
                print(f"volume: {new}")
            except ValueError:
                print(f"volume read failed: {vol}")
        elif cmd == "-":
            vol = dlna_get_volume()
            try:
                new = max(0, int(vol) - 5)
                dlna_set_volume(new)
                print(f"volume: {new}")
            except ValueError:
                print(f"volume read failed: {vol}")
        elif cmd == "i":
            pos, dur = dlna_get_position()
            state = dlna_get_state()
            vol = dlna_get_volume()
            print(f"  state: {state}  position: {pos} / {dur}  volume: {vol}")
        elif cmd == "q":
            print("stopping playback...")
            dlna_stop()
            break
        else:
            print(f"unknown command: {cmd!r}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Stream a file to a DLNA renderer.")
    parser.add_argument("file", help="Path to the media file to stream")
    parser.add_argument("--port", type=int, default=8080, help="HTTP server port (default 8080)")
    args = parser.parse_args()

    file_path = os.path.abspath(args.file)
    if not os.path.isfile(file_path):
        sys.exit(f"file not found: {file_path}")

    local_ip = get_local_ip()
    title = os.path.splitext(os.path.basename(file_path))[0]
    size_mb = os.path.getsize(file_path) / (1024 * 1024)

    print(f"file:      {file_path} ({size_mb:.0f} MB)")
    print(f"server:    http://{local_ip}:{args.port}/")
    print(f"renderer:  {PROJECTOR_IP}:{DLNA_PORT}")

    url, ctype, server = start_http_server(file_path, "0.0.0.0", args.port)
    # Use our routable IP in the URL we send to the projector
    url = f"http://{local_ip}:{args.port}/{urllib.parse.quote(os.path.basename(file_path))}"
    print(f"media url: {url}")
    print(f"mime type: {ctype}")

    print("\nsending to projector...")
    dlna_set_uri(url, title=title)
    time.sleep(1)
    dlna_play()

    state = dlna_get_state()
    print(f"state: {state}")

    control_loop()
    server.shutdown()
    print("done.")


if __name__ == "__main__":
    main()
