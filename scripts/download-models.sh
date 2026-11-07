#!/usr/bin/env bash
# download-models.sh — Download WisprAlt model weights from Hugging Face.
#
# Models (both public, no token needed):
#   - mlx-community/parakeet-tdt-0.6b-v2      dictation (resident at startup)
#   - mlx-community/whisper-large-v3-turbo    file transcription (loaded lazily)
#
# Pre-flight: ≥4 GB free disk space on $HOME.
# Post-download: reports total cache size.
#
# Usage: ./scripts/download-models.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Disk space pre-flight ─────────────────────────────────────────────────────
# df -k reports in 1KB blocks; 4 GB = 4 * 1024 * 1024 = 4194304 blocks
REQUIRED_KB=4194304
AVAIL_KB="$(df -k "$HOME" | awk 'NR==2 {print $4}')"
if [[ "$AVAIL_KB" -lt "$REQUIRED_KB" ]]; then
    AVAIL_GB=$(( AVAIL_KB / 1048576 ))
    echo "ERROR: Insufficient disk space on $HOME." >&2
    echo "  Available: ${AVAIL_GB} GB — need at least 4 GB free." >&2
    echo "  Free up space and retry (largest consumers: ~/Library/Caches, ~/Downloads)." >&2
    exit 1
fi
echo "Disk pre-flight: OK ($(( AVAIL_KB / 1048576 )) GB free)"

# ── Locate `hf` CLI (prefer server venv) ──────────────────────────────────────
HF_CLI=""
if [[ -x "$REPO_ROOT/server/.venv/bin/hf" ]]; then
    HF_CLI="$REPO_ROOT/server/.venv/bin/hf"
elif command -v hf >/dev/null 2>&1; then
    HF_CLI="$(command -v hf)"
else
    echo "ERROR: 'hf' CLI not found." >&2
    echo "  Run 'cd server && uv sync' first to install it into the venv." >&2
    exit 1
fi
echo "Using HF CLI: $HF_CLI"

# ── Download helper: 3 attempts with 5s backoff (network / rate-limit) ───────
hf_download() {
    local REPO="$1"
    for attempt in 1 2 3; do
        "$HF_CLI" download "$REPO" && return 0
        if [[ "$attempt" -lt 3 ]]; then
            echo "  Download of $REPO failed (attempt $attempt). Retrying in 5s..." >&2
            sleep 5
        fi
    done
    echo "ERROR: Could not download $REPO after 3 attempts." >&2
    echo "  Check your internet connection; if rate-limited (HTTP 429), wait and retry." >&2
    exit 1
}

# ── Download each model ───────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo " Downloading WisprAlt model weights (~2.7 GB total)"
echo "============================================================"
echo ""

echo "--- Parakeet TDT 0.6B v2 (~1.2 GB) ---"
hf_download "mlx-community/parakeet-tdt-0.6b-v2"
echo "Parakeet: done"
echo ""

echo "--- mlx-community/whisper-large-v3-turbo (~1.5 GB) ---"
hf_download "mlx-community/whisper-large-v3-turbo"
echo "mlx-whisper turbo: done"
echo ""

# ── Post-download size report ─────────────────────────────────────────────────
HF_CACHE="${HF_HOME:-$HOME/.cache/huggingface}/hub"
echo "============================================================"
echo " Post-download summary"
echo "============================================================"
if [[ -d "$HF_CACHE" ]]; then
    CACHE_SIZE="$(du -sh "$HF_CACHE" 2>/dev/null | cut -f1)"
    echo "Total Hugging Face cache size ($HF_CACHE): $CACHE_SIZE"
else
    echo "HF cache directory not found at $HF_CACHE (models may be in a custom HF_HOME)."
fi

AVAIL_AFTER="$(df -k "$HOME" | awk 'NR==2 {print $4}')"
echo "Remaining disk space on \$HOME: $(( AVAIL_AFTER / 1048576 )) GB"
echo ""
echo "All model downloads complete. Run doctor.sh to verify the server is ready."
