#!/usr/bin/env bash
# build-client.sh — Build, sign, notarize, and staple the WisprAlt macOS client DMG.
#
# Usage:
#   ./scripts/build-client.sh "Developer ID Application: Your Name (TEAMID)"
#
# Required environment variables:
#   APPLE_ID                Notarytool Apple ID (e.g. you@example.com)
#   APP_SPECIFIC_PASSWORD   App-specific password generated at appleid.apple.com
#   TEAM_ID                 Your Apple Developer Team ID (10-char string)
#
# Optional environment variables:
#   SPARKLE_ED_PRIVATE_KEY  Path to Sparkle EdDSA private key file.
#                           If set, sign_update is called and an appcast snippet
#                           is written to client/build/appcast-snippet.xml.
#
# Output:
#   client/build/WisprAlt.app     (signed app bundle)
#   client/build/WisprAlt.dmg     (signed, notarized, stapled DMG)
#   client/build/appcast-snippet.xml  (if SPARKLE_ED_PRIVATE_KEY is set)

set -euo pipefail

# ── Validate required argument ────────────────────────────────────────────────
DEVELOPER_ID_APP="${1:-}"
if [[ -z "$DEVELOPER_ID_APP" ]]; then
    echo 'usage: build-client.sh "Developer ID Application: Your Name (TEAMID)"' >&2
    echo "" >&2
    echo "The identity string must match exactly what is in your keychain." >&2
    echo "  List available identities: security find-identity -v -p codesigning" >&2
    exit 1
fi

# ── Validate required environment variables ───────────────────────────────────
for VAR in APPLE_ID APP_SPECIFIC_PASSWORD TEAM_ID; do
    if [[ -z "${!VAR:-}" ]]; then
        echo "ERROR: $VAR is not set." >&2
        echo "  Set it in your shell environment or CI secrets before running this script." >&2
        exit 1
    fi
done

# ── Locate repo root ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLIENT_DIR="$REPO_ROOT/client"
BUILD_DIR="$CLIENT_DIR/build"
ARCHIVE_PATH="$BUILD_DIR/WisprAlt.xcarchive"
APP_PATH="$BUILD_DIR/WisprAlt.app"
DMG_PATH="$BUILD_DIR/WisprAlt.dmg"
ENTITLEMENTS="$CLIENT_DIR/WisprAlt/WisprAlt.entitlements"
EXPORT_OPTIONS_PLIST="$BUILD_DIR/ExportOptions.plist"

echo "Building WisprAlt client..."
echo "  Identity:   $DEVELOPER_ID_APP"
echo "  Team ID:    $TEAM_ID"
echo "  Apple ID:   $APPLE_ID"
echo "  Client dir: $CLIENT_DIR"
echo ""

# ── Create build directory ────────────────────────────────────────────────────
mkdir -p "$BUILD_DIR"

# ── Generate ExportOptions.plist ─────────────────────────────────────────────
# method=developer-id: sign for distribution outside the Mac App Store.
# signingStyle=manual: use the explicit identity we pass rather than automatic.
cat > "$EXPORT_OPTIONS_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>method</key>
    <string>developer-id</string>
    <key>signingStyle</key>
    <string>manual</string>
    <key>signingCertificate</key>
    <string>${DEVELOPER_ID_APP}</string>
    <key>teamID</key>
    <string>${TEAM_ID}</string>
    <key>destination</key>
    <string>export</string>
</dict>
</plist>
PLIST
echo "ExportOptions.plist written."

# ── Step 1: xcodebuild archive ────────────────────────────────────────────────
echo ""
echo "Step 1: xcodebuild archive..."
xcodebuild \
    -scheme WisprAlt \
    -configuration Release \
    -archivePath "$ARCHIVE_PATH" \
    archive \
    CODE_SIGN_IDENTITY="$DEVELOPER_ID_APP" \
    DEVELOPMENT_TEAM="$TEAM_ID" \
    | grep -E "^(error:|warning:|=== BUILD|Archive Succeeded)" || true
