#!/usr/bin/env bash
# setup-server.sh — End-to-end WisprAlt server setup orchestrator.
#
# Run once on the Mac mini after cloning the repo:
#   ./scripts/setup-server.sh
#
# Steps:
#   1. macOS version check (≥ 13.0)
#   2. Python 3.11 check
#   3. Homebrew check + install ffmpeg
#   4. uv venv + uv sync (server Python deps)
#   5. server/.env from .env.example if missing; absolute DATA_DIR
#   6. download-models.sh
#   7. First admin: python -m wispralt.bootstrap (prints the client config once)
#   8. setup-cloudflared.sh
#   9. server-launchd.sh install
#  10. doctor.sh
#  11. Point at the server URL + token printed in step 7

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/server/.env"

# ── Colours for output ────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'  # No Colour

step() { echo -e "\n${GREEN}==>${NC} $*"; }
warn() { echo -e "${YELLOW}WARNING:${NC} $*" >&2; }
die()  { echo -e "${RED}ERROR:${NC} $*" >&2; exit 1; }

# ── 0. Platform + Xcode CLT pre-flight (G7) ──────────────────────────────────
step "Checking Apple Silicon..."
if [ "$(uname -m)" != "arm64" ]; then
    die "WisprAlt server requires Apple Silicon (M-series Mac). Detected: $(uname -m)"
fi
echo "Apple Silicon detected — OK"

step "Checking Xcode Command Line Tools..."
if ! xcrun -p >/dev/null 2>&1; then
    die "Xcode Command Line Tools are required. Install them with: xcode-select --install"
fi
echo "Xcode CLT found at: $(xcrun -p) — OK"

# ── 1. macOS version ──────────────────────────────────────────────────────────
step "Checking macOS version (require ≥ 13.0)..."
OS_VERSION="$(sw_vers -productVersion)"
MAJOR="$(echo "$OS_VERSION" | cut -d. -f1)"
if [[ "$MAJOR" -lt 13 ]]; then
    die "macOS $OS_VERSION detected. WisprAlt server requires macOS 13.0 (Ventura) or later."
fi
echo "macOS $OS_VERSION — OK"

# ── 2. Python 3.11 ───────────────────────────────────────────────────────────
step "Checking Python 3.11..."
if ! command -v python3.11 >/dev/null 2>&1; then
    die "python3.11 not found.
  Install it via: brew install python@3.11
  Or: pyenv install 3.11  (if you use pyenv)
  Then re-run this script."
fi
PY_VERSION="$(python3.11 --version)"
echo "$PY_VERSION — OK"

# ── 3. Homebrew + ffmpeg ──────────────────────────────────────────────────────
step "Checking Homebrew..."
if ! command -v brew >/dev/null 2>&1; then
    die "Homebrew is not installed.
  Install it first: https://brew.sh
  Then re-run this script."
fi
echo "Homebrew found: $(brew --version | head -1)"

step "Installing ffmpeg (required by librosa audio backends)..."
if brew list ffmpeg &>/dev/null; then
    echo "ffmpeg already installed: $(ffmpeg -version 2>&1 | head -1)"
else
    brew install ffmpeg
    echo "ffmpeg installed."
fi

# ── 4. Python virtual environment + dependencies ─────────────────────────────
step "Setting up Python virtual environment..."
if ! command -v uv >/dev/null 2>&1; then
    die "uv not found. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh
  Then open a new shell and re-run this script."
fi

cd "$REPO_ROOT/server"
uv venv --python python3.11
uv sync
cd "$REPO_ROOT"
echo "Python environment ready."

# ── 5. server/.env ────────────────────────────────────────────────────────────
step "Configuring server/.env..."
if [[ ! -f "$ENV_FILE" ]]; then
    if [[ -f "$REPO_ROOT/server/.env.example" ]]; then
        cp "$REPO_ROOT/server/.env.example" "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        echo "Created $ENV_FILE from .env.example"
    else
        touch "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        echo "Created empty $ENV_FILE"
    fi
fi

