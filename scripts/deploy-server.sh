#!/bin/bash
# Versioned server-deploy script for the prod Mac mini.
#
# Run this ON THE MINI. Typical invocation via /macmini paste with the path of
# a tarball cloned from a gist on the dev side.
#
# Contract:
#   1. Backup current /Users/$USER/wispralt/server + scripts/ to .wf-deploy-backup-<TS>.
#   2. Replace server/src/ and server/pyproject.toml with the contents of the
#      provided tarball.
#   3. Run uv sync (or pip install -e .) inside the existing venv.
#   4. Kickstart launchd: `launchctl kickstart -k gui/$UID/co.wispralt.server`.
#   5. Poll /healthz until 200 or 60s timeout (curl errors are NORMAL while
#      uvicorn is binding; we use `|| echo "000"` to keep set -e happy).
#   6. Print final status. Exit non-zero only on a real failure (rsync error,
#      uv sync error, post-restart timeout).
#
# Idempotent. Safe to re-run after a partial failure.
#
# Required env (or arguments):
#   SRC_DIR — absolute path to a directory containing the new server/ tree
#             (typically /tmp/wf-deploy-<gist-id>/server/). Pass as $1.
#
# Optional env:
#   DEPLOY_BACKUP_DIR — override the default backup parent (~/.wf-deploy-backups/).
#   SKIP_PIP — set to 1 to skip `uv sync` / pip install (if you know deps unchanged).
#   SKIP_RESTART — set to 1 to skip launchctl kickstart (rare; for dry runs).
#
# Exit codes:
#   0   — success
#   2   — bad invocation (missing SRC_DIR)
#   3   — rsync failure
#   4   — uv sync failure
#   5   — healthz never returned 200 within timeout
#
# Lives in scripts/ so it's tracked in git. Edit the script + commit; don't
# edit the deployed copy in-place — that drift caused a deploy bug last session.

set -euo pipefail

SRC_DIR="${1:-${SRC_DIR:-}}"
SKIP_PIP="${SKIP_PIP:-0}"
SKIP_RESTART="${SKIP_RESTART:-0}"

if [ -z "$SRC_DIR" ]; then
    echo "ERROR: SRC_DIR not set. Usage: $0 /path/to/new/server-tree" >&2
    exit 2
fi
if [ ! -d "$SRC_DIR" ]; then
    echo "ERROR: SRC_DIR does not exist: $SRC_DIR" >&2
    exit 2
fi

TS=$(date +%s)
TARGET="$HOME/wispralt"
BACKUP_PARENT="${DEPLOY_BACKUP_DIR:-$HOME/.wf-deploy-backups}"
BACKUP="$BACKUP_PARENT/wf-deploy-backup-$TS"

if [ ! -d "$TARGET/server" ]; then
    echo "ERROR: target tree missing: $TARGET/server" >&2
    exit 2
fi

echo "=== WisprAlt server deploy (TS=$TS) ==="
echo "SRC_DIR:   $SRC_DIR"
echo "TARGET:    $TARGET"
echo "BACKUP:    $BACKUP"
echo

echo "=== Step 1: backup current server tree ==="
mkdir -p "$BACKUP"
# rsync rather than cp so we preserve metadata cleanly and the operation is
# idempotent if the backup dir already exists.
rsync -a --delete "$TARGET/server/" "$BACKUP/server/"
if [ -d "$TARGET/scripts" ]; then
    rsync -a --delete "$TARGET/scripts/" "$BACKUP/scripts/"
fi
echo "  backup written to $BACKUP"
echo

echo "=== Step 2: copy new files into place ==="
# Copy src/ and pyproject.toml — leave .venv/ + .env intact.
if [ -d "$SRC_DIR/server/src" ]; then
    rsync -a --delete "$SRC_DIR/server/src/" "$TARGET/server/src/" || {
        echo "ERROR: rsync of src/ failed" >&2
        exit 3
    }
fi
if [ -f "$SRC_DIR/server/pyproject.toml" ]; then
    cp "$SRC_DIR/server/pyproject.toml" "$TARGET/server/pyproject.toml"
fi
# Optional: scripts (e.g. prefetch + benchmark)
if [ -d "$SRC_DIR/server/scripts" ]; then
    mkdir -p "$TARGET/server/scripts"
    rsync -a "$SRC_DIR/server/scripts/" "$TARGET/server/scripts/" || true
fi
# Top-level scripts (deploy, launchd, etc) — install but do NOT clobber
# anything not in $SRC_DIR.
if [ -d "$SRC_DIR/scripts" ]; then
    mkdir -p "$TARGET/scripts"
    rsync -a "$SRC_DIR/scripts/" "$TARGET/scripts/" || true
fi
echo "  new files in place"
echo

echo "=== Step 3: dependency install ==="
if [ "$SKIP_PIP" = "1" ]; then
    echo "  SKIP_PIP=1 → skipping"
else
    cd "$TARGET/server"
    if [ -x ".venv/bin/python" ]; then
        # The mini's venv has no pip binary historically; bootstrap if missing.
        if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
            echo "  bootstrapping pip..."
            .venv/bin/python -m ensurepip --upgrade >/dev/null 2>&1 || true
        fi
        # Install from pyproject in editable mode so future deploys can be
        # src-only without re-running pip.
        echo "  pip install -e ."
        .venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1 || true
        .venv/bin/python -m pip install -e . 2>&1 | tail -10 || {
            echo "ERROR: pip install -e . failed" >&2
            exit 4
        }
    else
        echo "ERROR: venv missing at $TARGET/server/.venv" >&2
        exit 4
    fi
fi
echo

echo "=== Step 4: kickstart launchd ==="
if [ "$SKIP_RESTART" = "1" ]; then
    echo "  SKIP_RESTART=1 → skipping"
else
    launchctl kickstart -k "gui/$(id -u)/co.wispralt.server" 2>&1 || true
    echo "  kickstart fired"
fi
echo

echo "=== Step 5: poll /healthz ==="
DEADLINE=$(( $(date +%s) + 60 ))
LAST_CODE=""
while [ $(date +%s) -lt $DEADLINE ]; do
    # `|| echo "000"` is load-bearing — without it, curl's exit 7 (connect
    # refused) trips `set -e` during the brief window when uvicorn is rebinding.
    CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://127.0.0.1:8000/healthz || echo "000")
    LAST_CODE="$CODE"
    if [ "$CODE" = "200" ]; then
        echo "  healthz=200 (uvicorn back up)"
        break
    fi
    sleep 1
done

if [ "$LAST_CODE" != "200" ]; then
    echo "ERROR: healthz never returned 200 within timeout (last=$LAST_CODE)" >&2
    exit 5
fi
echo

echo "=== Step 6: post-deploy snapshot ==="
launchctl print "gui/$(id -u)/co.wispralt.server" 2>/dev/null | grep -E "state|pid|last exit code" | head -5
echo
echo "Done."
