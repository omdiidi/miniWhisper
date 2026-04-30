#!/usr/bin/env bash
# Smoke test: synthetic 2-ch WAV → upload → poll → download → assert RSS grew.
# Run against a live server (default: $WISPRALT_BASE_URL or transcribe.integrateapi.ai).

set -euo pipefail

BASE_URL="${WISPRALT_BASE_URL:-https://transcribe.integrateapi.ai}"
KEY="$(security find-generic-password -s co.wispralt -w 2>/dev/null \
       || echo "${WISPRALT_API_KEY:-}")"
[ -z "$KEY" ] && { echo "No API key (Keychain or WISPRALT_API_KEY)"; exit 1; }

# 0. Capture pre-meeting RSS via /admin/metrics (if reachable; skip if not).
RSS_BEFORE="$(curl -fsS --max-time 5 -H "Authorization: Bearer $KEY" \
  "$BASE_URL/admin/metrics" 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["memory"]["rss_mb"])' \
  2>/dev/null || echo 0)"

# 1. Generate a 5-second 2-channel 16kHz WAV with sine on ch1 (mic), silence on ch2.
WAV="$(mktemp -t wispralt-smoke-XXXXXX).wav"
python3 -c "
import numpy as np, soundfile as sf, sys
sr, dur = 16000, 5.0
t = np.arange(int(sr*dur)) / sr
ch1 = (0.3 * np.sin(2*np.pi*440*t)).astype('float32')
ch2 = np.zeros_like(ch1)
sf.write(sys.argv[1], np.stack([ch1, ch2], axis=1), sr, subtype='FLOAT')
" "$WAV"

cleanup() { rm -f "$WAV"; }
trap cleanup EXIT

# 2. Submit.
RESP="$(curl -fsS --max-time 30 -H "Authorization: Bearer $KEY" \
  -F "file=@$WAV;type=audio/wav" "$BASE_URL/transcribe/meeting")"
JOB_ID="$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')"
echo "submitted: $JOB_ID"

# 3. Poll up to 5 minutes.
DEADLINE=$(( $(date +%s) + 300 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  STATUS="$(curl -fsS --max-time 10 -H "Authorization: Bearer $KEY" \
    "$BASE_URL/transcribe/meeting/$JOB_ID" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
  echo "  status=$STATUS"
  case "$STATUS" in
    done)   break ;;
    failed) echo "FAILED"; exit 1 ;;
  esac
  sleep 5
done

# 4. Download JSON output and assert it parses + has segments.
#    Pipe via stdin, NOT shell interpolation — prevents JSON-content injection
#    into the python literal string (Round 3 F8).
curl -fsS --max-time 30 -H "Authorization: Bearer $KEY" \
  "$BASE_URL/transcribe/meeting/$JOB_ID/download/json" \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert "segments" in d and "speakers" in d, "missing fields"
print(f"OK: mode={d[\"mode\"]} segments={len(d[\"segments\"])} duration={d[\"duration_s\"]}s")
'

# 5. RSS-delta check (optional — only if metrics were reachable in step 0).
if [ "$RSS_BEFORE" != "0" ]; then
  RSS_AFTER="$(curl -fsS --max-time 5 -H "Authorization: Bearer $KEY" \
    "$BASE_URL/admin/metrics" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["memory"]["rss_mb"])')"
  DELTA=$(( RSS_AFTER - RSS_BEFORE ))
  echo "RSS: ${RSS_BEFORE} → ${RSS_AFTER} MB (Δ ${DELTA} MB)"
  # LOG ONLY — never gate on this value (Round 2 F5).
  # glibc/jemalloc/macOS allocator can hold/release RSS lazily; deltas vary.
  # Cold-start typical: +1500-2500 MB. Warm-run typical: +0-500 MB transient.
  # Operator interprets directionally, not as a pass/fail threshold.
fi
