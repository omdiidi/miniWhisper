---
title: Troubleshooting
---

# Troubleshooting

Each entry follows the format: **Symptom → Diagnosis → Fix**.

---

## `wispralt doctor`

`wispralt doctor` (add `--client` on a client, `--json` for scripts) runs
read-only checks and exits 3 if any fails. Each failing line ends with
`-> <fix>`; the table explains them.

| Check | Fails when | Fix |
|---|---|---|
| `apple-silicon` | the Mac is not arm64 (a warning on a client-only Mac) | the host needs MLX on Apple Silicon; use this Mac as a client |
| `macos` | older than macOS 14 | update macOS |
| `disk` | under 6 GB free where the models live | free space, then `wispralt setup --role host` |
| `ffmpeg` / `uv` | not on `PATH` | `brew install ffmpeg` / `brew install uv` (or rerun `./install.sh`) |
| `env-file` | `~/.config/wispralt/.env` missing, not 0600 or not yours | `chmod 600 ~/.config/wispralt/.env`; if missing, `wispralt setup --role host` |
| `env-data-dir` | the `.env`'s `DATA_DIR` differs from the data dir in `install-state.json` | make them agree (edit `DATA_DIR`, or rerun setup with the matching `--data-dir`) |
| `data-dir` | the data dir is missing or not 0700 | `chmod 700` it, or `wispralt setup --role host` |
| `db-integrity` | `PRAGMA integrity_check` (read-only) is not `ok` | `wispralt restore <newest backup> --yes` |
| `service` | the LaunchAgent is not loaded or its plist lacks `RunAtLoad` | `wispralt restart` |
| `/healthz`, `/readyz/dictation`, `/readyz/db` | a probe is not HTTP 200 | read `~/Library/Logs/WisprAlt/server.err.log` and `wispralt.log`, then `wispralt restart`. `/readyz/dictation` stays 503 while the model loads (up to a minute after start) |
| `legacy-agent` | the mini-era `co.wispralt.server` is loaded (fail: it competes for the port) or its plist is still in LaunchAgents (warning) | `wispralt setup --role host` boots it out and moves its plist to `~/.config/wispralt/legacy/` |
| `cloudflared` (warning) | the tunnel agent is loaded but its token file is missing | rerun `scripts/setup-cloudflared.sh`, or `launchctl bootout gui/$UID/co.wispralt.cloudflared` |
| `warmup` (`--warmup` only) | the dictation model cannot load or transcribe a test tone | `wispralt setup --role host` (re-verifies and repairs the models) |
| `xcode`, `signing-identity` (client, warnings) | only Command Line Tools, or no Apple Development identity | needed only to build the app: install Xcode, then Xcode > Settings > Accounts. A DMG install needs neither |
| `app` (client) | `/Applications/WisprAlt.app` is missing | `wispralt setup --role client` |
| `app-running`, `keychain` (client, warnings) | the app is not running / no key is stored | open the app; add a key in its Settings |

Two lines are information only: the FileVault status, and a reminder that the
host is a per-user agent, so it runs only while you are logged in.

---

## Server Issues

### Model download fails

**Symptom:** `wispralt setup` stops with "cannot fetch <repo>" or "download of <repo> failed".

**Diagnosis:** Both models (`mlx-community/parakeet-tdt-0.6b-v2`, `mlx-community/whisper-large-v3-turbo`) are public, so no token or license acceptance is involved. The failure is network access to huggingface.co, a rate limit (HTTP 429), or a full disk.

**Fix:**
1. Check connectivity: `curl -fsSI https://huggingface.co` should return 200.
2. On HTTP 429, wait a few minutes and re-run `wispralt setup --role host` (downloads resume; a verified model is not fetched again).
3. Confirm ≥ 6 GB free on `$HOME` (`df -h ~`).

---

### Native wheel build fails (Xcode tools)

**Symptom:** `./install.sh` or `wispralt setup` (`uv tool install`) fails with a build error mentioning missing C++ headers, missing compiler, or `clang` not found. (Historically also triggered by `CTranslate2`; that dep was removed in Phase 10 but the symptom can still surface for other native wheels like `torch`.)

**Diagnosis:** Xcode Command Line Tools are missing or outdated.

**Fix:**
```bash
xcode-select --install
```
Wait for installation to complete, then re-run `./install.sh`. If already installed, update it:
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

