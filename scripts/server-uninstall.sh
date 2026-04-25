#!/usr/bin/env bash
# server-uninstall.sh — Remove the WisprAlt server from this machine.
#
# Prompts before each destructive action:
#   1. Unload the FastAPI LaunchAgent (via server-launchd.sh uninstall)
#   2. Uninstall the cloudflared tunnel service (with confirmation)
#   3. Optionally remove server/.venv/ (Python environment)
#   4. Optionally remove ~/Library/Logs/WisprAlt/ (log files)
#   5. Optionally remove ~/Library/Application Support/WisprAlt/ (DB, staging, outputs)
#
# The server/.env file and model cache (~/.cache/huggingface/) are NOT touched by
# default — HF models are large and useful to keep. Mention how to remove manually.
#
# Usage: ./scripts/server-uninstall.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

confirm() {
    local PROMPT="$1"
    local REPLY
    read -r -p "$PROMPT [y/N] " REPLY
    [[ "$REPLY" =~ ^[Yy]$ ]]
}

echo ""
echo -e "${RED}WisprAlt Server Uninstall${NC}"
echo "This will remove WisprAlt server components from your Mac."
echo ""

# ── 1. Unload + remove LaunchAgent ───────────────────────────────────────────
echo "Step 1: Unload FastAPI LaunchAgent (co.wispralt.server)"
bash "$SCRIPT_DIR/server-launchd.sh" uninstall
echo ""

# ── 2. cloudflared service ────────────────────────────────────────────────────
echo "Step 2: cloudflared tunnel service"
if command -v cloudflared >/dev/null 2>&1; then
    if launchctl list 2>/dev/null | grep -q "cloudflared"; then
        if confirm "  Uninstall cloudflared tunnel service? (stops the HTTPS tunnel)"; then
            sudo cloudflared service uninstall 2>/dev/null || {
                echo -e "  ${YELLOW}WARNING:${NC} 'cloudflared service uninstall' failed — you may need to remove it manually." >&2
                echo "  Manual removal: launchctl bootout system/com.cloudflare.cloudflared" >&2
            }
            echo "  cloudflared service uninstalled."
        else
            echo "  Skipped cloudflared service uninstall."
        fi
    else
        echo "  cloudflared service is not loaded — nothing to uninstall."
    fi
else
    echo "  cloudflared not found — skipping."
fi
echo ""

# ── 3. Python virtual environment ────────────────────────────────────────────
echo "Step 3: Python virtual environment"
VENV_PATH="$REPO_ROOT/server/.venv"
if [[ -d "$VENV_PATH" ]]; then
    VENV_SIZE="$(du -sh "$VENV_PATH" 2>/dev/null | cut -f1)"
    if confirm "  Remove server/.venv/ ($VENV_SIZE)? (Python packages; re-installable via uv sync)"; then
        rm -rf "$VENV_PATH"
        echo "  Removed $VENV_PATH"
    else
        echo "  Skipped."
    fi
else
    echo "  server/.venv not found — nothing to remove."
fi
echo ""

# ── 4. Log files ──────────────────────────────────────────────────────────────
echo "Step 4: Log files"
LOG_DIR="$HOME/Library/Logs/WisprAlt"
if [[ -d "$LOG_DIR" ]]; then
    LOG_SIZE="$(du -sh "$LOG_DIR" 2>/dev/null | cut -f1)"
    if confirm "  Remove log directory $LOG_DIR ($LOG_SIZE)?"; then
        rm -rf "$LOG_DIR"
        echo "  Removed $LOG_DIR"
    else
        echo "  Skipped."
    fi
else
    echo "  $LOG_DIR not found — nothing to remove."
fi
echo ""

# ── 5. Application Support data (DB, staging, meeting outputs) ───────────────
echo "Step 5: Application data"
APP_SUPPORT_DIR="$HOME/Library/Application Support/WisprAlt"
if [[ -d "$APP_SUPPORT_DIR" ]]; then
    APP_SIZE="$(du -sh "$APP_SUPPORT_DIR" 2>/dev/null | cut -f1)"
    echo "  This contains: jobs SQLite DB, staging WAVs, server-side meeting outputs."
    if confirm "  Remove $APP_SUPPORT_DIR ($APP_SIZE)?"; then
        rm -rf "$APP_SUPPORT_DIR"
        echo "  Removed $APP_SUPPORT_DIR"
    else
        echo "  Skipped."
    fi
else
    echo "  $APP_SUPPORT_DIR not found — nothing to remove."
fi
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo "──────────────────────────────────────────────"
echo -e "${GREEN}Server uninstall complete.${NC}"
echo ""
echo "The following were NOT removed (to avoid losing data):"
echo "  server/.env            — contains HF_TOKEN and API key; delete manually if desired"
echo "  ~/.cache/huggingface/  — HF model cache (~5.6 GB); remove with: rm -rf ~/.cache/huggingface/"
echo ""
echo "To fully remove the repo:  rm -rf $REPO_ROOT"
