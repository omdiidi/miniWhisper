# WisprAlt — Codebase Overview & File-to-Doc Map

This file is the single source of truth for which documentation file covers each source file. Every code change must consult this map and update the listed doc.

## Root files

| File | Covers |
|---|---|
| `README.md` | Top-level project pitch, architecture diagram, quickstart links |
| `CLAUDE.md` | Claude Code project rules, slash command index, key conventions |
| `CHANGELOG.md` | (it IS a doc — root index) — release-by-release list of notable changes |
| `install.sh` | [INSTALL.md](INSTALL.md) — curl one-liner installer (downloads latest release DMG, verifies SHA256, mounts + copies app, seeds Keychain with API key, opens System Settings panes) |
| `docs/INSTALL.md` | (it IS a doc — root index) — canonical employee install guide for the `install.sh` curl one-liner |
| `docs/INTEGRATION-GUIDE.md` | (it IS a doc — root index) — third-party drop-in integration guide for the OpenAI-compatible `/v1` endpoint |
| `.gitignore` | — (no separate doc) |
| `.editorconfig` | — (no separate doc) |
| `LICENSE` | — (no separate doc) |

## Server (`server/`)

| File | Covered by |
|---|---|
| `server/pyproject.toml` | [SETUP-SERVER.md](SETUP-SERVER.md) — dependency versions and install instructions |
| `server/.env.example` | [SETUP-SERVER.md](SETUP-SERVER.md) — environment variable reference |
| `server/README.md` | [SETUP-SERVER.md](SETUP-SERVER.md) — server-specific quickstart |
| `server/src/wispralt_server/config.py` | [SETUP-SERVER.md](SETUP-SERVER.md) — configuration options |
| `server/src/wispralt_server/auth.py` | [API.md](API.md), [ARCHITECTURE.md](ARCHITECTURE.md) — multi-token bearer auth, sha256→cache→Postgres→break-glass |
| `server/src/wispralt_server/audio.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — audio decode/resample pipeline |
| `server/src/wispralt_server/db.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — asyncpg pool factory, `PostgresUnavailable` typed error, `health_check`/`recreate_pool` for the lifespan watcher loop |
| `server/src/wispralt_server/main.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — startup lifecycle, route mounting, `_seed_admin_if_empty`, drainer task wiring |
| `server/src/wispralt_server/dictate/parakeet.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — Parakeet service, warm load, single-thread executor |
| `server/src/wispralt_server/users/__init__.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — users package (auth-time identity + admin-UI rows) |
| `server/src/wispralt_server/users/store.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — CRUD: `lookup`, `lookup_by_id`, `mint`, `rotate`, `revoke`, `list_all`, `hash_token` |
| `server/src/wispralt_server/users/cache.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — `TokenCache` (LRU 256 × 60s TTL, thread-safe) |
| `server/src/wispralt_server/usage/__init__.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — usage-event package |
| `server/src/wispralt_server/usage/events.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — `UsageEvent` dataclass |
| `server/src/wispralt_server/usage/queue.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — bounded `asyncio.Queue` with drop-oldest overflow |
| `server/src/wispralt_server/usage/writer.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — drain loop, batch INSERT, FK-violation retry |
| `server/src/wispralt_server/admin/__init__.py` | [ADMIN.md](ADMIN.md) — admin package marker (templates only) |
| `server/src/wispralt_server/admin/templates/*.html.j2` | [ADMIN.md](ADMIN.md) — Jinja2 templates: base / login / overview / users / user_detail / usage / token_minted / add_employee / employee_added |
| `server/src/wispralt_server/routes/dictate.py` | [API.md](API.md) — `/transcribe/dictate` endpoint (incl. `X-Smart-Format` and `X-WisprAlt-Client-Version` header gating); fire-and-forget background INSERT into the `dictations` table after the response is built (skips break-glass admin + empty text). |
| `server/src/wispralt_server/routes/health.py` | [API.md](API.md) — `/healthz`, `/readyz/dictation`, `/readyz/meeting` |
| `server/src/wispralt_server/routes/v1_transcriptions.py` | [API.md](API.md), [INTEGRATION-GUIDE.md](INTEGRATION-GUIDE.md) — OpenAI-compatible `/v1/audio/transcriptions` |
| `server/src/wispralt_server/routes/me.py` | [API.md](API.md), [ARCHITECTURE.md](ARCHITECTURE.md) — JSON `GET /me` + `PATCH /me` for self-service display name |
| `server/src/wispralt_server/routes/admin.py` | [API.md](API.md), [ARCHITECTURE.md](ARCHITECTURE.md) — `/admin/rotate-key` (legacy single-key shim) plus `GET /admin/active` (rich projection of the in-flight job) and `GET /admin/server-log/{job_id}` (100 lines bracketing the job in `settings.server_log_path`) |
| `server/src/wispralt_server/routes/transcribe_file.py` | [ARCHITECTURE.md](ARCHITECTURE.md), [API.md](API.md) — `POST /transcribe/file` with `mode: ProcessingMode = Form(ProcessingMode.FILE)`; pre-flight disk gate (free < `Content-Length` × 2 → 507) and RAM gate (available < 4 GiB → 503); streams to staging then hands off to `MeetingRunner.submit_source_or_429`. **Also hosts the chunked-upload endpoints** (`/transcribe/file/chunked/init`, `/transcribe/file/chunked/{upload_id}/{chunk_index}`, `/transcribe/file/chunked/{upload_id}/finalize`) for files >50 MB that need to bypass Cloudflare's 100 MB request-body cap; chunks are streamed via `request.stream()` to `staging/chunked/<upload_id>/chunk-NNNN.part`, owner is verified per request via `api_key_id` recorded in `meta.json`, and finalize concatenates in `run_in_executor` before the same `submit_source_or_429` path. |
| `server/src/wispralt_server/routes/admin_ui.py` | [ADMIN.md](ADMIN.md), [API.md](API.md) — Jinja2 admin UI under `/admin/*` (two-router pattern) |
| `server/src/wispralt_server/routes/meeting.py` | [API.md](API.md) — meeting POST/GET/download/DELETE endpoints |
| `server/src/wispralt_server/meeting/__init__.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — package init that runs torch.load + huggingface_hub compat shims (PyTorch 2.6 weights_only fix + pyannote use_auth_token→token translation) |
| `server/src/wispralt_server/meeting/silence.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — in-person mode detection |
| `server/src/wispralt_server/meeting/deepfilter.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — denoise no-op stub (DeepFilterNet was dropped due to numpy<2 conflict with parakeet-mlx) |
| `server/src/wispralt_server/meeting/mlx_whisper_loader.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — `mlx-community/whisper-large-v3-turbo` singleton. `load()` warmup + idempotent flag; `transcribe_channel(audio_16k, *, word_timestamps, progress_cb, cancel_cb)` wraps `mlx_whisper.transcribe` with a `tqdm.auto.tqdm.update` monkeypatch (and 5 s wall-clock fallback) for chunk progress. |
| `server/src/wispralt_server/meeting/diarize.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — Pyannote diarization, MPS device |
| `server/src/wispralt_server/meeting/merge.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — segment merging, speaker labeling |
| `server/src/wispralt_server/meeting/output.py` | [TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md) — atomic output write, SRT/VTT/TXT formats |
| `server/src/wispralt_server/meeting/pipeline.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — full meeting pipeline orchestration |
| `server/src/wispralt_server/jobs/store.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — SQLite job store, orphan recovery; Phase 1 transcript-storage columns (`transcript_text`, `word_count`, `client_app_version`, `api_key_id`) on `jobs`, separate `dictations` table for request-response dictation captures, `sweep_transcripts(days)` daily TTL helper, chmod 0600 on `jobs.db`/`-wal`/`-shm` at startup |
| `server/src/wispralt_server/jobs/runner.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — asyncio.to_thread runner, semaphore |
| `server/src/wispralt_server/ops/staging.py` | [ARCHITECTURE.md](ARCHITECTURE.md), [API.md](API.md) — staging area management. Hosts `stream_to_staging_raw` (the sync-open-inside-async pattern reused by the chunked routes — repo deliberately has no async-file dep), the `_ALLOWED_EXTENSIONS` allowlist used at chunked `/init` for filename validation, `sweep_old` (24 h WAV TTL) AND `sweep_chunked` (1 h chunked-dir TTL keyed off `meta.json` mtime so active uploads are never reaped). |
| `server/src/wispralt_server/ops/env_writer.py` | [ARCHITECTURE.md](ARCHITECTURE.md), [SETUP-SERVER.md](SETUP-SERVER.md) — atomic .env rewrite, verify_env_perms, key rotation |
| `server/src/wispralt_server/middleware/rate_limit.py` | [ARCHITECTURE.md](ARCHITECTURE.md), [API.md](API.md) — per-IP rate limiting middleware |
| `server/src/wispralt_server/middleware/openai_errors.py` | [API.md](API.md), [INTEGRATION-GUIDE.md](INTEGRATION-GUIDE.md) — translates HTTPExceptions on `/v1/*` into the OpenAI error envelope |
| `server/src/wispralt_server/smart_format/mercury_client.py` | [ARCHITECTURE.md](ARCHITECTURE.md), [SETUP-SERVER.md](SETUP-SERVER.md) — OpenRouter Mercury 2 cleanup client (header-gated, fail-soft 1500ms timeout, gated above `SMART_FORMAT_MIN_WORDS` default 100, soft length-window safety rail [0.7×, 1.10×]) |
| `server/src/wispralt_server/constants.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — shared constants (`MAX_DISPLAY_NAME_LEN`, `OPENAI_COMPAT_SIZE_CAP`) |
| `server/src/wispralt_server/observability.py` | [API.md](API.md), [ARCHITECTURE.md](ARCHITECTURE.md) — thread-safe counters, time-windowed latency histogram, `usage_queue` singleton, `process_started_at_monotonic` for uptime |
| `server/src/wispralt_server/_errors.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — typed domain exceptions |
| `server/migrations/2026-04-27-v1-wispralt-schema.sql` | [DEPLOY-TEAM.md](DEPLOY-TEAM.md) — v1 Postgres schema (wispralt.users + wispralt.usage_events + wispralt.schema_version) |
| `server/migrations/2026-04-27-v2-display-name.sql` | [ARCHITECTURE.md](ARCHITECTURE.md), [ADMIN.md](ADMIN.md) — v2 migration: add `display_name` column to `wispralt.users` |
| `server/migrations/2026-05-05-v3-fallback-events.sql` | [FALLBACK.md](FALLBACK.md) — v3 migration (applied but UNUSED by the simplified fallback design — table + RPCs sit dormant; no code inserts because no Worker holds the role JWT). Safe to leave; drop later via v4 if cleanup is desired. |
| `server/src/wispralt_server/routes/dev_faults.py` | [FALLBACK.md](FALLBACK.md) — dev-only `?fault=503` injection (only mounted with `WISPRALT_DEV_FAULTS=1` on a non-prod host) |

## Tests (`server/tests/`)

| File | Covered by |
|---|---|
| `server/tests/__init__.py` | — (package marker) |
| `server/tests/test_dictate_corrupt_audio.py` | [API.md](API.md) — unit tests on the LibsndfileError → CorruptAudioError boundary |
| `server/tests/test_dictate_route_422.py` | [API.md](API.md) — route-level integration tests pinning the HTTP 422 / 415 / 413 / 200 contract on `/transcribe/dictate` |
| `server/tests/test_observability_time_window.py` | [API.md](API.md) — pins the recent-window p50 + low-traffic fallback contract on `/metrics` |
| `server/tests/test_token_cache.py` | [ADMIN.md](ADMIN.md) — `TokenCache` LRU + 60s TTL behavior (no DB, no asyncio) |
| `server/tests/test_usage_writer.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — `UsageEventQueue` overflow + drainer batch flush + FK-violation retry |
| `server/tests/test_admin_routes_auth.py` | [ADMIN.md](ADMIN.md) — `/admin/*` 403 for employee role, 200 for admin role |
| `server/tests/test_db_health.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — coverage for `db.health_check` + `db.recreate_pool` (the watcher's primitives) |
| `server/tests/test_auth_break_glass.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — Postgres-unreachable + env-var bearer → admin path |
| `.github/workflows/test-server.yml` | [CONTRIBUTING.md](CONTRIBUTING.md) — runs `pytest server/tests/` on PR + push to main |

## Client (`client/`)

| File | Covered by |
|---|---|
| `client/Package.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — macOS 14.0 target, Sparkle 2 dependency |
| `client/README.md` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — client build and run |
| `client/WisprAlt/Info.plist` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — permission usage descriptions, Sparkle config |
| `client/WisprAlt/WisprAlt.entitlements` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — required entitlements |
| `client/WisprAlt/WisprAltApp.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — SwiftUI App entry point, AppDelegate bridge |
| `client/WisprAlt/App/AppDelegate.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — app lifecycle, AppDelegate.shared accessor, defensive cleanup of stale legacy mic-override key |
| `client/WisprAlt/App/MenuBarController.swift` | [ARCHITECTURE.md](ARCHITECTURE.md), [SETUP-CLIENT.md](SETUP-CLIENT.md) — state machine, mic exclusion, composite REC NSImage, human-readable meeting filenames. Owns the **extended `RecordingState`** (`phase`, `chunkIndex`, `totalChunks`, `activeJobID` persisted to UserDefaults, `serverFinishingJobID`, `phaseLabel` computed property). `runFileTranscriptionJob(sourceURL:outputDirectory:stem:mode:)` is shared by meeting + custom upload paths; `cancelActiveTranscription()` invalidates the URLSession, calls `MeetingAPI.cancel`, and routes the row into `serverFinishingJobID` if the server-side cancel is advisory (mid-transcribe). |
| `client/WisprAlt/Audio/MicEnumerator.swift` | [ARCHITECTURE.md](ARCHITECTURE.md), [SETUP-CLIENT.md](SETUP-CLIENT.md) — AVCaptureDevice + CoreAudio HAL bridge; powers the SettingsView Input Mic picker |
| `client/WisprAlt/App/PermissionGate.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — 4-permission wizard, 14.4+ restart |
| `client/WisprAlt/Hotkeys/FNKeyMonitor.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — FN key state machine |
| `client/WisprAlt/Hotkeys/HotkeyEvents.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — delegate protocol |
| `client/WisprAlt/Capture/DictationRecorder.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — AVAudioEngine dictation |
| `client/WisprAlt/Capture/MeetingRecorder.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — SCStream dual-channel capture; `.meetingConfigChanged` notification; partial-WAV cleanup on abort |
| `client/WisprAlt/Capture/AudioDeviceListener.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — CoreAudio HAL listener for default-input-device changes; posts `.meetingConfigChanged` for MenuBarController to abort cleanly |
| `client/WisprAlt/Capture/AlignedRingBuffer.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — sample-aligned ring buffer |
| `client/WisprAlt/Capture/AudioFormat.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — format conversion, CMSampleBuffer→AVAudioPCMBuffer |
| `client/WisprAlt/Server/ServerClient.swift` | [API.md](API.md), [FALLBACK.md](FALLBACK.md) — URLSession, multipart upload, progress, `RequestAttempt` + `isOfflineSignature` classifier |
| `client/WisprAlt/Server/DictationAPI.swift` | [API.md](API.md), [FALLBACK.md](FALLBACK.md) — dictation client + origin→retry→OpenRouter direct fallback |
| `client/WisprAlt/Server/MeetingAPI.swift` | [API.md](API.md), [ARCHITECTURE.md](ARCHITECTURE.md) — meeting + file submit/poll/download/delete. `submitFile(_:mode:progress:)` POSTs to `/transcribe/file` (mode part before file part in multipart envelope). `ProgressInfo` Codable; `cancel(_:)` DELETEs the job (server sets `cancel_requested=1`); `fetchServerLog(_:)` GETs `/admin/server-log/{id}` for the popover sheet. `URLSessionConfiguration` uses `timeoutIntervalForRequest=300` + `request.timeoutInterval=6h` so 90-min uploads don't trip the default 60 s inactivity timeout. Single-shot path — `MenuBarController` switches to `ChunkedUploader` once `uploadSize > 50 MiB`. |
| `client/WisprAlt/Server/ChunkedUploader.swift` | [API.md](API.md), [ARCHITECTURE.md](ARCHITECTURE.md) — chunked upload client used by `MenuBarController.runFileTranscriptionJob` for files >50 MiB. One `URLSession` is created at the start of the upload and reused for `/init`, every `/chunk`, and `/finalize` so cancel hits the right session. Per-byte progress via `URLSessionTaskDelegate.didSendBodyData` keeps `lastUploadProgressAt` ticking within a chunk (so a slow 50 MiB chunk on a poor link never blows the 120 s stall watchdog). Single retry on transient `URLError` per chunk; 4 GB client-side hard cap mirrors the server's `_MAX_TOTAL_BYTES`. Emits a `ChunkedPhase` callback (`.initRequest` / `.chunk` / `.finalize`) so the caller can flip `recordingState.isFinalizing` and render an indeterminate "Finalizing" bar during the server-side concat window. Wraps init/chunk/finalize failures in `ChunkedUploaderError.initFailed/.chunkUploadFailed/.finalizeFailed` so `MenuBarController` surfaces specific causes through `recordingState.uploadError` instead of dismissing silently. Per-chunk start + elapsed time logged at INFO via OSLog category `transcribe`. |
| `client/WisprAlt/Capture/AudioExtractor.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — pre-upload helper that extracts the audio track from a video container into a temp `.m4a` (or `.mp4` fallback) via `AVAssetExportSession` + `AVAssetExportPresetPassthrough`. Uses the completion-handler form `exportAsynchronously(completionHandler:)` wrapped in `withCheckedContinuation` so behaviour is uniform across SDKs. Never throws — every failure mode degrades to "return original URL" so the caller can still attempt the upload and surface a meaningful server-side error if the file really is unprocessable. Called from `MenuBarController.runFileTranscriptionJob` BEFORE the chunk-threshold decision so a 200 MB MP4 becomes a ~15 MB audio file and often skips chunking entirely. |
| `client/WisprAlt/Server/MeAPI.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — JSON `GET /me` + `PATCH /me` client wrapper for the Identity section |
| `client/WisprAlt/Server/ServerError.swift` | [API.md](API.md) — typed errors |
| `client/WisprAlt/Inject/TextInjector.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — injection strategy (focused-context capture, secure-field gate, AX→clipboard fallback) |
| `client/WisprAlt/Inject/AccessibilityInjector.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — AX injection with read-back |
| `client/WisprAlt/Inject/ClipboardInjector.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — clipboard fallback |
| `client/WisprAltCore/InjectionPredicate.swift` | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — pure `didInjectionLand(...)` predicate (unit-tested) |
| `client/WisprAltCore/FocusContext.swift` | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — pure focus-context data type (bundleID/pid/role/subrole) |
| `client/WisprAltCore/SecureFieldGate.swift` | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — pure `shouldRefuseInjection(for:)` gate for native secure fields |
| `client/Tests/WisprAltCoreTests/InjectionPredicateTests.swift` | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — 11-row truth table including the empty/empty/success regression pin |
| `client/Tests/WisprAltCoreTests/SecureFieldGateTests.swift` | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — 5 cases pinning the `AXSecureTextField` refusal rule + the derived-`isSecureField` invariant |
| `client/WisprAlt/Storage/Settings.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — UserDefaults keys |
| `client/WisprAlt/Storage/KeychainHelper.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md), [FALLBACK.md](FALLBACK.md) — API key in Keychain (`co.wispralt`); plus optional OpenRouter fallback key (`co.wispralt.openrouter`) |
| `client/WisprAlt/Storage/PendingUploadsQueue.swift` | [FALLBACK.md](FALLBACK.md) — FS-backed retry queue for meeting uploads when the mini is offline (atomic enqueue, drain coordinator actor, 4 drain triggers) |
| `client/WisprAlt/Storage/KeychainHelper.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — API key in Keychain |
| `client/WisprAlt/Storage/TranscriptStore.swift` | [TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md) — local file index, atomic rewrites |
| `client/WisprAlt/Storage/TranscriptDocument.swift` | [TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md) — JSON model, speaker rename |
| `client/WisprAlt/Update/SparkleController.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — auto-update via Sparkle 2 |
| `client/WisprAlt/UI/SettingsView.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md), [ARCHITECTURE.md](ARCHITECTURE.md) — **this IS the menubar popover content** (file name is historical; the whole popover lives here, not a separate Preferences window). Settings UI (Smart formatting toggle, Identity section pinned to the bottom). `QuickActionsSection` hosts: an Apple-glass active-job card (real `ProgressView` for chunked transcribe + upload fraction, Cancel + Log actions) via the inline private `GlassCard` wrapper, Transcribe file… picker (`.borderedProminent` tinted accent), Open Custom Transcriptions, Copy last meeting / Copy last custom transcription each combined on one row with the relative-age caption, **"Previous transcription still finishing on server" banner** (`recordingState.serverFinishingJobID != nil`) that blocks new file submissions with a tooltip, and a **View server log** button that opens a sheet polling `MeetingAPI.fetchServerLog` every 5 s. |
| `client/WisprAlt/Storage/CustomTranscriptionsStore.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — per-job folder helpers under `~/Documents/WisprAlt/Custom Transcriptions/`; collision-safe stem naming. |
| `client/WisprAlt/UI/LastTranscriptCaption.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — caption view-model behind the "Copy last meeting / Copy last custom transcription" buttons: DispatchSource folder watcher + 10 s timer + `Notification.Name.wisprAltTranscriptWritten` observer. |
| `client/WisprAlt/Util/TranscriptNotifications.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — defines `Notification.Name.wisprAltTranscriptWritten` posted by the upload path on completion. |
| `client/WisprAlt/UI/DisplayNameSheet.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — first-launch display-name entry sheet |
| `client/WisprAlt/UI/FirstLaunchCoordinator.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — coordinates the first-launch display-name sheet (`/me` GET → present sheet if `display_name == null`) |
| `client/WisprAlt/UI/PermissionsView.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — permissions UI |
| `client/WisprAlt/Resources/Assets.xcassets/AppIcon.appiconset/` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — generated icon set (10 PNGs + `Contents.json`); produced by `scripts/build-icon.sh` |
| `client/WisprAlt/UI/TranscriptListView.swift` | [TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md) — transcript list |
| `client/WisprAlt/UI/TranscriptDetailView.swift` | [TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md) — rename UI, offline |
| `client/WisprAlt/UI/RecordingIndicatorView.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — uploading/processing/done states; reads `RecordingState` via `@EnvironmentObject` and renders `phaseLabel` (friendly map) plus `chunk i/n` only when `phase == "transcribe"`. |
| `client/WisprAlt/Util/Logger.swift` | — (no separate doc) |
| `client/WisprAlt/Util/Notifications.swift` | — (no separate doc) |

## Scripts (`scripts/`)

| File | Covered by |
|---|---|
| `scripts/setup-server.sh` | [SETUP-SERVER.md](SETUP-SERVER.md) |
| `scripts/setup-cloudflared.sh` | [SETUP-SERVER.md](SETUP-SERVER.md) |
| `scripts/download-models.sh` | [SETUP-SERVER.md](SETUP-SERVER.md) |
| `scripts/generate-api-key.sh` | [SETUP-SERVER.md](SETUP-SERVER.md) |
| `scripts/server-launchd.sh` | [SETUP-SERVER.md](SETUP-SERVER.md) |
| `scripts/doctor.sh` | [SETUP-SERVER.md](SETUP-SERVER.md) |
| `scripts/server-uninstall.sh` | [SETUP-SERVER.md](SETUP-SERVER.md) |
| `scripts/build-client.sh` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — Developer-ID signed + notarized DMG (distribution path) |
| `scripts/build-client-local.sh` | [DEPLOYMENT-NOTES.md](DEPLOYMENT-NOTES.md), [SETUP-CLIENT.md](SETUP-CLIENT.md) — Apple-Development-signed `.app` for personal use; requires free Apple Development cert from Xcode (no Apple Developer Program enrollment); fails clearly if cert is missing or multiple ambiguous identities exist. Verifies `@executable_path/../Frameworks` rpath (set in `Package.swift` `linkerSettings`) so bundled `Sparkle.framework` resolves at runtime |
| `scripts/setup-local-codesign.sh` | [CONTRIBUTING.md](CONTRIBUTING.md) — Legacy self-signed cert script; no longer wired into the build flow; retained for `--ad-hoc` developer fallback only; see CONTRIBUTING.md |
| `scripts/uninstall-client.sh` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — full client removal including Keychain, UserDefaults, app bundle |
| `scripts/release-client.sh` | [DEPLOY-TEAM.md](DEPLOY-TEAM.md) — local-only release script: bump version, build signed `.app`, package DMG, compute SHA256, tag + push + `gh release create` |
| `scripts/build-icon.sh` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — regenerate `AppIcon.appiconset` PNGs from the master SVG/PNG source |
| `scripts/measure-dictation-latency.sh` | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — placeholder; future helper that times `/transcribe/dictate` round-trips against a fixture WAV (not yet committed) |
| `scripts/deploy-server.sh` | [DEPLOYMENT-NOTES.md](DEPLOYMENT-NOTES.md) — versioned server deploy: copies `server/` + `scripts/` to the mini, `uv sync`, prefetches the mlx-whisper model, kickstarts the LaunchAgent. Includes the `set -e` polling fix (`code=$(curl ... || echo "000")`) from the 2026-05-09 deploy bug. |
| `server/scripts/prefetch-mlx-whisper.sh` | [DEPLOYMENT-NOTES.md](DEPLOYMENT-NOTES.md), [SETUP-SERVER.md](SETUP-SERVER.md) — `huggingface_hub.snapshot_download` for `mlx-community/whisper-large-v3-turbo` at a pinned revision; `resume_download=True`; asserts `model.safetensors > 800 MB` post-download to catch a torn snapshot. |
| `server/scripts/benchmark-mlx-whisper.py` | [TESTING.md](TESTING.md) — Phase 0 spike helper: separately times `ffmpeg_decode_s`, `transcribe_s`, `pyannote_s`; samples RSS every 2 s via psutil; emits `{audio_duration_s, ..., realtime_ratio, peak_rss_mb, segments_count, speakers_detected}` JSON. |

## CI / GitHub (`github/`)

| File | Covered by |
|---|---|
| `.github/workflows/build-client.yml` | [CONTRIBUTING.md](CONTRIBUTING.md) |
| `.github/ISSUE_TEMPLATE/bug_report.md` | [CONTRIBUTING.md](CONTRIBUTING.md) |

## Claude commands (`.claude/commands/`)

| File | Covered by |
|---|---|
| `.claude/commands/setup-server.md` | [CLAUDE.md](../CLAUDE.md) |
| `.claude/commands/setup-client.md` | [CLAUDE.md](../CLAUDE.md) |
| `.claude/commands/test-connection.md` | [CLAUDE.md](../CLAUDE.md) |
| `.claude/commands/docs-check.md` | [CLAUDE.md](../CLAUDE.md) |
| `.claude/commands/update-models.md` | [CLAUDE.md](../CLAUDE.md) |
| `.claude/commands/verify-autostart.md` | [CLAUDE.md](../CLAUDE.md) |

## User-scoped slash commands (`~/.claude-dotfiles/commands/`)

Developer-facing slash command for in-place updates. Lives in the user's dotfiles, not in this repo. (Fresh installs use the `install.sh` curl one-liner — see [INSTALL.md](INSTALL.md).)

| File | Covered by |
|---|---|
| `~/.claude-dotfiles/commands/wispralt-update.md` | [SETUP-CLIENT.md](SETUP-CLIENT.md), [DEPLOY-TEAM.md](DEPLOY-TEAM.md) — pull-based update: diff installed vs latest tag, replace + TCC reset cycle if cdhash changed |
