#!/bin/bash
# Build Mediacast Home launcher APK for the FUDONI GC888 projector
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

SDK="/usr/lib/android-sdk"
BUILD_TOOLS="$SDK/build-tools/28.0.3"
PLATFORM="$SDK/platforms/android-28/android.jar"

AAPT2="$BUILD_TOOLS/aapt2"
D8="$BUILD_TOOLS/d8"
ZIPALIGN="$BUILD_TOOLS/zipalign"
APKSIGNER="$BUILD_TOOLS/apksigner"

OUT="$SCRIPT_DIR/build"
APK_UNSIGNED="$OUT/launcher-unsigned.apk"
APK_ALIGNED="$OUT/launcher-aligned.apk"
APK_SIGNED="$OUT/launcher.apk"
KEYSTORE="$SCRIPT_DIR/../debug.jks"

echo "=== Mediacast Home Build ==="

# Clean
rm -rf "$OUT"
mkdir -p "$OUT/classes"

# 1. Generate debug keystore if missing
if [ ! -f "$KEYSTORE" ]; then
    echo "[1/7] Generating debug keystore..."
    keytool -genkeypair -v \
        -keystore "$KEYSTORE" \
        -alias debug \
        -keyalg RSA -keysize 2048 \
        -validity 10000 \
        -storepass android \
        -keypass android \
        -dname "CN=Debug,O=Mediacast,C=US"
else
    echo "[1/7] Debug keystore exists."
fi

# 2. Link manifest to create base APK
echo "[2/7] Linking manifest..."
"$AAPT2" link \
    --manifest AndroidManifest.xml \
    -I "$PLATFORM" \
    --min-sdk-version 28 \
    --target-sdk-version 28 \
    --version-code 1 \
    --version-name "1.0" \
    -o "$APK_UNSIGNED" \
    --java "$OUT"

# 3. Compile Java
echo "[3/7] Compiling Java..."
find java -name "*.java" > "$OUT/sources.txt"
if [ -f "$OUT/com/mediacast/launcher/R.java" ]; then
    echo "$OUT/com/mediacast/launcher/R.java" >> "$OUT/sources.txt"
fi
javac \
    -source 1.8 -target 1.8 \
    -bootclasspath "$PLATFORM" \
    -classpath "$PLATFORM" \
    -d "$OUT/classes" \
    @"$OUT/sources.txt"

# 4. Convert to DEX
echo "[4/7] Converting to DEX..."
"$D8" \
    --lib "$PLATFORM" \
    --min-api 28 \
    --output "$OUT" \
    $(find "$OUT/classes" -name "*.class")

# 5. Add classes.dex to APK
echo "[5/7] Adding DEX to APK..."
cd "$OUT"
zip -j "$APK_UNSIGNED" classes.dex
cd "$SCRIPT_DIR"

# 6. Align APK
echo "[6/7] Aligning APK..."
"$ZIPALIGN" -f 4 "$APK_UNSIGNED" "$APK_ALIGNED"

# 7. Sign APK
echo "[7/7] Signing APK..."
"$APKSIGNER" sign \
    --ks "$KEYSTORE" \
    --ks-key-alias debug \
    --ks-pass pass:android \
    --key-pass pass:android \
    --out "$APK_SIGNED" \
    "$APK_ALIGNED"

echo ""
echo "=== Build complete: $APK_SIGNED ==="
ls -lh "$APK_SIGNED"

# Install if device is connected
if adb devices | grep -q "device$"; then
    echo ""
    echo "Installing on device..."
    adb install -r "$APK_SIGNED"
    echo "Installed."
else
    echo ""
    echo "No device connected. Install manually with:"
    echo "  adb install -r $APK_SIGNED"
fi
