#!/usr/bin/env bash
# verify-openai-compat.sh — Live smoke test for /v1/* OpenAI-compatibility surface.
#
# Usage:
#   BASE_URL=https://transcribe.integrateapi.ai/v1 API_KEY=<token> bash scripts/verify-openai-compat.sh
#   bash scripts/verify-openai-compat.sh --slow    # adds rate-limit enforcement check (60+ calls)
#
# Run after a mini deploy to confirm the /v1 surface is healthy end-to-end.
# Exits 0 on full pass, non-zero with a summary of failures.

set -uo pipefail

BASE_URL="${BASE_URL:-https://transcribe.integrateapi.ai/v1}"
API_KEY="${API_KEY:-}"
SLOW="${1:-}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE_WAV="$REPO_ROOT/server/tests/fixtures/tiny.wav"
FIXTURE_MP3="$REPO_ROOT/server/tests/fixtures/tiny.mp3"
FIXTURE_M4A="$REPO_ROOT/server/tests/fixtures/tiny.m4a"

if [[ -z "$API_KEY" ]]; then
  echo "ERROR: API_KEY env var required. Mint one via /admin/keys/new on the admin UI." >&2
  exit 2
fi

PASS_COUNT=0
FAIL_COUNT=0
FAILED_CHECKS=()

ok() {
  echo "  ✓ $1"
  PASS_COUNT=$((PASS_COUNT + 1))
}
fail() {
  echo "  ✗ $1"
  echo "    $2" >&2
  FAIL_COUNT=$((FAIL_COUNT + 1))
  FAILED_CHECKS+=("$1")
}

# ── helpers ──────────────────────────────────────────────────────────────────
curl_post() {
  # $1 = response_format
  # $2 = file fixture path
  # Echoes HTTP status and body separated by a newline
  curl -sS -o /tmp/wf-body -w "%{http_code}\n" \
    -X POST \
    -H "Authorization: Bearer $API_KEY" \
    -F "file=@$2" \
    -F "response_format=$1" \
    "$BASE_URL/audio/transcriptions"
}

# ── checks ───────────────────────────────────────────────────────────────────

check_01_text_format() {
  local status=$(curl_post text "$FIXTURE_WAV")
  local content_type=$(curl -sS -o /dev/null -w "%{content_type}" \
    -X POST -H "Authorization: Bearer $API_KEY" \
    -F "file=@$FIXTURE_WAV" -F "response_format=text" \
    "$BASE_URL/audio/transcriptions")
  if [[ "$status" == "200" ]] && [[ "$content_type" =~ text/plain ]]; then
    ok "1. response_format=text returns 200 + text/plain"
  else
    fail "1. response_format=text" "status=$status content_type=$content_type"
  fi
}

check_02_json_format() {
  local status=$(curl_post json "$FIXTURE_WAV")
  if [[ "$status" == "200" ]] && jq -e '.text != null' /tmp/wf-body >/dev/null 2>&1; then
    ok "2. response_format=json returns 200 + valid JSON with text field"
  else
    fail "2. response_format=json" "status=$status body=$(cat /tmp/wf-body)"
  fi
}

check_03_verbose_json() {
  local status=$(curl_post verbose_json "$FIXTURE_WAV")
  if [[ "$status" == "200" ]] \
    && jq -e '.task == "transcribe" and .language == "english" and (.segments | type == "array")' /tmp/wf-body >/dev/null 2>&1; then
    ok "3. response_format=verbose_json returns canonical shape"
  else
    fail "3. response_format=verbose_json" "status=$status body=$(cat /tmp/wf-body)"
  fi
}

check_04_srt_format() {
  local status=$(curl_post srt "$FIXTURE_WAV")
  local body=$(cat /tmp/wf-body)
  if [[ "$status" == "200" ]] && ([[ "$body" =~ ^1$'\n'00:00: ]] || [[ -z "$body" ]]); then
    # body may be empty on tonal sine — accept both empty (Parakeet returned "") and valid SRT
    ok "4. response_format=srt returns 200 + valid SRT (or empty for tonal input)"
  else
    fail "4. response_format=srt" "status=$status body[0..50]=${body:0:50}"
  fi
}

check_05_vtt_format() {
  local status=$(curl_post vtt "$FIXTURE_WAV")
  local body=$(cat /tmp/wf-body)
  if [[ "$status" == "200" ]] && [[ "$body" =~ ^WEBVTT ]]; then
    ok "5. response_format=vtt returns 200 + WEBVTT magic line"
  else
    fail "5. response_format=vtt" "status=$status body[0..30]=${body:0:30}"
  fi
}

check_06_mp3_roundtrip() {
  local status=$(curl_post json "$FIXTURE_MP3")
  if [[ "$status" == "200" ]]; then
    ok "6. mp3 fixture roundtrip returns 200"
  else
    fail "6. mp3 roundtrip" "status=$status body=$(cat /tmp/wf-body)"
  fi
}

check_07_m4a_roundtrip() {
  local status=$(curl_post json "$FIXTURE_M4A")
  if [[ "$status" == "200" ]]; then
    ok "7. m4a fixture roundtrip returns 200"
  else
    fail "7. m4a roundtrip" "status=$status body=$(cat /tmp/wf-body)"
  fi
}

