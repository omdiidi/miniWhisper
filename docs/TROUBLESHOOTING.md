---
title: Troubleshooting
---

# Troubleshooting

Each entry follows the format: **Symptom → Diagnosis → Fix**.

---

## Server Issues

### HuggingFace token denied (401 during model download)

**Symptom:** `download-models.sh` or server startup fails with a 401 or "gated model" error mentioning `pyannote/speaker-diarization-3.1` or `pyannote/segmentation-3.0`.

**Diagnosis:** Either the token is invalid, or you have not accepted the model license terms for one or both gated models.

**Fix:**
1. Accept terms for both models (must be done while logged in):
   - [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
   - [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
2. Verify your token is valid: `huggingface-cli whoami` (should print your username).
3. If you regenerated the token after setup, update `HF_TOKEN` in `server/.env` and restart the server.

---

### Native wheel build fails (Xcode tools)

**Symptom:** `uv sync` fails with a build error mentioning missing C++ headers, missing compiler, or `clang` not found. (Historically also triggered by `CTranslate2`; that dep was removed in Phase 10 but the symptom can still surface for other native wheels like `torch` or `pyannote`.)

**Diagnosis:** Xcode Command Line Tools are missing or outdated.

**Fix:**
```bash
xcode-select --install
```
Wait for installation to complete, then re-run `uv sync`. If already installed, update it:
```bash
sudo rm -rf /Library/Developer/CommandLineTools
xcode-select --install
```

---

### cloudflared port conflict or service fails to start

**Symptom:** `sudo cloudflared service install` prints an error, or `launchctl list | grep cloudflared` shows an error exit code.

**Diagnosis:** Cloudflare tunnels use outbound port 7844 (not an inbound port). The issue is usually a pre-existing `cloudflared` service installation or a corporate firewall blocking outbound 7844.

**Fix:**
1. Check for existing service: `launchctl list | grep cloudflared`
2. If present, uninstall: `sudo cloudflared service uninstall`
3. Re-run `setup-cloudflared.sh`.
4. If a firewall blocks port 7844, confirm with your network admin that outbound HTTPS to Cloudflare edge nodes is allowed.

---

### WAV upload fails with 413 (Content Too Large)

**Symptom:** Meeting WAV upload returns HTTP 413.

**Diagnosis:** The recording exceeds `MAX_UPLOAD_BYTES` (default 2 GiB, which covers ~4h of dual-channel 16kHz Float32 audio). Alternatively, the Cloudflare free-tier network-level limit (~100 MB, community-reported) was reached before the server responded.

**Fix:**
- The client should warn the user at 60 minutes and stop automatically at 90 minutes. A 90-minute recording is ~460 MB.
- For recordings over ~100 MB, the Cloudflare free tier may truncate the body at the network level before it reaches the server. Consider upgrading your Cloudflare plan or splitting long meetings.
- Do not increase `MAX_UPLOAD_BYTES` above the Cloudflare limit for your plan — the server limit is moot if Cloudflare drops the connection first.

---

### WAV upload fails with 422 "truncated" or MD5 mismatch

**Symptom:** Meeting WAV upload returns HTTP 422 with a message about "truncated" or "Content-MD5 mismatch".

**Diagnosis:** Cloudflare's free-tier body limit (~100 MB, community-reported) silently truncated the upload before it reached the server. The server detects this via `Content-MD5` header verification.

**Fix:**
1. Test with a short recording (< 10 MB) to confirm the tunnel is working.
2. If short uploads succeed, the issue is the Cloudflare plan body limit. Options:
   - Upgrade to a Cloudflare paid plan.
   - Connect directly (VPN or LAN) when recording sessions expected to exceed 100 MB.

---

### `.env` permissions warning at startup

**Symptom:** Server log shows `SECURITY WARNING: server/.env has mode 6XX, expected 0600`.

**Diagnosis:** The `.env` file was created with overly permissive mode (e.g. 0644 set by a text editor).

**Fix:**
```bash
chmod 600 server/.env
```
Verify: `ls -la server/.env` should show `-rw-------`.

---

### 429 "Meeting in progress"

**Symptom:** `POST /transcribe/meeting` returns 429 with `{"error": "Another meeting is currently being transcribed"}`.

**Diagnosis:** WisprAlt processes one meeting at a time (enforced by `asyncio.Semaphore(1)` in `jobs/runner.py`).

**Fix:**
1. Check `GET /metrics` for `meeting.active: true` and `meeting.current_eta_s`.
2. Wait for the current job to complete, then resubmit.
3. If `meeting.active` is `false` but you still get 429, check `memory.available_mb` — the OOM guard also returns 429 if less than 2 GiB of RAM is free.

---

### High dictation latency

**Symptom:** Dictation round-trip is consistently > 500ms after warmup.

**Diagnosis steps:**
1. Check `GET /metrics` for `meeting.active: true` — if a meeting is running, dictation competes for unified memory.
2. Check `GET /readyz/dictation` for the `X-Dictation-Degraded: true` header — this confirms memory pressure.
3. Check `parakeet.p50_ms` in `/metrics` — if > 300ms, the Mac mini may be thermally throttled.

**Fix:**
- Wait for the meeting job to complete.
- If consistently slow without a meeting running, check Activity Monitor for other processes consuming unified memory (ML workloads, large Xcode builds, etc.).

---

### Server disk full (507)

**Symptom:** `POST /transcribe/meeting` returns 507. Server log shows "insufficient storage".

**Diagnosis:** The staging directory or meeting output directory is on a full or nearly full volume.

**Fix:**
1. Check `GET /metrics` for `disk.free_gb` and `disk.staging_count`.
2. Clean up stale staging files: `ls -lh $STAGING_DIR` and remove any `.wav` files older than a few days.
3. Clean up old meeting outputs from `MEETING_OUTPUT_DIR`.
4. The disk guard requires free space ≥ 1.5× the upload size before accepting a WAV.

---

## Client Issues

### FN tap not detected

**Symptom:** Holding FN or triple-tapping FN has no effect. The menubar icon does not change.

**Diagnosis:** Input Monitoring permission is not granted, or (on macOS 14.4+) the app has not been restarted after the permission was granted.

**Fix:**
1. Open **System Settings → Privacy & Security → Input Monitoring**.
2. Ensure WisprAlt is listed and the toggle is **on**.
3. On macOS 14.4+: `CGRequestListenEventAccess` returns `true` but the real grant requires a process restart. The app will show a "Quit and Reopen Required" sheet — click **Quit Now** and relaunch.

---

### Dictation returns empty text or random one-word hallucinations

**Symptoms:**
- FN-hold dictation completes (icon returns to idle) but no text is injected.
- Server returns `duration_ms=0.0` for clearly-audible speech, OR
- Server returns one-word random hallucinations like "Yeah." / "Proof." / "Okay." for an actual sentence.

**Two distinct historical root causes — both fixed:**

**Cause 1 — `AVAudioConverter` downmix bug.** The first version of `DictationRecorder` used `AVAudioConverter` to downmix to 16 kHz mono Float32 client-side. AVAudioConverter's default channel-mix sums channels without averaging, producing peak floats ≈ 3.97 on stereo input. Server's libsndfile rejected the WAV → `duration_ms=0`. **Fixed:** removed AVAudioConverter, kept native sample rate and channel count.

**Cause 2 — `AVAudioFile` Float→Int16 amplification.** The second version asked AVAudioFile.write to convert Float32 buffers to Int16 PCM inline. AVAudioFile's internal converter applies a buggy ~140x normalization: a clean 0.24-peak voice landed in the WAV as a 32750/32767 (rail-clipped) Int16. Audio "decoded" fine on the server side (right format, right size), but Parakeet saw heavily distorted speech and returned random one-word hallucinations. **Fixed:** the recorder now writes **Float32 PCM** at native rate, format-matched to the tap buffer so AVAudioFile streams the float bytes verbatim without any conversion.

**How to verify the fix is working:**
1. Console.app, filter by `co.wispralt`.
2. FN-hold and speak a sentence in TextEdit.
3. Look for a log line like:
   ```
   DictationRecorder: stopped, 100800 frames at 48000Hz 1ch. peak=0.286
   ```
4. **Healthy ranges:** `peak` between **0.05 and 0.95** (Float32 normalized magnitude). `0.1–0.5` is typical for normal speech.
5. **Red flags:**
   - `peak > 0.95` followed by `(CLAMP ENGAGED — mic delivered out-of-range floats)` → input chain is over-amplifying. Check Control Center → Mic Mode → set to "Standard" (not Voice Isolation / Wide Spectrum). Check input volume (`osascript -e "input volume of (get volume settings)"`) and lower it.
   - `peak < 0.005` → microphone effectively silent. Check the right device is the default input (`Settings → Sound → Input`).
   - No log line at all → recording was empty. Client surfaces this as `DictationError.emptyRecording` (silent — no toast, since it would be noisy on accidental FN taps).
   - "Could not parse the server response" error → server returned `duration_ms` as a float (e.g. `119.94`) but the client struct decoded it as `Int`. Fixed; client struct is now `Double`.

**Related errors:**
- `DictationError.writeFailed` — toasts if a tap-side `AVAudioFile.write` fails (disk full, ENOSPC). These were silently swallowed in the original implementation.
- `DictationError.emptyRecording` — thrown when fewer than ~50ms of frames captured (FN tap-and-release without speech, or mic permission revoked mid-recording). Logged but not toasted.

---

### Text injection in Electron apps, iMessages, and Pane (clipboard fallback by design)

**Symptom:** Dictation completes successfully (status returns to idle) but the focused field initially looks unchanged in Electron apps (VS Code, Slack, Discord, Cursor, Notion, Figma, Linear, …) or in **Apple Messages** (`com.apple.MobileSMS`) and **Pane** (`com.dcouple.pane`).

**Diagnosis:** These apps do not honour `AXUIElementSetAttributeValue(kAXSelectedTextAttribute)` — either they silently no-op the write or they don't expose a usable `kAXValueAttribute`. `AccessibilityInjector.tryInsertWith` detects this by reading `kAXValueAttribute` before and after the write; if both reads return the same value (or fail), the predicate `didInjectionLand(...)` returns `false` and `TextInjector` falls through to `ClipboardInjector.injectViaCmdV` which writes the text to `NSPasteboard.general`, synthesises ⌘V, and restores the original pasteboard 200 ms later. iMessages and Pane are expected to take this path — it's not a bug.

**Diagnostic log line format:**

```
[inject] inject: target_at_start=<bundleID>/pid=<n>/role=<r>/subrole=<s>
[inject] Text injected via AX. target=…           ← AX path landed
[inject] Text injected via Cmd+V. target=…        ← clipboard fallback path
```

Exactly one `info` line — `via AX` *or* `via Cmd+V`, never both — should appear per dictation event. (Multiple `debug` lines are normal.)

**Fix:** The clipboard fallback should activate automatically. If text still doesn't appear:
1. Open **Console.app**, filter by `co.wispralt`, and look for `[inject]` entries to see which path was taken and against which `target_at_start`.
2. Ensure Accessibility permission is granted (Step 1 of the permission wizard).
3. Confirm the focused app accepts ⌘V from a synthetic `CGEvent`. A handful of niche apps (custom event loops) ignore it; record the bundle ID in an issue.

**Honest caveat about clipboard managers (Maccy, Raycast Clipboard History, Paste.app, Alfred Clipboard).** These tools watch `NSPasteboard.general` and copy every change into their history. WisprAlt restores your original clipboard 200 ms after pasting, **but** if a clipboard manager wrote during that window the restore is skipped (we won't stomp something the user just copied) and your previous clipboard contents are lost. If you rely on clipboard history, avoid selecting items from it immediately after dictating into a clipboard-fallback app.

**Web password caveat.** The native secure-field gate (next section) catches AppKit `NSSecureTextField` and SwiftUI `SecureField`, but **web password inputs** (Safari, Chrome, Electron `<input type="password">`) usually do **not** surface `kAXSubroleAttribute == AXSecureTextField`, so the gate cannot detect them. **Do not dictate web passwords** — they may transit the system pasteboard where any clipboard manager could capture them.

**Known limitation: secure-field skip notification debounce.** When dictation is refused due to a secure field, you'll see one local notification "Dictation Skipped — `<bundleID>` is asking for a password. Type the value manually." Subsequent skips against the same focus within 60 seconds are logged but not re-notified, to avoid spamming Notification Center if the field stays focused (e.g. unlocking 1Password).

**Known limitation: repeat dictation within 200 ms.** Two FN-tap dictations in rapid succession can interact with the clipboard restore window: the second snapshot captures the first dictation's text rather than your true original, so when both restores complete the clipboard ends up holding the first dictation's text. Avoid back-to-back dictations if your clipboard contents matter.

**Known limitation: post-capture focus shift on the clipboard path.** The secure-field gate captures focus context once at the top of `TextInjector.inject` and uses it to decide whether to refuse and which AX element to write to. The synthesised ⌘V, however, goes to whatever app is focused at the moment the `CGEvent` posts — which is usually the same app, but if you click into a different field (especially a password field) during the brief window inside `inject(_:)` between focus capture and the synthesised ⌘V (typically sub-millisecond, up to a few hundred ms if the AX-attempt times out on a hung target), the paste will land in the new focus. The gate cannot prevent this. If you need to switch fields right after dictating, wait for the text to land first.

---

### Meeting upload progress stuck

**Symptom:** The menubar shows "Uploading" for an unusually long time (more than a few minutes for a typical 30-minute meeting).

**Diagnosis:** Either the Cloudflare Tunnel body limit was hit and the upload stalled, or the network connection is slow.

**Fix:**
1. Open Activity Monitor → Network tab; check upload bytes/sec for WisprAlt.
2. If upload speed is near zero, the connection may have been dropped by Cloudflare. Cancel and retry.
3. Check `MEETING_OUTPUT_DIR` free space with `doctor.sh`.

---

### "Quit and Reopen Required" sheet appeared

**Symptom:** A blocking sheet titled "Quit and Reopen Required" appeared during the permission wizard.

**Diagnosis:** This is expected on macOS 14.4+ after granting Input Monitoring. The CGEventTap grant requires a process restart to take effect.

**Fix:** Click **Quit Now**, then relaunch WisprAlt from `/Applications`. The permission wizard will resume at Step 3 (Microphone).

---

### Speaker rename failed with "name conflict"

**Symptom:** Attempting to rename a speaker shows an error about a name conflict.

**Diagnosis:** The chosen name is already used by another speaker in the same transcript. `TranscriptDocument.renameSpeaker(raw:to:)` throws `.speakerNameConflict` when `display_name` collision is detected.

**Fix:** Choose a unique name that is not already assigned to another speaker in this transcript.

---

### Meeting stuck in "Processing" indefinitely

**Symptom:** The menubar shows "Processing" for far longer than expected (more than twice the recording duration), and the job never transitions to done.

**Diagnosis:** The poll loop in `MenuBarController` enforces a deadline of `max(2 × recording_duration, 600 s)`. When the deadline elapses the client automatically cancels the job via `DELETE /transcribe/meeting/{job_id}` and surfaces a `pollTimedOut` error notification. If you see the error, the server-side pipeline likely crashed or was killed mid-run.

**Fix:**
1. Check `GET /metrics` for `meeting.active` and `meeting.active_job_id`. If active is `false` but the client was still polling, the runner task died silently — inspect `~/Library/Logs/WisprAlt/server.log` for Python tracebacks.
2. Restart the server (`scripts/server-launchd.sh restart`). At startup, `recover_orphans` will mark the abandoned job as `failed` and clean up the staging WAV.
3. If the server is healthy but the job consistently times out for long recordings, consider whether available RAM (`memory.available_mb` in `/metrics`) was below 2 GiB when the job started.

---

### Auto-update failed

**Symptom:** An error notification from WisprAlt says the automatic update could not be applied, or the app's "Check for Updates…" menu item shows an error badge.

**Diagnosis:** `SparkleController` implements `updater(_:didAbortWithError:)` and logs the failure to `~/Library/Logs/WisprAlt/client.log`. Common causes: network error fetching the appcast, EdDSA signature mismatch (self-built binary with wrong key pair), or Sparkle was unable to replace the app bundle (SIP / permissions issue).

**Fix:**
1. Open **Console.app**, filter by `co.wispralt`, and look for `[sparkle]` error entries — the Sparkle error description is logged there.
2. If the error mentions "signature invalid": for self-built versions, confirm that `SPARKLE_ED_PRIVATE_KEY` in your GitHub Actions secrets matches the `SUPublicEDKey` embedded in `Info.plist`.
3. If the error mentions "permission denied" or "code signing": ensure WisprAlt lives in `/Applications` and is owned by your user account (`chown -R $(whoami) /Applications/WisprAlt.app`).
4. As a last resort, download the latest release DMG from the project releases page and replace the app manually.

---

### Meeting transcript has a "warnings" field with "mono input"

**Symptom:** The downloaded meeting transcript JSON contains a `"warnings"` array with an entry like `"mono input — dual-channel mode unavailable"`. Speaker diarization may be less accurate than expected.

**Diagnosis:** `MeetingRecorder` is supposed to produce a 2-channel WAV (ch1 = mic, ch2 = system audio). The server's `pipeline.py` probes the file at the start of processing and detected only 1 channel. This usually means `SCStream` did not deliver system-audio frames during the recording (e.g. screen capture permission was denied or no system audio was playing), so `AlignedRingBuffer` had nothing to mix into channel 2 and the WAV was written as mono.

**Fix:**
1. Open **System Settings → Privacy & Security → Screen & System Audio Recording** and confirm WisprAlt is listed and enabled.
2. On macOS 14.4+, this permission requires a process restart after granting — use the in-app permission wizard (Settings → Permissions).
3. Re-record the meeting after confirming the permission is active. If system audio is intentionally absent (e.g. in-person meeting with no system playback), the mono path is expected and diarization will run on mic audio only — quality may be lower for multi-speaker scenarios.

---

### Sparkle update not appearing or blocked

**Symptom:** "Check for Updates…" reports no update, or the update sheet is deferred.

**Diagnosis (deferred):** Sparkle updates are intentionally deferred while a meeting recording is active (`SparkleController` checks `MeetingRecorder.isActive`). This is by design to prevent interrupting an in-progress recording.

**Fix (deferred):** Stop the current recording, then check for updates again.

**Diagnosis (not appearing):** The appcast URL may be unreachable, or the Sparkle EdDSA signature may not match the embedded `SUPublicEDKey` in `Info.plist` (affects self-built versions).

**Fix (not appearing):** Check network connectivity to GitHub Pages. For self-built versions, ensure `SPARKLE_ED_PRIVATE_KEY` in your GitHub secrets matches the public key in `Info.plist`.

---

## Server Auto-start and Cloudflare Tunnel

### Cloudflared not running after Mac mini reboot

**Symptom:** The public endpoint (`https://transcribe.yourdomain.com`) is unreachable after a reboot or power cycle.

**Diagnosis:**
```bash
launchctl print gui/$UID/co.wispralt.cloudflared
```
Look for `state = running`. If the service is absent from the output, the LaunchAgent is not loaded.

**Fix:**
1. If the service is absent: re-run `bash scripts/setup-cloudflared.sh` to regenerate and reload the plist.
2. If the service is present but `state = running` says "waiting" or keeps cycling: cloudflared is crashing and KeepAlive is restarting it. Check stderr first:
   ```bash
   tail -50 ~/Library/Logs/WisprAlt/cloudflared.err.log
   ```
   Auth errors (invalid token, tunnel not found) appear here. **Check the log before assuming a process or network fault** — a KeepAlive hot-loop is almost always a bad token.

---

### Mac mini rebooted with no internet

**Symptom:** Mac mini rebooted while the network was down. cloudflared didn't come up.

**Diagnosis:** This is expected and self-healing. The LaunchAgent uses `KeepAlive: {NetworkState: true}` — launchd waits for a network interface to come up before starting cloudflared. Once the network returns, launchd starts the process automatically. `ThrottleInterval: 10` enforces at most one restart per 10 seconds during flapping.

**Fix:** No manual intervention needed. Wait ~30 seconds after the network returns, then check `launchctl print gui/$UID/co.wispralt.cloudflared | grep state`.

---

### Token file missing or corrupted

**Symptom:** cloudflared is restarting repeatedly (KeepAlive loop) with auth errors in the log. Typical log line: `Couldn't start tunnel` or `failed to authenticate`.

**Diagnosis:**
```bash
ls -la ~/.config/wispralt/cloudflare-token
```
Confirm the file exists and is mode 0600. If missing or zero-length, the token was not written correctly during setup.

**Fix:** Recreate the token file via the rotation procedure in [DEPLOYMENT-NOTES.md](DEPLOYMENT-NOTES.md) under "Cloudflared LaunchAgent — Token rotation". Use Procedure A (modern) or Procedure B (legacy) depending on your cloudflared version.

---

## Client Auto-start

### Client menubar app didn't appear after login

**Symptom:** After logging in, the WisprAlt menubar icon does not appear. Opening the app manually from `/Applications` works fine.

**Diagnosis:**
1. **System Settings → General → Login Items & Extensions** — check whether WisprAlt is listed and enabled.
2. If the toggle shows "requires approval", it is in a pending state waiting for user confirmation.
3. If the entry is not listed at all, the SMAppService registration may be stale after an OS upgrade or a `resetbtm` event.

**Fix:**
- If "requires approval": toggle it on in System Settings → Login Items & Extensions.
- If not listed: run `sfltool resetbtm` in Terminal, then reboot. This clears the Launch Services BTM (Background Task Management) database — Apple's recommended recovery for stale login-item entries. On next launch, WisprAlt re-registers automatically.

---

### Friend's Mac shows "WisprAlt cannot be opened because the developer cannot be verified"

**Symptom:** Gatekeeper dialog blocks launch on a friend's Mac (or on your own Mac after downloading a DMG).

**Diagnosis:** This is a first-time Gatekeeper quarantine warning. Apple Development-signed builds are not notarized (no Apple Developer Program enrollment), so Gatekeeper flags them on first open from a download.

**Fix (command line):**
```bash
xattr -dr com.apple.quarantine /path/to/WisprAlt.app
open /path/to/WisprAlt.app
```

**Fix (Finder):** Right-click the app → **Open** → click **Open** (not just double-clicking). The dialog will have an Open button the second time.

This is a one-time step per version. After the app is approved, all subsequent launches and login-launches open silently.

---

### TCC prompts returned out of nowhere — I didn't rebuild

**Symptom:** Accessibility, Input Monitoring, Microphone, or Screen Recording prompts appeared even though you haven't installed a new version.

**Diagnosis:** This is most likely the annual Apple Development certificate renewal. Xcode auto-renews the cert while signed in, but the renewed cert has a new SHA-1 and a new Designated Requirement — macOS TCC sees the next launch as a new app identity.

**To confirm:**
```bash
security find-certificate -c "Apple Development:" -p login.keychain | \
  openssl x509 -noout -dates
```
A "Not Before" date within the past few days confirms a recent renewal.

**Fix:** Same as after any rebuild — run the canonical TCC reset and re-grant:
```bash
tccutil reset Accessibility   co.wispralt.WisprAlt
tccutil reset ListenEvent     co.wispralt.WisprAlt
tccutil reset ScreenCapture   co.wispralt.WisprAlt
tccutil reset Microphone      co.wispralt.WisprAlt
```
Then reopen WisprAlt and grant all four permissions. This happens at most once a year.

---

## Multi-tenant auth and admin UI

### API key rejected (employee install)

**Symptom:** Employee runs the `install.sh` curl one-liner (see [INSTALL.md](INSTALL.md)) with their texted token in `WISPRALT_API_KEY`, the app launches, and Test Connection returns `401 Invalid bearer token`. Or dictation worked previously and now starts returning 401.

**Diagnosis ladder:**

1. **Was the token revoked?** Open `/admin/users` on the Mac mini admin UI. If their row has a non-null `revoked_at`, this is expected — text them a new token via the **Mint** flow.
2. **Cache TTL window.** Token state is cached in-process for 60 seconds (`TokenCache._TTL_S`). If the row was just rotated, cache hits for the OLD hash will keep failing for up to 60s. Wait one minute, retry. For instant lockout: restart the launchd agent (`bash scripts/server-launchd.sh restart`).
3. **Can the server reach Postgres?**
   ```bash
   curl -s --max-time 4 https://transcribe.<your-domain>/healthz
   tail -100 ~/Library/Logs/WisprAlt/server.log | grep -i 'postgres\|asyncpg\|db_pool'
   ```
   If the log shows `Postgres unavailable at startup; only break-glass admin will work`, the asyncpg pool failed to initialize. Check `SUPABASE_DATABASE_URL` in `server/.env` (mode 0600), check Supabase project status, restart the agent.
4. **Right token?** A token's plaintext is shown **once** by `/admin/users/<id>/mint`. If the employee misplaced it, mint a new one — there is no recovery for the old plaintext.

---

### Admin UI returns 401 / 403

**Symptom:** Browsing to `/admin/` or curling `/admin/users` returns 401 or 403.

**Diagnosis:**

- **401** means the bearer / cookie was missing or invalid. Check that you're sending `Authorization: Bearer <token>` (curl) or that the `wispralt_admin_token` cookie is set in your browser (visit `/admin/login` to set it).
- **403** means the bearer is valid but the user's `role` is not `'admin'`. Open `/admin/users` (with an admin token) and confirm — only admin-role tokens can access the rest of the UI.

**Fix:**

- For 401 from a browser: visit `/admin/login`, paste the admin token, submit. The cookie is set with `max_age=8h`.
- For 403: use the admin token, not an employee token. If you don't have an admin token in hand, the env-var `WISPRALT_API_KEY` from `server/.env` is the break-glass admin and resolves to the seeded admin row (or to the synthetic `User(id=-1)` if Postgres is unreachable).
- If you see **503** instead of 401/403 with body "Admin UI unavailable: Postgres degraded.": the asyncpg pool is `None`. The break-glass path lets you authenticate to `/transcribe/*`, but the admin UI requires Postgres. Fix the DB URL, restart the agent.

---

### Browser shows "Not Secure" on /admin/login

**Symptom:** Hitting `/admin/login` in a browser shows a "Not Secure" warning, or the login form posts but the cookie never appears in DevTools → Application → Cookies.

**Diagnosis:** The session cookie is set with `secure=True`, which means browsers refuse to attach it unless the connection is HTTPS. If you're hitting `http://` (e.g. `http://localhost:8000/admin/login` for local dev), the cookie is silently dropped.

**Fix:**

- In production, the Cloudflare Tunnel (`https://transcribe.<your-domain>`) terminates HTTPS for you; the cookie works as long as you use the public URL, not `127.0.0.1`.
- For local development against an HTTP-only server: edit `routes/admin_ui.py:login_submit` and temporarily set `secure=False`, or use the `Authorization: Bearer ...` header path (curl/Postman) instead of the cookie.

---

## Multi-sentence dictation feels slow (3-5 seconds vs <1s for short clips)

**Symptom:** A 1-2 word dictation feels instant; a multi-sentence dictation has a noticeable wait between FN-release and the text appearing.

**What's NOT the cause:** Server-side Parakeet inference. `/metrics` reports `parakeet.p50_ms` ≈ 150ms regardless of clip length up to ~10 seconds. The bottleneck is elsewhere.

**Where to look:** WisprAlt now logs a per-stage timing breakdown to OSLog under subsystem `co.wispralt`, category `dictation`. Run a 3-sentence dictation, then read the breakdown:

```bash
log show --last 5m \
  --predicate 'subsystem == "co.wispralt" AND category == "dictation"' \
  --style compact --info | grep 'dictation/timing'
```

You'll see three lines per dictation:

```
dictation/timing: stop_ms=12.3 bytes=1234567
dictation/timing: net_total_ms=1842.5 chars=234
dictation/timing: inject_ms=18.7 total_ms=1873.5
```

| Field | What it tells you |
|---|---|
| `stop_ms` | Time to finalize the WAV after FN-release. Should be <50ms. |
| `bytes` | Size of the upload payload. ~96 KB/sec at 48kHz Float32 mono. |
| `net_total_ms` | Wall time of `DictationAPI.transcribe(wavData)` — covers multipart upload + Cloudflare Tunnel hop + server queue + Parakeet inference + response. |
| `inject_ms` | Time inside `TextInjector.inject(text)`. Covers focused-context capture (system-wide AX → focused element → role/subrole), the secure-field gate, the AX `kAXSelectedTextAttribute` write with read-back verification, and (if AX is unverified) the clipboard `Cmd+V` fallback. Normal case <50 ms. Each AX call is bounded by a 250 ms messaging timeout, so worst case on a hung target is ~1.5 s. |
| `total_ms` | Sum of all three. |

**Diagnosis ladder:**

1. If `inject_ms` > 200ms — the AX-inject path is slow on the focused app (likely Electron app falling through to clipboard fallback). Switch focus to a native AppKit text field to confirm.
2. If `net_total_ms` >> server-side `parakeet.p50_ms` (visible in `GET /metrics`) — the gap is upload + tunnel. For a 3-sentence (≈3-second) clip at 48kHz Float32 mono, the WAV is ~580 KB. On a typical home connection that's ~50-150ms upload. If `net_total_ms` minus server inference exceeds 800ms, suspect the Cloudflare Tunnel route (cross-region, slow first-byte) or local network egress.
3. If `stop_ms` > 100ms — `AVAudioFile` close is taking unexpectedly long; investigate the dictation IO queue.

**Next step:** the timestamps are deltas, not absolute boundaries. To correlate with server-side inference, cross-reference the OSLog event time with the matching server log line (`dictate: queue_wait_ms=… inference_ms=… chars=…`) on the Mac mini at `~/Library/Logs/WisprAlt/server.log`.
