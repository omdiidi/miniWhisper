#!/usr/bin/env bash
# doctor.sh — WisprAlt server health and configuration checks.
#
# Checks performed:
#   1. server/.env exists, mode is 0600, owner is $USER
#   2. cloudflared service status (best-effort)
#   3. Disk free on $HOME (warn if < 4 GB)
#   4. STAGING_DIR and MEETING_OUTPUT_DIR on same filesystem
#   5. Poll /healthz until 200 or 401 (max 60s)
#   6. Poll /readyz/dictation until 200 (max 60s × 5s intervals)
#   7. Poll /readyz/meeting until 200 (max 180s × 5s intervals — heavy models)
#   8. Generate test WAV via Python + POST to /transcribe/dictate
#   9. GET /metrics and pretty-print JSON
#
# Exit code: 0 if all checks pass, 1 if any required check fails.
#
# Usage: ./scripts/doctor.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/server/.env"

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

pass()  { echo -e "  ${GREEN}[PASS]${NC} $*"; }
fail()  { echo -e "  ${RED}[FAIL]${NC} $*"; FAILURES=$(( FAILURES + 1 )); }
warn()  { echo -e "  ${YELLOW}[WARN]${NC} $*"; }
info()  { echo -e "        $*"; }

FAILURES=0

# ── Source server/.env ────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
    echo -e "${RED}ERROR:${NC} $ENV_FILE not found." >&2
    echo "  Run setup-server.sh first." >&2
    exit 1
fi

# Source only the variables we need — do not blindly eval the whole file
_read_env_var() {
    grep "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-
}

SERVER_URL="$(_read_env_var SERVER_URL)"
WISPRALT_API_KEY="$(_read_env_var WISPRALT_API_KEY)"
STAGING_DIR="$(_read_env_var STAGING_DIR)"
MEETING_OUTPUT_DIR="$(_read_env_var MEETING_OUTPUT_DIR)"

echo ""
echo -e "${BOLD}WisprAlt Doctor${NC} — $(date)"
echo "──────────────────────────────────────────────"

# ── 1. .env permissions ───────────────────────────────────────────────────────
echo ""
echo "Check 1: server/.env permissions"
ENV_PERMS="$(stat -f "%Sp" "$ENV_FILE" 2>/dev/null || stat --format="%A" "$ENV_FILE" 2>/dev/null || echo "unknown")"
ENV_OWNER="$(stat -f "%Su" "$ENV_FILE" 2>/dev/null || stat --format="%U" "$ENV_FILE" 2>/dev/null || echo "unknown")"

if [[ "$ENV_PERMS" == "-rw-------" ]]; then
    pass ".env mode is $ENV_PERMS (0600) — correct"
else
    fail ".env mode is $ENV_PERMS — should be -rw------- (0600)"
    info "Fix: chmod 600 $ENV_FILE"
fi

if [[ "$ENV_OWNER" == "$USER" ]]; then
    pass ".env owner is $ENV_OWNER — correct"
else
    fail ".env owner is $ENV_OWNER — should be $USER"
    info "Fix: chown $USER $ENV_FILE"
fi

# ── 2. cloudflared service ────────────────────────────────────────────────────
echo ""
echo "Check 2: cloudflared service status"
if command -v cloudflared >/dev/null 2>&1; then
    if cloudflared service status 2>/dev/null; then
        pass "cloudflared service is running"
    elif launchctl list 2>/dev/null | grep -q "cloudflared"; then
        pass "cloudflared found in launchctl list"
    else
        warn "cloudflared service does not appear to be running"
        info "Start it: sudo cloudflared service install <token>  (or check System Preferences → Login Items)"
    fi
else
    warn "cloudflared not installed — tunnel will not work"
    info "Run: ./scripts/setup-cloudflared.sh"
fi

# ── 3. Disk space ─────────────────────────────────────────────────────────────
echo ""
echo "Check 3: Disk space"
df -h "$HOME" | tail -1
AVAIL_KB="$(df -k "$HOME" | awk 'NR==2 {print $4}')"
AVAIL_GB=$(( AVAIL_KB / 1048576 ))
if [[ "$AVAIL_GB" -lt 4 ]]; then
    warn "Only ${AVAIL_GB} GB free on \$HOME — recommend ≥ 4 GB for safe operation"