check_08_models_list() {
  local status=$(curl -sS -o /tmp/wf-body -w "%{http_code}" \
    -H "Authorization: Bearer $API_KEY" \
    "$BASE_URL/models")
  if [[ "$status" == "200" ]]; then
    local count=$(jq -r '.data | length' /tmp/wf-body 2>/dev/null)
    if [[ "$count" == "5" ]]; then
      ok "8. GET /v1/models returns 5 models"
    else
      fail "8. /v1/models count" "expected 5, got $count"
    fi
  else
    fail "8. GET /v1/models" "status=$status"
  fi
}

check_09_models_whisper_1() {
  local status=$(curl -sS -o /tmp/wf-body -w "%{http_code}" \
    -H "Authorization: Bearer $API_KEY" \
    "$BASE_URL/models/whisper-1")
  if [[ "$status" == "200" ]] && jq -e '.id == "whisper-1"' /tmp/wf-body >/dev/null 2>&1; then
    ok "9. GET /v1/models/whisper-1 returns single model"
  else
    fail "9. /v1/models/whisper-1" "status=$status body=$(cat /tmp/wf-body)"
  fi
}

check_10_models_diarize_404() {
  local status=$(curl -sS -o /tmp/wf-body -w "%{http_code}" \
    -H "Authorization: Bearer $API_KEY" \
    "$BASE_URL/models/gpt-4o-transcribe-diarize")
  if [[ "$status" == "404" ]] && jq -e '.error.code == "model_not_found"' /tmp/wf-body >/dev/null 2>&1; then
    ok "10. /v1/models/gpt-4o-transcribe-diarize returns 404 model_not_found"
  else
    fail "10. diarize 404" "status=$status body=$(cat /tmp/wf-body)"
  fi
}

check_11_bad_auth_envelope() {
  local status=$(curl -sS -o /tmp/wf-body -w "%{http_code}" \
    -H "Authorization: Bearer not-a-real-token" \
    "$BASE_URL/models")
  if [[ "$status" == "401" ]] && jq -e '.error.type and .error.code' /tmp/wf-body >/dev/null 2>&1; then
    ok "11. Bad bearer returns 401 + OpenAI error envelope"
  else
    fail "11. bad auth" "status=$status body=$(cat /tmp/wf-body)"
  fi
}

check_12_cors_preflight() {
  local headers=$(curl -sS -D /tmp/wf-headers -o /dev/null \
    -X OPTIONS \
    -H "Origin: https://foo.example" \
    -H "Access-Control-Request-Method: POST" \
    -H "Access-Control-Request-Headers: Authorization, Content-Type" \
    "$BASE_URL/audio/transcriptions")
  if grep -qi "access-control-allow-origin" /tmp/wf-headers; then
    ok "12. OPTIONS preflight returns CORS headers"
  else
    fail "12. CORS preflight" "no Access-Control-Allow-Origin in: $(cat /tmp/wf-headers)"
  fi
}

check_13_429_carries_cors() {
  # Difficult to provoke a real 429 in a single shot — best-effort skip unless we
  # have a way to flood the bucket. Skip unless --slow.
  if [[ "$SLOW" != "--slow" ]]; then
    echo "  ⊘ 13. Rate-limit 429 CORS check (skipped — pass --slow to enable)"
    return
  fi
  # Send 65 rapid calls; if any return 429 with ACAO, pass.
  local got_429_with_cors=0
  for i in $(seq 1 65); do
    curl -sS -D /tmp/wf-headers -o /dev/null \
      -H "Authorization: Bearer $API_KEY" \
      "$BASE_URL/models" >/dev/null
    if grep -qi "^HTTP.*429" /tmp/wf-headers && grep -qi "access-control-allow-origin" /tmp/wf-headers; then
      got_429_with_cors=1
      break
    fi
  done
  if [[ $got_429_with_cors -eq 1 ]]; then
    ok "13. 429 rate-limit response carries CORS header"
  else
    fail "13. 429 + CORS" "no 429 observed after 65 calls"
  fi
}

check_14_models_cache_control() {
  local headers=$(curl -sS -D /tmp/wf-headers -o /dev/null \
    -H "Authorization: Bearer $API_KEY" \
    "$BASE_URL/models")
  if grep -qi "cache-control:.*no-cache" /tmp/wf-headers; then
    ok "14. /v1/models has Cache-Control: no-cache"
  else
    fail "14. cache-control" "no no-cache in: $(grep -i cache-control /tmp/wf-headers)"
  fi
}

# ── main ─────────────────────────────────────────────────────────────────────
echo "Verifying $BASE_URL"
echo ""

[[ ! -f "$FIXTURE_WAV" ]] && { echo "ERROR: missing $FIXTURE_WAV — generate via server/tests/fixtures/README.md" >&2; exit 2; }

check_01_text_format
check_02_json_format
check_03_verbose_json
check_04_srt_format
check_05_vtt_format
check_06_mp3_roundtrip
check_07_m4a_roundtrip
check_08_models_list
check_09_models_whisper_1
check_10_models_diarize_404
check_11_bad_auth_envelope
check_12_cors_preflight
check_13_429_carries_cors
check_14_models_cache_control

echo ""
echo "Summary: $PASS_COUNT passed, $FAIL_COUNT failed"
if [[ $FAIL_COUNT -gt 0 ]]; then
  echo "Failed checks: ${FAILED_CHECKS[*]}" >&2
  exit 1
fi
exit 0
