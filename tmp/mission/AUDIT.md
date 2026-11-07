# Part 1 audit (merged, 2026-11-07)

Sources (all read-only, verified against source):
- `audit-claude-explorer.md` (Claude, end-to-end paths + sizes + tests)
- `audit-codex-client.md` (Swift client)
- `audit-codex-server.md` (server runtime, models, Mercury, /v1)
- `audit-codex-data-ops.md` (DB, auth, portal, scripts, personal config)

## Current architecture (one paragraph)
Swift menu-bar client → FastAPI server on a Mac mini over a Cloudflare tunnel. Hold-FN dictation streams VAD chunks over HTTP to Parakeet MLX (resident), finalize joins + optional Mercury (OpenRouter chat) + persists to SQLite `dictations`. Uploaded files go through `/transcribe/file` (+chunked) → `MeetingRunner` → `meeting/pipeline.py` mono branch (MLX Whisper turbo) → TXT/SRT/VTT/JSON, polled via `/transcribe/meeting/{id}`. Triple-tap-FN meetings use ScreenCaptureKit + mic → stereo WAV → same runner, dual-channel branch with pyannote. Auth/users/usage live in Supabase Postgres (asyncpg); jobs/dictations/insights in SQLite. Offline dictation falls back to OpenRouter audio from the client.

## Prioritized findings
P0
1. No Online/Local routing; everything requires `Settings.serverURL` (Settings.swift:23, DictationAPI.swift:65).
2. Cross-user job access: poll/download/delete never check job owner (routes/meeting.py:124,198,225).
3. Plaintext admin key printed to stdout → launchd log (routes/admin.py:67,120).
P1
4. File jobs load pyannote + require HF_TOKEN though mono path never uses it (pipeline.py:96-130, config.py:31).
5. "meeting"-named code (MeetingRunner, MeetingAPI, routes/meeting.py, meeting/output.py) carries file jobs; `/v1` imports SRT/VTT helpers from meeting/output.py.
6. Admin cookie mutations lack CSRF (admin_ui.py:353,405,478,554).
7. Chunk finalize doesn't verify every index exists (transcribe_file.py:525-554).
8. Unknown `/v1` model names silently run Parakeet (v1_transcriptions.py:144-239).
9. Team weekly_insights upsert duplicates (nullable PK column, store.py:259).
10. Two DBs disagree on ownership/availability; SQLite rows reference Postgres ids with no FK.
11. Client file-job recovery loses output dir after relaunch (+Transcription.swift:197,412).
12. Settings rejects http:// (blocks loopback Local) (Settings.swift:40,218).
13. Server-only features (LastDictationAPI, MeAPI, /me/history, Mercury) break silently in Online mode.
P2
14. Mercury rarely applies: disabled unless env + key, 80-word floor, 600 ms budget, silent fallbacks (mercury_client.py:79-250, config.py:114-145).
15. Usage queue lossy by design (usage/queue.py:53).
16. Full dictation text logged public (DictationAPI.swift:189, Logger.swift:17); insights logs 200 chars.
17. Package.swift macOS 15 vs Info.plist 14.
18. Personal values: transcribe.integrateapi.ai, omdiidi/miniWhisper, omidsmacmini.local, Supabase ref lmaffmygjrfgkwrapfax, /Users/omidzahrai, owner email (install.sh:10-11, UpdateChecker.swift:37, dev_faults.py:44, migrations, scripts, templates, .claude/commands).
19. Thin tests: client 16 pure-core cases; no tests for JobStore, runner, chunked upload, streaming, DictationAPI.

## Removal boundaries
DELETE (meeting recorder): client MeetingRecorder, AlignedRingBuffer, AudioFormat (verify), AudioDeviceListener (verify dictation mic picker doesn't need it), PendingUploadsQueue, MenuBarController+Meeting (move MeetingProcessingError first), triple-tap in FNKeyMonitor + HotkeyEvents.toggleMeetingRecording, Screen Recording permission, meeting settings/icons/updater guards, meeting-only TranscriptDocument/Store/List/Detail views (if file UI doesn't use them). Server: submit_meeting route, diarize.py, merge.py (move label_all if shared), silence.py, deepfilter.py stub, meeting/__init__ torch shims, dual-channel pipeline branch + eviction, /readyz/meeting, pyannote/torch/torchaudio/matplotlib deps, HF_TOKEN, smoke-meeting.sh, meeting tests.
DELETE (OpenRouter audio fallback): DictationAPI ladder rung 3 + callOpenRouter + parser + fallback model, OpenRouter Keychain item, DictationFallbackQueue + drains, X-Client-Dedup-Id plumbing, /telemetry/cloud-dictation, insert_cloud_fallback_dictation, dev_faults.py, docs/FALLBACK.md; archive fallback_events data on export.
KEEP + RENAME (file transcription): /transcribe/file + chunked, JobStore jobs, runner source path, mono Whisper branch, output writers (move SRT/VTT helpers out of meeting/), poll/download/delete → file-oriented URLs, client MeetingAPI file methods → FileJobAPI, ChunkedUploader, AudioExtractor, CustomTranscriptionsStore.
KEEP: Mercury smart_format (fix), /v1 OpenAI-compatible API (fix model validation), hold-FN streaming, injection, mic selection.

## Working test commands
- Client: `cd client && swift build && swift test` (needs writable cache; real Xcode toolchain check in Part 2).
- Server, no MLX: from `server/`, export HF_TOKEN WISPRALT_API_KEY SERVER_URL MEETING_OUTPUT_DIR JOB_DB_PATH STAGING_DIR, then `PYTHONPATH=src pytest tests/test_mercury_safety.py tests/test_v1_transcriptions.py tests/test_v1_models.py tests/test_observability_time_window.py -q`. Full suite needs MLX (Apple Silicon) or CI stubs (`PYTHONPATH=src:../ci-stubs`).
- Scripts: `bash -n install.sh scripts/*.sh`.
