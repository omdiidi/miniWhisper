#!/usr/bin/env bash
# doctor.sh — WisprAlt server health and configuration checks.
#
# Checks performed:
#   1. server/.env exists, mode is 0600, owner is $USER
#   2. cloudflared service status (best-effort)
#   3. Disk free on $HOME (warn if < 4 GB)
#   4. DATA_DIR/staging and DATA_DIR/outputs on same filesystem
#   5. Poll /healthz until 200 or 401 (max 60s)
#   6. Poll /readyz/dictation until 200 (max 60s × 5s intervals)
#   7. /readyz/db (local SQLite DB answers, schema current)
#   8. An enabled admin exists (python -m wispralt.bootstrap --check)
#
# No API key is needed: every probe here is unauthenticated, and doctor never
# reads or prints a token.
#
# The file-transcription model (MLX Whisper) loads lazily on the first file
# job, so there is no readiness probe for it here.
#
# Exit code: 0 if all checks pass, 1 if any required check fails.
#
# Usage: ./scripts/doctor.sh

set -euo pipefail

# ── Argument parsing ──────────────────────────────────────────────────────────
for arg in "$@"; do
    echo "Unknown argument: $arg" >&2; exit 2
done

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

# ── Resolve python (prefer server venv) ───────────────────────────────────────
PYTHON_BIN="python3"

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
DATA_DIR="$(_read_env_var DATA_DIR | tr -d '"' || true)"

if [[ -f "$REPO_ROOT/server/.venv/bin/python" ]]; then
    PYTHON_BIN="$REPO_ROOT/server/.venv/bin/python"
fi

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
        info "Start it: bash scripts/setup-cloudflared.sh  (NOT 'sudo cloudflared service install' — broken on macOS 14/15)"
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
echo "Check 4: staging and outputs on same filesystem"
if [[ -z "$DATA_DIR" ]]; then
    fail "DATA_DIR not set in .env — run setup-server.sh"
    DATA_DIR="$HOME/Library/Application Support/WisprAlt"
fi
STAGING_DIR="$DATA_DIR/staging"
OUTPUTS_DIR="$DATA_DIR/outputs"
mkdir -p "$STAGING_DIR" "$OUTPUTS_DIR"
STAGING_DEV="$(stat -f "%d" "$STAGING_DIR" 2>/dev/null || echo "unknown")"
OUTPUT_DEV="$(stat -f "%d" "$OUTPUTS_DIR" 2>/dev/null || echo "unknown")"
if [[ "$STAGING_DEV" == "$OUTPUT_DEV" && "$STAGING_DEV" != "unknown" ]]; then
    pass "staging and outputs are on the same filesystem (device $STAGING_DEV)"
else
    fail "staging ($STAGING_DIR, dev=$STAGING_DEV) and outputs ($OUTPUTS_DIR, dev=$OUTPUT_DEV) are on DIFFERENT filesystems"
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
if [[ -z "$SERVER_URL" ]]; then
    fail "SERVER_URL not set — skipping readyz checks"
else
    DICTATION_OK=false
    for i in $(seq 1 12); do
        HTTP_CODE="$(curl -sS -o /dev/null -w "%{http_code}" \
            --connect-timeout 5 --max-time 10 \
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
        info "No module named 'mlx' / 'parakeet_mlx' in the log? Install the MLX stack:"
        info "  cd server && uv sync --extra host"
        info "Logs: tail -50 $HOME/Library/Logs/WisprAlt/server.err.log"
    fi
fi

# ── 7. /readyz/db ─────────────────────────────────────────────────────────────
echo ""
echo "Check 7: /readyz/db (local SQLite database)"
if [[ -z "$SERVER_URL" ]]; then
    fail "SERVER_URL not set — skipping /readyz/db"
else
    DB_RESP="$(curl -sS --connect-timeout 5 --max-time 10 \
        -w "\n%{http_code}" "$SERVER_URL/readyz/db" 2>/dev/null || echo "000")"
    DB_CODE="$(echo "$DB_RESP" | tail -1)"
    if [[ "$DB_CODE" == "200" ]]; then
        pass "/readyz/db returned 200"
    else
        fail "/readyz/db returned HTTP $DB_CODE"
        info "Response: $(echo "$DB_RESP" | sed '$d')"
        info "reason=db_error: DATA_DIR/wispralt.db unreadable; reason=schema_outdated: restart the server to migrate."
    fi
fi

# ── 8. An enabled admin exists ────────────────────────────────────────────────
echo ""
echo "Check 8: first admin exists"
if (cd "$REPO_ROOT/server" && "$PYTHON_BIN" -m wispralt.bootstrap --check) >/dev/null 2>&1; then
    pass "An enabled admin exists"
else
    fail "No enabled admin (or the database is unreadable)"
    info "Create one: cd server && .venv/bin/python -m wispralt.bootstrap --label owner"
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
