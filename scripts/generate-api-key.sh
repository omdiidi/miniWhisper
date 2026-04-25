#!/usr/bin/env bash
# generate-api-key.sh — Generate a 32-byte hex API key and write it to server/.env.
#
# Idempotent: replaces an existing WISPRALT_API_KEY line if present, or appends one.
# Always sets server/.env to mode 0600 after writing.
#
# Usage: ./scripts/generate-api-key.sh
#   Run from the repo root, OR any directory — the script locates server/.env
#   relative to its own location.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/server/.env"

# ── Ensure server/.env exists ───────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
    if [[ -f "$REPO_ROOT/server/.env.example" ]]; then
        cp "$REPO_ROOT/server/.env.example" "$ENV_FILE"
        echo "Created $ENV_FILE from .env.example"
    else
        touch "$ENV_FILE"
        echo "Created empty $ENV_FILE"
    fi
fi

# ── Generate key ─────────────────────────────────────────────────────────────
NEW_KEY="$(openssl rand -hex 32)"

# ── Write key into .env (replace existing line or append) ────────────────────
if grep -q "^WISPRALT_API_KEY=" "$ENV_FILE" 2>/dev/null; then
    # Replace in-place using a temp file in the same directory (atomic)
    TMP_ENV="$(dirname "$ENV_FILE")/.env.tmp.$$"
    # Use a delimiter unlikely to appear in keys (@ character)
    sed "s@^WISPRALT_API_KEY=.*@WISPRALT_API_KEY=$NEW_KEY@" "$ENV_FILE" > "$TMP_ENV"
    mv "$TMP_ENV" "$ENV_FILE"
    echo "Replaced existing WISPRALT_API_KEY in $ENV_FILE"
else
    printf '\nWISPRALT_API_KEY=%s\n' "$NEW_KEY" >> "$ENV_FILE"
    echo "Appended WISPRALT_API_KEY to $ENV_FILE"
fi

# ── Lock down permissions ─────────────────────────────────────────────────────
chmod 600 "$ENV_FILE"

# ── Report ───────────────────────────────────────────────────────────────────
echo ""
echo "Generated API key:"
echo "  $NEW_KEY"
echo ""
echo "Key is in $ENV_FILE (mode 0600)."
echo "Copy it to the client: Settings → API Key field (or Keychain via setup-server.sh)."
