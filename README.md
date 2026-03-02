# Mediacast — Custom Streaming & Control Apps for Budget Android Projectors

Turn a cheap Android projector into a proper media center with real-time EQ, a clean home screen, and no vendor bloat. Designed for the FUDONI GC888 (Siviton/MediaTek MT5862, Android 9), but should work on similar budget projectors.

## What's included

| Component | Description |
|-----------|-------------|
| `castweb.py` | Web UI — browse files, cast, and control playback from any browser |
| `cast.py` | CLI tool — cast a single file with keyboard controls |
| `android/` | **EQ Player** APK — video player with real-time 5-band equalizer |
| `android/launcher/` | **Mediacast Home** APK — replacement home launcher |
| `setup.sh` | One-command setup: disable OTA, install APKs, set home launcher |

## How it works

```
  Phone/Laptop                         Projector (Android 9)
 ┌──────────────┐                    ┌─────────────────────────┐
 │  castweb.py  │── HTTP serve ──>   │  EQ Player (port 8081)  │
 │  (port 8080) │── ADB launch ──>   │  MediaPlayer+SurfaceView│
 │              │── POST /eq   ──>   │  Equalizer AudioEffect  │
 │  Browser UI  │                    │                         │
 │  - file list │                    │  Mediacast Home         │
 │  - EQ sliders│                    │  (replaces stock UI)    │
 │  - controls  │                    └─────────────────────────┘
 └──────────────┘
```

1. `castweb.py` serves media files over HTTP and sends ADB commands to the projector
2. **EQ Player** runs on the projector, plays the video with `MediaPlayer` + `SurfaceView`
3. EQ sliders in the web UI POST to EQ Player's HTTP server (port 8081), which adjusts Android's `Equalizer` AudioEffect in real-time — no transcoding, no interruption
4. **Mediacast Home** replaces the stock Siviton launcher with a clean home screen

## Requirements

- Python 3.7+
- `adb`, `ffmpeg`, `ffprobe`
- Android SDK build tools (only for building APKs from source)

```bash
# Ubuntu/Debian
sudo apt install adb ffmpeg

# Android SDK (only needed to build APKs)
sudo apt install android-sdk
```

## Quick Start

### 1. Setup the projector

```bash
# Connect to projector
adb connect 192.168.86.248:5555

# Run the setup script (installs APKs, disables OTA, sets home launcher)
./setup.sh
```

Or do it manually:

```bash
# Build and install EQ Player
cd android && bash build.sh && cd ..

# Build and install Mediacast Home launcher
cd android/launcher && bash build.sh && cd ..

# Disable OTA updates (Baidu OTA service)
adb shell pm disable-user --user 0 com.baidu.ota

# Set our launcher as default home
adb shell cmd package set-home-activity com.mediacast.launcher/.HomeActivity
```

### 2. Start castweb

```bash
./castweb.py --root /path/to/your/media
./castweb.py --port 9090 --root /mnt/media
```

Then open `http://<your-ip>:8080/` on any device on the same network.

### 3. Cast and control

- **Browse**: click folders, breadcrumbs to go back
- **Cast**: click any media file
- **EQ**: click the EQ button in the header, drag sliders or pick a preset
- **Controls**: play/pause, seek, volume, stop
- **Keyboard shortcuts**: space = play/pause, arrows = seek/volume, `m` = mute, `s` = stop, `e` = toggle EQ

## Configuration

Edit the top of `castweb.py` if your projector has a different IP:

```python
PROJECTOR_IP = "192.168.86.248"
```

## EQ Player

The EQ Player app runs on the projector and plays video with real-time equalization.

### Why client-side EQ?

Server-side EQ (ffmpeg transcoding) has fundamental problems:
- Pre-transcoding takes ~30 seconds before playback starts
- Pipe streaming (MPEG-TS) has no duration/seek support
- Changing EQ requires re-transcoding from the current position

Client-side EQ via Android's `Equalizer` AudioEffect gives:
- Instant playback (raw file served directly)
- Full seek/duration support
- Real-time EQ changes with zero interruption

### EQ band mapping

