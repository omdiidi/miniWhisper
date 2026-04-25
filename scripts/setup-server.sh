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
#   5. server/.env from .env.example if missing; prompt for HF_TOKEN
#   6. download-models.sh
#   7. generate-api-key.sh
#   8. setup-cloudflared.sh
#   9. server-launchd.sh install
#  10. doctor.sh
#  11. Print client config one-liner (SERVER_URL + API_KEY)

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

# Prompt for HF_TOKEN if not set
HF_TOKEN_CURRENT="$(grep "^HF_TOKEN=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
if [[ -z "$HF_TOKEN_CURRENT" ]]; then
    echo ""
    echo "Your Hugging Face token is needed to download gated models (Pyannote)."
    echo "Get one at: https://huggingface.co/settings/tokens  (read access is sufficient)"
    echo ""
    read -r -s -p "HF_TOKEN: " HF_TOKEN_INPUT
    echo ""
    if [[ -z "$HF_TOKEN_INPUT" ]]; then
        die "HF_TOKEN cannot be empty. Re-run the script and enter your token."
    fi
    # Write into .env
    if grep -q "^HF_TOKEN=" "$ENV_FILE" 2>/dev/null; then
        TMP_ENV="$(dirname "$ENV_FILE")/.env.tmp.$$"
        sed "s@^HF_TOKEN=.*@HF_TOKEN=$HF_TOKEN_INPUT@" "$ENV_FILE" > "$TMP_ENV"
        mv "$TMP_ENV" "$ENV_FILE"
    else
        printf '\nHF_TOKEN=%s\n' "$HF_TOKEN_INPUT" >> "$ENV_FILE"
    fi
    unset HF_TOKEN_INPUT
    chmod 600 "$ENV_FILE"
    echo "HF_TOKEN saved to $ENV_FILE"
else
    echo "HF_TOKEN already set in $ENV_FILE"
fi

# ── 6. Download model weights ─────────────────────────────────────────────────
step "Downloading model weights..."
bash "$SCRIPT_DIR/download-models.sh"

# ── 7. Generate API key ───────────────────────────────────────────────────────
step "Generating API key..."
bash "$SCRIPT_DIR/generate-api-key.sh"

# ── 8. Set up Cloudflare Tunnel ───────────────────────────────────────────────
step "Setting up Cloudflare Tunnel..."
bash "$SCRIPT_DIR/setup-cloudflared.sh"

# ── 9. Register FastAPI LaunchAgent ──────────────────────────────────────────
step "Registering FastAPI server as a LaunchAgent..."
bash "$SCRIPT_DIR/server-launchd.sh" install

# ── 10. Run doctor ────────────────────────────────────────────────────────────
step "Running system health check (doctor.sh)..."
bash "$SCRIPT_DIR/doctor.sh"

# ── 11. Print client configuration ───────────────────────────────────────────
step "Setup complete! Client configuration:"
SERVER_URL_VAL="$(grep "^SERVER_URL=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
API_KEY_VAL="$(grep "^WISPRALT_API_KEY=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║             WisprAlt Client Configuration                   ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  SERVER_URL   $SERVER_URL_VAL"
echo "  API_KEY      $API_KEY_VAL"
echo ""
echo "Paste these into the macOS client:"
echo "  WisprAlt menubar → Settings → Server URL  (paste SERVER_URL)"
echo "  WisprAlt menubar → Settings → API Key     (paste API_KEY)"
echo "  Click 'Test Connection' — expect green ✓"
echo ""
echo "To re-print this config at any time:  grep -E 'SERVER_URL|WISPRALT_API_KEY' server/.env"
