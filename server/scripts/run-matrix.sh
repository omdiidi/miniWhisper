#!/bin/bash
# Phase 8 test matrix runner. Submits 12 jobs sequentially through the
# server's `/transcribe/file` endpoint, captures phase timings and final
# wall clock, and produces a markdown summary table.
#
# Run ON THE MINI. Reads API key from ~/wispralt/server/.env.
# Test files expected at ~/wf-bench/:
#   - sammamish-source.wav (105.7 min, 2ch 16kHz)
#   - gen-6min.m4a (5.5 min synthetic)
# Generates from those: 30s, 30m via ffmpeg trim.
#
# Output: ~/wf-bench/matrix-results.md (markdown table)
#         ~/wf-bench/matrix-raw.jsonl (per-run JSON for replay)
#
# Cancel test is SEPARATE — not run as part of the matrix loop.

set -uo pipefail

BENCH_DIR="$HOME/wf-bench"
RESULTS_MD="$BENCH_DIR/matrix-results.md"
RAW_JSONL="$BENCH_DIR/matrix-raw.jsonl"
SERVER="http://127.0.0.1:8000"
API_KEY=$(grep "^WISPRALT_API_KEY=" "$HOME/wispralt/server/.env" | cut -d= -f2 | tr -d '"')

cd "$BENCH_DIR" || exit 2

echo "=== Phase 8 12-run matrix ==="
echo "API key len: ${#API_KEY}"
echo "Results will land at: $RESULTS_MD"
echo

# ---------- Generate missing fixture files ----------
echo "=== Generating fixture files (idempotent) ==="
# Short: 30s mono from gen-6min
if [ ! -f gen-30s-mono.m4a ]; then
    ffmpeg -y -loglevel error -i gen-6min.m4a -t 30 -ac 1 -ar 48000 -c:a aac -b:a 64k gen-30s-mono.m4a
    echo "  ✓ gen-30s-mono.m4a"
fi
# Short stereo: 30s from a duplicated channel
if [ ! -f gen-30s-stereo.m4a ]; then
    ffmpeg -y -loglevel error -i gen-6min.m4a -t 30 -af "channelmap=0|0:stereo" -ar 48000 -c:a aac -b:a 96k gen-30s-stereo.m4a
    echo "  ✓ gen-30s-stereo.m4a"
fi
# Medium mono: 5m = use gen-6min (it's 5.5 min, close enough)
[ -f gen-5m-mono.m4a ] || ln -s gen-6min.m4a gen-5m-mono.m4a
# Medium stereo: 5m stereo via channelmap
if [ ! -f gen-5m-stereo.m4a ]; then
    ffmpeg -y -loglevel error -i gen-6min.m4a -af "channelmap=0|0:stereo" -ar 48000 -c:a aac -b:a 96k gen-5m-stereo.m4a
    echo "  ✓ gen-5m-stereo.m4a"
fi
# Long mono: 30m extracted from Sammamish (it's already 105m stereo, take first 30m of ch1)
if [ ! -f sammamish-30m-mono.m4a ]; then
    ffmpeg -y -loglevel error -i sammamish-source.wav -t 1800 -ac 1 -ar 48000 -c:a aac -b:a 64k sammamish-30m-mono.m4a
    echo "  ✓ sammamish-30m-mono.m4a"
fi
# Long stereo: 30m from Sammamish (keep 2 channels)
if [ ! -f sammamish-30m-stereo.m4a ]; then
    ffmpeg -y -loglevel error -i sammamish-source.wav -t 1800 -ar 48000 -c:a aac -b:a 96k sammamish-30m-stereo.m4a
    echo "  ✓ sammamish-30m-stereo.m4a"
fi
# Huge mono: 105m from Sammamish ch1
if [ ! -f sammamish-105m-mono.m4a ]; then
    ffmpeg -y -loglevel error -i sammamish-source.wav -ac 1 -ar 48000 -c:a aac -b:a 64k sammamish-105m-mono.m4a
    echo "  ✓ sammamish-105m-mono.m4a"
fi
# Huge stereo = sammamish-source.wav (already 2ch)
echo "  fixtures ready"
echo

# ---------- Matrix definition ----------
# Format: NAME|FILE|MODE|EXPECTED_DUR_S
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
    "12-105m-meet-stereo|sammamish-source.wav|meeting|6341"
)

# ---------- Run loop ----------
echo "# Phase 8 12-run matrix results" > "$RESULTS_MD"
echo "" >> "$RESULTS_MD"
echo "Started: $(date)" >> "$RESULTS_MD"
echo "Server: $SERVER" >> "$RESULTS_MD"
echo "" >> "$RESULTS_MD"
echo "| # | name | mode | audio_s | wall_s | ratio | segs | speakers | peak_rss_mb | status |" >> "$RESULTS_MD"
echo "|---|---|---|---|---|---|---|---|---|---|" >> "$RESULTS_MD"