The web UI has 3 sliders (bass/mid/treble, -12 to +12 dB). These map to Android's 5 EQ bands:

| Slider | Android bands | Typical frequencies |
|--------|--------------|-------------------|
| Bass | Band 0, Band 1 | ~60 Hz, ~230 Hz |
| Mid | Band 2 | ~910 Hz |
| Treble | Band 3, Band 4 | ~3.6 kHz, ~14 kHz |

dB values are converted to millibels (dB * 100) for the Android API.

### EQ Player HTTP API (port 8081)

| Endpoint | Method | Body | Description |
|----------|--------|------|-------------|
| `/eq` | POST | `{"bands": [300, 0, -200, 0, 200]}` | Set EQ levels (millibels) |
| `/loudnorm` | POST | `{"enabled": true, "gain": 600}` | Toggle loudness enhancer |
| `/info` | GET | — | Band frequencies, levels, player state |

### Player controls

- **Tap screen**: show/hide controls overlay (seek bar + play/pause)
- **Remote OK/Enter**: toggle controls
- **Remote left/right**: seek 10 seconds
- **Remote back**: stop and exit

### Building from source

Requires Android SDK build tools 28.0.3 and platform android-28:

```bash
cd android
bash build.sh      # builds and installs EQ Player
cd launcher
bash build.sh      # builds and installs Mediacast Home
```

The build scripts handle the full pipeline: aapt2 link, javac, d8, zipalign, apksigner.

## Mediacast Home (launcher)

A clean replacement for the stock Siviton launcher. Shows:
- Clock and IP address
- Buttons for: Original Launcher, Settings, File Manager
- Hint about casting from castweb

The original launcher is always accessible via the "Original Launcher" button — nothing is deleted.

## Disabling OTA updates

The projector ships with `com.baidu.ota`, a Baidu OTA update service. Updating is risky on these budget projectors:

- Updates can remove ADB access, lock down sideloading, or add unwanted apps
- Recovery on MediaTek TV chips requires UART + SP Flash Tool
- Android 9 is end-of-life anyway — no meaningful security benefit
- The projector is on a local network playing video — attack surface is minimal

To disable:
```bash
adb shell pm disable-user --user 0 com.baidu.ota
```

To re-enable if needed:
```bash
adb shell pm enable com.baidu.ota
```

## Device info

| Field | Value |
|-------|-------|
| Model | FUDONI GC888 |
| OEM | Siviton (white-label) |
| SoC | MediaTek MT5862 |
| RAM | 512 MB |
| Android | 9 (API 28) |
| ABI | armeabi-v7a |
| Build | TVOS-04.16.031.01.12 |
| Security patch | 2021-01-05 |
| ADB | TCP port 5555 |
| Stock launcher | com.siviton.blcastlauncher |
| OTA updater | com.baidu.ota |

## castweb.py API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | HTML UI |
| `/api/browse?path=...` | GET | List directory (JSON) |
| `/api/cast` | POST | Cast a file `{"path": "/abs/path"}` |
| `/api/stop` | POST | Stop playback |
| `/api/control` | POST | Send command `{"action": "play_pause"}` |
| `/api/dsp` | POST | Set EQ `{"bass": 6, "mid": 0, "treble": -2}` |
| `/api/status` | GET | Current state (JSON) |
| `/media/<file>` | GET | Range-request media serving |

## Audio transcoding

Transcoding only happens for codecs incompatible with Bluetooth output (AC-3, EAC-3, DTS, TrueHD). When detected, audio is transcoded to AAC stereo via ffmpeg (flat, no EQ filters) while copying the video stream. EQ is always handled by the player app, never by ffmpeg.

## Supported formats

`.mp4`, `.mkv`, `.avi`, `.mov`, `.wmv`, `.flv`, `.ts`, `.webm`, `.mp3`, `.flac`, `.wav`, `.aac`

## cast.py — CLI (legacy)

```bash
./cast.py '/path/to/movie.mp4'
```

| Key | Action |
|-----|--------|
| `p` | play/pause |
| `s` | stop |
| `f` / `b` | seek forward / back |
| `+` / `-` | volume up / down |
| `m` | mute |
| `q` | stop & quit |
