#!/usr/bin/env bash
# uninstall-client.sh — Remove WisprAlt client app and all associated data (v3 P5#7).
#
# Run on the client Mac (not the server Mac mini).
#
# Removes:
#   1. Running WisprAlt process (graceful quit via osascript)
#   2. ~/Documents/WisprAlt/  (local meeting transcripts — with confirmation)
#   3. UserDefaults domain: co.wispralt.WisprAlt
#   4. Keychain item: service co.wispralt
#   5. /Applications/WisprAlt.app  (moved to Trash via Finder)
#
# Usage: ./scripts/uninstall-client.sh

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# ── Top-level confirmation ────────────────────────────────────────────────────
echo ""
echo -e "${RED}WisprAlt Client Uninstall${NC}"
echo "This will remove WisprAlt and all associated data from this Mac."
echo ""
read -r -p "Remove WisprAlt and all data? [y/N] " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi
echo ""

ERRORS=0

# ── 1. Quit the running app ───────────────────────────────────────────────────
echo "Step 1: Quitting WisprAlt (if running)..."
osascript -e 'quit app "WisprAlt"' 2>/dev/null && {
    echo "  WisprAlt quit."
    # Give the app a moment to finish any in-flight activity
    sleep 2
} || {
    echo "  WisprAlt was not running (or could not be quit via AppleScript — continuing)."
}

# ── 2. Remove local meeting transcripts ──────────────────────────────────────
echo ""
echo "Step 2: Local meeting transcripts"
WISPRALT_DOCS_DIR="$HOME/Documents/WisprAlt"
if [[ -d "$WISPRALT_DOCS_DIR" ]]; then
    DIR_SIZE="$(du -sh "$WISPRALT_DOCS_DIR" 2>/dev/null | cut -f1)"
    echo "  Found: $WISPRALT_DOCS_DIR ($DIR_SIZE)"
    echo "  This contains all your saved meeting transcripts (JSON, SRT, VTT, TXT)."
    read -r -p "  Remove ~/Documents/WisprAlt/ permanently? [y/N] " REMOVE_DOCS
    if [[ "$REMOVE_DOCS" =~ ^[Yy]$ ]]; then
        rm -rf "$WISPRALT_DOCS_DIR"
        echo "  Removed $WISPRALT_DOCS_DIR"
    else
        echo "  Kept $WISPRALT_DOCS_DIR"
    fi
else
    echo "  $WISPRALT_DOCS_DIR not found — nothing to remove."
fi

# ── 3. Delete UserDefaults ────────────────────────────────────────────────────
echo ""
echo "Step 3: Removing UserDefaults (co.wispralt.WisprAlt)..."
defaults delete co.wispralt.WisprAlt 2>/dev/null && {
    echo "  UserDefaults domain deleted."
} || {
    echo "  UserDefaults domain not found (already removed or app was never launched)."
}

# ── 4. Delete Keychain item ───────────────────────────────────────────────────
echo ""
echo "Step 4: Removing Keychain item (service: co.wispralt)..."
security delete-generic-password -s co.wispralt 2>/dev/null && {
    echo "  Keychain item deleted (API key removed)."
} || {
    echo "  Keychain item not found (already removed or key was never stored)."
}

# ── 5. Remove Sparkle caches and application support data (G10) ──────────────
echo ""
echo "Step 5: Removing Sparkle caches and application support data..."

_remove_if_exists() {
    local path="$1"
    if [[ -e "$path" ]]; then
        rm -rf "$path" && echo "  Removed: $path" || {
            echo "  WARNING: Could not remove $path" >&2
            ERRORS=$(( ERRORS + 1 ))
        }
    else
        echo "  Not found (already removed): $path"
    fi
}

_remove_if_exists "$HOME/Library/Caches/Sparkle"
_remove_if_exists "$HOME/Library/Caches/co.wispralt.WisprAlt"
_remove_if_exists "$HOME/Library/Application Support/co.wispralt"
_remove_if_exists "$HOME/Library/Application Support/co.wispralt.WisprAlt"
_remove_if_exists "$HOME/Library/Saved Application State/co.wispralt.WisprAlt.savedState"

# ── 6. Move app to Trash ──────────────────────────────────────────────────────
echo ""
echo "Step 6: Moving /Applications/WisprAlt.app to Trash..."
if [[ -d "/Applications/WisprAlt.app" ]]; then
    osascript \
        -e 'tell application "Finder" to delete POSIX file "/Applications/WisprAlt.app"' \
        2>/dev/null && {
        echo "  WisprAlt.app moved to Trash."
    } || {
        echo -e "  ${YELLOW}WARNING:${NC} Could not move app to Trash via Finder." >&2
        echo "  You can drag it to Trash manually, or run:" >&2
        echo "    rm -rf /Applications/WisprAlt.app" >&2
        ERRORS=$(( ERRORS + 1 ))
    }
else
    echo "  /Applications/WisprAlt.app not found — already removed."
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "──────────────────────────────────────────────"
if [[ "$ERRORS" -eq 0 ]]; then
    echo -e "${GREEN}Client uninstall complete.${NC}"
else
    echo -e "${YELLOW}Client uninstall finished with $ERRORS warning(s).${NC}"
    echo "Review the messages above for any items that require manual attention."
fi
echo ""
echo "Note: This script only removed the client-side components."
echo "The server (Mac mini) is unaffected. To remove the server, run:"
echo "  ./scripts/server-uninstall.sh  (on the Mac mini)"
