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

### CTranslate2 wheel build fails

**Symptom:** `uv sync` fails with a build error mentioning `CTranslate2`, `ctranslate2`, or missing C++ headers.

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

### Text injection silently fails in Electron apps (VS Code, Slack, etc.)

**Symptom:** Dictation completes successfully (status goes back to idle) but no text appears in the focused field in an Electron app.

**Diagnosis:** `AXUIElementSetAttributeValue(kAXSelectedTextAttribute)` returns `.success` but Electron's AX layer does not actually insert the text. `AccessibilityInjector.tryInsert()` detects this by reading `kAXValueAttribute` before and after — if unchanged, it returns `false`.

**Fix:** The clipboard fallback (`ClipboardInjector`) should activate automatically. If it is not:
1. Open **Console.app**, filter by `co.wispralt`, and look for `[inject]` log entries to see which path was taken.
2. Ensure Accessibility permission is granted (Step 1 of the permission wizard).

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
