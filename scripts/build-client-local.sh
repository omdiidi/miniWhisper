#!/usr/bin/env bash
# build-client-local.sh — Build WisprAlt.app for personal use without an Apple Developer ID.
#
# This produces an ad-hoc signed .app bundle that runs on the local machine.
# For distribution to other users, see build-client.sh (requires Developer ID).
#
# Usage:
#   ./scripts/build-client-local.sh
#
# Output:
#   client/build/WisprAlt.app   (ad-hoc signed, runnable via right-click → Open)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLIENT_DIR="$REPO_ROOT/client"
BUILD_DIR="$CLIENT_DIR/build"
APP_PATH="$BUILD_DIR/WisprAlt.app"

cd "$CLIENT_DIR"

# ── Step 1: SPM build ─────────────────────────────────────────────────────────
echo "Step 1/4: swift build -c release..."
swift build -c release

EXEC_PATH="$CLIENT_DIR/.build/arm64-apple-macosx/release/WisprAlt"
SPARKLE_PATH="$CLIENT_DIR/.build/arm64-apple-macosx/release/Sparkle.framework"

if [[ ! -x "$EXEC_PATH" ]]; then
    echo "ERROR: build did not produce $EXEC_PATH" >&2
    exit 1
fi

# ── Step 2: Assemble .app bundle ──────────────────────────────────────────────
echo "Step 2/4: Assembling $APP_PATH..."
rm -rf "$BUILD_DIR"
mkdir -p "$APP_PATH/Contents/MacOS" \
         "$APP_PATH/Contents/Resources" \
         "$APP_PATH/Contents/Frameworks"

# ditto with --norsrc --noextattr keeps xattrs/resource forks out, which would
# otherwise cause codesign to refuse the bundle.
ditto --norsrc --noextattr "$EXEC_PATH" "$APP_PATH/Contents/MacOS/WisprAlt"
ditto --norsrc --noextattr "$CLIENT_DIR/WisprAlt/Info.plist" "$APP_PATH/Contents/Info.plist"
ditto --norsrc --noextattr "$SPARKLE_PATH" "$APP_PATH/Contents/Frameworks/Sparkle.framework"

xattr -cr "$APP_PATH"

# ── Step 3: Ad-hoc sign with entitlements ─────────────────────────────────────
echo "Step 3/4: Codesigning (ad-hoc)..."
codesign \
    --force \
    --deep \
    --sign - \
    --entitlements "$CLIENT_DIR/WisprAlt/WisprAlt.entitlements" \
    "$APP_PATH" \
    2>&1 | tail -3

# ── Step 4: Verify ────────────────────────────────────────────────────────────
echo "Step 4/4: Verifying signature..."
codesign --verify --strict "$APP_PATH" && echo "  ✓ valid"

echo ""
echo "Build complete."
echo "  $APP_PATH"
echo ""
echo "First launch:"
echo "  1. Drag $APP_PATH to /Applications/."
echo "  2. Right-click → Open the first time (Gatekeeper bypass for ad-hoc signed)."
echo "  3. Grant the four permissions (Accessibility, Input Monitoring, Microphone, Screen Recording)."
echo "  4. Open Settings, paste server URL + API key, click Test Connection."
