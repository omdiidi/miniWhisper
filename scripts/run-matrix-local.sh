#!/bin/bash
# Phase 8 12-run matrix — RUN FROM MACBOOK (not mini).
# Uses the Keychain API key (the same one the WisprAlt app uses).
# Submits to the public Cloudflare-tunneled endpoint.
#
# Generates fixture files locally via ffmpeg from a source Sammamish file.
# Output: ./tmp/matrix-results.md + ./tmp/matrix-raw.jsonl
#
# Set SKIP_ROWS as comma-separated indices (e.g. "10") to skip rows.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BENCH_DIR="$REPO_ROOT/tmp/matrix-bench"
RESULTS_MD="$REPO_ROOT/tmp/matrix-results.md"
RAW_JSONL="$REPO_ROOT/tmp/matrix-raw.jsonl"
SERVER="${WISPRALT_SERVER:-https://transcribe.integrateapi.ai}"
API_KEY=$(security find-generic-password -s "co.wispralt" -w 2>/dev/null)
SKIP_ROWS="${SKIP_ROWS:-}"

SAMMAMISH_SRC="/Users/omidzahrai/Documents/WisprAlt/Meetings/Custom Transcriptions/Sammamish Endodontics__20260509-222429/Sammamish Endodontics.m4a"

mkdir -p "$BENCH_DIR"
cd "$BENCH_DIR"

echo "=== Phase 8 12-run matrix (local) ==="
echo "Server: $SERVER  API key len: ${#API_KEY}"
echo "Source: $SAMMAMISH_SRC"
echo "Results: $RESULTS_MD"
echo

# ---------- Generate fixture files ----------
echo "=== Fixture generation (idempotent) ==="
if [ ! -f gen-6min.m4a ]; then
    # 6 min synthetic via macOS say
    TXT_FILE=$(mktemp)
    PARA="Welcome to the WisprAlt benchmark. This is a synthetic test recording for measuring the realtime processing ratio of MLX whisper on Apple Silicon. We are reading a paragraph aloud multiple times to fill approximately six minutes of speech audio."
    for i in $(seq 1 16); do echo "$PARA Repetition number $i." >> "$TXT_FILE"; done
    say -f "$TXT_FILE" -o gen.aiff -r 150
    ffmpeg -y -loglevel error -i gen.aiff -ac 1 -ar 48000 -c:a aac -b:a 64k gen-6min.m4a
    rm -f gen.aiff "$TXT_FILE"
    echo "  ✓ gen-6min.m4a"
fi

# Sizes
[ -f gen-30s-mono.m4a ]   || ffmpeg -y -loglevel error -i gen-6min.m4a -t 30 -ac 1 -ar 48000 -c:a aac -b:a 64k gen-30s-mono.m4a
[ -f gen-30s-stereo.m4a ] || ffmpeg -y -loglevel error -i gen-6min.m4a -t 30 -af "channelmap=0|0:stereo" -ar 48000 -c:a aac -b:a 96k gen-30s-stereo.m4a
[ -f gen-5m-mono.m4a ]    || cp gen-6min.m4a gen-5m-mono.m4a
[ -f gen-5m-stereo.m4a ]  || ffmpeg -y -loglevel error -i gen-6min.m4a -af "channelmap=0|0:stereo" -ar 48000 -c:a aac -b:a 96k gen-5m-stereo.m4a
[ -f sammamish-30m-mono.m4a ]   || ffmpeg -y -loglevel error -i "$SAMMAMISH_SRC" -t 1800 -ac 1 -ar 48000 -c:a aac -b:a 64k sammamish-30m-mono.m4a
[ -f sammamish-30m-stereo.m4a ] || ffmpeg -y -loglevel error -i "$SAMMAMISH_SRC" -t 1800 -ac 2 -ar 48000 -c:a aac -b:a 96k sammamish-30m-stereo.m4a
[ -f sammamish-105m-mono.m4a ]  || ffmpeg -y -loglevel error -i "$SAMMAMISH_SRC" -ac 1 -ar 48000 -c:a aac -b:a 64k sammamish-105m-mono.m4a
[ -f sammamish-105m-stereo.m4a ] || cp "$SAMMAMISH_SRC" sammamish-105m-stereo.m4a
echo "  fixtures ready (sizes: $(ls -la *.m4a 2>/dev/null | awk '{print $5"  "$9}' | head -10))"
echo

# ---------- Matrix ----------
MATRIX=(
    "01-30s-file|gen-30s-mono.m4a|file|30"
    "02-30s-meet-mono|gen-30s-mono.m4a|meeting|30"
    "03-30s-meet-stereo|gen-30s-stereo.m4a|meeting|30"
    "04-5m-file|gen-5m-mono.m4a|file|330"
    "05-5m-meet-mono|gen-5m-mono.m4a|meeting|330"
    "06-5m-meet-stereo|gen-5m-stereo.m4a|meeting|330"
    "07-30m-file|sammamish-30m-mono.m4a|file|1800"
    "08-30m-meet-mono|sammamish-30m-mono.m4a|meeting|1800"
    "09-30m-meet-stereo|sammamish-30m-stereo.m4a|meeting|1800"
    "10-105m-file|sammamish-105m-mono.m4a|file|6341"
    "11-105m-meet-mono|sammamish-105m-mono.m4a|meeting|6341"
    "12-105m-meet-stereo|sammamish-105m-stereo.m4a|meeting|6341"
)

