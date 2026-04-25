#!/usr/bin/env bash
# setup-cloudflared.sh — Install cloudflared, configure the tunnel, and persist
# SERVER_URL to server/.env.
#
# Security contract:
#   - The Cloudflare Tunnel token is read from stdin (never echoed).
#   - The token is passed directly to `sudo cloudflared service install` and
#     immediately unset — it is NOT written to .env, any file, or LaunchAgent plist.
#   - cloudflared stores its credential in the macOS system keychain automatically.
#
# Usage: ./scripts/setup-cloudflared.sh
#   Must be run from the repo root (or any directory — script is location-aware).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/server/.env"

# ── Helper: write/replace a key=value line in server/.env ────────────────────
persist_env_var() {
    local KEY="$1"
    local VALUE="$2"
    if [[ ! -f "$ENV_FILE" ]]; then
        if [[ -f "$REPO_ROOT/server/.env.example" ]]; then
            cp "$REPO_ROOT/server/.env.example" "$ENV_FILE"
        else
            touch "$ENV_FILE"
        fi
    fi
    if grep -q "^${KEY}=" "$ENV_FILE" 2>/dev/null; then
        TMP_ENV="$(dirname "$ENV_FILE")/.env.tmp.$$"
        sed "s@^${KEY}=.*@${KEY}=${VALUE}@" "$ENV_FILE" > "$TMP_ENV"
        mv "$TMP_ENV" "$ENV_FILE"
    else
        printf '\n%s=%s\n' "$KEY" "$VALUE" >> "$ENV_FILE"
    fi
    chmod 600 "$ENV_FILE"
}

# ── 1. Install cloudflared ────────────────────────────────────────────────────
if command -v cloudflared >/dev/null 2>&1; then
    EXISTING_VERSION="$(cloudflared version 2>&1 | head -1)"
    echo "cloudflared already installed: $EXISTING_VERSION"
else
    if ! command -v brew >/dev/null 2>&1; then
        echo "ERROR: Homebrew is not installed." >&2
        echo "  Install Homebrew first: https://brew.sh" >&2
        echo "  Then re-run this script." >&2
        exit 1
    fi
    echo "Installing cloudflared via Homebrew..."
    brew install cloudflared
    echo "cloudflared installed: $(cloudflared version 2>&1 | head -1)"
fi

# ── 2. Prompt for SERVER_URL ──────────────────────────────────────────────────
echo ""
echo "Enter your full HTTPS tunnel URL for this server."
echo "  Example: https://transcribe.example.com"
echo "  The subdomain must be configured as a Cloudflare Tunnel route pointing to"
echo "  http://127.0.0.1:8000 on this machine."
echo ""

SERVER_URL=""
while true; do
    read -r -p "Server URL (https://...): " SERVER_URL
    if [[ "$SERVER_URL" == https://* ]]; then
        break
    fi
    echo "  ERROR: URL must start with 'https://'. Please try again." >&2
done

# Persist to .env
persist_env_var "SERVER_URL" "$SERVER_URL"
echo "SERVER_URL=$SERVER_URL written to $ENV_FILE"

# ── 3. Prompt for Cloudflare Tunnel token (silent — never stored) ─────────────
echo ""
echo "Enter your Cloudflare Tunnel token."
echo "  Find it in the Cloudflare Zero Trust dashboard:"
echo "    Networks → Tunnels → <your tunnel> → Configure → Install connector"
echo "  The token will NOT be saved to any file on disk."
echo ""

# Read silently; token is held only in this local variable
read -r -s -p "Cloudflare Tunnel token: " CF_TOKEN
echo ""  # newline after silent read

if [[ -z "$CF_TOKEN" ]]; then
    echo "ERROR: No token entered. Aborting." >&2
    exit 1
fi

# ── 4. Install the tunnel as a system service ─────────────────────────────────
echo "Installing cloudflared as a system service (requires sudo)..."
sudo cloudflared service install "$CF_TOKEN"

# Discard token immediately from shell memory
unset CF_TOKEN
echo "Tunnel token discarded from shell — cloudflared has stored it in the system keychain."

# ── 5. Verify tunnel service status ──────────────────────────────────────────
echo ""
echo "Checking cloudflared service status..."
# `cloudflared service status` is the preferred command but may not exist on all
# versions; fall back to launchctl as a secondary check.
if cloudflared service status 2>/dev/null; then
    echo "cloudflared service: running"
elif launchctl list | grep -q "cloudflared" 2>/dev/null; then
    echo "cloudflared appears in launchctl list — service is loaded"
else
    echo "WARNING: Could not confirm cloudflared service status." >&2
    echo "  Check manually: launchctl list | grep cloudflared" >&2
    echo "  If it is not listed, re-run: sudo cloudflared service install <token>" >&2
fi

# ── 6. Verify tunnel connectivity ────────────────────────────────────────────
echo ""
echo "Verifying tunnel connectivity at $SERVER_URL/healthz ..."
echo "  NOTE: The WisprAlt FastAPI server must be running for a full check."
echo "  Expected responses:"
echo "    401 — tunnel is up, bearer auth required (success signal)"
echo "    000 — tunnel not yet propagated (DNS/CF edge needs 1-2 minutes)"
echo "    502 — tunnel is up but FastAPI is not running yet"
echo ""

HTTP_CODE="$(curl -fsS -o /dev/null -w "%{http_code}" \
    --connect-timeout 10 --max-time 20 \
    "$SERVER_URL/healthz" 2>/dev/null || echo "000")"

case "$HTTP_CODE" in
    401)
        echo "PASS: Got HTTP 401 — tunnel is routing correctly."
        echo "  Start the FastAPI server and a bearer-auth request will return 200/ready."
        ;;
    200)
        echo "PASS: Got HTTP 200 — tunnel is up and server is already running."
        ;;
    000)
        echo "INFO: No HTTP response (code 000)."
        echo "  The tunnel may not have propagated yet (Cloudflare DNS can take 1-2 minutes)."
        echo "  Re-run doctor.sh after the server is started to recheck connectivity."
        ;;
    502)
        echo "INFO: Got HTTP 502 — tunnel is routing to this machine, but FastAPI is not"
        echo "  running yet. Run: launchctl bootstrap gui/\$UID ~/Library/LaunchAgents/co.wispralt.server.plist"
        ;;
    *)
        echo "WARNING: Unexpected HTTP $HTTP_CODE from $SERVER_URL/healthz." >&2
        echo "  Check Cloudflare dashboard → Tunnels for connector health." >&2
        ;;
esac

echo ""
echo "Cloudflared setup complete."
echo "Next step: server-launchd.sh install   (registers FastAPI as a LaunchAgent)"