echo "Archive: $ARCHIVE_PATH"

# ── Step 2: Export archive ────────────────────────────────────────────────────
echo ""
echo "Step 2: xcodebuild -exportArchive..."
xcodebuild \
    -exportArchive \
    -archivePath "$ARCHIVE_PATH" \
    -exportPath "$BUILD_DIR" \
    -exportOptionsPlist "$EXPORT_OPTIONS_PLIST" \
    | grep -E "^(error:|warning:|=== BUILD|Export Succeeded)" || true
echo "Exported app: $APP_PATH"

# ── Step 2.5: Verify Sparkle rpath ───────────────────────────────────────────
# Package.swift adds @executable_path/../Frameworks via linkerSettings so the
# bundled Sparkle.framework resolves at runtime. If anyone strips that out,
# the resulting .app crashes at first launch with a dyld error — but only
# AFTER it's been signed, notarized, stapled, and shipped. Catch it here.
echo ""
echo "Step 2.5: Verifying Sparkle rpath..."
if ! otool -l "$APP_PATH/Contents/MacOS/WisprAlt" \
        | grep -A2 LC_RPATH \
        | grep -q '@executable_path/../Frameworks'; then
    echo "ERROR: rpath @executable_path/../Frameworks missing from $APP_PATH/Contents/MacOS/WisprAlt" >&2
    echo "       The bundle would fail at launch with:" >&2
    echo "       'Library not loaded: @rpath/Sparkle.framework/Versions/B/Sparkle'" >&2
    echo "       Check Package.swift linkerSettings." >&2
    exit 1
fi
echo "  ✓ rpath present"

# ── Step 3: Deep codesign with hardened runtime ───────────────────────────────
# --options runtime: mandatory for notarization (hardened runtime).
# --timestamp: embed a secure timestamp from Apple's time-stamp server.
# --deep: recursively sign all embedded dylibs and frameworks.
echo ""
echo "Step 3: codesign app bundle..."
codesign \
    --force \
    --deep \
    --timestamp \
    --options runtime \
    --entitlements "$ENTITLEMENTS" \
    --sign "$DEVELOPER_ID_APP" \
    "$APP_PATH"
echo "App bundle signed."

# ── Step 4: Verify codesign ───────────────────────────────────────────────────
echo ""
echo "Step 4: Verifying codesign..."
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
echo "Codesign verification passed."

# ── Step 5: Create DMG ───────────────────────────────────────────────────────
echo ""
echo "Step 5: Creating DMG..."
# Remove any stale DMG first (hdiutil create fails if the file exists)
rm -f "$DMG_PATH"
hdiutil create \
    -volname "WisprAlt" \
    -srcfolder "$APP_PATH" \
    -ov \
    -format UDZO \
    "$DMG_PATH"
echo "DMG created: $DMG_PATH"

# ── Step 6: Sign the DMG ──────────────────────────────────────────────────────
echo ""
echo "Step 6: Signing DMG..."
codesign \
    --force \
    --timestamp \
    --sign "$DEVELOPER_ID_APP" \
    "$DMG_PATH"
echo "DMG signed."

# ── Step 7: Notarize ─────────────────────────────────────────────────────────
# Using --apple-id/--password/--team-id (NOT --keychain-profile) so this works
# in CI where there is no keychain available.
echo ""
echo "Step 7: Notarizing DMG (this may take several minutes)..."
xcrun notarytool submit "$DMG_PATH" \
    --apple-id "$APPLE_ID" \
    --password "$APP_SPECIFIC_PASSWORD" \
    --team-id "$TEAM_ID" \
    --wait
echo "Notarization complete."

# ── Step 8: Staple notarization ticket ───────────────────────────────────────
echo ""
echo "Step 8: Stapling notarization ticket to DMG..."
xcrun stapler staple "$DMG_PATH"
echo "Staple complete."

