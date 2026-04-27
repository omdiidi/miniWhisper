#!/usr/bin/env bash
# download-models.sh — Download all WisprAlt model weights from Hugging Face.
#
# Pre-flight checks:
#   - ≥8 GB free disk space on $HOME
#   - Valid HF_TOKEN (3-retry with 5s backoff; distinguishes 401 from 429/network)
#   - Gated-model terms accepted for pyannote repos (probes with *.yaml download)
#
# Post-download: reports total cache size.
#
# Usage: ./scripts/download-models.sh
#   HF_TOKEN may also be set in the environment before calling this script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/server/.env"

# ── Source .env for HF_TOKEN if not already in environment ──────────────────
if [[ -z "${HF_TOKEN:-}" && -f "$ENV_FILE" ]]; then
    # Only export HF_TOKEN — do not re-export everything
    HF_TOKEN_LINE="$(grep "^HF_TOKEN=" "$ENV_FILE" 2>/dev/null || true)"
    if [[ -n "$HF_TOKEN_LINE" ]]; then
        HF_TOKEN="${HF_TOKEN_LINE#HF_TOKEN=}"
        export HF_TOKEN
    fi
fi

if [[ -z "${HF_TOKEN:-}" ]]; then
    echo "ERROR: HF_TOKEN is not set." >&2
    echo "  Set it in server/.env or export HF_TOKEN=<token> before running." >&2
    exit 1
fi

# ── Disk space pre-flight ─────────────────────────────────────────────────────
# df -k reports in 1KB blocks; 8 GB = 8 * 1024 * 1024 = 8388608 blocks
REQUIRED_KB=8388608
AVAIL_KB="$(df -k "$HOME" | awk 'NR==2 {print $4}')"
if [[ "$AVAIL_KB" -lt "$REQUIRED_KB" ]]; then
    AVAIL_GB=$(( AVAIL_KB / 1048576 ))
    echo "ERROR: Insufficient disk space on $HOME." >&2
    echo "  Available: ${AVAIL_GB} GB — need at least 8 GB free." >&2
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

# ── Authenticate — 3 retries with 5s backoff ─────────────────────────────────
echo "Checking Hugging Face authentication..."
AUTH_OK=false
for attempt in 1 2 3; do
    HF_WHOAMI_OUTPUT="$(HUGGING_FACE_HUB_TOKEN="$HF_TOKEN" "$HF_CLI" auth whoami 2>&1)" && {
        AUTH_OK=true
        echo "Authenticated as: $HF_WHOAMI_OUTPUT"
        break
    }
    EXIT_CODE=$?
    # Distinguish 401 (bad token) from transient network/rate-limit errors
    if echo "$HF_WHOAMI_OUTPUT" | grep -qi "401\|invalid.*token\|not.*authenticated\|credentials"; then
        echo "ERROR: HF_TOKEN is invalid or expired (HTTP 401)." >&2
        echo "  Get a valid token at: https://huggingface.co/settings/tokens" >&2
        echo "  Update HF_TOKEN in server/.env and retry." >&2
        exit 2
    fi
    if [[ "$attempt" -lt 3 ]]; then
        echo "  Auth attempt $attempt failed (network/rate-limit). Retrying in 5s..." >&2
        sleep 5
    fi
done

if [[ "$AUTH_OK" != "true" ]]; then
    echo "ERROR: Could not authenticate with Hugging Face after 3 attempts." >&2
    echo "  Check your internet connection and try again." >&2
    echo "  If rate-limited (HTTP 429), wait a few minutes and retry." >&2
    exit 1
fi

# ── Probe gated models (accept-terms check) ───────────────────────────────────
# Download a tiny *.yaml probe — if the repo is gated and terms not accepted, HF
# returns 401 with a terms URL. We catch that and print the exact accept URL.

probe_gated_repo() {
    local REPO="$1"
    echo "Probing gated repo: $REPO"
    PROBE_OUTPUT="$(HUGGING_FACE_HUB_TOKEN="$HF_TOKEN" "$HF_CLI" download \
        "$REPO" \
        --include "*.yaml" \
        2>&1)" && return 0
    # If we get here the download failed
    if echo "$PROBE_OUTPUT" | grep -qi "401\|gated\|terms\|accept"; then
        echo "ERROR: Access denied to $REPO — model is gated." >&2
        echo "  You must accept the usage terms at:" >&2
        echo "    https://huggingface.co/$REPO" >&2
        echo "  Log in with your browser, click 'Agree and access repository', then retry." >&2
        exit 3
    fi
    echo "WARNING: Probe of $REPO returned non-zero but not a known 401 pattern." >&2
    echo "  Probe output: $PROBE_OUTPUT" >&2
    echo "  Continuing — the full download may still succeed." >&2
}

probe_gated_repo "pyannote/speaker-diarization-3.1"
probe_gated_repo "pyannote/segmentation-3.0"
echo "Gated-model access confirmed."

# ── Download helper with --resume-download ───────────────────────────────────
hf_download() {
    local REPO="$1"
    HUGGING_FACE_HUB_TOKEN="$HF_TOKEN" "$HF_CLI" download "$REPO"
}

# ── Download each model ───────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo " Downloading WisprAlt model weights (~5.6 GB total)"
echo "============================================================"
echo ""

echo "--- Parakeet TDT 0.6B v2 (~1.2 GB) ---"
hf_download "mlx-community/parakeet-tdt-0.6b-v2"
echo "Parakeet: done"
echo ""

echo "--- faster_CrisperWhisper (~3.1 GB) ---"
hf_download "nyrahealth/faster_CrisperWhisper"
echo "faster_CrisperWhisper: done"
echo ""

# wav2vec2 alignment model is loaded by whisperx at runtime (not a standalone HF repo download).
echo "--- wav2vec2-base-960h alignment model (~360 MB) ---"
echo "  This model is fetched by whisperx on first use."
echo "  Triggering download now via Python..."
# Run inside the server venv if present; fall back to system python3
PYTHON_BIN="python3"
if [[ -f "$REPO_ROOT/server/.venv/bin/python" ]]; then
    PYTHON_BIN="$REPO_ROOT/server/.venv/bin/python"
fi
HUGGING_FACE_HUB_TOKEN="$HF_TOKEN" "$PYTHON_BIN" -c \
    "import whisperx; whisperx.load_align_model(language_code='en', device='cpu')" \
    2>&1 || {
    echo "  WARNING: wav2vec2 align download failed. It will be retried on first meeting job." >&2
    echo "  This is non-fatal — proceeding." >&2
}
echo "wav2vec2 alignment model: done (or deferred)"
echo ""

echo "--- Pyannote speaker-diarization-3.1 (~800 MB combined with segmentation-3.0) ---"
# These were already probed above; re-download with --resume-download ensures full fetch.
hf_download "pyannote/speaker-diarization-3.1"
hf_download "pyannote/segmentation-3.0"
echo "Pyannote diarization + segmentation: done"
echo ""

echo "--- DeepFilterNet 3 — SKIPPED ---"
echo "  DeepFilterNet was removed (numpy<2.0 conflicts with parakeet-mlx)."
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
