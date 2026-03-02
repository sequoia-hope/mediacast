#!/bin/bash
# Mediacast setup — build APKs, install on projector, configure device
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECTOR="192.168.86.248:5555"

echo "=== Mediacast Setup ==="
echo ""

# 1. Connect to projector
echo "[1/5] Connecting to projector..."
adb connect "$PROJECTOR"
adb -s "$PROJECTOR" wait-for-device
echo "Connected."
echo ""

# 2. Build and install EQ Player
echo "[2/5] Building EQ Player..."
cd "$SCRIPT_DIR/android"
bash build.sh
echo ""

# 3. Build and install Mediacast Home
echo "[3/5] Building Mediacast Home launcher..."
cd "$SCRIPT_DIR/android/launcher"
bash build.sh
echo ""

# 4. Disable OTA updates
echo "[4/5] Disabling OTA updates (com.baidu.ota)..."
adb -s "$PROJECTOR" shell pm disable-user --user 0 com.baidu.ota 2>/dev/null || true
echo "OTA disabled."
echo ""

# 5. Set our launcher as default home
echo "[5/5] Setting Mediacast Home as default launcher..."
adb -s "$PROJECTOR" shell cmd package set-home-activity com.mediacast.launcher/.HomeActivity
echo "Launcher set."
echo ""

echo "=== Setup complete ==="
echo ""
echo "Installed apps:"
echo "  - EQ Player (com.mediacast.eqplayer)"
echo "  - Mediacast Home (com.mediacast.launcher)"
echo ""
echo "Start castweb:"
echo "  ./castweb.py --root /path/to/media"
echo ""
echo "To undo everything:"
echo "  adb shell pm enable com.baidu.ota"
echo "  adb shell pm uninstall com.mediacast.launcher"
echo "  adb shell pm uninstall com.mediacast.eqplayer"