### File upload fails with 413 (Content Too Large)

**Symptom:** "Transcribe file…" fails with HTTP 413.

**Diagnosis:** The file exceeds `MAX_UPLOAD_BYTES` (default 2 GiB), or the chunked-upload ceiling (4 GB). Files over 50 MiB are uploaded in 50 MiB chunks, so Cloudflare's ~100 MB body cap does not apply to them; a 413 from a single-shot upload below 50 MiB points at a proxy in between.

**Fix:**
- For video files the client extracts the audio track first; if a huge file still fails, export just the audio (m4a) and retry.
- Do not raise `MAX_UPLOAD_BYTES` past what your disk can stage twice (the disk gate needs free ≥ 2× the upload).

---

### `.env` permissions warning at startup

**Symptom:** Server log shows `SECURITY WARNING: .env has mode 6XX, expected 0600` (or `wispralt doctor` fails `env-file`).

**Diagnosis:** The `.env` file was created with overly permissive mode (e.g. 0644 set by a text editor).

**Fix:**
```bash
chmod 600 ~/.config/wispralt/.env
```
Verify: `ls -la ~/.config/wispralt/.env` should show `-rw-------`.

---

### 429 "previous transcription still finishing"

**Symptom:** `POST /transcribe/file` (or chunked finalize) returns 429 with `"code": "file_job_busy"`; the client shows "previous transcription still finishing".

**Diagnosis:** WisprAlt processes one file job at a time (enforced by `asyncio.Semaphore(1)` in `jobs/runner.py`). This includes a job you just cancelled: Whisper transcribes the whole file in one call, so a cancelled running job holds the slot until that call returns, then its result is discarded.

**Fix:**
1. Check `GET /metrics` for `file.active: true` and `file.active_job_id`.
2. Wait for the current job to finish, then resubmit.
3. A 429 with `"code": "insufficient_memory"` means less than 2 GiB of RAM is free; check `memory.available_mb`. A 429 with no `code` is the per-IP rate limiter.

---

### High dictation latency

**Symptom:** Dictation round-trip is consistently > 500ms after warmup.

**Diagnosis steps:**
1. Check `GET /metrics` for `file.active: true` — if a file job is running, dictation competes for unified memory.
2. Check `GET /readyz/dictation` for the `X-Dictation-Degraded: true` header — this confirms memory pressure.
3. Check `parakeet.p50_ms` in `/metrics` — if > 300ms, the host may be thermally throttled.

**Fix:**
- Wait for the file job to complete (the file model is evicted shortly after).
- If consistently slow without a file job running, check Activity Monitor for other processes consuming unified memory (ML workloads, large Xcode builds, etc.).

---

### Server disk full (507)

**Symptom:** `POST /transcribe/file` returns 507. Server log shows "insufficient storage".

**Diagnosis:** The staging directory or file-job output directory is on a full or nearly full volume.

**Fix:**
1. Check `GET /metrics` for `disk.free_gb` and `disk.staging_count`.
2. Clean up stale staging files: `ls -lh $STAGING_DIR` and remove any files older than a few days.
3. Clean up old file-job outputs from `$DATA_DIR/outputs`. Retention is the DB setting `retention_days` (default 0 = keep forever); when it is set, the daily purge removes finished jobs and dictations older than that. Also check for old `<DATA_DIR>.pre-restore-*` directories: a restore keeps the replaced tree aside and never deletes it.
4. The disk guard requires free space ≥ 2× the upload size before accepting a file.

---

## Local database (`/readyz/db`) and usage-write failures

Users, API keys and usage events live in `DATA_DIR/wispralt.db` (SQLite). There is no pool to rebuild and no watcher.

- `curl https://transcribe.<your-domain>/readyz/db` → 200 `{"status":"ok","usage_write_failures":0,"dictation_save_failures":0}` when healthy.
- 503 `reason: "db_error"` — the DB file is missing or unreadable (`ls -l "$DATA_DIR/wispralt.db"`; it should be 0600 and owned by the server user; check free disk). Every authenticated request returns 503 "Auth temporarily unavailable" meanwhile — there is no fallback admin.
- 503 `reason: "schema_outdated"` — a migration is not applied; restart the server (it migrates at startup) and check its log for `MigrationError`.
- `usage_write_failures` > 0 (also `usage_write_failures_total` in `/metrics`) — that many usage rows were lost to a `sqlite3.Error`; the requests themselves succeeded. The log line `usage write failed: <ErrorType>` names the cause (usually `OperationalError: database is locked` under heavy contention).
- `dictation_save_failures` > 0 (also `dictation_save_failures_total` in `/metrics`) — that many dictations were returned to the client with `"saved": false` (log line `dictation save failed: <ErrorType>`). The client kept those recordings under Retry; a Retry with the same dictation id saves them once the DB is healthy.

