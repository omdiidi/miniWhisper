---
description: Non-destructive reboot-survival smoke test for server LaunchAgent, cloudflared LaunchAgent, and client login-launch via SMAppService.
---

# /verify-autostart

Non-destructive reboot-survival test. Does NOT reboot anything. Simulates a restart by cycling the LaunchAgents with `bootout`/`bootstrap`, then checks the client login-launch entry.

## Mac mini side

### Step 1 — Server LaunchAgent bootstrap-test

Run on the Mac mini:

```bash
bash scripts/server-launchd.sh bootstrap-test
```

This subcommand does a `launchctl bootout` → `launchctl bootstrap` cycle on `co.wispralt.server`, then polls `http://127.0.0.1:8000/healthz` until it returns 200 or times out. Expect output ending with `✓ server reboot-survival OK`.

### Step 2 — Cloudflared LaunchAgent cycle + public healthz poll

Run on the Mac mini:

```bash
launchctl bootout "gui/$UID/co.wispralt.cloudflared" && \
  launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/co.wispralt.cloudflared.plist"
```

Then poll the public endpoint with retry (run from any Mac — substituting the actual tunnel URL):

```bash
for i in $(seq 1 15); do
  curl --max-time 3 -s https://transcribe.integrateapi.ai/healthz | grep -q ok && echo OK && break
  sleep 2
done
```

If the loop prints `OK`, the tunnel survived the cycle. If it exits without `OK`, check `~/Library/Logs/WisprAlt/cloudflared.err.log` on the Mac mini for auth errors.

## Client side

Three independent checks. Run on the MacBook (or whichever client Mac you are verifying).

### Check 1 — launchctl disabled list (stable, recommended)

```bash
launchctl print-disabled "gui/$UID" | grep '"co.wispralt.WisprAlt" => disabled'
```

This prints one line if WisprAlt is explicitly in the disabled list. The entry should NOT appear — if it does, go to System Settings → General → Login Items & Extensions and enable WisprAlt.

### Check 2 — sfltool dumpbtm (private tool, cross-check only)

```bash
sfltool dumpbtm | grep co.wispralt.WisprAlt
```

Note: `sfltool` is a private Apple tool. Its output format is unstable across macOS versions (Ventura, Sonoma, Sequoia all differ). Use this as a fast cross-check, not a definitive test. A missing result here does not confirm failure.

### Check 3 — Process running, with retry

```bash
ok=0
for i in $(seq 1 10); do
  if pgrep -lf '/Applications/WisprAlt.app/Contents/MacOS/WisprAlt' >/dev/null; then
    ok=1; break
  fi
  sleep 1
done
[ $ok -eq 1 ] && echo "✓ menubar app running" || echo "✗ menubar app not running after 10s"
```

The retry loop handles the asynchronous launch delay. If the app is not running after 10 seconds, open System Settings → General → Login Items & Extensions → confirm WisprAlt is enabled and not grayed out.

## If any check fails

- **Server not up**: `bash scripts/server-launchd.sh status` then `cat ~/Library/Logs/WisprAlt/server.err.log`.
- **Cloudflared not up**: `launchctl print "gui/$UID/co.wispralt.cloudflared"` — check `state = running`. Auth errors in `~/Library/Logs/WisprAlt/cloudflared.err.log` mean the token file is wrong or expired; re-run `bash scripts/setup-cloudflared.sh`.
- **Client not auto-launching**: SMAppService may need approval. System Settings → General → Login Items & Extensions → enable WisprAlt. If the entry is missing entirely, run `sfltool resetbtm` and reboot (Apple-recommended for stale Launch Services records).