# ── Step 9: Validate staple ───────────────────────────────────────────────────
echo ""
echo "Step 9: Validating staple..."
xcrun stapler validate "$DMG_PATH"
echo "Staple validation passed."

# ── Step 10: Sparkle appcast snippet (optional) ───────────────────────────────
APPCAST_SNIPPET="$BUILD_DIR/appcast-snippet.xml"
if [[ -n "${SPARKLE_ED_PRIVATE_KEY:-}" ]]; then
    echo ""
    echo "Step 10: Generating Sparkle appcast snippet..."
    if [[ ! -f "$SPARKLE_ED_PRIVATE_KEY" ]]; then
        echo "WARNING: SPARKLE_ED_PRIVATE_KEY path does not exist: $SPARKLE_ED_PRIVATE_KEY" >&2
        echo "  Skipping appcast snippet generation." >&2
    else
        SPARKLE_SIGN_BIN="$CLIENT_DIR/.build/artifacts/sparkle/Sparkle/bin/sign_update"
        if [[ ! -f "$SPARKLE_SIGN_BIN" ]]; then
            # Try common SPM resolved path
            SPARKLE_SIGN_BIN="$(find "$CLIENT_DIR" -name sign_update -type f 2>/dev/null | head -1 || true)"
        fi
        if [[ -z "$SPARKLE_SIGN_BIN" || ! -f "$SPARKLE_SIGN_BIN" ]]; then
            echo "WARNING: Sparkle sign_update binary not found." >&2
            echo "  Build the project once so SPM resolves Sparkle, then retry." >&2
        else
            # sign_update outputs the edSignature and length for the appcast item
            DMG_SIZE="$(stat -f "%z" "$DMG_PATH")"
            SIGN_OUTPUT="$("$SPARKLE_SIGN_BIN" "$DMG_PATH" "$SPARKLE_ED_PRIVATE_KEY")"
            ED_SIGNATURE="$(echo "$SIGN_OUTPUT" | grep -o 'sparkle:edSignature="[^"]*"' || echo 'sparkle:edSignature="UNKNOWN"')"

            cat > "$APPCAST_SNIPPET" <<XML
<!-- Paste this <item> into your appcast.xml for this release -->
<item>
    <title>WisprAlt</title>
    <pubDate>$(date -u "+%a, %d %b %Y %H:%M:%S +0000")</pubDate>
    <enclosure
        url="https://github.com/YOUR_ORG/wisprflowALT/releases/latest/download/WisprAlt.dmg"
        sparkle:version="$(defaults read "$CLIENT_DIR/WisprAlt/Info" CFBundleVersion 2>/dev/null || echo "1.0.0")"
        sparkle:shortVersionString="$(defaults read "$CLIENT_DIR/WisprAlt/Info" CFBundleShortVersionString 2>/dev/null || echo "1.0")"
        length="${DMG_SIZE}"
        type="application/x-apple-diskimage"
        ${ED_SIGNATURE}
    />
</item>
XML
            echo "Appcast snippet written to: $APPCAST_SNIPPET"
        fi
    fi
else
    echo ""
    echo "Step 10: Skipping Sparkle appcast (SPARKLE_ED_PRIVATE_KEY not set)."
fi

# ── Final report ──────────────────────────────────────────────────────────────
echo ""
echo "──────────────────────────────────────────────"
echo "Build complete:"
echo "  App:  $APP_PATH"
echo "  DMG:  $DMG_PATH"
[[ -f "$APPCAST_SNIPPET" ]] && echo "  Appcast snippet: $APPCAST_SNIPPET"
echo ""
echo "To verify Gatekeeper assessment:"
echo "  spctl --assess --verbose=4 --type execute \"$APP_PATH\""
echo "  xcrun stapler validate \"$DMG_PATH\""
