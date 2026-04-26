# Mac mini end-to-end test plan — driven via CRD-over-DevTools

**Goal:** Validate that the Mac mini server + cloudflared survive a real reboot, that the public tunnel comes back up automatically, and that dictation + meeting paths work end-to-end against the real production stack.

**Driver:** Me, via Chrome DevTools MCP → Chrome Remote Desktop → Mac mini. You do nothing on the mini; you only need to keep the CRD page open in Chrome on the MacBook so the tab stays alive.

**Estimated runtime:** 30–45 minutes (most of it is waiting for reboot and tunnel come-back).

**Prerequisites:**
- MacBook checklist in `2026-04-26-MACBOOK-checklist.md` complete and PASSING (we don't want a broken client confusing server-side test results).
- Mac mini accessible via CRD (current page id 21 in DevTools per prior session).
- Tailscale up between MacBook and mini (sanity check the side-channel before depending on the public tunnel).
- I already have the API key cached locally: `security find-generic-password -s co.wispralt -w` works on the MacBook.

---

## What this plan validates

| Layer | Tested by phase | Pass condition |
|---|---|---|
| Server `RunAtLoad: true` survives reboot | Phase 4 | uvicorn auto-starts, /healthz 200 within ~30s of login |
| Cloudflared user-level LaunchAgent survives reboot | Phase 4 | Public tunnel reachable from MacBook within ~60s of login |
| `bootstrap-test` subcommand works | Phase 2 | Returns 200 with retries |
| `setup-cloudflared.sh` migration path works | Phase 3 | Old `sudo cloudflared service install` daemon removed; new user-level LaunchAgent loaded |
| `--token-file` probe works under pipefail (Codex fix) | Phase 3 | Plist generated with `--token-file`, NOT inlined token (assuming cloudflared ≥ 2025.4.0) |
| Token rotation works without breaking the tunnel | Phase 5 | New token applied; tunnel comes back up; healthz still 200 |
| Real dictation roundtrip via public URL | Phase 6 | Test WAV transcribes correctly; latencies are healthy |
| Cold-start from a real reboot | Phase 7 | After Apple → Restart, full path comes back in <90s |
| `/verify-autostart` slash command works | Phase 8 | All checks green |

---

## Phase 0 — Pre-flight (5 min)

Before touching the mini I confirm the local state from the MacBook:

```bash
# 1. Tailscale online + mini reachable
tailscale status --json | grep -E 'Self|HostName' | head -5

# 2. Public tunnel currently healthy (baseline before we touch anything)
KEY="$(security find-generic-password -s co.wispralt -w)"
curl -s --max-time 5 -H "Authorization: Bearer $KEY" \
  https://transcribe.integrateapi.ai/healthz
curl -s --max-time 5 -H "Authorization: Bearer $KEY" \
  https://transcribe.integrateapi.ai/readyz/dictation
curl -s --max-time 5 -H "Authorization: Bearer $KEY" \
  https://transcribe.integrateapi.ai/readyz/meeting

# 3. Capture baseline metrics
curl -s --max-time 5 -H "Authorization: Bearer $KEY" \
  https://transcribe.integrateapi.ai/metrics | head -30
```

If any of those fail, abort the test plan and triage first.

---

## Phase 1 — Sync repo to Mac mini (5 min)

Drive the mini's Terminal via CRD. Use `pbcopy` + `Cmd+V` for any command containing `|`, `>`, `<`, or `&` (CRD strips Shift modifiers).

```bash
cd ~/wispralt
git fetch origin
git status                    # Confirm clean working tree
git log --oneline -5          # Note current HEAD
git pull origin main          # Or whatever branch the new commits land on
```

**If the mini is behind** (which it will be — we haven't committed/pushed yet):
- Drive the test against the EXISTING mini state for now.
- After all MacBook tests pass and you approve a commit/push, repeat Phase 1 to land the new server-launchd.sh + setup-cloudflared.sh changes.
- THEN re-run Phases 2–8 against the updated scripts.

For this test plan I assume we're testing the BRAND-NEW scripts on the mini.

---

## Phase 2 — Server LaunchAgent reboot survival (3 min)

```bash
cd ~/wispralt

# Regenerate the plist with RunAtLoad=true
bash scripts/server-launchd.sh install

# Confirm the plist has RunAtLoad=true
plutil -p ~/Library/LaunchAgents/co.wispralt.server.plist | grep -A1 RunAtLoad

# Run the new bootstrap-test subcommand
bash scripts/server-launchd.sh bootstrap-test
```

**Pass criteria:**
- `RunAtLoad => 1` in plutil output.
- `bootstrap-test` returns "✓ server reboot-survival OK" or equivalent.
- `curl http://127.0.0.1:8000/healthz` returns 200.

---

## Phase 3 — Cloudflared LaunchAgent migration (10 min)

```bash
cd ~/wispralt

# Pre-check: capture current cloudflared state
sudo launchctl list | grep -i cloudflare
launchctl list | grep -i cloudflare
ls -la /Library/LaunchDaemons/com.cloudflare.cloudflared.plist 2>/dev/null

# Tear down the broken sudo install
sudo launchctl bootout system/com.cloudflare.cloudflared 2>/dev/null || true
sudo launchctl bootout system/cloudflared 2>/dev/null || true
sudo rm -f /Library/LaunchDaemons/com.cloudflare.cloudflared.plist
sudo rm -f /Library/LaunchDaemons/cloudflared.plist

# Verify cleanup
sudo launchctl list | grep -i cloudflare || echo "✓ no system daemon"
```

Now run the new user-level setup. **You'll need the Cloudflare tunnel token ready** — I'll prompt you when we get to this point and you paste it directly into the CRD terminal (silent prompt).

```bash
bash scripts/setup-cloudflared.sh
# Paste token at the silent prompt when asked.
```

**Pass criteria:**
- Script reports "cloudflared --token-file support: true" (validates the Codex pipefail fix on a real cloudflared install).
- Plist generated at `~/Library/LaunchAgents/co.wispralt.cloudflared.plist`.
- Plist contains `--token-file` reference, NOT inlined token (verify with `plutil -p` and grep).
- `launchctl print gui/$UID/co.wispralt.cloudflared` shows `state = running`.
- Token file at `~/.config/wispralt/cloudflare-token` exists with mode 0600.
- Tail `~/Library/Logs/WisprAlt/cloudflared.log` for "Connection registered" within 30s.

---

## Phase 4 — Real reboot survival test (15 min)

This is the only phase that REQUIRES a real reboot. Heads up: if you're using the Mac mini for anything else right now, save your work first.

```bash
# From the mini terminal, capture pre-reboot state
date '+%Y-%m-%d %H:%M:%S' > /tmp/reboot-test-start.txt
launchctl print gui/$UID/co.wispralt.server | head -5
launchctl print gui/$UID/co.wispralt.cloudflared | head -5
```

I drive the mini's Apple menu → Restart… via CRD. Confirm the restart dialog and click Restart.

While the mini reboots, **I monitor from the MacBook side** (CRD will disconnect — that's expected):

```bash
# Loop polling the public tunnel from the MacBook
KEY="$(security find-generic-password -s co.wispralt -w)"
START="$(date +%s)"
echo "Polling https://transcribe.integrateapi.ai/healthz from $(date '+%H:%M:%S')"

while :; do
    NOW="$(date +%s)"
    ELAPSED=$((NOW - START))
    RESP="$(curl -s --max-time 3 -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $KEY" \
        https://transcribe.integrateapi.ai/healthz 2>/dev/null)"
    if [ "$RESP" = "200" ]; then
        echo "✓ tunnel + server back at $(date '+%H:%M:%S') (${ELAPSED}s after restart)"
        break
    fi
    if [ "$ELAPSED" -gt 300 ]; then
        echo "✗ FAIL: tunnel did not come back in 5 minutes"
        break
    fi
    echo "  ${ELAPSED}s: HTTP $RESP — still down"
    sleep 5
done
```

**Pass criteria:**
- Tunnel comes back within 90 seconds of the mini's actual login (typically the mini takes 30–60s to boot to login screen, then 10–30s for cloudflared to dial out).
- `/readyz/dictation` and `/readyz/meeting` both 200 within 30s after `/healthz`.
- No manual intervention needed.

After it comes back, I drive CRD to reconnect to the mini and verify:

```bash
# On the mini after reboot
date '+%Y-%m-%d %H:%M:%S'
launchctl print gui/$UID/co.wispralt.server | head -10
launchctl print gui/$UID/co.wispralt.cloudflared | head -10
tail -30 ~/Library/Logs/WisprAlt/server.log
tail -30 ~/Library/Logs/WisprAlt/cloudflared.log
```

**Fail modes I'll flag:**
- Server LaunchAgent shows `state = exited` instead of `running` → uvicorn crashed on startup, check server.err.log.
- Cloudflared shows `state = running` but tunnel still 5xx → token may be invalid or revoked.
- Both running but `/readyz/dictation` returns 503 → Parakeet model failed to load (HF token expired? weights missing?).

---

## Phase 5 — Token rotation test (5 min)

Validates that the rotation procedure documented in DEPLOYMENT-NOTES actually works on a live system.

**For this test, we need a NEW Cloudflare tunnel token.** If you don't have one ready, skip to Phase 6 and come back to this later.

```bash
cd ~/wispralt

# Capture current state
launchctl print gui/$UID/co.wispralt.cloudflared | grep state

# Tear down
launchctl bootout gui/$UID/co.wispralt.cloudflared

# Confirm the public tunnel is DOWN
curl -s --max-time 5 -o /dev/null -w '%{http_code}\n' \
    https://transcribe.integrateapi.ai/healthz
# Expect: 502 or timeout — that's the proof we actually killed it.

# Atomically replace the token file
read -r -s -p "New Cloudflare Tunnel token: " NEW_TOKEN; echo
TMP="$(mktemp)"
chmod 0600 "$TMP"
printf '%s' "$NEW_TOKEN" > "$TMP"
mv "$TMP" ~/.config/wispralt/cloudflare-token
unset NEW_TOKEN

# Re-bootstrap
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/co.wispralt.cloudflared.plist
sleep 5
launchctl print gui/$UID/co.wispralt.cloudflared | grep state
```

Then poll from the MacBook:
```bash
KEY="$(security find-generic-password -s co.wispralt -w)"
curl -s --max-time 10 --retry 6 --retry-delay 2 \
    -H "Authorization: Bearer $KEY" \
    https://transcribe.integrateapi.ai/healthz
```

**Pass criteria:** new token works, tunnel reachable within ~30s.

---

## Phase 6 — Real dictation roundtrip via public URL (5 min)

From the MacBook, send the known-good test WAV through the actual public stack.

```bash
KEY="$(security find-generic-password -s co.wispralt -w)"

# Use the test audio from prior session. If absent, generate a tiny silence WAV.
if [ ! -f /tmp/test-dictation.wav ]; then
    # Generate a tiny silence WAV (1s, 16kHz, mono, Int16) — server will reject
    # this with empty transcription, but we're testing the path, not content.
    python3 -c "
import wave, struct
with wave.open('/tmp/test-dictation.wav', 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b'\x00\x00' * 16000)
"
fi

# Round-trip test
time curl -s --max-time 30 \
    -H "Authorization: Bearer $KEY" \
    -F "file=@/tmp/test-dictation.wav;type=audio/wav" \
    https://transcribe.integrateapi.ai/transcribe/dictate
```

**Pass criteria:**
- HTTP 200 with valid JSON: `{"text": "...", "duration_ms": ...}`.
- `duration_ms` is a float, not a string (validates the Float32 fix from prior session).
- End-to-end latency under 2 seconds for a 1-second WAV.

If you have a real voice recording to send, that's the better test:
```bash
# If you've got a voice memo, convert + send it
ffmpeg -y -i ~/Desktop/your-voice-memo.m4a -ar 16000 -ac 1 -c:a pcm_s16le /tmp/voice.wav 2>/dev/null
curl -s -H "Authorization: Bearer $KEY" \
    -F "file=@/tmp/voice.wav;type=audio/wav" \
    https://transcribe.integrateapi.ai/transcribe/dictate
```

---

## Phase 7 — `/verify-autostart` slash command end-to-end (3 min)

Run the new slash command we just shipped:

```bash
# In Claude Code on the MacBook, invoke:
/verify-autostart
```

Or, manually run its checks:

```bash
# Mac mini side (drive via CRD)
bash scripts/server-launchd.sh bootstrap-test

# Cloudflared restart cycle + retry poll
launchctl bootout gui/$UID/co.wispralt.cloudflared
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/co.wispralt.cloudflared.plist

# From MacBook, retry-poll the public URL
for i in $(seq 1 15); do
    if curl --max-time 3 -s https://transcribe.integrateapi.ai/healthz | grep -q ok; then
        echo "OK at iteration $i (${i} * 2s)"
        break
    fi
    sleep 2
done
```

**Pass criteria:** all green. Note the actual recovery time as a baseline for future reboots.

---

## Phase 8 — Edge case stress (10 min, optional)

If everything above passed, push it harder:

**8a. Network down at boot (simulated)**
- On the mini: turn off Wi-Fi via menubar.
- Reboot the mini.
- After login, confirm cloudflared is in KeepAlive retry loop:
  ```bash
  tail -50 ~/Library/Logs/WisprAlt/cloudflared.err.log
  launchctl print gui/$UID/co.wispralt.cloudflared | grep state
  ```
- Turn Wi-Fi back on.
- Confirm tunnel self-heals within 30s without manual intervention.

**8b. Server crash recovery**
```bash
# On the mini — force-kill uvicorn
pkill -9 -f 'uvicorn.*wispralt_server' || true
sleep 5
# KeepAlive should have re-launched it
launchctl print gui/$UID/co.wispralt.server | grep state
curl -s --max-time 5 http://127.0.0.1:8000/healthz
```

**8c. Token file corruption**
```bash
# Rename the token file to simulate corruption
mv ~/.config/wispralt/cloudflare-token ~/.config/wispralt/cloudflare-token.bak

# Restart cloudflared — should fail and log auth error
launchctl kickstart -k gui/$UID/co.wispralt.cloudflared
sleep 10
tail -30 ~/Library/Logs/WisprAlt/cloudflared.err.log

# Restore
mv ~/.config/wispralt/cloudflare-token.bak ~/.config/wispralt/cloudflare-token
launchctl kickstart -k gui/$UID/co.wispralt.cloudflared
sleep 10
launchctl print gui/$UID/co.wispralt.cloudflared | grep state
```

---

## Phase 9 — Final report

I compile a single report covering:
- Pass/fail per phase
- Cold-boot recovery time (real number, e.g., 47s)
- Token-rotation recovery time
- Server p50 latency post-reboot vs baseline
- Any KeepAlive hot-loops, retry cascades, or unexpected log output
- A go/no-go recommendation: "ship it" or "fix X first."

---

## Logistics — what I need from you to start

1. **MacBook checklist green** (paste me your "1-7 all good" reply when done).
2. **The Mac mini accessible via CRD** — confirm the existing CRD page in DevTools is still alive (or open a new one and tell me).
3. **A new Cloudflare tunnel token** (only if you want to run Phase 5). Generate at https://one.dash.cloudflare.com/ → Networks → Tunnels → your tunnel → Configure → Token. If you don't want to rotate, we skip Phase 5.
4. **Confirmation that you're OK with a real reboot of the Mac mini** during Phase 4 (it's the only phase that touches the host's run state).
5. **Approval to commit + push the implementation** before Phase 1 (otherwise we test against the OLD scripts on the mini, which defeats the point).

When you're ready, just say "go" and I'll start at Phase 0.
