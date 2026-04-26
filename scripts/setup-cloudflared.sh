#!/usr/bin/env bash
# setup-cloudflared.sh — Install cloudflared and configure it as a user-level
# LaunchAgent, persisting SERVER_URL to server/.env.
#
# Security contract:
#   - The Cloudflare Tunnel token is read from stdin (never echoed to terminal).
#   - The token is written only to ~/.config/wispralt/cloudflare-token (mode 0600).
#   - Modern cloudflared (>= 2025.4.0) reads the token via --token-file at launch;
#     the token is never inlined into the plist.
#   - Legacy cloudflared (< 2025.4.0) inlines the token in a mode-0600 plist as a
#     fallback. Run this script again after upgrading cloudflared to migrate.
#   - Token vars are scrubbed on ANY exit path via trap (including set -e aborts).
#   - The broken `sudo cloudflared service install` daemon path is NOT used;
#     it fails silently on macOS 14/15 after reboots.
#
# Usage: ./scripts/setup-cloudflared.sh
#   Must be run from the repo root (or any directory — script is location-aware).

set -euo pipefail

# Defense-in-depth: ensure token vars are scrubbed on ANY exit path,
# including unexpected set -e aborts between read and unset.
trap 'unset CF_TOKEN TOKEN_VALUE NEW_TOKEN' EXIT

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

# ── 3. Tear down any stale system LaunchDaemon from the broken sudo-install path ─
# The `sudo cloudflared service install` daemon fails silently on macOS 14/15 after
# reboots. Remove it if present so it doesn't conflict with the user-level agent.
echo ""
echo "Removing legacy system LaunchDaemon (sudo cloudflared service install path) if present..."
sudo launchctl bootout system/com.cloudflare.cloudflared 2>/dev/null || true
sudo launchctl bootout system/cloudflared 2>/dev/null || true
sudo rm -f /Library/LaunchDaemons/com.cloudflare.cloudflared.plist
sudo rm -f /Library/LaunchDaemons/cloudflared.plist
echo "Legacy daemon cleanup done (errors above are harmless if it was never installed)."

# ── 4. Prompt for Cloudflare Tunnel token (silent — persisted to 0600 file) ──
echo ""
echo "Enter your Cloudflare Tunnel token."
echo "  Find it in the Cloudflare Zero Trust dashboard:"
echo "    Networks -> Tunnels -> <your tunnel> -> Configure -> Install connector"
echo "  The token will be stored at ~/.config/wispralt/cloudflare-token (mode 0600)."
echo ""

read -r -s -p "Cloudflare Tunnel token: " CF_TOKEN
echo  # newline after silent read

if [[ -z "$CF_TOKEN" ]]; then
    echo "ERROR: No token entered. Aborting." >&2
    exit 1
fi

# Persist token to a 0600 file outside the repo
TOKEN_DIR="$HOME/.config/wispralt"
TOKEN_FILE="$TOKEN_DIR/cloudflare-token"
mkdir -p "$TOKEN_DIR"
chmod 700 "$TOKEN_DIR"
install -m 0600 /dev/null "$TOKEN_FILE"
printf '%s' "$CF_TOKEN" > "$TOKEN_FILE"
unset CF_TOKEN
echo "Token stored at $TOKEN_FILE (mode 0600)."

# ── 5. Prepare LaunchAgent paths ──────────────────────────────────────────────
LABEL="co.wispralt.cloudflared"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/WisprAlt"
mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

# Detect cloudflared binary path
CLOUDFLARED_BIN="$(command -v cloudflared)"
if [[ -z "$CLOUDFLARED_BIN" ]]; then
    echo "ERROR: cloudflared binary not found after install step." >&2
    exit 1
fi

# Build PATH for the LaunchAgent (launchd inherits a minimal env with no Homebrew)
HOMEBREW_PREFIX="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
LAUNCHD_PATH="$HOMEBREW_PREFIX/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# ── 6. Feature-probe --token-file support ────────────────────────────────────
# Codex review caught a regression here: under `set -euo pipefail`, if cloudflared
# exits non-zero on `--help` (some 2024.x versions do), the pipe inherits that exit
# code BEFORE grep gets to act and the whole script dies. The previous "grep is
# last in pipe" workaround was brittle because pipefail propagates ANY non-zero in
# the chain when set. The fix: capture help output in a command-substitution
# (which DOES NOT trigger pipefail because it's not a pipe), then grep against
# the captured string.
HELP_OUTPUT="$(cloudflared tunnel run --help 2>&1 || true)"
if printf '%s\n' "$HELP_OUTPUT" | grep -q -- '--token-file'; then
    SUPPORTS_TOKEN_FILE=true
