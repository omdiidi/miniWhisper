#!/usr/bin/env bash
# build-client-local.sh — Build WisprAlt.app for personal use.
#
# Requires a free Apple Development certificate from Xcode (no Apple Developer
# Program enrollment needed). Open Xcode → Settings → Accounts → sign in with
# any Apple ID; Xcode auto-creates a Personal Team and issues the cert.
#
# For distribution to other users, see build-client.sh (requires Developer ID).
#
# Usage:
#   ./scripts/build-client-local.sh
#
#   # Override identity when multiple Apple Development certs exist:
#   SIGN_IDENTITY="Apple Development: you@example.com (TEAMID)" ./scripts/build-client-local.sh
#
# Output:
#   client/build/WisprAlt.app   (Apple-Development-signed, runnable via right-click → Open)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLIENT_DIR="$REPO_ROOT/client"
BUILD_DIR="$CLIENT_DIR/build"
APP_PATH="$BUILD_DIR/WisprAlt.app"

# Detect arch — supports both Apple Silicon and Intel hosts. SPM emits the
# binary under .build/<triple>/release/. Fall back to whichever directory
# exists (or fail explicitly if neither does).
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
    arm64)  SPM_TRIPLE="arm64-apple-macosx" ;;
    x86_64) SPM_TRIPLE="x86_64-apple-macosx" ;;
    *)
        echo "ERROR: unsupported host arch '$HOST_ARCH' — expected arm64 or x86_64" >&2
        exit 1
        ;;
esac

cd "$CLIENT_DIR"

# ── Step 1: SPM build ─────────────────────────────────────────────────────────
echo "Step 1/4: swift build -c release..."
swift build -c release

EXEC_PATH="$CLIENT_DIR/.build/$SPM_TRIPLE/release/WisprAlt"
SPARKLE_PATH="$CLIENT_DIR/.build/$SPM_TRIPLE/release/Sparkle.framework"

if [[ ! -x "$EXEC_PATH" ]]; then
    echo "ERROR: build did not produce $EXEC_PATH (host arch: $HOST_ARCH)" >&2
    exit 1
fi

if [[ ! -d "$SPARKLE_PATH" ]]; then
    echo "ERROR: Sparkle.framework not found at $SPARKLE_PATH" >&2
    echo "       SPM did not link Sparkle — check Package.swift dependencies." >&2
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

# Compile the asset catalog with actool. SPM does NOT auto-run actool on .xcassets
# resources — it just copies the directory raw. We compile manually so the bundle
# ships a proper Assets.car.
ASSETS_SRC="$CLIENT_DIR/WisprAlt/Resources/Assets.xcassets"
if [[ ! -d "$ASSETS_SRC" ]]; then
    echo "ERROR: $ASSETS_SRC missing. Run scripts/build-icon.sh first to generate the source." >&2
    exit 1
fi
ACTOOL_TMP="$(mktemp -d)"
trap "rm -rf '$ACTOOL_TMP'" EXIT
xcrun actool \
    --compile "$ACTOOL_TMP" \
    --platform macosx \
    --minimum-deployment-target 14.0 \
    --app-icon AppIcon \
    --output-partial-info-plist "$ACTOOL_TMP/AppIcon-info.plist" \
    "$ASSETS_SRC" >/dev/null 2>&1 || {
    echo "ERROR: actool failed to compile $ASSETS_SRC into Assets.car." >&2
    xcrun actool \
        --compile "$ACTOOL_TMP" \
        --platform macosx \
        --minimum-deployment-target 14.0 \
        --app-icon AppIcon \
        --output-partial-info-plist "$ACTOOL_TMP/AppIcon-info.plist" \
        "$ASSETS_SRC" >&2 || true
    exit 1
}
if [[ ! -f "$ACTOOL_TMP/Assets.car" ]]; then
    echo "ERROR: actool ran but did not produce Assets.car." >&2
    ls -la "$ACTOOL_TMP" >&2
    exit 1
fi
mkdir -p "$APP_PATH/Contents/Resources"
cp "$ACTOOL_TMP/Assets.car" "$APP_PATH/Contents/Resources/Assets.car"
echo "  Compiled + copied Assets.car"

xattr -cr "$APP_PATH"

# Add the @executable_path/../Frameworks rpath so the bundled Sparkle.framework
# resolves at runtime. Swift Package Manager's executable target doesn't add
# this by default — without it, dyld fails with "Library not loaded:
# @rpath/Sparkle.framework/Versions/B/Sparkle" on launch.
#
# Idempotent: if the rpath is already present (re-running over an existing
# build), install_name_tool errors with "would duplicate path" — we treat
# that one specific message as success. Any other failure is fatal so we
# don't ship a bundle that crashes at launch.
INSTALL_NAME_OUTPUT="$(install_name_tool -add_rpath '@executable_path/../Frameworks' \
    "$APP_PATH/Contents/MacOS/WisprAlt" 2>&1 || true)"
if [[ -n "$INSTALL_NAME_OUTPUT" ]]; then
    if echo "$INSTALL_NAME_OUTPUT" | grep -q "would duplicate path"; then
        echo "  rpath already present — skipping (idempotent)."
    else
        echo "ERROR: install_name_tool failed: $INSTALL_NAME_OUTPUT" >&2
        exit 1
    fi
fi

