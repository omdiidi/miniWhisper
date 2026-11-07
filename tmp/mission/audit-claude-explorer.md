# Claude explorer audit (Part 1, 2026-11-07)

## Paths
- (a) FN dictation: FNKeyMonitor (Hotkeys/FNKeyMonitor.swift:173-257) -> dictationStart (+Dictation.swift) -> DictationRecorder.start (Capture/DictationRecorder.swift:347) always streams (:453), DictationStreamSession (:464), EnergyVAD chunks, emitChunk (:963) -> POST /transcribe/dictate/stream/{id}/chunk/{i} (Server/DictationStreamSession.swift:155) -> finalize (:213). Fallback ladder runExistingPath (DictationAPI.swift ~100): /transcribe/dictate, retry, callOpenRouter -> DictationFallbackQueue. Server: routes/dictate_stream.py:70/148, routes/dictate.py:48, ParakeetService (main.py:251), Mercury server-only (dictate.py ~137, dictate_stream.py:239, config.py:108), SQLite dictations (jobs/store.py:196). Auth require_api_key (auth.py:227) -> Postgres wispralt.users (users/store.py:72); usage -> observability.usage_queue -> usage/writer.py -> Postgres usage_events.
- (b) File upload: transcribePickedFile/runFileTranscriptionJob (+Transcription.swift:530/39) -> MeetingAPI.submitFile(mode:"file") (Server/MeetingAPI.swift:254) -> /transcribe/file (+ ChunkedUploader >50MB). Server routes/transcribe_file.py:136/249/355/470 -> MeetingRunner.submit_source_or_429 (jobs/runner.py:187) -> _run_source (:419) -> meeting/pipeline.py mono branch (~519-580) -> write_outputs_atomic TXT/SRT/VTT/JSON. Poll/download/delete via /transcribe/meeting/{id} (routes/meeting.py:115/180/219).
- (c) Meeting: triple-tap (FNKeyMonitor tapTimes :80) -> toggleMeetingRecording (+Meeting.swift:304) -> MeetingRecorder (SCK + mic, AlignedRingBuffer) -> stereo WAV -> submitFile(mode:"meeting") or PendingUploadsQueue; server dual-channel branch (pipeline.py ~620-726): silence.py, diarize.py (pyannote), merge.py.
- SHARED with file path (must survive, rename away from "meeting"): MeetingRunner, JobStore, meeting/pipeline.py, mlx_whisper_loader.py, output.py, /transcribe/meeting/{id} routes, ChunkedUploader, client MeetingAPI file-job methods, TranscriptStore + transcript UI. merge.label_all shared.

## Risky seams
1. _ensure_models_loaded (pipeline.py:96-130) loads pyannote on every first job; HF_TOKEN required setting; install_compat_shims main.py ~340; eviction pipeline.py:217-237; /readyz/meeting; pyproject.toml:12-15,30-37.
2. "meeting"-named code serves file jobs; reenqueue_pending defaults unknown rows to MEETING (runner.py:262-268); chunked meta mode (transcribe_file.py:587).
3. Online mode: LastDictationAPI, MeAPI (AppDelegate.swift:107-112), /me/history, Mercury are server-only -> must be mode-gated.
4. Fallback removal: ServerClient.isOfflineSignature also used by +Meeting.swift:190; DictationFallbackQueue.drain wired in 3 places; X-Client-Dedup-Id only for fallback; auth.py:283 forbid_integration_kind for /telemetry.
5. Supabase replacement touches auth+cache, usage_events, fallback_events, admin UI, me.py, insights/cron.py, ops/background.py db_watcher_loop, break-glass token; lifespan asyncpg assert main.py:144; 10 test files mock the pool.
6. Screen Recording permission only for meetings (PermissionGate, PermissionsView); migrate/clean pendingMeetingDefaultInputUID (AppDelegate:38), pending-upload files, co.wispralt.openrouter Keychain item.
7. Online history needs a client-side store (server writes dictations rows today).

## Size (LOC approx)
Meeting-only client 2,100-2,400; meeting-only server 1,300-1,600; fallback-only ~1,100-1,200 (client queue 465, telemetry.py 131, dev_faults.py 180, FALLBACK.md 168, migration v3); Supabase-dependent ~4,500 + tests. jobs/store.py (1273) already holds jobs, dictations, weekly insights -> base for single SQLite DB.

## Tests
- Client: `cd client && swift test` — 16 cases, WisprAltCore only (injection predicates, secure-field gate).
- Server: `cd server && pytest tests/ -v`; CI .github/workflows/test-server.yml stubs mlx/parakeet_mlx in ci-stubs, PYTHONPATH=server/src:ci-stubs, env WISPRALT_API_KEY, HF_TOKEN, SERVER_URL, MEETING_OUTPUT_DIR, JOB_DB_PATH, STAGING_DIR. No server/.venv. Solid: test_v1_transcriptions (23), test_mercury_safety (34), test_v1_models, 422/corrupt contracts. Mock-asyncpg tests need rewrite. Untested: JobStore, MeetingRunner, chunked upload, streaming session, client recorder, DictationAPI, E2E (manual 12-run matrix docs/TESTING.md).

## Recommended Online seam
protocol DictationBackend { makeSession() } reusing DictationStreamSession shape (enqueueChunk, finalize(tail:smartFormat:), abort). DictationRecorder holds the protocol (:237,:464,:963). Settings.mode @Published default Online (near smartFormatting Settings.swift:120). Local = existing stream path minus OpenRouter rung. Online batch (gpt-transcribe: POST full WAV at stop) and realtime (WebSocket forwarding chunks). Keychain co.wispralt.openai modeled on KeychainHelper.swift:111-165. Decide Mercury for Online (client-side OpenRouter call). Gate MeAPI/LastDictationAPI/file upload by mode; Online file upload -> OpenAI direct (25MB limit -> compress/chunk) or disabled.