else
    pass "${AVAIL_GB} GB free — OK"
fi

# ── 4. Same filesystem check ──────────────────────────────────────────────────
echo ""
echo "Check 4: STAGING_DIR and MEETING_OUTPUT_DIR on same filesystem"
if [[ -z "$STAGING_DIR" || -z "$MEETING_OUTPUT_DIR" ]]; then
    warn "STAGING_DIR or MEETING_OUTPUT_DIR not set in .env — using defaults"
    STAGING_DIR="$HOME/Library/Application Support/WisprAlt/staging"
    MEETING_OUTPUT_DIR="$HOME/Library/Application Support/WisprAlt/meetings"
fi
mkdir -p "$STAGING_DIR" "$MEETING_OUTPUT_DIR"
STAGING_DEV="$(stat -f "%d" "$STAGING_DIR" 2>/dev/null || echo "unknown")"
OUTPUT_DEV="$(stat -f "%d" "$MEETING_OUTPUT_DIR" 2>/dev/null || echo "unknown")"
if [[ "$STAGING_DEV" == "$OUTPUT_DEV" && "$STAGING_DEV" != "unknown" ]]; then
    pass "STAGING_DIR and MEETING_OUTPUT_DIR are on the same filesystem (device $STAGING_DEV)"
else
    fail "STAGING_DIR ($STAGING_DIR, dev=$STAGING_DEV) and MEETING_OUTPUT_DIR ($MEETING_OUTPUT_DIR, dev=$OUTPUT_DEV) are on DIFFERENT filesystems"
    info "Atomic renames across filesystems are not truly atomic. Move both to the same volume."
fi

# ── 5. Poll /healthz ──────────────────────────────────────────────────────────
echo ""
echo "Check 5: /healthz (max 60s)"
if [[ -z "$SERVER_URL" ]]; then
    fail "SERVER_URL is not set in $ENV_FILE — cannot check connectivity"
else
    HEALTHZ_OK=false
    for i in $(seq 1 12); do
        HTTP_CODE="$(curl -fsS -o /dev/null -w "%{http_code}" \
            --connect-timeout 5 --max-time 10 \
            "$SERVER_URL/healthz" 2>/dev/null || echo "000")"
        if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "401" ]]; then
            pass "/healthz returned HTTP $HTTP_CODE (server reachable)"
            HEALTHZ_OK=true
            break
        fi
        if [[ "$i" -lt 12 ]]; then
            info "  Attempt $i/12: HTTP $HTTP_CODE — waiting 5s..."
            sleep 5
        fi
    done
    if [[ "$HEALTHZ_OK" != "true" ]]; then
        fail "/healthz did not respond within 60s (last code: $HTTP_CODE)"
        info "Check: launchctl list | grep co.wispralt"
        info "Logs:  tail -50 $HOME/Library/Logs/WisprAlt/server.err.log"
    fi
fi

# ── 6. Poll /readyz/dictation ─────────────────────────────────────────────────
echo ""
echo "Check 6: /readyz/dictation (max 60s — Parakeet model load)"
if [[ -z "$SERVER_URL" || -z "$WISPRALT_API_KEY" ]]; then
    fail "SERVER_URL or WISPRALT_API_KEY not set — skipping readyz checks"