### IMPORTANT: /readyz/db is observability-only
Do NOT wire `/readyz/db` into Cloudflare origin health-check (use `/healthz`),
launchd KeepAlive (uses process exit), or any liveness probe that RESTARTS
the server on failure.

## History import

`wispralt import …` (runbook in [ADMIN.md](ADMIN.md#importing-old-history-supabase--transcript-files)).

- **Exit 3 / `BLOCKED.json`** — the Supabase project is paused or unreachable (DNS,
  connection refused, timeout, "Tenant or user not found"). The snapshot folder holds
  only `BLOCKED.json`. Nothing tried to unpause it: resume the project yourself in the
  Supabase dashboard if you want the export, then run `export` again (a new `-2`
  folder). Transcript-file import still works meanwhile.
- **Exit 1 `ExportUnavailable`** — `psql` is missing (`brew install libpq` or
  `postgresql`), no connection string (env `WISPRALT_IMPORT_PG_URL` or Keychain
  `co.wispralt.import-pg`), or Postgres refused the query (e.g. permissions).
- **Exit 4 integrity failure** — a `.jsonl` file no longer matches `counts.json`
  (edited or truncated), the export's psql output did not match its own counts, a
  first import found rows already present, or not every planned row was written.
  Nothing from the failing batch was written; export a fresh snapshot rather than
  editing one.
- **Exit 2 refused** — no terminal and no `--yes`; the owner mapping is ambiguous
  (more than one enabled admin on either side: use `--owner-map PG_ID=LOCAL_ID`); no
  local admin yet (run `wispralt setup --role host`); or the server holds
  `<DATA_DIR>.lock` (stop it first with `wispralt stop`).
- **`unmatched` rows** — usage rows whose Supabase user is not in the snapshot (or was
  malformed). They stay in the snapshot and are listed in the report.
- **A crash mid-import** — just run the same command again: it resumes after the last
  committed batch, and already-imported rows count as duplicates.

## Trust invariants, not flags — eviction & release checks

Two outages in May 2026 had the same shape: code trusted a return value or
flag instead of verifying the invariant it claimed to enforce.

- 2026-05-17 (the former Postgres pool): `db.health_check` returned False;
  the watcher's rebuild branch should have run; an over-narrow exception
  class caused the failure to escape into the outer Exception handler instead.
- 2026-05-19 (mlx-whisper): `evict_if_idle()` logged "released
  models" and set `models_warm = False`; 2.7 GB of MLX active memory
  stayed resident because mlx_whisper's internal `ModelHolder.model`
  cache survived our reset.

Lesson: every release/cleanup function should ALSO verify its effect.
Flags are necessary but not sufficient.

### Check recipe — file-model eviction (manual)

After a file job finishes and `idle_seconds > 120`:

    TOKEN=$(security find-generic-password -w -s co.wispralt)
    curl -fsS -H "Authorization: Bearer $TOKEN" \
      http://127.0.0.1:8000/metrics | jq '.memory, .file'

Expected steady-state (post-eviction, no jobs running):

    memory.mlx_active_mb: ~1218       (Parakeet baseline only)
    memory.mlx_cache_mb: 0
    file.models_warm: false
    file.idle_seconds: > 60
    file.eviction_failures_total: 0  (cumulative; non-zero = regression)

If `mlx_active_mb > 2000 AND models_warm=false AND idle_seconds > 120`,
the eviction invariant is broken. Check
`~/Library/Logs/WisprAlt/server.err.log` for a CRITICAL line containing
"Eviction invariant failed".

### Tuning the self-check

Two settings control the gate (see `config.py`):

- `file_eviction_delta_floor_mb` (default 1000): minimum MB the
  active memory must drop during eviction. Lower if you want stricter
  detection; raise if you see false positives.
- `file_eviction_pre_active_gate_mb` (default 1500): only fire when
  active was above this BEFORE eviction. Excludes cold-start and
  partial-load failures.

Override in `~/.config/wispralt/.env`, then `wispralt restart`:
    FILE_EVICTION_DELTA_FLOOR_MB=1500

---

## Client Issues

### FN tap not detected

**Symptom:** Holding FN has no effect. The menubar icon does not change.

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
1. Console.app, filter by `co.wispralt`. (Release builds log messages as `<private>`; read them in a DEBUG build, or see [Reading client logs](#reading-client-logs).)
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

**Diagnosis:** These apps do not honour `AXUIElementSetAttributeValue(kAXSelectedTextAttribute)` — either they silently no-op the write or they don't expose a usable `kAXValueAttribute`. `AccessibilityInjector.tryInsertWith` detects this by reading `kAXValueAttribute` before and after the write; if both reads return the same value (or fail), the predicate `didInjectionLand(...)` returns `false` and `TextInjector` falls through to `ClipboardInjector.injectViaCmdV` which writes the text to `NSPasteboard.general`, synthesises ⌘V, and leaves the text on the clipboard. iMessages and Pane are expected to take this path — it's not a bug.

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

**The text stays on the clipboard.** When the Cmd+V fallback is used, WisprAlt cannot confirm the paste landed, so it leaves the dictation on the clipboard instead of restoring what was there before (a restore could erase the only copy). Clipboard managers will record it. **Copy last dictation** also returns it for 7 days.

**Web password caveat.** The native secure-field gate (next section) catches AppKit `NSSecureTextField` and SwiftUI `SecureField`, but **web password inputs** (Safari, Chrome, Electron `<input type="password">`) usually do **not** surface `kAXSubroleAttribute == AXSecureTextField`, so the gate cannot detect them. **Do not dictate web passwords** — they may transit the system pasteboard where any clipboard manager could capture them.

**Known limitation: secure-field skip notification debounce.** When dictation is refused due to a secure field, you'll see one local notification "Dictation Skipped — `<bundleID>` is asking for a password. Type the value manually." Subsequent skips against the same focus within 60 seconds are logged but not re-notified, to avoid spamming Notification Center if the field stays focused (e.g. unlocking 1Password).

**Known limitation: post-capture focus shift on the clipboard path.** The secure-field gate captures focus context once at the top of `TextInjector.inject` and uses it to decide whether to refuse and which AX element to write to. The synthesised ⌘V, however, goes to whatever app is focused at the moment the `CGEvent` posts — which is usually the same app, but if you click into a different field (especially a password field) during the brief window inside `inject(_:)` between focus capture and the synthesised ⌘V (typically sub-millisecond, up to a few hundred ms if the AX-attempt times out on a hung target), the paste will land in the new focus. The gate cannot prevent this. If you need to switch fields right after dictating, wait for the text to land first.

---

### File upload progress stuck

**Symptom:** The menubar shows "Uploading" for an unusually long time (more than a few minutes for a typical 30-minute recording).

**Diagnosis:** The network connection is slow or dropped. (Files over 50 MiB go up in chunks, so the Cloudflare body cap is not the cause; a 120 s stall watchdog fails a truly stuck upload.)

**Fix:**
1. Open Activity Monitor → Network tab; check upload bytes/sec for WisprAlt.
2. If upload speed is near zero, the connection may have been dropped by Cloudflare. Cancel and retry.
3. Check free space with `wispralt status` (sizes) and `wispralt doctor` (`disk`).

---

### "Quit and Reopen Required" sheet appeared

**Symptom:** A blocking sheet titled "Quit and Reopen Required" appeared during the permission wizard.

**Diagnosis:** This is expected on macOS 14.4+ after granting Input Monitoring. The CGEventTap grant requires a process restart to take effect.

**Fix:** Click **Quit Now**, then relaunch WisprAlt from `/Applications`. The permission wizard will resume at Step 3 (Microphone).

---

### File job stuck in "Processing" indefinitely

**Symptom:** The menubar shows "Processing" for far longer than expected, and the job never transitions to done.

**Diagnosis:** The poll loop in `MenuBarController+Transcription` enforces a deadline of `max(600 s, min(6 h, 3 × audio duration + 300 s))`. When the deadline elapses the client cancels the job via `DELETE /transcribe/file/{job_id}` and surfaces a `pollTimedOut` error notification. If you see the error, the server-side pipeline likely crashed or was killed mid-run.

**Fix:**
1. Check `GET /metrics` for `file.active` and `file.active_job_id`. If active is `false` but the client was still polling, the runner task died silently — inspect `~/Library/Logs/WisprAlt/wispralt.log` for Python tracebacks.
2. Restart the server (`wispralt restart`). At startup, `recover_orphans` will mark the abandoned job as `failed` and clean up the staged source.
3. If the server is healthy but the job consistently times out for long files, consider whether available RAM (`memory.available_mb` in `/metrics`) was below 2 GiB when the job started.

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

### Sparkle update not appearing or blocked

**Symptom:** "Check for Updates…" reports no update, or the update sheet is deferred.

**Diagnosis (deferred):** Updates are intentionally deferred while the app is busy (`MenuBarController.isBusy`: dictating, uploading or processing a file job, or a dictation still in flight). This prevents an update relaunch from killing in-progress work.

**Fix (deferred):** Let the dictation or file job finish, then check for updates again.

**Diagnosis (not appearing):** The appcast URL may be unreachable, or the Sparkle EdDSA signature may not match the embedded `SUPublicEDKey` in `Info.plist` (affects self-built versions).

**Fix (not appearing):** Check network connectivity to GitHub Pages. For self-built versions, ensure `SPARKLE_ED_PRIVATE_KEY` in your GitHub secrets matches the public key in `Info.plist`.

---

## Server Auto-start and Cloudflare Tunnel

### Cloudflared not running after a host reboot

**Symptom:** The public endpoint (`https://wispralt.example.com`) is unreachable after a reboot or power cycle.

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

### Host rebooted with no internet

**Symptom:** The host rebooted while the network was down. cloudflared didn't come up.

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

**Symptom:** Accessibility, Input Monitoring, or Microphone prompts appeared even though you haven't installed a new version.

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
tccutil reset Microphone      co.wispralt.WisprAlt
```
Then reopen WisprAlt and grant all three permissions. This happens at most once a year.

---

## Multi-tenant auth and admin UI

### API key rejected (employee install)

**Symptom:** Employee installs the client (see [INSTALL.md](INSTALL.md)), pastes the server URL and their texted key into Settings, and Test Connection returns `401 Invalid bearer token`. Or dictation worked previously and now starts returning 401.

**Diagnosis ladder:**

1. **Was the key revoked, or the person disabled?** Open **People** → their page in the portal. If they show `disabled`, or that device's key shows `revoked`, this is expected — send an **Add device** invite, or **Rotate key** (rotate revokes all their keys, shows one new key and re-enables them). There is no token cache: every request looks the key up in `wispralt.db`, so a revoke, disable or mint applies to the very next request.
2. **Is the local database healthy?**
   ```bash
   curl -s --max-time 4 https://wispralt.example.com/readyz/db
   tail -100 ~/Library/Logs/WisprAlt/wispralt.log | grep -i 'sqlite\|database\|migration'
   ```
   A DB error makes auth answer **503** (never 401, and there is no fallback admin). `/readyz/db` gives the reason (`db_error` / `schema_outdated`); see "Local database" above.
3. **Right token?** A key's plaintext is shown **once** (Add employee, Rotate key, or an invite redeemed by the app). If the employee misplaced it, send an invite or rotate — there is no recovery for the old plaintext.

---

### Admin UI returns 401 / 403

**Symptom:** Browsing to `/admin/` or curling `/admin/people` returns 401 or 403.

**Diagnosis:**

- **401** means the bearer / cookie was missing or invalid. Check that you're sending `Authorization: Bearer <token>` (curl) or that the `wispralt_admin_token` cookie is set in your browser (visit `/admin/login` to set it).
- **403** means the bearer is valid but the user's `role` is not `'admin'`. Open `/admin/people` (with an admin token) and confirm — only admin-role tokens can access the rest of the UI.

**Fix:**

- For 401 from a browser: visit `/admin/login`, paste the admin token, submit. The cookie is set with `max_age=8h`.
- For 403: use the admin token, not an employee token. There is no env-var admin key and no break-glass admin. If the admin token is lost, mint a new one on the host's terminal: `wispralt keys rotate-admin` (prints the new token once, or writes it to a new `--token-file`; add `--user-id N` when there are several admins).
- **403 "CSRF token missing or invalid"** on a form submit (Add employee / key, Rotate, Disable, Revoke device, Invites, AI runs, Settings) or on `PATCH /me` from a browser: every cookie-authenticated mutation needs the CSRF token the page embedded (it is bound to the session, so it changes when you sign in again; the login form's own `wispralt_csrf` cookie lasts 30 min). Reload the page and submit again. Scripts should send `Authorization: Bearer <token>`, which needs no CSRF token; a `Basic` or other non-Bearer `Authorization` header does not count.
- **503** instead of 401/403: the account lookup hit a `sqlite3.Error`. Check `/readyz/db` and the "Local database" section; there is no fallback admin.

---

### Browser shows "Not Secure" on /admin/login

**Symptom:** Hitting `/admin/login` in a browser shows a "Not Secure" warning, or the login form posts but the cookie never appears in DevTools → Application → Cookies.

**Diagnosis:** The session and CSRF cookies are `Secure`, so browsers drop them on plain `http://` to a non-loopback host (a LAN IP, or a hostname other than `localhost`). Plain http to a loopback host (`http://127.0.0.1:8000/admin`, `http://localhost:8000/admin`) gets cookies without `Secure` and works.

**Fix:**

- Use the public HTTPS URL (the Cloudflare Tunnel), or `http://127.0.0.1:8000/admin` on the host itself.
- For scripts, use the `Authorization: Bearer ...` header instead of the cookie.

---

## Portal: setup, invites, search and AI insights

### No admin yet / where is the setup code?

While no enabled admin exists the server writes a one-time code to `DATA_DIR/setup-code`
(mode 0600) at startup and logs its path (`grep setup-code ~/Library/Logs/WisprAlt/wispralt.log`).
On the server: `cat "$DATA_DIR/setup-code"` and paste it at `/admin/setup`. Wrong code → 403.
`/admin/setup` returns **404** once an enabled admin exists (by design); a lost admin key is
recovered on the server's terminal with `python -m wispralt.bootstrap --rotate-admin`. The code
file is deleted after a successful setup, and a stale one is removed at the next start.

### Invite says `invite_invalid`

`POST /invites/redeem` answers every failure with the same `400 {"error":"invite_invalid"}`
on purpose. Usual causes: the invite is older than **10 minutes**, was already used (each is
single use), was revoked on `/admin/invites`, the admin who created it was disabled, or (for
"add device") the person is disabled. Create a new invite. `429 rate_limited` means more than
10 attempts a minute from one network: wait a minute.

### History search misses a record, or `/me/history?q=` errors

Search needs 2+ characters and matches whole words plus a prefix of the last word
(`roadm` finds `roadmap`; `oadmap` does not). Deleted rows never match. If a record that
exists is not found, check the full-text index on the server (safe while the server runs;
it changes nothing):

```bash
sqlite3 "$DATA_DIR/wispralt.db" \
  "INSERT INTO dictations_fts(dictations_fts, rank) VALUES('integrity-check', 1);
   INSERT INTO jobs_fts(jobs_fts) VALUES('integrity-check');"
```

No output means both indexes match their tables. An error (`database disk image is
malformed`) means the index drifted: rebuild it with
`INSERT INTO dictations_fts(dictations_fts) VALUES('rebuild');` and, for jobs,
`DELETE FROM jobs_fts; INSERT INTO jobs_fts(text, job_id) SELECT transcript_text, id FROM jobs WHERE transcript_text IS NOT NULL;`
(take a backup first). A server whose SQLite lacks FTS5 refuses to migrate with
`MigrationError: SQLite lacks FTS5`.

### AI insights do not appear, or a run stopped

- **"AI interpretation is off"** — an admin must turn it on in **Settings** after accepting
  the disclosure; measured stats work without it.
- **A run started but no new row appears** — another run is already `running`
  (perhaps a `python -m wispralt.insights.cron --manual` in a terminal): one run at a
  time, enforced in the database. A row left `running` by a crash stops blocking after
  2 hours.
- **Runs cannot start / "OPENROUTER_API_KEY is not configured"** — set the key in
  `~/.config/wispralt/.env` (`OPENROUTER_API_KEY=…`), then `wispralt restart`.
- **Data → AI runs shows a status other than `done`:**

  | Status / error | Meaning | What to do |
  |---|---|---|
  | `aborted` · `budget` | The next call's worst-case cost (prompt + a full reply at the rate card) would cross the 30-day cap (checked before every call). | Wait for older spend to age out, or raise the cap in Settings. |
  | `aborted` · `rate_limit` | OpenRouter returned 429. | The scheduler retries after 6 hours; or start the week again later. |
  | `failed` · `all_people_failed` | Every person's reply was unusable or errored. | See the failures column (`openrouter_error`, `json_decode`, `schema`); check the model id in Settings. |
  | `failed` · `some_people_failed` | At least one person's reply was unusable or errored; the others were saved, and no team summary was written (it would have left them out). | The scheduler retries after 6 hours (or start the week again): only the failed people and the team summary are paid for. A person who fails every time keeps the team summary away; check the failures column. |
  | `aborted` · `cancelled` | The server shut down mid-run. | Start the week again (or let the scheduler). |
  | `failed` · `team_openrouter_error` / `team_unusable` | People were analysed but the team summary call errored, or its reply was unusable twice. | The scheduler retries after 6 hours (or start the week again); saved people are not paid for twice. |
  | `failed` · `<ExceptionName>` | The run crashed. | Check `wispralt.log` around the start time (logs hold ids and classes, never text). |
  | `skipped` · `ai_disabled` / `no_client` | AI is off, or no key. | See above. |

- **A person is missing from AI insights** — they need 5+ dictations or 1+ file
  transcription in that week.
- **"Older format" notice** — the insight predates source refs; run that week again with
  **Regenerate existing**.
- **"stale" notice** — records arrived after the insight was generated; regenerate to
  include them.

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

**Next step:** the timestamps are deltas, not absolute boundaries. To correlate with server-side inference, cross-reference the OSLog event time with the matching server log line (`dictate: queue_wait_ms=… inference_ms=… chars=…`) on the host at `~/Library/Logs/WisprAlt/wispralt.log`.

## Reading client logs

Client log messages are `.private` in release builds, so Console.app and `log stream` show `<private>` for them; DEBUG builds (`swift build`, `swift run`) log them in full. Either way the client never logs transcript text, server responses or reasons, file names, paths, URLs or keys; errors appear only as a type and code (for example `server(status: 500)` or `NSURLErrorDomain#-1009`). To see message text on a release build, run a DEBUG build from source.

### Online: holding FN does nothing ("key missing")

**Symptom:** in Online mode FN shows a notice and opens the popover at the OpenAI key field; the status line reads `Online · <model> · key missing`.

**Fix:** paste the key in **Show advanced settings → OpenAI API Key**, or run `security add-generic-password -U -s co.wispralt.openai -a default -w` in Terminal (works while the app runs; the next FN press picks it up). After a new build, macOS asks once for Keychain access: choose **Always Allow**. "OpenAI rejected the API key" means the key itself is wrong or revoked; "quota exceeded" means the OpenAI project's budget is used up.

### Online file transcription failed

- "This file type needs Local mode": this Mac cannot decode the file (e.g. a video whose audio is not AAC). Use Local mode, or convert it to m4a first.
- "This video has no usable audio track" / "Could not extract audio": the video has no sound, or the audio could not be copied out of it.
- Nothing is written to the transcripts folder unless the whole file succeeded, and a job interrupted by quit is not resumed: pick the file again.

### A dictation failed: Retry, Export…, Discard

**Symptom:** "Dictation Failed … Audio saved under Retry." or "N saved recordings from last session — see Retry".

**What happened:** the audio of a failed, possibly cut-off, or "No speech detected" dictation is kept in `~/Library/Application Support/WisprAlt/retry/` (newest 2, 7 days, 16 kHz mono). A crash or quit mid-dictation leaves it in `spool/`, and the next launch moves it to Retry. Nothing is re-sent automatically.

**Fix:** open the popover → **Retry N**. **Retry** re-sends it in the mode it was recorded in and puts the text on the clipboard (and in **Copy last dictation**). A recording from the other mode shows "Switch to Local to retry" (or Online). **Export…** saves the audio file; **Discard** deletes it. A Local server that answers 410 `dictation_deleted` or 409 `dedup_conflict` refuses that dictation for good: its recording is discarded.

