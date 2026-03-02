# cast — Stream Media to FUDONI GC888 Projector

Two tools for casting local media files to the projector over the network via HTTP + ADB.

## Requirements

- Python 3.7+
- `adb` (Android Debug Bridge)
- `ffmpeg` and `ffprobe`

On Ubuntu/Debian:

```
sudo apt install adb ffmpeg
```

## Network Setup

The projector must be on the same network as the machine running the script. The projector's IP and ADB port are hardcoded at the top of both scripts:

```python
PROJECTOR_IP = "192.168.86.248"
ADB_TARGET = "192.168.86.248:5555"
```

Edit these if your projector has a different IP.

ADB over TCP must be enabled on the projector (port 5555). The scripts call `adb connect` automatically on startup.

## Files

| File | Description |
|------|-------------|
| `cast.py` | CLI tool — cast a single file with interactive keyboard controls |
| `castweb.py` | Web UI — browse files, cast, and control playback from a browser |

## cast.py — CLI

Cast a single file:

```
./cast.py '/path/to/movie.mp4'
./cast.py '/path/to/movie.mp4' --port 9090
./cast.py '/path/to/movie.mkv' --no-transcode
```

Interactive controls appear after playback starts:

| Key | Action |
|-----|--------|
| `p` | play/pause |
| `s` | stop |
| `f` / `b` | seek forward / back |
| `F` / `B` | fast-forward / rewind |
| `+` / `-` | volume up / down |
| `m` | mute toggle |
| `q` | stop & quit |

## castweb.py — Web UI

Start the server:

```
./castweb.py
./castweb.py --port 9090
./castweb.py --port 8080 --root /mnt/media
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 8080 | HTTP server port |
| `--root` | `~` (home dir) | Root directory for the file browser |

Then open `http://<your-ip>:8080/` on any device (phone, tablet, laptop) on the same network.

### Web UI usage

- **Browse**: click folders to navigate, breadcrumbs at the top to go back
- **Cast**: click any media file to start casting to the projector
- **Controls**: play/pause, seek, volume, mute, stop — buttons at the bottom
- **Keyboard shortcuts** (when focused in browser): space = play/pause, arrows = seek/volume, `m` = mute, `s` = stop
- **Status bar**: shows "Transcoding time=00:05:23" during transcode, "Casting filename.mp4" during playback

### API endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | HTML UI |
| `/api/browse?path=...` | GET | List directory (JSON) |
| `/api/cast` | POST | Cast a file `{"path": "/abs/path"}` |
| `/api/stop` | POST | Stop playback |
| `/api/control` | POST | Send command `{"action": "play_pause"}` |
| `/api/status` | GET | Current state (JSON) |
| `/media/<file>` | GET | Range-request media serving |

Control actions: `play_pause`, `stop`, `seek_fwd`, `seek_back`, `ff`, `rw`, `vol_up`, `vol_down`, `mute`.

## Audio Transcoding

Both tools auto-detect audio codecs incompatible with Bluetooth A2DP output (AC-3, DTS, EAC-3, TrueHD). When found, the audio is transcoded to AAC stereo via ffmpeg while copying the video stream. The temp file is cleaned up on stop or shutdown.

## Supported Media Formats

`.mp4`, `.mkv`, `.avi`, `.mov`, `.wmv`, `.flv`, `.ts`, `.webm`, `.mp3`, `.flac`, `.wav`, `.aac`

The file browser only shows directories and files with these extensions.