: > "$RAW_JSONL"

for entry in "${MATRIX[@]}"; do
    IFS='|' read -r NAME FILE MODE EXP_DUR <<< "$entry"
    echo "=== [$NAME] mode=$MODE file=$FILE ==="
    if [ ! -f "$FILE" ]; then
        echo "  FAIL: file missing"
        echo "| $NAME | $MODE | $EXP_DUR | -- | -- | -- | -- | -- | MISSING |" >> "$RESULTS_MD"
        continue
    fi
    # Submit
    T_SUBMIT=$(date +%s.%N)
    RESP=$(curl -sf -X POST "$SERVER/transcribe/file" \
        -H "Authorization: Bearer $API_KEY" \
        -F "mode=$MODE" \
        -F "file=@$FILE")
    if [ -z "$RESP" ]; then
        echo "  FAIL: submit returned empty"
        echo "| $NAME | $MODE | $EXP_DUR | -- | -- | -- | -- | -- | SUBMIT_FAIL |" >> "$RESULTS_MD"
        continue
    fi
    JOB=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))")
    echo "  job_id=$JOB"

    # Poll
    PEAK_RSS=0
    STATUS=""
    while true; do
        STATUS_JSON=$(curl -sf -H "Authorization: Bearer $API_KEY" "$SERVER/transcribe/meeting/$JOB" 2>/dev/null || echo "{}")
        STATUS=$(echo "$STATUS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null)
        # Track RSS from admin/active
        ACTIVE=$(curl -sf -H "Authorization: Bearer $API_KEY" "$SERVER/admin/active" 2>/dev/null || echo "{}")
        RSS=$(echo "$ACTIVE" | python3 -c "
import sys, json
try:
    j = json.load(sys.stdin)
    for job in j.get('jobs', []):
        if job.get('id', '').startswith('$JOB'.split('-')[0]):
            print(int(job.get('current_rss_mb', 0)))
            break
    else:
        print(0)
except: print(0)
" 2>/dev/null)
        if [ "$RSS" -gt "$PEAK_RSS" ]; then PEAK_RSS=$RSS; fi
        case "$STATUS" in
            done|failed) break ;;
        esac
        sleep 5
    done
    T_DONE=$(date +%s.%N)
    WALL=$(echo "$T_DONE - $T_SUBMIT" | bc)

    # Capture metrics
    SEGS=0; SPEAKERS=0; ERR=""
    if [ "$STATUS" = "done" ]; then
        TXT=$(curl -sf -H "Authorization: Bearer $API_KEY" "$SERVER/transcribe/meeting/$JOB/txt" 2>/dev/null || echo "")
        SEGS=$(echo "$TXT" | grep -c "^\[Speaker")
        SPEAKERS=$(echo "$TXT" | grep -oE "\[Speaker [^]]+\]" | sort -u | wc -l | tr -d ' ')
    elif [ "$STATUS" = "failed" ]; then
        ERR=$(echo "$STATUS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error','')[:60])")
    fi
    RATIO=$(echo "scale=2; $EXP_DUR / $WALL" | bc)
    echo "  status=$STATUS wall=${WALL}s ratio=${RATIO}x segs=$SEGS speakers=$SPEAKERS peak_rss=${PEAK_RSS}MB"

    echo "| $NAME | $MODE | $EXP_DUR | ${WALL}s | ${RATIO}x | $SEGS | $SPEAKERS | $PEAK_RSS | $STATUS $ERR |" >> "$RESULTS_MD"
    echo "{\"name\":\"$NAME\",\"mode\":\"$MODE\",\"file\":\"$FILE\",\"expected_dur_s\":$EXP_DUR,\"wall_s\":$WALL,\"ratio\":$RATIO,\"segs\":$SEGS,\"speakers\":$SPEAKERS,\"peak_rss_mb\":$PEAK_RSS,\"status\":\"$STATUS\",\"error\":\"$ERR\"}" >> "$RAW_JSONL"

    # Clean up job on server to save disk
    curl -sf -X DELETE -H "Authorization: Bearer $API_KEY" "$SERVER/transcribe/meeting/$JOB" >/dev/null 2>&1 || true
    sleep 3
done

echo "" >> "$RESULTS_MD"
echo "Finished: $(date)" >> "$RESULTS_MD"
echo
echo "=== DONE — full results at $RESULTS_MD ==="
cat "$RESULTS_MD"