# Backfill OpenRouter keys to existing .env files so re-runs of setup-server.sh on
# upgraded checkouts pick up the new optional smart-formatting envs without losing
# the current values. Each block is idempotent — only appends if the key is absent.
if ! grep -q "^OPENROUTER_API_KEY=" "$ENV_FILE" 2>/dev/null; then
    printf '\n# Optional: enable smart formatting via OpenRouter Mercury 2.\n' >> "$ENV_FILE"
    printf '# Get a key at https://openrouter.ai/keys. Costs ~$0.0001 per dictation cleanup.\n' >> "$ENV_FILE"
    printf '# Leave unset to disable smart formatting (clients toggling it on get raw text).\n' >> "$ENV_FILE"
    printf 'OPENROUTER_API_KEY=\n' >> "$ENV_FILE"
fi
if ! grep -q "^OPENROUTER_MODEL=" "$ENV_FILE" 2>/dev/null; then
    printf 'OPENROUTER_MODEL=inception/mercury-2\n' >> "$ENV_FILE"
fi
if ! grep -q "^OPENROUTER_TIMEOUT_MS=" "$ENV_FILE" 2>/dev/null; then
    printf '# 1500 ms covers cross-region OpenRouter calls + TLS handshake.\n' >> "$ENV_FILE"
    printf 'OPENROUTER_TIMEOUT_MS=1500\n' >> "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

# DATA_DIR (wispralt.db, outputs/, staging/) must be an absolute path. Write the
# default when the key is absent or empty; refuse a relative value, which would
# put the database wherever the server happens to start.
DATA_DIR_VAL="$(grep "^DATA_DIR=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true)"
if [[ -z "$DATA_DIR_VAL" ]]; then
    sed -i '' '/^DATA_DIR=/d' "$ENV_FILE"
    printf 'DATA_DIR="%s"\n' "$HOME/Library/Application Support/WisprAlt" >> "$ENV_FILE"
    echo "DATA_DIR set to $HOME/Library/Application Support/WisprAlt"
elif [[ "$DATA_DIR_VAL" != /* ]]; then
    die "DATA_DIR in $ENV_FILE must be an absolute path (got: $DATA_DIR_VAL)"
fi
chmod 600 "$ENV_FILE"

# ── 6. Download model weights ─────────────────────────────────────────────────
step "Downloading model weights..."
bash "$SCRIPT_DIR/download-models.sh"

# ── 7. First admin (the client credential handoff) ───────────────────────────
# Prints the client config (server URL + one-time admin token) to this
# terminal only; it refuses a non-TTY stdout so no log ever holds the token.
# Re-running it on a host that already has an admin is a no-op error; a lost
# key is recovered with `--rotate-admin`.
step "Creating the first admin (copy the token it prints — shown once)..."
(cd "$REPO_ROOT/server" && .venv/bin/python -m wispralt.bootstrap --label owner) \
    || warn "Bootstrap did not create an admin (one may already exist). Lost key? cd server && .venv/bin/python -m wispralt.bootstrap --rotate-admin"

# ── 8. Set up Cloudflare Tunnel ───────────────────────────────────────────────
step "Setting up Cloudflare Tunnel..."
bash "$SCRIPT_DIR/setup-cloudflared.sh"

# ── 9. Register FastAPI LaunchAgent ──────────────────────────────────────────
step "Registering FastAPI server as a LaunchAgent..."
bash "$SCRIPT_DIR/server-launchd.sh" install

# ── 10. Run doctor ────────────────────────────────────────────────────────────
step "Running system health check (doctor.sh)..."
bash "$SCRIPT_DIR/doctor.sh"

# ── 11. Client configuration ─────────────────────────────────────────────────
step "Setup complete!"
SERVER_URL_VAL="$(grep "^SERVER_URL=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
echo ""
echo "Configure the macOS client with the server URL and the token printed in step 7:"
echo "  WisprAlt menubar → Settings → Server URL  ($SERVER_URL_VAL)"
echo "  WisprAlt menubar → Settings → API Key     (the token from step 7)"
echo "  Click 'Test Connection' — expect green ✓"
echo ""
echo "The token is never stored in plaintext. Lost it? Issue a new one:"
echo "  cd server && .venv/bin/python -m wispralt.bootstrap --rotate-admin"
