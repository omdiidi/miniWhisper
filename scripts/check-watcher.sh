#!/bin/bash
# Manually exercise the db_watcher recovery path end-to-end.
#
# REQUIREMENTS:
#   - Server running with WISPRALT_DEV_FAULTS=1 (NOT enabled on prod mini).
#   - For ad-hoc verification on a dev/staging mini, set the env var in
#     server/.env, kickstart launchd, then run this script.
#   - Server must be reachable at $BASE_URL.
#
# WHAT THIS DOES:
#   1. POST /dev/db/close — closes the live asyncpg pool, reproducing the
#      EXACT 2026-05-17 failure mode (InterfaceError "pool is closed").
#   2. Poll /readyz/db for 2-3 seconds expecting 503.
#   3. Wait up to 30s for the watcher's 10s tick to detect + rebuild,
#      then for /readyz/db to flip back to 200.
#   4. Exit 0 on full recovery, 1 if recovery didn't happen.
#
# USAGE: ./scripts/check-watcher.sh http://localhost:8080
#        ./scripts/check-watcher.sh https://staging.example.com
#        # DO NOT run against prod unless WISPRALT_DEV_FAULTS=1 is set there
#        # — and it should NEVER be set on prod per CLAUDE.md / FALLBACK.md.

set -euo pipefail
BASE_URL="${1:-http://localhost:8080}"

echo "INFO: forcing pool closure via /dev/db/close"
CLOSE_RESP=$(curl -sS -X POST --max-time 5 "$BASE_URL/dev/db/close")
echo "  response: $CLOSE_RESP"

# Expect /readyz/db to flip 503 within ~2s (next health_check probe).
echo "INFO: watching /readyz/db for 503 (closed) → 200 (recovered) transition"
saw_503=0
deadline=$(( $(date +%s) + 30 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$BASE_URL/readyz/db")
  ts=$(date +%H:%M:%S)
  echo "  $ts /readyz/db=$code"
  if [ "$code" = "503" ] && [ "$saw_503" = "0" ]; then
    echo "  ✓ saw 503 (pool detected closed)"
    saw_503=1
  fi
  if [ "$saw_503" = "1" ] && [ "$code" = "200" ]; then
    echo "  ✓ recovered to 200 (watcher rebuilt the pool)"
    echo "PASS: watcher recovery proven end-to-end"
    exit 0
  fi
  sleep 1
done
echo "FAIL: did not recover within 30s (last code=$code, saw_503=$saw_503)"
exit 1