# Verify the rpath actually wound up in the binary. Catches the case where
# install_name_tool silently no-ops on a write-protected or pre-stripped binary.
if ! otool -l "$APP_PATH/Contents/MacOS/WisprAlt" \
        | grep -A2 LC_RPATH \
        | grep -q '@executable_path/../Frameworks'; then
    echo "ERROR: rpath @executable_path/../Frameworks missing after install_name_tool" >&2
    echo "       The bundle would fail at launch with a dyld error." >&2
    exit 1
fi
xattr -cr "$APP_PATH"

# ── Step 3: Apple Development sign with entitlements ─────────────────────────
# `--deep` recursively re-signs Sparkle.framework with the same identity
# in a single pass. Apple has deprecated `--deep` for distribution builds, but
# it's the simplest path for local builds and avoids stale-xattr issues that
# occur when signing nested bundles independently.
#
# Pipefail (set -o pipefail via `set -euo pipefail` above) ensures the script
# fails if codesign fails — `tee` preserves output without masking exit codes.
#
# Use Apple Development identity (free, from Xcode → Settings → Accounts → any Apple ID).
# Required for SMAppService.mainApp.register() — see Apple Developer Forums thread 799910.
# Multiple identities trigger an explicit-disambiguation error; set SIGN_IDENTITY env var
# to override.

# Find ALL Apple Development identities. security find-identity output looks like:
#   1) <SHA1HASH> "Apple Development: user@example.com (TEAMID)"
APPLE_DEV_IDENTITIES="$(security find-identity -v -p codesigning \
    | sed -n 's/.*"\(Apple Development:[^"]*\)".*/\1/p')"

NUM_IDENTITIES="$(printf '%s\n' "$APPLE_DEV_IDENTITIES" | grep -c 'Apple Development:' || true)"

if [ "$NUM_IDENTITIES" -eq 0 ]; then
    cat >&2 <<'EOM'
ERROR: WisprAlt requires an Apple Development code-signing identity.

Setup (one time, no Apple Developer Program enrollment needed):
  1. Open Xcode.
  2. Settings → Accounts → "+" → sign in with any Apple ID.
  3. Xcode auto-creates a Personal Team and issues an Apple Development cert.
  4. Re-run this script.

Why required: SMAppService.mainApp.register() (login-at-startup) refuses
ad-hoc / self-signed identities. Apple Developer Forums thread 799910.
EOM
    exit 1
fi

if [ "$NUM_IDENTITIES" -gt 1 ] && [ -z "${SIGN_IDENTITY:-}" ]; then
    cat >&2 <<EOM
ERROR: Multiple Apple Development identities found. Set SIGN_IDENTITY explicitly:

$APPLE_DEV_IDENTITIES

Example:
  SIGN_IDENTITY="Apple Development: you@example.com (TEAMID)" bash $0
EOM
    exit 1
fi

# Single identity OR explicit override via SIGN_IDENTITY env var.
SIGN_IDENTITY="${SIGN_IDENTITY:-$(printf '%s' "$APPLE_DEV_IDENTITIES" | head -1)}"
echo "Step 3/4: Codesigning with '$SIGN_IDENTITY'..."

# Strip Apple metadata. Real-identity signing (unlike ad-hoc) refuses to
# sign bundles with resource-fork / FinderInfo xattrs. macOS Sequoia/Tahoe
# auto-tags Sparkle.framework's nested files with com.apple.provenance and
# com.apple.FinderInfo, and re-applies them faster than `xattr -d` can strip.
#
# Workaround: tar archives DON'T include xattrs by default, so round-trip
# the bundle through tar. The extracted copy starts xattr-clean, and we
# sign immediately before the OS can re-tag it.
TAR_TMP="$(mktemp -t wispralt-build-XXXXXX).tar"
( cd "$BUILD_DIR" && tar -cf "$TAR_TMP" "WisprAlt.app" )
rm -rf "$APP_PATH"
( cd "$BUILD_DIR" && tar -xf "$TAR_TMP" )
rm -f "$TAR_TMP"

# NOTE: We do NOT use --options runtime here. Hardened Runtime would require
# the bundled Sparkle.framework to be either same-team-signed (impossible for
# ad-hoc / self-signed) or have `com.apple.security.cs.disable-library-validation`
# in the entitlements. Local builds skip this — see build-client.sh for the
# notarization-eligible path.
codesign \
    --force \
    --deep \
    --sign "$SIGN_IDENTITY" \
    --entitlements "$CLIENT_DIR/WisprAlt/WisprAlt.entitlements" \
    "$APP_PATH"

# ── Step 4: Verify ────────────────────────────────────────────────────────────
# Use plain --verify (NOT --strict). macOS Sequoia/Tahoe re-applies
# com.apple.provenance and com.apple.FinderInfo to the bundle within
# milliseconds of signing — strict mode rejects those even though they
# don't invalidate the signature itself. Plain --verify confirms the
# signature is well-formed and matches the code; that's what dyld checks
# at launch, and what we actually care about.
echo "Step 4/4: Verifying signature..."
codesign --verify --verbose=1 "$APP_PATH"
echo "  ✓ valid"

echo ""
echo "Build complete."
echo "  $APP_PATH"
echo ""
echo "First launch:"
echo "  1. Drag $APP_PATH to /Applications/."
echo "  2. Right-click → Open the first time (Gatekeeper: 'Apple Development' cert, not yet trusted)."
echo "  3. Grant the four TCC permissions (Accessibility, Input Monitoring, Microphone, Screen Recording)."
echo "  4. Open Settings, paste server URL + API key, click Test Connection."
echo "  5. Toggle 'Launch at login' in Settings to register WisprAlt as a Login Item."