else
    SUPPORTS_TOKEN_FILE=false
fi
unset HELP_OUTPUT
echo "cloudflared --token-file support: $SUPPORTS_TOKEN_FILE"

# ── 7. Tear down any stale user-level plist before regenerating ───────────────
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true

# ── 8. Generate the LaunchAgent plist (two separate heredocs, no XML splicing) ─
if [ "$SUPPORTS_TOKEN_FILE" = "true" ]; then
    # Modern path: token stays in the 0600 file; plist references it by path.
    # The plist itself contains no secret and can be mode 0644.
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>            <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${CLOUDFLARED_BIN}</string>
        <string>tunnel</string>
        <string>--loglevel</string>
        <string>info</string>
        <string>run</string>
        <string>--token-file</string>
        <string>${TOKEN_FILE}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>         <string>${LAUNCHD_PATH}</string>
    </dict>
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>   <false/>
        <key>NetworkState</key>     <true/>
    </dict>
    <key>ThrottleInterval</key> <integer>10</integer>
    <key>StandardOutPath</key>  <string>${LOG_DIR}/cloudflared.log</string>
    <key>StandardErrorPath</key><string>${LOG_DIR}/cloudflared.err.log</string>
</dict>
</plist>
EOF
else
    # Legacy path: cloudflared < 2025.4.0 does not support --token-file.
    # Inline the token into ProgramArguments. Plist gets mode 0600 to limit exposure.
    # Re-run this script after upgrading cloudflared to migrate to the --token-file path.
    TOKEN_VALUE="$(cat "$TOKEN_FILE")"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>            <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${CLOUDFLARED_BIN}</string>
        <string>tunnel</string>
        <string>--loglevel</string>
        <string>info</string>
        <string>run</string>
        <string>--token</string>
        <string>${TOKEN_VALUE}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>         <string>${LAUNCHD_PATH}</string>
    </dict>
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>   <false/>
        <key>NetworkState</key>     <true/>
    </dict>
    <key>ThrottleInterval</key> <integer>10</integer>
    <key>StandardOutPath</key>  <string>${LOG_DIR}/cloudflared.log</string>
    <key>StandardErrorPath</key><string>${LOG_DIR}/cloudflared.err.log</string>
</dict>
</plist>
EOF
    unset TOKEN_VALUE
fi
chmod 0600 "$PLIST"

# ── 9. Bootstrap the LaunchAgent ──────────────────────────────────────────────
echo "Bootstrapping LaunchAgent $LABEL..."
launchctl bootstrap "gui/$UID" "$PLIST"

# ── 10. Verify with retry (cloudflared takes a beat to dial out) ─────────────
echo "Verifying LaunchAgent loaded..."
LOADED=0
for i in $(seq 1 10); do
    if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
        LOADED=1
        break
    fi
    sleep 1
done

if [ "$LOADED" -eq 1 ]; then
    echo "cloudflared LaunchAgent loaded successfully."
else
    echo "ERROR: cloudflared LaunchAgent failed to load after 10s." >&2
    echo "  Check: $LOG_DIR/cloudflared.err.log" >&2
    exit 1
fi

# ── 11. Verify tunnel connectivity ────────────────────────────────────────────
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
        echo "  Re-check connectivity after the server is started."
        ;;
    502)
        echo "INFO: Got HTTP 502 — tunnel is routing to this machine, but FastAPI is not"
        echo "  running yet. Run: bash scripts/server-launchd.sh install"
        ;;
    *)
        echo "WARNING: Unexpected HTTP $HTTP_CODE from $SERVER_URL/healthz." >&2
        echo "  Check Cloudflare dashboard -> Tunnels for connector health." >&2
        ;;
esac

echo ""
echo "Cloudflared setup complete."
echo "  LaunchAgent: $PLIST"
echo "  Token file:  $TOKEN_FILE (mode 0600)"
echo "  Logs:        $LOG_DIR/cloudflared.{log,err.log}"
echo ""
echo "Token rotation: see docs/DEPLOYMENT-NOTES.md for the canonical rotation procedure."
echo "Next step: bash scripts/server-launchd.sh install   (registers FastAPI as a LaunchAgent)"
