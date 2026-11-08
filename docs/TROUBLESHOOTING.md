---
title: Troubleshooting
---

# Troubleshooting

Each entry follows the format: **Symptom → Diagnosis → Fix**.

---

## Server Issues

### Model download fails

**Symptom:** `download-models.sh` exits with "Could not download <repo> after 3 attempts".

**Diagnosis:** Both models (`mlx-community/parakeet-tdt-0.6b-v2`, `mlx-community/whisper-large-v3-turbo`) are public, so no token or license acceptance is involved. The failure is network access to huggingface.co, a rate limit (HTTP 429), or a full disk.

**Fix:**
1. Check connectivity: `curl -fsSI https://huggingface.co` should return 200.
2. On HTTP 429, wait a few minutes and re-run `bash scripts/download-models.sh` (downloads resume).
3. Confirm ≥ 4 GB free on `$HOME` (`df -h ~`).

---

### Native wheel build fails (Xcode tools)

**Symptom:** `uv sync` fails with a build error mentioning missing C++ headers, missing compiler, or `clang` not found. (Historically also triggered by `CTranslate2`; that dep was removed in Phase 10 but the symptom can still surface for other native wheels like `torch`.)

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

### File upload fails with 413 (Content Too Large)

**Symptom:** "Transcribe file…" fails with HTTP 413.

**Diagnosis:** The file exceeds `MAX_UPLOAD_BYTES` (default 2 GiB), or the chunked-upload ceiling (4 GB). Files over 50 MiB are uploaded in 50 MiB chunks, so Cloudflare's ~100 MB body cap does not apply to them; a 413 from a single-shot upload below 50 MiB points at a proxy in between.

**Fix:**
- For video files the client extracts the audio track first; if a huge file still fails, export just the audio (m4a) and retry.
- Do not raise `MAX_UPLOAD_BYTES` past what your disk can stage twice (the disk gate needs free ≥ 2× the upload).

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

### 429 "previous transcription still finishing"

**Symptom:** `POST /transcribe/file` (or chunked finalize) returns 429 with `"code": "file_job_busy"`; the client shows "previous transcription still finishing".

**Diagnosis:** WisprAlt processes one file job at a time (enforced by `asyncio.Semaphore(1)` in `jobs/runner.py`). This includes a job you just cancelled: Whisper transcribes the whole file in one call, so a cancelled running job holds the slot until that call returns, then its result is discarded.

**Fix:**
1. Check `GET /metrics` for `meeting.active: true` (the `meeting` block is the file-job block) and `meeting.active_job_id`.
2. Wait for the current job to finish, then resubmit.
3. A 429 with `"code": "insufficient_memory"` means less than 2 GiB of RAM is free; check `memory.available_mb`. A 429 with no `code` is the per-IP rate limiter.

---

### High dictation latency

**Symptom:** Dictation round-trip is consistently > 500ms after warmup.

**Diagnosis steps:**
1. Check `GET /metrics` for `meeting.active: true` — if a file job is running, dictation competes for unified memory.
2. Check `GET /readyz/dictation` for the `X-Dictation-Degraded: true` header — this confirms memory pressure.
3. Check `parakeet.p50_ms` in `/metrics` — if > 300ms, the Mac mini may be thermally throttled.

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
3. Clean up old file-job outputs from `MEETING_OUTPUT_DIR` (the retention sweep removes finished jobs after `MEETING_JOB_RETENTION_DAYS`).
4. The disk guard requires free space ≥ 2× the upload size before accepting a file.

---

## Local database (`/readyz/db`) and usage-write failures

Users, API keys and usage events live in `DATA_DIR/wispralt.db` (SQLite). There is no pool to rebuild and no watcher.

- `curl https://transcribe.<your-domain>/readyz/db` → 200 `{"status":"ok","usage_write_failures":0}` when healthy.
- 503 `reason: "db_error"` — the DB file is missing or unreadable (`ls -l "$DATA_DIR/wispralt.db"`; it should be 0600 and owned by the server user; check free disk). Every authenticated request returns 503 "Auth temporarily unavailable" meanwhile — there is no fallback admin.
- 503 `reason: "schema_outdated"` — a migration is not applied; restart the server (it migrates at startup) and check its log for `MigrationError`.
- `usage_write_failures` > 0 (also `usage_write_failures_total` in `/metrics`) — that many usage rows were lost to a `sqlite3.Error`; the requests themselves succeeded. The log line `usage write failed: <ErrorType>` names the cause (usually `OperationalError: database is locked` under heavy contention).

### IMPORTANT: /readyz/db is observability-only
Do NOT wire `/readyz/db` into Cloudflare origin health-check (use `/healthz`),
launchd KeepAlive (uses process exit), or any liveness probe that RESTARTS
the server on failure.

## Multi-tenant auth and admin UI

### API key rejected (employee install)

**Symptom:** Employee runs the `install.sh` curl one-liner (see [INSTALL.md](INSTALL.md)) with their texted token in `WISPRALT_API_KEY`, the app launches, and Test Connection returns `401 Invalid bearer token`. Or dictation worked previously and now starts returning 401.

**Diagnosis ladder:**

1. **Was the token revoked?** Open `/admin/users` on the Mac mini admin UI. If their row shows **revoked**, this is expected — text them a new token via the **Mint** flow (it re-enables them). Auth has no cache, so a revoke or mint applies to the very next request.
2. **Is the database healthy?** `curl -s https://transcribe.<your-domain>/readyz/db` — a 503 here means auth returns 503 (not 401); see "Local database" above.
3. **Right token?** A token's plaintext is shown **once** by `/admin/users/<id>/mint`. If the employee misplaced it, mint a new one — there is no recovery for the old plaintext.

---

### Admin UI returns 401 / 403

**Symptom:** Browsing to `/admin/` or curling `/admin/users` returns 401 or 403.

**Diagnosis:**

- **401** means the bearer / cookie was missing or invalid. Check that you're sending `Authorization: Bearer <token>` (curl) or that the `wispralt_admin_token` cookie is set in your browser (visit `/admin/login` to set it).
- **403** means the bearer is valid but the user's `role` is not `'admin'`. Open `/admin/users` (with an admin token) and confirm — only admin-role tokens can access the rest of the UI.

**Fix:**

- For 401 from a browser: visit `/admin/login`, paste the admin token, submit. The cookie is set with `max_age=8h`.
- For 403: use the admin token, not an employee token. Lost the admin token? On the server: `cd server && .venv/bin/python -m wispralt.bootstrap --rotate-admin` prints a new one (terminal only). There is no env admin key.
- If you see **503** ("Auth temporarily unavailable"): the auth lookup failed — check `/readyz/db`.

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

**Next step:** the timestamps are deltas, not absolute boundaries. To correlate with server-side inference, cross-reference the OSLog event time with the matching server log line (`dictate: queue_wait_ms=… inference_ms=… chars=…`) on the Mac mini at `~/Library/Logs/WisprAlt/wispralt.log`.

## Reading client logs

Client log messages are `.private` in release builds, so Console.app and `log stream` show `<private>` for them; DEBUG builds (`swift build`, `swift run`) log them in full. Either way the client never logs transcript text, server responses or reasons, file names, paths, URLs or keys; errors appear only as a type and code (for example `server(status: 500)` or `NSURLErrorDomain#-1009`). To see message text on a release build, run a DEBUG build from source.