else
    DICTATION_OK=false
    for i in $(seq 1 12); do
        HTTP_CODE="$(curl -fsS -o /dev/null -w "%{http_code}" \
            --connect-timeout 5 --max-time 10 \
            -H "Authorization: Bearer $WISPRALT_API_KEY" \
            "$SERVER_URL/readyz/dictation" 2>/dev/null || echo "000")"
        if [[ "$HTTP_CODE" == "200" ]]; then
            pass "/readyz/dictation returned 200 — Parakeet model ready"
            DICTATION_OK=true
            break
        fi
        if [[ "$i" -lt 12 ]]; then
            info "  Attempt $i/12: HTTP $HTTP_CODE — waiting 5s (model loading)..."
            sleep 5
        fi
    done
    if [[ "$DICTATION_OK" != "true" ]]; then
        fail "/readyz/dictation not ready within 60s"
        info "The Parakeet model may still be loading, or the server is in an error state."
        info "Logs: tail -50 $HOME/Library/Logs/WisprAlt/server.err.log"
    fi

    # ── 7. Poll /readyz/meeting ───────────────────────────────────────────────
    echo ""
    echo "Check 7: /readyz/meeting (max 180s — WhisperX + Pyannote load)"
    MEETING_OK=false
    for i in $(seq 1 36); do
        HTTP_CODE="$(curl -fsS -o /dev/null -w "%{http_code}" \
            --connect-timeout 5 --max-time 10 \
            -H "Authorization: Bearer $WISPRALT_API_KEY" \
            "$SERVER_URL/readyz/meeting" 2>/dev/null || echo "000")"
        if [[ "$HTTP_CODE" == "200" ]]; then
            pass "/readyz/meeting returned 200 — meeting pipeline ready"
            MEETING_OK=true
            break
        fi
        if [[ "$i" -lt 36 ]]; then
            info "  Attempt $i/36: HTTP $HTTP_CODE — waiting 5s (heavy models loading)..."
            sleep 5
        fi
    done
    if [[ "$MEETING_OK" != "true" ]]; then
        fail "/readyz/meeting not ready within 180s"
        info "WhisperX or Pyannote may still be downloading or loading."
        info "Logs: tail -100 $HOME/Library/Logs/WisprAlt/server.err.log"
    fi
fi

# ── 8. WAV round-trip test ────────────────────────────────────────────────────
echo ""
echo "Check 8: Dictation round-trip (generate WAV → POST /transcribe/dictate)"
TEST_WAV="/tmp/wispralt_test_$$.wav"
# P4#9 / validation-loop fallback: use Python+numpy+soundfile (works without ffmpeg)
PYTHON_BIN="python3"
if [[ -f "$REPO_ROOT/server/.venv/bin/python" ]]; then
    PYTHON_BIN="$REPO_ROOT/server/.venv/bin/python"
fi

if "$PYTHON_BIN" -c \
    "import numpy as np, soundfile as sf; sf.write('$TEST_WAV', np.zeros(16000, dtype='float32'), 16000)" \
    2>/dev/null; then
    if [[ -z "$SERVER_URL" || -z "$WISPRALT_API_KEY" ]]; then
        warn "SERVER_URL or API key not set — skipping round-trip test"
    else
        ROUNDTRIP_RESP="$(curl -fsS \
            --connect-timeout 10 --max-time 30 \
            -H "Authorization: Bearer $WISPRALT_API_KEY" \
            -F "file=@$TEST_WAV;type=audio/wav" \
            "$SERVER_URL/transcribe/dictate" 2>/dev/null || echo "")"
        if echo "$ROUNDTRIP_RESP" | grep -q '"text"'; then
            pass "Round-trip dictation response contains 'text' field"
            info "Response: $ROUNDTRIP_RESP"
        else
            fail "Round-trip dictation did not return expected JSON with 'text' field"
            info "Response was: $ROUNDTRIP_RESP"
        fi
    fi
    rm -f "$TEST_WAV"
else
    warn "Could not generate test WAV (numpy/soundfile not available in $PYTHON_BIN)"
    info "Install with: cd server && uv sync"
fi

# ── 9. /metrics ───────────────────────────────────────────────────────────────
echo ""
echo "Check 9: /metrics"
if [[ -z "$SERVER_URL" || -z "$WISPRALT_API_KEY" ]]; then
    warn "SERVER_URL or API key not set — skipping /metrics"
else
    METRICS_RESP="$(curl -fsS \
        --connect-timeout 5 --max-time 10 \
        -H "Authorization: Bearer $WISPRALT_API_KEY" \
        "$SERVER_URL/metrics" 2>/dev/null || echo "")"
    if [[ -n "$METRICS_RESP" ]]; then
        pass "/metrics returned data"
        echo "$METRICS_RESP" | "$PYTHON_BIN" -m json.tool 2>/dev/null || echo "$METRICS_RESP"
    else
        warn "/metrics returned empty response (server may not yet support this endpoint)"
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "──────────────────────────────────────────────"
if [[ "$FAILURES" -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}All checks passed.${NC} WisprAlt server is healthy."
else
    echo -e "${RED}${BOLD}$FAILURES check(s) failed.${NC} Resolve the issues above and re-run doctor.sh."
    exit 1
fi
