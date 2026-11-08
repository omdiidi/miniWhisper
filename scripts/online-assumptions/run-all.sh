#!/usr/bin/env bash
# Real-API assumption gates for Online mode (OpenAI). Costs real money.
#
# Builds WisprAltProbe, then runs `preflight` (P5 Keychain read, P1 long-clip
# report, P3 one 600 s gpt-live-transcribe commit (+ P3b at 210 s only on a
# commit error), P4 live latency report), `suite-file` (every file model on a
# short clip + one direct m4a upload) and `suite-live` (both live models on a
# short clip + one recovery case).
#
# The key is read inside the probe from the Keychain (co.wispralt.openai);
# this script never touches it. Every billable call is appended by the probe,
# before it is sent, to the spend ledger as {ts, run, probe, model, seconds,
# est_usd}. Caps (enforced by the probe): pre-flight $0.30 and suites $1 per
# run, the whole ledger $5.
#
# Exit: 0 all PASS | 1 any FAIL (halts at the first failing step) | 2 BLOCKED with no FAIL.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LEDGER="${WISPRALT_SPEND_LEDGER:-$ROOT/tmp/mission/part3/openai-spend.jsonl}"
FIXTURE="$ROOT/scripts/online-assumptions/fixtures/speech.txt"
RUN="$(date -u +%Y%m%dT%H%M%SZ)-$$"

(cd "$ROOT/client" && swift build --product WisprAltProbe) >/dev/null || { echo "run-all: probe build FAILED"; exit 1; }
BIN="$(cd "$ROOT/client" && swift build --show-bin-path)/WisprAltProbe"

final=0
for step in preflight suite-file suite-live; do
  echo "== $step (run $RUN)"
  "$BIN" "$step" --ledger "$LEDGER" --run "$RUN" --fixture "$FIXTURE"
  rc=$?
  case $rc in
    0) ;;
    2) final=2 ;;
    *) echo "run-all: $step FAILED (exit $rc); halting"; exit 1 ;;
  esac
done

TOTAL="$(python3 -c 'import json,sys
run=sys.argv[2]; t=r=0.0
for line in open(sys.argv[1]):
    line=line.strip()
    if not line: continue
    e=json.loads(line); u=float(e.get("est_usd",0)); t+=u
    if e.get("run")==run: r+=u
print(f"this run ${r:.4f}, ledger ${t:.4f}")' "$LEDGER" "$RUN" 2>/dev/null || echo "ledger empty")"
echo "run-all: exit $final; spend: $TOTAL ($LEDGER)"
exit $final