{
    echo "# Phase 8 12-run matrix results (local)"
    echo ""
    echo "Started: $(date)"
    echo "Server: $SERVER"
    echo ""
    echo "| # | name | mode | audio_s | wall_s | ratio | segs | speakers | status |"
    echo "|---|---|---|---|---|---|---|---|---|"
} > "$RESULTS_MD"
: > "$RAW_JSONL"

for entry in "${MATRIX[@]}"; do
    IFS='|' read -r NAME FILE MODE EXP_DUR <<< "$entry"
    ROW_NUM="${NAME%%-*}"
    if echo ",$SKIP_ROWS," | grep -q ",$ROW_NUM,"; then
        echo "=== [$NAME] SKIPPED ==="
        echo "| $ROW_NUM | $NAME | $MODE | $EXP_DUR | -- | -- | -- | -- | SKIPPED |" >> "$RESULTS_MD"
        continue
    fi
    echo "=== [$NAME] mode=$MODE ==="
    if [ ! -f "$FILE" ]; then
        echo "  fixture missing: $FILE"
        echo "| $ROW_NUM | $NAME | $MODE | $EXP_DUR | -- | -- | -- | -- | MISSING |" >> "$RESULTS_MD"
        continue
    fi
    T0=$(date +%s)
    RESP=$(curl -sf -X POST "$SERVER/transcribe/file" \
        -H "Authorization: Bearer $API_KEY" \
        -F "mode=$MODE" \
        -F "file=@$FILE" 2>&1)
    if [ -z "$RESP" ]; then
        echo "  SUBMIT_FAIL"
        echo "| $ROW_NUM | $NAME | $MODE | $EXP_DUR | -- | -- | -- | -- | SUBMIT_FAIL |" >> "$RESULTS_MD"
        continue
    fi
    JOB=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))" 2>/dev/null)
    if [ -z "$JOB" ]; then
        echo "  SUBMIT_FAIL — RESP: $RESP"
        echo "| $ROW_NUM | $NAME | $MODE | $EXP_DUR | -- | -- | -- | -- | SUBMIT_FAIL |" >> "$RESULTS_MD"
        continue
    fi
    echo "  job_id=$JOB"

    STATUS=""
    while true; do
        SJ=$(curl -sf -H "Authorization: Bearer $API_KEY" "$SERVER/transcribe/meeting/$JOB" 2>/dev/null || echo "{}")
        STATUS=$(echo "$SJ" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null)
        PHASE_INFO=$(echo "$SJ" | python3 -c "
import sys,json
try:
    d = json.load(sys.stdin)
    p = d.get('progress', {}) or {}
    s = d.get('status', '?')
    print(f'{s} phase={p.get(\"phase\")} chunk={p.get(\"chunk_index\")}/{p.get(\"total_chunks\")}')
except: print('?')
" 2>/dev/null)
        echo "    [poll] $PHASE_INFO"
        case "$STATUS" in
            done|failed) break ;;
        esac
        sleep 8
    done
    WALL=$(( $(date +%s) - T0 ))
    RATIO=$(echo "scale=2; $EXP_DUR / $WALL" | bc 2>/dev/null || echo "0")
    SEGS=0; SPEAKERS=0
    if [ "$STATUS" = "done" ]; then
        TXT=$(curl -sf -H "Authorization: Bearer $API_KEY" "$SERVER/transcribe/meeting/$JOB/download/txt" 2>/dev/null)
        SEGS=$(echo "$TXT" | grep -c "^\[Speaker")
        SPEAKERS=$(echo "$TXT" | grep -oE "\[Speaker [^]]+\]" | sort -u | wc -l | tr -d ' ')
    fi
    echo "  result: status=$STATUS wall=${WALL}s ratio=${RATIO}x segs=$SEGS speakers=$SPEAKERS"

    echo "| $ROW_NUM | $NAME | $MODE | $EXP_DUR | ${WALL}s | ${RATIO}x | $SEGS | $SPEAKERS | $STATUS |" >> "$RESULTS_MD"
    echo "{\"name\":\"$NAME\",\"mode\":\"$MODE\",\"audio_s\":$EXP_DUR,\"wall_s\":$WALL,\"ratio\":\"$RATIO\",\"segs\":$SEGS,\"speakers\":$SPEAKERS,\"status\":\"$STATUS\",\"job_id\":\"$JOB\"}" >> "$RAW_JSONL"

    # Delete job to free disk + semaphore
    curl -sf -X DELETE -H "Authorization: Bearer $API_KEY" "$SERVER/transcribe/meeting/$JOB" >/dev/null 2>&1 || true
    sleep 2
done

echo "" >> "$RESULTS_MD"
echo "Finished: $(date)" >> "$RESULTS_MD"
echo
echo "=== DONE ==="
cat "$RESULTS_MD"
