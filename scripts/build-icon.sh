#!/usr/bin/env bash
# build-icon.sh — Generate AppIcon.appiconset PNGs from the 1254×1254 source.
# Idempotent: re-run any time to refresh from the source.
#
# Usage:
#   ./scripts/build-icon.sh
#
# Sources from:
#   /Users/omidzahrai/.pane/images/43793091-b8c4-4d81-9906-efc05b7914f6_3_1777325113499_yazq1hk.png
# Outputs to:
#   client/WisprAlt/Resources/Assets.xcassets/AppIcon.appiconset/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE_PNG="/Users/omidzahrai/.pane/images/43793091-b8c4-4d81-9906-efc05b7914f6_3_1777325113499_yazq1hk.png"
ICONSET_DIR="$REPO_ROOT/client/WisprAlt/Resources/Assets.xcassets/AppIcon.appiconset"

if [[ ! -f "$SOURCE_PNG" ]]; then
    echo "ERROR: source PNG missing: $SOURCE_PNG" >&2
    exit 1
fi

mkdir -p "$ICONSET_DIR"

# Generate all 5 sizes × 2 scales. macOS expects: 16, 32, 128, 256, 512 each at @1x and @2x.
# 512@2x = 1024px (the largest asset).
#
# Use --resampleHeightWidthMax instead of -z (zoom). The latter uses sips' default
# bicubic; the former runs through a higher-quality filter that preserves detail
# at the small (16/32) sizes critical for Finder list view rendering.
declare -a sizes=(16 32 128 256 512)
for size in "${sizes[@]}"; do
    one_x=$size
    two_x=$((size * 2))
    sips -s format png --resampleHeightWidthMax "$one_x" "$SOURCE_PNG" --out "$ICONSET_DIR/icon_${size}.png" >/dev/null
    sips -s format png --resampleHeightWidthMax "$two_x" "$SOURCE_PNG" --out "$ICONSET_DIR/icon_${size}@2x.png" >/dev/null
done

# Write Contents.json for the iconset.
cat > "$ICONSET_DIR/Contents.json" <<'EOF'
{
  "images" : [
    { "idiom" : "mac", "size" : "16x16", "scale" : "1x", "filename" : "icon_16.png" },
    { "idiom" : "mac", "size" : "16x16", "scale" : "2x", "filename" : "icon_16@2x.png" },
    { "idiom" : "mac", "size" : "32x32", "scale" : "1x", "filename" : "icon_32.png" },
    { "idiom" : "mac", "size" : "32x32", "scale" : "2x", "filename" : "icon_32@2x.png" },
    { "idiom" : "mac", "size" : "128x128", "scale" : "1x", "filename" : "icon_128.png" },
    { "idiom" : "mac", "size" : "128x128", "scale" : "2x", "filename" : "icon_128@2x.png" },
    { "idiom" : "mac", "size" : "256x256", "scale" : "1x", "filename" : "icon_256.png" },
    { "idiom" : "mac", "size" : "256x256", "scale" : "2x", "filename" : "icon_256@2x.png" },
    { "idiom" : "mac", "size" : "512x512", "scale" : "1x", "filename" : "icon_512.png" },
    { "idiom" : "mac", "size" : "512x512", "scale" : "2x", "filename" : "icon_512@2x.png" }
  ],
  "info" : { "version" : 1, "author" : "xcode" }
}
EOF

# Top-level Assets.xcassets/Contents.json (parent of AppIcon.appiconset).
mkdir -p "$REPO_ROOT/client/WisprAlt/Resources/Assets.xcassets"
cat > "$REPO_ROOT/client/WisprAlt/Resources/Assets.xcassets/Contents.json" <<'EOF'
{
  "info" : { "version" : 1, "author" : "xcode" }
}
EOF

echo "AppIcon set written to $ICONSET_DIR"
ls -la "$ICONSET_DIR"
