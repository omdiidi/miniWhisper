---
title: Architecture
---

# WisprAlt Architecture

## System Overview

WisprAlt is a two-component system: a native macOS menubar client running on your MacBook and a FastAPI server running on your always-on Mac mini, connected via a Cloudflare Tunnel.

```
┌─────────────────────────  Client (MacBook Air M4)  ──────────────────────────┐
│ FNKeyMonitor (CGEventTap, serial queue)                                       │
│   └ hold ──▶ DictationRecorder  (AVAudioEngine)                               │
│ "Transcribe file…" ──▶ source file (any container) ──▶ FileJobAPI             │
│                ↓                                                              │
│         ServerClient (URLSession, bearer, streaming multipart, progress)      │
│         ChunkedUploader (large files, 50 MiB chunks)                          │
│                ↓                                                              │
│         TextInjector (AX → clipboard) for dictation                           │
│         SparkleController (bundled, auto-update disabled)                     │
└──────────────────────────────────────┬────────────────────────────────────────┘
                                       │ HTTPS
                          ┌────────────▼────────────┐
                          │ Cloudflare Tunnel        │
                          │ transcribe.<user-domain> │ (token in cloudflared keychain only)
                          └────────────┬────────────┘
                                       │
┌────────────────────  Server (Mac mini M4 16GB) ──────────────────────────────┐
│ uvicorn 1 worker → FastAPI (lifespan: load models, run staging+orphan sweep) │
│   /healthz  /readyz/dictation  /readyz/db                                    │
│   /me  (GET, PATCH)   ──▶ JSON self-service identity (label, display_name)   │
│   /v1/audio/transcriptions ──▶ OpenAI-compat shim → ParakeetService          │
│       (sync, dictate-only, 25 MB cap)                                        │
│   /transcribe/dictate ──▶ ParakeetService (warm, single-thread executor)     │
│       (raw text; server smart formatting removed in Part 4)                  │
│   /transcribe/file (+ /chunked/*) ──▶ stage → JobStore (SQLite) → runner     │
│                          ↓                                                    │
│                    FileJobRunner (1 job, dedicated executor)                  │
│                          ↓                                                    │
│       ffprobe → ffmpeg → files.pipeline: mlx-whisper turbo, mono              │
│                (model lazy-loaded, evicted when idle)                         │
│                          ↓                                                    │
│       output.py: JSON+SRT+VTT+TXT atomic write (tempfile in same dir)         │
│   /transcribe/file/{id}                ── poll (includes progress block)      │
│   /transcribe/file/{id}/download/{fmt} ── stream                              │
│   /transcribe/file/{id}                ── DELETE (202 cancel / 204 remove)    │
│   /admin/active           ── rich projection of in-flight job                 │
│   /admin/server-log/{id}  ── 100 lines bracketing the job in server.log       │
└───────────────────────────────────────────────────────────────────────────────┘
```

The Cloudflare Tunnel is **outbound-only** from the Mac mini — no inbound port-forwarding, no firewall rule changes required. The tunnel token is read from stdin during setup and persisted to `~/.config/wispralt/cloudflare-token` (mode 0600), referenced by the user-level `co.wispralt.cloudflared` LaunchAgent via `--token-file` (or inlined into a 0600 plist on cloudflared < 2025.4.0). It is never committed to the repo.

---

## Components

### Client

| Component | File | Responsibility |
|---|---|---|
| **FNKeyMonitor** | `client/WisprAlt/Hotkeys/FNKeyMonitor.swift` | CGEventTap (`kCGSessionEventTap`, `.listenOnly`) on `flagsChanged | keyDown`. Hops to serial private queue immediately. Tracks FN-DOWN time; detects the 300ms hold for dictation. (The meeting toggle gesture was removed with meetings.) |
| **DictationRecorder** | `client/WisprAlt/Capture/DictationRecorder.swift` | AVAudioEngine input tap that writes a native-format **Float32 PCM** CAF to the spool (`~/Library/Application Support/WisprAlt/spool/<id>.caf`; CAF stays readable after a crash) via `AVAudioFile.write(from:)`. Sample rate, channel count, and PCM format all match the tap's buffer exactly so AVAudioFile performs zero conversion — it streams the float bytes byte-for-byte to disk. Server (`audio.py`) resamples to 16 kHz and downmixes via `np.mean`. AVAudioConverter is intentionally avoided (default channel-mix sums channels rather than averaging, producing peak floats > 3.0). Int16 PCM output is also avoided (AVAudioFile's internal Float→Int16 converter applies a ~140x normalization that rail-clips the signal and destroys ASR accuracy). Tap callback dispatches each write onto a serial `ioQueue` to keep the realtime render thread free. Defensive in-tap clamp to ±0.95 catches any future regression of out-of-range floats. Streaming `chunkBuffer` memory: appended without exact-size `reserveCapacity` (geometric growth), cuts use `removeSubrange`, guarded by `chunkLock` (tap section and a `stop()` fence serialize on it), capped at ~120 s of pending audio by shipping it as a chunk (pure silence is dropped, speech never is), and taken (consumed) by `stop()`. `start(dictationId:endpoint:maxSeconds:)`: the first line is `guard !slot.isActive else { return false }` (Core `CaptureSlot` replaces `isRecording` and the public `streamSession`); a `DictationStreamSession` is created **only for a Local endpoint** (Online never streams) and captured by the tap closure; nothing in `start()` aborts a session; `slot.begin(session)` is the last step, so every throw leaves the slot free. `stop()` fences the tap and drains `ioQueue` / `chunkEncodeQueue`, closes the file, copies every per-dictation value into locals, then `slot.end()` (the next `start()` may run from there) and returns `RecorderOutput { recording: Recording, local: LocalCapture? }`; on a throw it aborts the session it took; the spool file is never deleted by the recorder (the dictation's `SpoolDisposition` owns it). A Local capture is re-encoded to native-rate Int16 (`LocalCapture.make`); a failed re-encode throws `.encodeFailed` (no Float32 fallback). The per-dictation cap (`maxSeconds`, 600 s for every model today) drops further tap audio and posts `.dictationMaxDurationReached` once with `userInfo["seconds"]` (the UI runs the FN-up path and names the limit). A device change records `engineStartFailed(NSError -11)`; `stop()` returns what was recorded with `interruptedByDeviceChange` (toast "Audio device changed — transcribed what was recorded"). |
| **TextInjector** | `client/WisprAlt/Inject/TextInjector.swift` | `@MainActor` strategy combinator. Captures `(FocusContext, AXUIElement?)` once via `captureFocus()` to close the TOCTOU window between security check and AX write. Refuses injection outright (no AX, no clipboard) when `WisprAltCore.shouldRefuseInjection(for:)` returns true (focused element subrole == `AXSecureTextField`); fires a 60-second-debounced local notification including the bundleID. Otherwise calls `AccessibilityInjector.tryInsertWith(element:text:)` and falls through to `ClipboardInjector` if AX cannot prove a value change (iMessages, Pane, every Electron app). Returns Core `InjectOutcome`: `.insertedVerified` (AX; clipboard untouched), `.pastedUnverified` (Cmd+V; the text stays on the clipboard), `.refusedSecureField` (nothing typed or copied), `.noTarget` (no frontmost app; copied only). |
| **AccessibilityInjector** | `client/WisprAlt/Inject/AccessibilityInjector.swift` | `tryInsertWith(element:text:)` accepts a pre-captured focused element from `TextInjector` (no internal focus walk → no TOCTOU). Sets a 250 ms `AXUIElementSetMessagingTimeout` to bound stalls on hung target apps, then `AXUIElementSetAttributeValue(kAXSelectedTextAttribute)` and reads `kAXValueAttribute` before/after. Returns true ONLY when the read-back proves a change — delegates the decision to the pure `WisprAltCore.didInjectionLand(...)` predicate (regression-pinned by 11 Swift Testing cases). The "empty before + write succeeded → assume success" heuristic is intentionally absent; that combination is the silent-no-op signature in Electron / custom NSTextView / iMessages compose. |
| **WisprAltCore** | `client/WisprAltCore/` (`InjectionPredicate`, `SecureFieldGate`, `FocusContext`, `FileJobContract`, `TranscriptsFolderDefault`, `KeychainItem`, `AppMode`, `OpenAIModels`, `OpenAIErrors`, `OpenAIRequests`, `Errors`, `Deadline`, `Audio/{MonoResampler,Mono16kWAV}`, `Dictation/{DictationProvider,CaptureSlot,LocalFinalizeRoute,OpenAIFileProvider,SpoolDisposition,RetryEntries,LastText}`, `Mercury/{MercuryPolicy,MercuryClient}`) | Pure-Swift library holding every testable decision (see "Online mode" below): `didInjectionLand(setSucceeded:beforeValue:afterValue:)`, `shouldRefuseInjection(for:)`, `FileJobContract.cancelledByUserReason` (the server's cancel reason, a silent end), `TranscriptsFolderDefault.resolve(stored:docs:exists:)` (transcripts-folder default), and the `Sendable, Equatable` `FocusContext` data type (`bundleID`, `pid`, `role`, `subrole`; `isSecureField` is derived from `subrole == "AXSecureTextField"` so callers can't construct an inconsistent context). No AppKit / ApplicationServices / Sparkle / resource dependencies — `Tests/WisprAltCoreTests` (Swift Testing) runs via `scripts/test-client.sh` / `swift test` without invoking actool or pulling Sparkle into the test runner. |
| **ClipboardInjector** | `client/WisprAlt/Inject/ClipboardInjector.swift` | `injectViaCmdV` writes the text and synthesizes Cmd+V (virtualKey 0x09, `.maskCommand` on both keyDown and keyUp); `copyOnly` writes it only. **Clipboard rule:** the pasteboard is never restored, because a Cmd+V paste cannot be verified and restoring could erase the only copy of a dictation that did not land. |
| **MenuBarController** | `client/WisprAlt/App/MenuBarController.swift` | Owns the activity state (main thread): `currentSession: DictationSession?` (set only after `recorder.start` returns true; the only "dictation in progress" check), `localUploadPhase` (Core `LocalUploadPhase`: `.none/.converting/.uploading/.polling`, mirrored to `RecordingState`), `localDoneFlash`, `onlineJobTask`, and the `pollGeneration` fence. The icon `mode` is **computed** (`ActivityState.icon(…)`, `Mode` = Core `ActivityIcon`) and never written or branched on. `isBusy` (`currentSession != nil \|\| dictationsInFlight > 0 \|\| fileJobsInFlight > 0`) is the single busy signal: the update relaunch refuses while busy. `beginRun` increments `dictationsInFlight` in the same main-actor turn that clears `currentSession`; the submit Task's `defer` decrements it. Only one file job runs at a time (`transcribePickedFile` ignores a second pick, and is Local-only for now). `applyModeEvent` / `applyModeSwitch(to:)` run Core `LaunchActions` (see "Online mode"). |
| **FileJobAPI** | `client/WisprAlt/Server/FileJobAPI.swift` | Poll / download / cancel for `/transcribe/file/{id}`. `cancel` returns the HTTP status; the caller cancels its poll Task, clears `activeJobID` and leaves the processing icon before the request; on 202 or 204 it clears `serverFinishingJobID` only if that still names the cancelled job. A later `failed: cancelled by user` poll result ends silently; any other `failed` clears `activeJobID` and its UserDefaults key (never resumed), while a poll timeout or transport error keeps the key so the next launch resumes. |
| **MicEnumerator** | `client/WisprAlt/Audio/MicEnumerator.swift` | Stateless static helper bridging AVFoundation device discovery to CoreAudio HAL property reads. `availableInputs()` uses `[.external, .microphone]` (macOS 15+ — `.builtInMicrophone` is deprecated, subsumed by `.microphone`); dedup by uniqueID. `audioDeviceID(forUID:)` translates an AV `uniqueID` to an `AudioDeviceID` via `kAudioHardwarePropertyTranslateUIDToDevice` (using `withUnsafePointer(to: &cfUID)` and `qualifierSize = MemoryLayout<CFString?>.size`). `systemDefaultInputName()` exposes the current default's localized name for the picker label. Powers the SwiftUI Input Mic picker in `SettingsView`. |
| **AppDelegate** | `client/WisprAlt/App/AppDelegate.swift` | App lifecycle. Exposes `static weak var shared: AppDelegate?` for cross-controller access. Recovers last session's spool into Retry (`recoverSpoolAtLaunch`; nothing is sent), warms the OpenAI key cache, then runs `menuBarController.applyModeEvent(.launch)`: in Local mode with a server URL + key it resumes a saved Local file job and fetches `/me` once; in Online it contacts neither. |
| **PermissionGate** | `client/WisprAlt/App/PermissionGate.swift` | Sequential 3-permission wizard: Accessibility → Input Monitoring → Microphone. On macOS 14.4+, posts a blocking "Quit and Reopen Required" sheet after Input Monitoring is granted (`CGRequestListenEventAccess` returns `true` but real grant requires process restart). |
| **SparkleController** | `client/WisprAlt/Update/SparkleController.swift` | Sparkle 2 wrapper. Defers the update sheet while `MenuBarController.isBusy`. Auto-updates intentionally **disabled** in be720a1 — `startingUpdater: false` + `SUEnableAutomaticChecks=false` in Info.plist. Tier 1.5 distribution rides on `install.sh` (curl one-liner; see docs/INSTALL.md) for fresh installs and `/wispralt-update` (Claude Code) for in-place updates. |

### Online mode (OpenAI direct)

Two modes, picked in the popover: **Online (OpenAI)** and **Local server** (`Settings.mode`; absent → `TranscriptionMode.default`, `.local` until Online file jobs land). A change applies to the next dictation.

- **FN press** (`dictationStart`, all synchronous): `DictationSnapshot` freezes `{id (lowercase UUID), mode, kind, model, endpoint}`; `RouteKind.decide(mode:modelKind:keyPresent:)` gives `.openAIFile`, `.local`, or `.needKey` (Online with no key: toast, the popover opens at the OpenAI key field, nothing records). Online reads only the OpenAI key (`OpenAIKey.current()`: memory cache, else a Keychain re-read, so a key added from Terminal works on the next press); Local reads only the Local server. `ProviderFactory.make` builds one fresh `OpenAIFileProvider` (nil for Local). `recorder.start(dictationId:endpoint:maxSeconds:)` gets the endpoint only for `.local`, so Online never streams to the Local host. `false` → "Still saving the last dictation — press again".
- **FN-up / cap / device change** → `beginRun` → `runDictation`: `recorder.stop()` → `DictationSession.route(local:)` → `.openAIFile(p)`: `Mono16kWAV` (16 kHz mono Int16, channels averaged by `MonoResampler`) → one `POST https://api.openai.com/v1/audio/transcriptions` (`audio.wav`, `json`, Bearer key; never `language`), one retry on 429/5xx/URLError, deadline `max(30, 10 + 0.5 × duration)` s via `withDeadline` (the timeout cancels the `URLSessionTask`, it never waits on the work); `.local(capture)`: `LocalServerProvider().finish(capture:)` → `DictationAPI.transcribe(capture:)` (`LocalFinalizeRoute`: stream finalize when a chunk went out, else whole file). Non-empty text is saved to `last.json` (Core `LastText`) **before** delivery, then `TextInjector.inject` returns an `InjectOutcome`, and `SpoolDisposition.decide(outcome, delivery:)` keeps or deletes the audio (see "Spool, Retry and last text"). The log line carries only `chars=`, `model=`, `mode=`, `saved=`, `truncated=`.
- **Smart formatting (Mercury, both modes)**: when `Settings.smartFormatting` is on, `MercuryStep.apply` runs after transcription and before the last-text save and delivery (also on a Retry). Core `MercuryClient` POSTs the text (never audio) to `https://openrouter.ai/api/v1/chat/completions` (`inception/mercury-2`, Bearer from Keychain `co.wispralt.openrouter` via `OpenRouterKey`, `X-Title: WisprAlt`) under a 2.5 s `withDeadline` whose timeout cancels the task; `MercuryPolicy` (prompt verbatim from the server, ≥ 20 words, 0.70–1.10 word-count window, `extractText` for every content shape) decides. Fail-soft: `DictationResult.formatting` is `.applied`, `.skipped("short" / "no key")` or `.failed("timeout" / "ratio" / "http" / "network" / "empty")` with the raw text; the popover shows it under the toggle. The client no longer sends `smart_format` / `X-Smart-Format`; server Mercury is removed in Part 4.
- **Errors**: `ErrorLog.describe` / `ErrorLog.outcome` are the only ways an `Error` reaches a log or a toast. `LogSafeError` conformances: `DictationRecorder.DictationError`, `ServerError` (status / kind only, never the body), `FileJobError` (never the server reason), `ProviderError`, `KeychainItemError`. An empty recording stays silent; `.deviceChanged` toasts "Audio device changed — press FN again".
- **Mode switch** (`applyModeSwitch(to:)` → `LaunchActions.decide(.switched(to:), state)`): to Online cancels only the Local poll task (`pollGeneration += 1`, `activeJobTask` cancelled; no server DELETE; the saved `wispralt.activeJobID` stays); to Local resumes polling a saved job only when no poller and no Online job runs; `/me` at most once per launch. Neither touches `currentSession`, so a recording finishes on its FN-press route. The picker is disabled while `localUploadPhase` is `.converting` / `.uploading` ("Finish or cancel the upload first").
- **Poll fence**: `transcribePickedFile` and `resumeInFlightJobIfNeeded` bump `pollGeneration` and pass the captured `gen` down (`handlePickedFile` → `processCustomTranscriptionUpload` → `runFileTranscriptionJob`, and `resumePollingForJob`). Every state write there (phase, done flash, `activeJobID` + defaults, `serverFinishingJobID`, progress fields, notifications, the `defer` that clears `activeJobTask`) runs only if `PollFence.mayWrite(isCancelled:captured:current:)`. `cancelActiveTranscription` bumps the generation first.
- **Popover status + model picker**: under the Mode picker a status line reads `Online · <model> · key saved|key missing` (Keychain presence only, no network) or `Local · <host> · reachable|unreachable` (`/healthz` runs only when the popover opens in Local or on a switch to Local, never in Online), plus `transcribing N` from `recordingState.dictationsInFlight` (mirrored by `MenuBarController.dictationsInFlight`'s `didSet`; not an icon state). Advanced → **OpenAI Model** lists every `OpenAIModels.all` row with `displayPrice` (`$0.0045/min`; `≈` for token-billed estimates) and `retireTag` (`retires Feb 26, 2027`), and the caption "Files always use a file model. Dictation stops at N min for this model."; it writes `Settings.openAIModel`, read at the next FN press. A dictation failing with `FailureKind.modelNotFound` resets the picker to its kind's default (`OpenAIModels.defaultID(for:)`) and toasts.
- **Online hides server-only UI**: connection / Test Connection, Server URL, API key + backup, Identity, Open Portal, Open My Dictations, the job Log button. "Transcribe file…" is disabled in Online for now; "Copy last dictation" reads the local `last.json` in both modes.

### Spool, Retry and last text

Files under `~/Library/Application Support/WisprAlt/` (folder 0700, files 0600, at rest protected by FileVault):

- `spool/<id>.caf` + `spool/<id>.json` (`SpoolSidecar {mode, model}`): the sidecar is written synchronously by `SpoolFiles.writeSidecarSync` in `dictationStart` **before** `recorder.start`, so audio on disk always knows its route (a failed write toasts "Could not save audio" and nothing records; a failed start removes it).
- After FN-up every file operation runs on the `SpoolStore` actor, serially. `SpoolDisposition.decide` (Core, table-tested): empty text / empty recording → delete, no toast; text delivered → delete (+ "No app to paste into — the text is on the clipboard" for `.noTarget`; a secure-field refusal adds nothing, `TextInjector` shows its own notice); possibly truncated → keep; a Local 200 with `"saved": false` → the text is delivered **and** the audio kept; text that never reached delivery → keep; any failure → keep, except the terminal Local `.rejected` (410 `dictation_deleted` / 409 `dedup_conflict`) → delete, never retried. A cancelled run arrives as `.failed(.other)` (kept).
- `keep` converts the spool to `retry/<id>.wav` (16 kHz mono via `Mono16kWAV`; the raw `.caf` is moved if conversion fails), moves the sidecar and applies `RetryEntries.plan`: newest 2, 7 days, no 0 s entries, no orphan sidecars; a 3rd entry drops the oldest with "Oldest saved recording discarded".
- **Launch**: the `spool/` ids are snapshotted in `applicationDidFinishLaunching` (later dictations have new ids); each `.caf` with duration > 0 goes to Retry, the rest and orphan sidecars are deleted; "N saved recordings from last session — see Retry". Nothing is sent.
- **Retry N** (popover): per entry "age · duration · mode" and `RetryEntries.actions(sidecar:currentMode:)`: a valid sidecar of the current mode → Retry, Export…, Discard; missing / corrupt → Export…, Discard; the other mode → Export…, Discard + "Switch to Local to retry" (or Online). Retry: Online → `OpenAIFileProvider(fileModel(for: sidecar.model))`; Local → `LocalServerProvider.retry` → `DictationAPI.wholeFile` to the **current** host with `X-Client-Dedup-Id: <id>` (the entry's dictation id). The result goes to the clipboard and `last.json`; the entry is deleted on success, kept on a Local `saved: false`, deleted on a terminal 410/409, kept on any other error.
- **Last text**: `last.json` (`LastText {text, at, mode, modelUsed}`, atomic 0600 write via `PrivateFile.write`) is read by "Copy last dictation" in both modes and expires after 7 days. It replaced the server `GET /me/dictations/last` call (`LastDictationAPI` removed).

### Server

| Component | File | Responsibility |
|---|---|---|
| **FastAPI app** | `server/src/wispralt/main.py` | Single uvicorn worker. Lifespan: requires `DATA_DIR`, migrates `wispralt.db`, validates `.env` permissions (warns if not 0600), runs `JobStore.recover_orphans()` BEFORE the staging sweep, loads Parakeet, then `await file_runner.reenqueue_pending()`. On exit it sets `shutting_down` and awaits `file_runner.shutdown()` first, before any loop cancel or store release. **The file model is NOT loaded here** — MLX Whisper loads lazily on the first file job via `files.pipeline._ensure_models_loaded()` and the eviction loop unloads it when idle. Registers SIGTERM handler, mounts routers. |
| **ParakeetService** | `server/src/wispralt/dictate/parakeet.py` | Warm-resident `mlx-community/parakeet-tdt-0.6b-v2` (float32 — bfloat16 caused a `[matmul] (128,257) vs (514,51)` shape error on first inference). Single `ThreadPoolExecutor(max_workers=1)` serializes all inference (MLX is not thread-safe per model instance). Warmup JIT pass at startup. Defensive return-type handling: checks `hasattr(result, 'text')` vs list of `AlignedToken`. Tracks p50/p95 latency percentiles over last 100 calls. **Audio decode boundary**: `_sync_transcribe` wraps `soundfile.read` in `try/except (LibsndfileError, RuntimeError) → CorruptAudioError` so malformed uploads convert to HTTP 422 (via the route handler) instead of leaking as 500. |
| **FileJobRunner** | `server/src/wispralt/jobs/runner.py` | `asyncio.Semaphore(1)` serialises execution; admission is decided by the set of spawned-but-unfinished worker tasks (`_workers`), so a second submit is refused even before the first worker has acquired the semaphore. Dedicated `ThreadPoolExecutor(max_workers=1, thread_name_prefix="wispralt-files")` isolates file-job work from the default asyncio thread pool. Busy or low-memory (< 2 GiB) submissions raise `FileJobInProgressError` with `code` `file_job_busy` / `insufficient_memory`, which the routes turn into a coded 429. `_run_source` owns the single `set_running` per attempt; `set_running` and `set_done` are conditional (`AND cancel_requested=0`), so a cancelled job can neither start nor finish `done` — it ends `failed: cancelled by user` with its outputs removed. The exception path also removes partial outputs. Staging files are cleaned up when the job ends, except when shutdown interrupted it (`CancelledError`, or `shutting_down` and the row still `running`): the input is kept and `requeue_interrupted` puts the row back to `pending` with its attempt refunded. `FileJobRunner(store, app.state)`; `shutdown()` cancels and awaits the workers, then `executor.shutdown(wait=False, cancel_futures=True)`. `reenqueue_pending()` called at startup for jobs whose inputs survived a restart (`MAX_ATTEMPTS = 3`). |
| **JobStore** | `server/src/wispralt/jobs/store.py` | `JobStore(db, data_dir)` on the one SQLite DB (`Database`: connection per call, `BEGIN IMMEDIATE` writes), so it is safe from any thread. `user_id` is a real FK to `users`; `wav_path` is stored relative to `data_dir` and returned absolute; outputs always live in `DATA_DIR/outputs`. Job lifecycle: `pending → running → done | failed`. `recover_orphans()` — see Failure Handling. |
| **files.pipeline** | `server/src/wispralt/files/pipeline.py` | `transcribe_file(...)`: `_ensure_models_loaded()` (lazy first-call load) → read the canonical WAV and average any channels to mono → resample to 16 kHz → **mlx-whisper turbo** (`mlx_whisper_loader.py`, `word_timestamps=False` hard-coded) → label every segment "Speaker 1" (`segments.py`) → atomic output write (`output.py`). Emits `phase_start`/`phase_done` structured logs at every seam; threads `progress_cb`/`cancel_cb` from the runner into the loader. Returns the v3 transcript dict (`mode="single"`, `model` lists the ASR model only). |
| **RateLimitMiddleware** | `server/src/wispralt/middleware/rate_limit.py` | In-memory per-IP rolling-window limiter. Two windows: `/transcribe/dictate` 60 req/60s; `/readyz/*` + `/healthz` 120 req/60s. Returns 429 with `Retry-After` header and no `code` field. File jobs are bounded by the runner, not the limiter. |
| **staging / env_writer** | `server/src/wispralt/ops/` | `staging.py` manages the staging directory; startup sweep removes orphaned WAVs older than 24h. `env_writer.py` atomically rewrites `.env` key-value pairs via tempfile-in-same-dir + `os.replace`, preserving mode 0600. |

---

**Lazy load and idle eviction.** The first file job after start (or after an eviction) pays a one-time load of MLX Whisper. Single-flight is enforced by a module-level `threading.RLock` (`_load_lock`) with double-checked locking around `_models_ready`. `ops/background.eviction_loop` calls `pipeline.evict_if_idle(settings.file_model_idle_eviction_seconds)` every minute: when the model is warm, no job is in flight and it has been idle past the threshold, it calls `_mlx_mod.reset()` (clears `mlx_whisper.transcribe.ModelHolder`), `gc.collect()`, `mx.clear_cache()` and `mx.reset_peak_memory()`, then runs a memory-delta self-check that increments `eviction_failures_total` and logs CRITICAL if the MLX pages did not come back. Observability snapshot: `pipeline.state() -> (warm, loading)` and `idle_seconds()`, surfaced on `/metrics` under the `meeting` key (name kept until a later schema pass).

---

## Processing Modes

There are two paths. Meetings (dual-channel recording + diarization) were removed in Part 2.

| Mode | Endpoint | Channel handling | Diarization | Model | `word_timestamps` |
|---|---|---|---|---|---|
| **Dictation** | `POST /transcribe/dictate` | mono (server downmixes) | n/a | Parakeet TDT 0.6B v2 (MLX, resident) | n/a |
| **File** | `POST /transcribe/file` (+ chunked) | any container; ffmpeg-decoded, channels averaged to mono | **no** | mlx-whisper large-v3-turbo (lazy, idle-evicted) | `False` (hard-coded) |

There is no `mode` field. The `jobs.request_mode` / `force_single_channel` / `output_dir` columns are gone (Part 4): every job is a file job, and its outputs are in `DATA_DIR/outputs`.

---

## MLX Whisper

WhisperX (CTranslate2 CPU int8 + wav2vec2 alignment) was replaced wholesale by [`mlx-community/whisper-large-v3-turbo`](https://huggingface.co/mlx-community/whisper-large-v3-turbo) in `server/src/wispralt/files/mlx_whisper_loader.py`. The model runs on Apple Neural Engine via MLX unified memory.

| Item | Value |
|---|---|
| Model repo | `mlx-community/whisper-large-v3-turbo` |
| Loader | `mlx_whisper_loader.load()` — one-shot warmup with 1s of silence; idempotent via module-level `_loaded` flag |
| Inference | `mlx_whisper_loader.transcribe_channel(audio_16k, *, word_timestamps, progress_cb, cancel_cb)` — wraps `mlx_whisper.transcribe` with a `tqdm.auto.tqdm.update` monkeypatch for chunk progress + a 5s wall-clock fallback if tqdm.update never fires |
| Disk footprint | ~1.6 GB at `~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo/` |
| Spike result (M4 mini, 16 GB) | 105 min audio (`audio_duration_s=6341.4`) → `transcribe_s=740.26`, **realtime ratio 8.57×**; wall clock 12.3 min including ffmpeg |
| Eviction | `mlx_whisper_loader.reset()` clears `mlx_whisper.transcribe.ModelHolder`; `pipeline.evict_if_idle` follows it with `mx.clear_cache()` so the unified-memory pages return to the OS (see Lazy load and idle eviction above) |
| Cancel semantics | Whisper transcribes the whole file in one call with no in-decode interrupt. A cancelled running job keeps the model and the job slot until that call returns; then the result is discarded (see Honest Limitations below) |

The wav2vec2 alignment pass that WhisperX did is **dropped**, and file jobs run without word timestamps (≈20% perf headroom; segments carry `words: []`).

**Historical:** through Phase 8 of the swap, `whisperx`, `ctranslate2`, and `faster-whisper` were retained in `pyproject.toml` as a revert-by-`git revert` safety net. Phase 10 (2026-05-10) deleted them along with `meeting/whisperx_loader.py` and `tests/test_whisperx_no_speech.py` after a 24h soak validated the swap. See [CHANGELOG-2026-05-10.md](CHANGELOG-2026-05-10.md) for the full deletion log + MLX RAM-reclaim follow-up patch.

---

## Observability

### Per-phase structured logs

`pipeline.py` and `runner._phase` emit greppable phase markers at every seam:

```
[<job_id>] phase_start name=ffprobe
[<job_id>] phase_done name=ffprobe duration_ms=82
[<job_id>] phase_start name=ffmpeg_decode
[<job_id>] phase_done name=ffmpeg_decode duration_ms=18432
[<job_id>] phase_start name=transcribe_load
[<job_id>] phase_done name=transcribe_load duration_ms=4127
[<job_id>] phase_start name=transcribe
[<job_id>] phase_done name=transcribe duration_ms=740258
[<job_id>] phase_start name=output_write
[<job_id>] phase_done name=output_write duration_ms=18
```

Phase names are the canonical keys used in `runner.PHASE_BUDGETS` / `PHASE_LABELS`: `queued → starting → ffprobe → ffmpeg_decode → transcribe_load → transcribe → output_write → done|failed`. (The diarize and merge phases left with the meeting pipeline.)

### Job-level progress on the poll response

`GET /transcribe/file/{id}` returns a `progress` block (and `serverFinishing: true` if `cancel_requested=1` AND status is still `running`):

```json
{
  "status": "running",
  "progress": {
    "phase": "transcribe",
    "phase_label": "Transcribing",
    "phase_started_at": 1715326123.4,
    "chunk_index": 47,
    "total_chunks": 211
  },
  "serverFinishing": true
}
```

`chunk_index`/`total_chunks` are populated for the `transcribe` phase only (driven by the tqdm monkeypatch's `progress_cb`). The client renders `phase_label` as-is and shows chunk progress only when `phase == "transcribe"`.

### Admin endpoints

- `GET /admin/active` — rich projection of the currently in-flight job: `id, status, mode, phase, phase_label, phase_elapsed_s, chunk_index, total_chunks, started_at, wav_path, audio_duration_s, attempts, cancel_requested, current_rss_mb`. Same auth dependency as `/metrics`.
- `GET /admin/server-log/{job_id}` — plain-text response: the 100 lines bracketing the job's first and last appearance in `settings.server_log_path` (resolved from settings, NOT hardcoded). Includes intervening lines (ffmpeg stderr, model warnings) for full-context diagnosis. The client's "View server log" sheet polls this every 5s.

**Role gate (v0.5.0).** `GET /admin/active`, `GET /admin/server-log/{job_id}`,
and `GET /metrics` are **admin-only** — previously they were behind
`require_api_key` (any authenticated bearer), which leaked operator-level
introspection to employee tokens. The dependency was tightened to
`require_admin` in `routes/admin.py` so employees get a 403 on these surfaces
and use the per-user `/me/*` routes (`/me`, `/me/insights`, `/me/history`,
`/me/dictations/last`) for their own data instead. The client's "View server
log" sheet handles the 403 with a friendly "Admin-only — ask your
administrator" message rather than a raw error.

---

## Honest Limitations

The cancel + per-phase-timeout story has **four real abort surfaces and two deferred ones**. This is documented up front so future debugging doesn't chase a non-existent kill primitive.

| Surface | Behavior |
|---|---|
| **`ffprobe` / `ffmpeg_decode` timeouts** | Real abort. Wrapped in `runner._phase` via `asyncio.wait_for` around a `run_in_executor` call. On timeout the executor task is cancelled and `set_failed` runs. |
| **`transcribe` timeout** | **Advisory only.** A separate `_phase_watchdog` task reads `phase_started_at` every 5s and calls `set_failed` if elapsed > 2× budget. The executor thread keeps running. The semaphore (`async with self._semaphore:` around `_run_source`) is held by the awaited executor and **is not released until that thread returns naturally.** New submissions get a real `429` with `code=file_job_busy`, which the client shows as "previous transcription still finishing" instead of a mysterious error. |
| **Cancel mid-upload** | Real abort. `URLSession.invalidateAndCancel()` on the client. |
| **Cancel mid-`ffmpeg_decode`** | Real abort. `staging.transcode_to_canonical_wav` runs ffmpeg as a `subprocess.Popen` and polls `jobs.cancel_requested` every 500ms; on True it `SIGTERM`s ffmpeg, cleans the `.partial` file, and raises `staging.StagingCancelled`. `_run_source` catches it, calls `set_failed(jid, "cancelled by user")`, exits the `async with` → semaphore released. |
| **Cancel while `pending`** | Real abort. `set_running` is conditional (`WHERE status='pending' AND cancel_requested=0`); when it refuses, `_run_source` deletes the staged source and records `failed: cancelled by user`. |
| **Cancel mid-`transcribe`** | **Deferred.** `DELETE` returns 202 and writes `cancel_requested=1`; the client stops polling and clears its job state at once. Whisper transcribes the whole file in one call, so the executor thread keeps the model and the job slot until that call returns; then the result is discarded. Same semaphore constraint as the advisory timeout. |

When `_run_pipeline_inner`'s executor eventually returns, `set_done` is conditional (`WHERE status='running' AND cancel_requested=0`) and returns whether a row changed. If it refuses (watchdog already wrote `failed`, or a cancel was requested), the outputs are removed, a cancelled job is recorded as `failed: cancelled by user`, and `transcript_text` stays NULL. On the DELETE side the status is re-read after setting the flag, so a job that finished in between is removed (204) rather than left `done` after a 202.

### Networking

The Cloudflare Tunnel maps `transcribe.<user-domain>` → `http://127.0.0.1:8000` on the Mac mini. `cloudflared` runs as a **user-level LaunchAgent** (`~/Library/LaunchAgents/co.wispralt.cloudflared.plist`), not as a system service — the `sudo cloudflared service install` path is broken on macOS 14/15+. The tunnel token is stored at `~/.config/wispralt/cloudflare-token` (mode 0600); the LaunchAgent reads it via `--token-file` on cloudflared ≥ 2025.4.0, or has it inlined in the plist (mode 0600) on older versions. The setup script (`scripts/setup-cloudflared.sh`) reads the token via `read -r -s` and unsets the shell variable immediately after writing the file.

Tunnel latency overhead: ~50–200ms same-region. The Cloudflare free tier has a community-reported body limit of approximately 100 MB; the server enforces `MAX_UPLOAD_BYTES` (default 2 GiB) at the application layer. Files over 50 MiB go through the chunked upload path below, so the edge cap never applies to them.

### Offline behaviour (no cloud fallback)

There is no cloud fallback for audio. When the Mac mini is offline, dictation retries the origin once (`DictationAPI`: origin, wait, origin; `X-Client-Dedup-Id` keeps a retry from double-persisting), then surfaces the existing dictation error toast. File jobs fail with the usual upload error; nothing is queued for later. `ServerClient.isOfflineSignature` still classifies offline errors for that retry decision. The OpenRouter Keychain item (`co.wispralt.openrouter`) is kept for Mercury smart formatting; it is not used for audio.

### Custom Transcriptions (menubar file picker)

The menubar QuickActions popover (`UI/SettingsView.swift`) exposes a "Transcribe file…" button that lets the user pick any audio/video container and upload it for transcription. The client copies the source **as-is** (no transcoding) into a per-job folder under `<transcripts folder>/Custom Transcriptions/` (default `~/Documents/WisprAlt/Transcripts/Custom Transcriptions/`, or `Meetings/` when that folder already existed), then POSTs to `/transcribe/file` via `FileJobAPI.submitFile`. Output JSON/SRT/VTT/TXT lands beside the source. `CustomTranscriptionsStore.swift` manages the per-job folders; `LastTranscriptCaption.swift` powers a "Copy last custom transcription" button with a live mtime caption (folder DispatchSource watcher + 10s timer + `Notification.Name.wisprAltTranscriptWritten` observer; see `Util/TranscriptNotifications.swift`).

`FileJobAPI.swift` uses `URLSessionConfiguration` with `timeoutIntervalForRequest=300` and `request.timeoutInterval=6*60*60` so a 90-min upload over a slow uplink can complete without hitting the default 60s inactivity timeout.

### Large-file uploads (audio extraction + chunked upload)

Cloudflare's free / pro / business plans cap inbound request bodies at **100 MB**. A 1-hour stereo WAV is ~460 MB, and a 3-hour AAC m4a can easily clear 200 MB — both would 413 at the edge before reaching the origin. Two complementary strategies extend the supported range to 4 GB without touching Cloudflare's plan tier.

**Step 0 — Audio extraction (`client/WisprAlt/Capture/AudioExtractor.swift`).** Before deciding which upload path to take, the client passes the user-picked URL to `AudioExtractor.extractAudioIfVideo(_:)`. If the asset has BOTH a video and an audio track, it runs `AVAssetExportSession` with `AVAssetExportPresetPassthrough` and writes just the audio track to a temp `.m4a` (or `.mp4` fallback when the source codec isn't AAC-compatible). A 200 MB MP4 typically becomes a ~15 MB audio file — often dropping under the chunking threshold entirely and letting the existing single-shot `/transcribe/file` path serve the request. The extractor never throws: any probe / export failure returns the original URL so the upload still proceeds.

**Step 1 — Chunked upload (`client/WisprAlt/Server/ChunkedUploader.swift` ↔ `server/src/wispralt/routes/transcribe_file.py`).** If `uploadSize > 50 MiB`, the client switches from `FileJobAPI.submitFile` (multipart single-shot) to `ChunkedUploader.upload`. The three-step wire protocol is:

1. `POST /transcribe/file/chunked/init` with JSON `{total_bytes, chunk_count, original_filename}` → server returns `{upload_id, chunk_size}` (22-char `secrets.token_urlsafe(16)`) and creates `staging/chunked/<upload_id>/meta.json` recording the caller's `user.id`.
2. `POST /transcribe/file/chunked/<upload_id>/<chunk_index>` with raw bytes (`application/octet-stream`, NOT multipart) for each 50 MiB chunk. The body is streamed via `request.stream()` to `chunk-NNNN.part.tmp`, then atomically renamed on success. `Content-Length` is mandatory and validated against bytes-written (corrupt chunks → 400). The path declares `{chunk_index:int}` — Starlette's `:int` converter constrains this segment to digits so `/finalize` cannot match the chunk route. Without this constraint, Starlette's registration-order matching sent every `/finalize` request into the wildcard chunk handler with `chunk_index="finalize"`; Pydantic int-coerce then produced a RequestValidationError → 422 with `{"detail":[…]}`, which the client surfaced as the misleading "appears incomplete or corrupted" banner.
3. `POST /transcribe/file/chunked/<upload_id>/finalize` with an empty body. Server verifies all chunks present + `Σ(part.size) == total_bytes`, concatenates in `run_in_executor` (deletes each `.part` as it copies to halve peak disk), then hands the assembled file to the SAME `FileJobRunner.submit_source_or_429` that the single-shot route uses. Returns `{job_id, status: "pending"}` — from here the existing poll/download/delete loop is unchanged.

**Resilience properties:**

- *Auth ownership.* The user.id from the `/init` caller's bearer token is recorded in `meta.json`; every subsequent chunk + finalize request is rejected (403) unless its bearer resolves to the same id. Break-glass admin (id=−1) is refused at `/init` because cross-request ownership cannot be reliably verified for that path.
- *Per-chunk bounds.* `Content-Length ≤ 50 MiB + 1 KiB slack` is enforced both up front (411 if header missing, 413 if declared too large) AND during the stream loop (413 if observed bytes exceed the ceiling — defends against chunked-transfer-encoded clients).
- *Disk + RAM gates.* `/init` requires `free ≥ 2 × total_bytes`; `/finalize` re-checks `free ≥ total_bytes` and `psutil.virtual_memory().available ≥ 4 GiB` before submitting to the runner.
- *Cloudflare 100s ceiling.* `/init` rejects `total_bytes > 4 GB` so a multi-GB concat never blocks the finalize handler beyond Cloudflare's proxy timeout (Mac mini M4 copies 4 GB in ~8 s — well inside budget).
- *Stale-upload TTL.* `ops/staging.py:sweep_chunked()` reaps any chunked directory whose `meta.json` mtime is older than 1 h (separate from the 24 h plain-WAV sweep, since abandoned chunked dirs pin much more disk per item). Each successful chunk write `touch`es `meta.json` so an active client is never reaped underneath itself.
- *Cancel.* One `URLSession` is created at the start of the chunked upload and reused across `/init`, every `/chunk`, and `/finalize`. `MenuBarController.cancelActiveTranscription()` calls `invalidateAndCancel()` on that single session — every in-flight chunk task tears down together.
- *Progress smoothness.* `URLSessionTaskDelegate.didSendBodyData` reports per-byte progress folded into an overall `[0,1]` fraction (`(chunksDone + chunkFraction) / totalChunks`). This keeps `lastUploadProgressAt` ticking within a slow 50 MiB chunk so the existing 120 s stall-watchdog never fires mid-chunk on a poor link.
- *Transient errors.* Each chunk is retried once on `URLError` (network reset, dropped connection). Beyond that, the upload tears down via the cancel path and the staging dir is reaped by the next sweep. Specific failure modes (`ChunkedUploaderError.initFailed` / `.chunkUploadFailed` / `.finalizeFailed` / `.fileTooLarge`) are mapped to user-readable strings via `MenuBarController.uploadErrorMessage(for:)` and surfaced through `recordingState.uploadError` so the popover renders a red "Upload failed" banner instead of dismissing silently.
- *Finalize UX.* `ChunkedUploader.upload` emits a `ChunkedPhase` callback (`.initRequest` / `.chunk` / `.finalize`); `MenuBarController` flips `recordingState.isFinalizing = true` for `.finalize` so the popover renders an indeterminate "Finalizing" progress bar during the 1-10 s server-side concat window instead of the misleading "Uploading 99%". The chunk-upload start, per-chunk elapsed time, server-side per-chunk receipt, and finalize concat elapsed are all logged at INFO via the `transcribe` OSLog category and the server's root logger respectively.

---

## Latency Budget (Dictation)

| Stage | Budget |
|---|---|
| FN release detection | <5ms |
| Mic buffer finalize | ~30ms |
| WAV encode in-memory | <5ms |
| Upload to CF edge | ~30–80ms |
| CF → Mini tunnel | ~50–150ms |
| Parakeet warm inference | ~80–200ms |
| Response back | ~50–150ms |
| AX / Cmd+V injection | <10ms |
| **p50 total** | **~250–400ms** |

The first dictation request after server start is slower (300ms–2s extra) due to MLX Metal kernel JIT compilation. The warmup pass in `ParakeetService.load()` runs at startup to front-load this cost.

---

## Streaming dictation (opt-in, experimental)

**Goal.** Drop *perceived* latency on long dictations (≥8 s of speech) by transcribing silence-bounded chunks **during** recording rather than uploading the full WAV at FN release. Wall-clock total Parakeet work is unchanged — the win is that by the time the user lifts FN, most chunks have already been transcribed and only the tail needs to clear the wire. Honest p50 reduction on the M4 mini is ~30–45% for utterances above the bypass threshold; short utterances bypass streaming entirely and use the existing single-shot path so they pay zero cost for the feature being available.

This whole feature is **off by default** and additive. Every existing dictation contract (route shape, response envelope, persistence semantics, fallback ladder) is preserved bit-identically when the toggle is off and when the `X-Client-Dedup-Id` header is absent — see [Validation gates §4–6 in the plan](../tmp/ready-plans/2026-05-18-streaming-dictation-chunks.md#validation-gates) for the byte-identity test that locks this in.

### Opt-in surface

Streaming runs only for **Local** dictations: the recorder creates a `DictationStreamSession` only when `start` is given a Local endpoint, so an Online dictation never sends chunks. The former `Settings.streamingDictation` UserDefaults flag has been removed from the client (it was unused). Two further client-side bypasses ensure the streaming path only runs when it can win:

- **Speech-length bypass.** If `EnergyVAD` reports cumulative voiced-frame time `< 8 s` at FN release, `DictationRecorder` discards any pending streaming session and uploads the full WAV via the existing `DictationAPI.transcribe` ladder. Short dictations would not amortize the streaming protocol's session-open + chunk-overhead round-trips.
- **Bootstrap bypass.** If the first chunk has not been emitted by FN release (e.g. the speaker never paused), the recorder falls through to the single-shot path. The streaming session that was opened on FN press but never advanced is left to the server-side sweeper (900 s TTL).

### Client architecture

| Component | File | Responsibility |
|---|---|---|
| Energy VAD | `client/WisprAlt/Capture/EnergyVAD.swift` | Pure-Swift RMS-based detector. Hysteresis (separate enter-voice / leave-voice thresholds) prevents flapping; a rolling noise floor (last ~1.5 s of minimum RMS) lets the gate self-calibrate to mic + room. 30 s hard cap on a single voiced run forces a chunk cut even if the speaker never pauses. Minimum-chunk gating drops chunks below `minChunkFrames` or whose average RMS is under `minChunkAverageDB` so silence-only or single-cough chunks never round-trip. |
| Streaming session actor | `client/WisprAlt/Server/DictationStreamSession.swift` | Owns one session's `UUID` (`sid`), `chunkIndex`, `clientDedupID`, and a long-lived `URLSession`. Mints `sid` lazily on the first chunk POST; chunks POSTed in order to `/transcribe/dictate/stream/{sid}/chunk/{i}`; FN release flushes the tail via `/finalize`. Falls back silently to `DictationAPI.transcribe` on any chunk 4xx/5xx or stream error — the safety buffer (see below) means the full WAV is always available locally. |
| Recorder integration | `client/WisprAlt/Capture/DictationRecorder.swift` | Streaming-only branch added inside the tap callback. Voiced frames accumulate in a sliding window; on a silence cut the recorder hands the cut WAV (and its starting sample offset) to the active `DictationStreamSession`. The non-streaming branch is unchanged — same `AVAudioEngine`, same Int16 PCM in-memory WAV at FN release. |
| Safety buffer | `client/WisprAlt/Capture/DictationRecorder.swift` | A **separate full-utterance WAV** is always written in parallel to the chunked stream. If `DictationStreamSession.finalize` fails or times out, the recorder reverts to the existing origin → retry ladder on the safety WAV (there is no cloud rung). The user-facing failure mode never gets worse than today. |

### Server architecture

The server side is intentionally small and lives in two new modules:

- `server/src/wispralt/dictate/streaming_session.py` hosts `StreamingSession` (dataclass: `sid`, `api_key_id`, `status ∈ {active, finalizing, done, aborted, expired}`, `partial_texts: list[str | None]`, `pending_tasks: dict[int, asyncio.Task]`, `cumulative_audio_ms`, `last_seen`, `lock: asyncio.Lock`) and `StreamingSessionStore` (the in-process registry).
- `server/src/wispralt/routes/dictate_stream.py` exposes the two HTTP surfaces. See [API.md → `POST /transcribe/dictate/stream/...`](API.md#post-transcribedictatestreamsession_idchunkindex) for wire-level details.

**Capacity caps (in-process, intentionally tight):**

- **2 active sessions globally.** Returns `503 capacity_exceeded` on the 3rd open. Sized to match the mini's single-Parakeet-executor reality plus one buffer slot.
- **1 active session per `api_key_id`.** Returns `409 per_user_busy` if the same user opens a second session while their first is still active. Forces clients to either reuse the existing `sid` (idempotent — `open_or_get` returns `existing`) or wait for the prior `finalize` to land.
- **270 s cumulative voiced audio mid-chunk; 300 s at finalize.** Mid-chunk cap → `413`. Finalize cap → `413`. Keeps any single session inside Parakeet's comfortable VRAM working-set even under HOL contention.
- **900 s session TTL.** A background sweeper task (`_streaming_sweeper`) reaps any session whose `last_seen` is older than 900 s, regardless of status. On reap, pending chunk tasks are cancelled and the slot is freed.
- **15 s finalize timeout.** `finalize` waits up to 15 s for all `pending_tasks` to drain before snapshotting `partial_texts` and joining. Past 15 s the route returns `504 timeout` and the session is marked `aborted`; the client safety buffer kicks in.

**Single Parakeet executor (no second instance).** Every chunk task is submitted to the same `ParakeetService` `run_in_executor` that `/transcribe/dictate` uses. There is no second model load, no second executor, no second thread pool. This means cross-session head-of-line blocking exists on a shared queue — see Known limitations below. With the 2-session cap, the worst case is 2 chunks contending and is acceptable for the experimental rollout.

**Joined text is returned raw.** Each chunk's Parakeet output goes into `partial_texts[index]`. On `finalize` the texts are joined with `" "` separators and returned as-is; server smart formatting (Mercury) was removed in Part 4.

### Idempotency

Both the streaming-finalize path and the single-shot fallback path (origin retry) write to the SAME `dictations` row by reusing one `client_dedup_id` UUID v4 per utterance:

- The client's dedup id is the dictation id minted at FN press (`DictationSnapshot.id`, lowercase UUID v4); `DictationStreamSession(endpoint:dictationId:speechStartedAt:)` takes it, so there is no second id.
- Core `LocalRequests` sends it as `X-Client-Dedup-Id` on **every** chunk, finalize and whole-file request; `POST /finalize` also sends it as the `client_dedup_id` form field.
- If finalize fails and the safety-buffer fallback wins, the same id goes to `POST /transcribe/dictate`.
- Every Local request goes to the `ServerEndpoint` captured at FN press (URL + key), never `Settings.serverURL`, so changing the host mid-dictation leaves the in-flight one on its host. `LocalServerProvider` bounds the whole Local finish by `max(30, 10 + 0.5 × duration)`; on timeout it aborts the session and cancels the request.
- Part 4 contract, already handled by the client: a 200 with `"saved": false` is delivered as usual and surfaces as `DictationResult.serverSaved == false` (`SpoolDisposition` keeps that audio and its Retry entry); 410 `{"error":"dictation_deleted"}` and 409 `{"error":"dedup_conflict"}` are terminal (`LocalDictationError`, `FailureKind.rejected`): no whole-file fallback, no retry, and the audio and Retry entry are discarded. A Local Retry reuses the entry's dictation id as `X-Client-Dedup-Id`. A 409/410 with a `detail` body (e.g. session not open) is not terminal.
- The `idx_dictations_client_dedup` **partial unique index** (`WHERE client_dedup_id IS NOT NULL`, see `jobs/store.py`) combined with `ON CONFLICT (client_dedup_id) DO NOTHING` guarantees **at most one row per utterance** no matter which path wins.
- When `X-Client-Dedup-Id` is absent (today's non-streaming clients, and `/v1/audio/transcriptions`), the column stays NULL and the partial index has no effect — backward compatibility is bit-identical.

### Honest fallback latency

The latency story when streaming wins is the §"Latency Budget (Dictation)" table above, minus the chunks that were already on the wire. The latency story when streaming **loses** is today's full ladder plus a small detection budget:

- Up to 15 s waiting on `finalize` to drain pending chunks (the cap).
- ~50 ms client-side fallback dispatch + safety-buffer reload.
- The existing origin → retry ladder runs on the safety WAV.

The safety buffer is always written, so a streaming failure costs the user **at worst** their normal single-shot latency + ≤15 s detection time. There is no path where a streaming failure produces a *worse* response than today's offline single-shot does.

### Known limitations

- **Cross-session head-of-line blocking on the shared executor.** With the 2-session global cap, session B's first chunk waits behind session A's in-flight chunk. At 2 sessions this is bounded and acceptable; raising the cap will require either a dedicated streaming executor or a smaller `parakeet-mlx` instance. Out of scope for v1.
- **`duration_ms` semantics drift.** On streaming rows, the persisted `duration_ms` reflects *cumulative* per-chunk inference time (sum of N Parakeet calls), not single-pass wall-clock. The single-shot path persists single-pass wall clock. Both are still useful as "server CPU spent on this utterance" but cannot be compared 1:1 across rows. The `audio_duration_s` column proposed in seq-17 carryovers (still deferred) would resolve this by separating audio length from inference cost.
- **In-process state.** The store is intentionally in-process — no Redis, no disk persistence. On a server restart, every active session is lost; the next chunk POST gets `410 session_not_found` and the client falls back. Acceptable because (a) restarts are rare, (b) the safety buffer always recovers the user-visible outcome, and (c) the dedup id keeps the row count to exactly one.
- **No streaming on `/v1/audio/transcriptions`.** The OpenAI-compatible shim is sync-only; streaming dictation is a WisprAlt-native feature that requires the additive header + session URL shape.

### Where this connects

| Concern | File:line landmark |
|---|---|
| Endpoint registration | `main.py` lifespan after `parakeet_service.load()`; `include_router(dictate_stream_routes.router)` after `dictate.router` |
| Sweeper lifecycle | `main.py` lifespan creates `_streaming_sweeper` task; shutdown cancels + aborts active sessions |
| Per-session lock contract | `streaming_session.py` — every reader/writer of `status / partial_texts / pending_tasks / last_seen / cumulative_audio_ms` acquires `session.lock` before mutating; `finalize` snapshots under lock before releasing for the `asyncio.gather` await |
| Dedup-id persistence | `jobs/store.py` — `dictations.client_dedup_id` column + `idx_dictations_client_dedup` partial-unique index (added in Plan A, reused here) |
| Header passthrough on non-streaming | `routes/dictate.py` — additive read of `X-Client-Dedup-Id` (UUID v4 form), forwarded to `insert_dictation(..., client_dedup_id=...)` |

---

### File Upload Latency

A 1-hour stereo 16kHz WAV is approximately 460 MB; compressed sources (m4a, mp3, video with the audio track extracted) are far smaller. At 100 Mbps symmetric a 460 MB file takes ~37 seconds to upload. `RecordingIndicatorView` shows three explicit states with progress: **Uploading (%)** via `URLSession` upload-progress callbacks, then **Processing** (server pipeline), then **Done**.

---

## Concurrency Model

```
FastAPI event loop (single thread)
    │
    ├── GET /healthz, /readyz/* → synchronous, no blocking
    ├── POST /transcribe/dictate
    │       └── ParakeetService.transcribe()
    │               └── loop.run_in_executor(
    │                       ParakeetService._exec,  ← ThreadPoolExecutor(max_workers=1)
    │                       ParakeetService._sync   ← serialized; MLX not thread-safe
    │                   )
    │
    └── POST /transcribe/file (+ chunked finalize)   (ffprobe + ffmpeg in worker)
            └── FileJobRunner.submit_source_or_429()
                    ├── Check a worker is queued/running (`_workers`) → 429 code=file_job_busy (non-blocking)
                    ├── Check available RAM < 2 GiB → 429 code=insufficient_memory
                    └── _spawn(): create_task(_run_source()) tracked in `_workers`
                            └── async with asyncio.Semaphore(1)
                                    └── loop.run_in_executor(
                                            FileJobRunner._executor,  ← dedicated 1-worker pool
                                            files.pipeline.transcribe_file
                                        )
```

The FastAPI event loop is never blocked by inference work. While a file job runs (`file_runner.active`) or the file model is loading (`files.pipeline.is_loading()`), `GET /readyz/dictation` adds `X-Dictation-Degraded: true` to signal that dictation inference may be slower due to unified memory pressure.

---

## Memory and Resource Model

| Component | Resident Memory |
|---|---|
| Parakeet TDT 0.6B v2 (MLX float32) | ~3.5–4.0 GB unified (≈2× bf16) |
| mlx-whisper large-v3-turbo (MLX, ANE/unified memory) — only while a file job runs or before idle eviction | ~2.5–3.0 GB |
| Python / FastAPI process | ~0.5 GB |
| **Total resident (file model warm)** | **~6.5–7.5 GB** |
| **Peak observed across the transcription matrix** | **< 14 GB** (gate criterion for Phase 10, measured with the old meeting rows) |

The Mac mini M4 16 GB configuration leaves ~8.7 GB for the OS and other processes. `FileJobRunner.submit_source_or_429()` rejects a new file job (429 `insufficient_memory`) if `psutil.virtual_memory().available < 2 GiB`; the route boundary returns 503 below 4 GiB before streaming the upload.

**Cold-start footprint:** before any file job has run, only Parakeet + Python/FastAPI are resident (~4.0–4.5 GB). mlx-whisper is loaded lazily on the first file job (see `files.pipeline._ensure_models_loaded`) and evicted again once idle past `FILE_MODEL_IDLE_EVICTION_SECONDS` (default 60): `pipeline.evict_if_idle` resets the loader and calls `mx.clear_cache()`, and a memory-delta self-check confirms the pages came back.

**Disk guard:** `staging.stream_to_staging()` checks that free disk is at least 1.5x the upload size before accepting a file. `GET /metrics` reports `disk.free_gb` and `disk.staging_count` for monitoring.

**Device matrix:** MLX for Parakeet **and mlx-whisper** (Apple Neural Engine / GPU unified memory). The CTranslate2 CPU-int8 path that powered WhisperX and the PyTorch MPS diarization path have both been retired.

---

## Failure Handling

### SIGTERM and graceful shutdown

The SIGTERM handler (registered in the lifespan when running on the main thread) only sets `app.state.shutting_down = True` and asks uvicorn to exit; it no longer fails running jobs. The lifespan shutdown then:

1. sets `shutting_down` and awaits `file_runner.shutdown()` FIRST, while the DB is open: each worker is cancelled; a job it interrupts keeps its staged input (WAV or source) and `requeue_interrupted` sets the row back to `pending` with the attempt `set_running` took refunded, so a deploy never spends an attempt;
2. cancels the daemon loops and streaming sessions, closes the insights client;
3. drops the stores (`app.state.db` etc.).

A worker that finished (`done`/`failed`) during shutdown cleans up its input as usual. The executor thread itself cannot be cancelled; it is abandoned (`shutdown(wait=False, cancel_futures=True)`).

### Orphan Recovery (`recover_orphans`)

Called once at startup, BEFORE the staging sweep (so a requeued input is never swept):

- `running` (the server died mid-job) → `pending` (phase cleared) when its input exists, no cancel was requested and `attempts < 3`; otherwise `failed` with `cancelled by user`, `staging file missing after restart` or `max retries exceeded`. A crash still spends the attempt.
- `pending` + input missing → `failed`; `pending` + truncated WAV → `failed` and the WAV unlinked.
- every surviving `pending` row is re-submitted by `FileJobRunner.reenqueue_pending()` on its absolute input path.

### Staging Sweep

`ops/staging.sweep_old()` is called at startup and removes staging WAV files older than 24 hours that are no longer referenced by a pending or running job.

---

## Authentication

`/healthz`, `/readyz/*`, and `/admin/login` are intentionally **unauthenticated** so Kubernetes-style probes, Cloudflare health checks, external monitoring, and the admin login form can be reached without API credentials. The probes expose only a ready-flag boolean and the `X-Dictation-Degraded` header — no user data, no audio, no model output.

Every other route requires `Authorization: Bearer <token>` (or, for browser pages, the session cookie).

### Auth (local SQLite, no cache)

```
Bearer <plaintext>  (or wispralt_admin_token cookie; never on /v1)
   │
   ▼
sha256(plaintext) ──► AccountStore.lookup_key(token_hash)      (asyncio.to_thread)
                        SELECT … FROM api_keys k JOIN users u ON u.id = k.user_id
                         WHERE k.token_hash = ? AND k.revoked_at IS NULL
                           AND u.disabled_at IS NULL
   │ row          → User(id, key_id, label, role, kind) → request.state.user,
   │                request.state.auth_via = "bearer" | "cookie"
   │ no row       → 401
   │ sqlite3.Error → 503 "Auth temporarily unavailable" (there is no fallback admin)
```

Source: `server/src/wispralt/auth.py` + `server/src/wispralt/accounts/{store.py,service.py}`.

Key invariants:

- **Lookup on every request.** There is no token cache: one indexed join per request (about the cost of a cache check), so a revoke, disable or rotate, made in this process or by another one (the Part 5 CLI), applies to the very next request.
- **Identity vs credentials.** `users` holds identity (`role admin|employee`, `kind employee|integration`, `disabled_at`); `api_keys` holds credentials (`token_hash`, `revoked_at`). Rotating a user's key revokes its active keys, inserts one new key and clears `disabled_at` in one transaction (`AccountStore.rotate_user_key`); the portal's Revoke disables the user.
- **Plaintext tokens exist only in `accounts/service.py`** and the one page or terminal that shows them. Logs carry ids only.
- **First admin:** `python -m wispralt.bootstrap --label NAME` (TTY-only) creates it and prints the client config once; `--rotate-admin` replaces a lost admin key. There is no env admin key and no break-glass path.
- **`request.state.user`** is set for every authenticated request; the usage middleware reads it.
- **Cookie fallback:** `require_api_key` falls back to the `wispralt_admin_token` cookie when no Bearer header is present (`/v1` never does). Set by `POST /admin/login` / `POST /me/login`; `HttpOnly`, `Secure`, `SameSite=Strict`, `max_age=8h`.

#### `users` and `api_keys` (SQLite, `migrations/0001_initial.sql`)

| Table | Columns |
|---|---|
| `users` | `id`, `label`, `display_name` (self-managed via `PATCH /me`, 1–40 chars, no control chars), `notes`, `role` (`admin`/`employee`), `kind` (`employee`/`integration`), `created_at`, `disabled_at`, `source`/`source_id` (Part 7 import) |
| `api_keys` | `id`, `user_id` → `users`, `token_hash` (sha256, unique), `label`, `created_at`, `revoked_at` |

Timestamps are REAL unix seconds; the repository returns UTC `datetime`s. The admin UI's user list renders `display_name (label)` when both are set. See [ADMIN.md](ADMIN.md).

### Usage event tracking (in-request, best-effort)

After `call_next`, `_ObservabilityMiddleware` (main.py) writes **one** `usage_events` row with `await asyncio.to_thread(UsageStore.record, event)` (about 1 ms) when:

- auth resolved a user (`request.state.user`),
- the route key is tracked and the method is POST (status-poll GETs are excluded), and
- the request was not a dedup replay (`request.state.dedup_replay`).

Best-effort: a `sqlite3.Error` loses that row, increments `usage_write_failures_total` and logs only the error type; the response is unaffected. The counter is exposed in `/metrics` and in the `/readyz/db` body, so a loss is visible. There is no queue and no background drainer.

Tracked routes: `transcribe/dictate`, `transcribe/file` (`kind = "file"`), `v1/audio/transcriptions` (`kind = "v1_dictate"`), plus the `/v1/models` and translations probes, **POST only**. Each stream chunk POST still records a row (request telemetry). The admin overview's Dictations 24h/7d/30d tiles count `dictations` rows (`JobStore.dictation_counts`), not requests; the user-detail page's tiles still count that user's `usage_events` requests.

### Admin UI

```
┌────── /admin (FastAPI APIRouter) ─────────────────┐
│                                                     │
│  public_router  ─► /admin/login (GET, POST)        │  no auth
│                                                     │
│  me_router      ─► /admin/me                       │  Depends=[require_api_key]
│                                                     │  any role — admin or employee
│                                                     │
│  authed_router  ─► everything else                  │  Depends=[require_admin]
│      /admin/                                        │
│      /admin/users  /admin/users/{id}               │
│      /admin/users/{id}/mint  …/revoke              │
│      /admin/usage  /admin/usage.csv                 │
└─────────────────────────────────────────────────────┘
```

Source: `server/src/wispralt/routes/admin_ui.py` +
`server/src/wispralt/admin/templates/*.html.j2`.

Three-router pattern (be720a1 split out `me_router`):

- **`public_router`** carries `/admin/login` (GET form + POST submit).
  Must be reachable without auth, otherwise neither role has a way to
  acquire the session cookie. **The login form accepts ANY valid
  token** — admin and employee — and redirects by role on success: admin
  → `/admin/`, employee → `/admin/me`.
- **`me_router`** carries `/admin/me`, gated by `require_api_key` (NOT
  `require_admin`). Admins are 303'd to `/admin/`. Employees see their
  own `user_detail` page; admin nav (Overview/Users/Usage) is hidden by
  `base.html.j2` for non-admin sessions and replaced with a single "My
  Usage" link. Header title flips to "Wispralt Portal".
- **`authed_router`** has `dependencies=[Depends(require_admin)]`, so a
  browser hitting `/admin/` without a valid admin token gets 401/403.
  Every page reads `AccountStore` / `UsageStore` in a worker thread.

Auth model: `Authorization: Bearer ...` (curl/Postman) or
`wispralt_admin_token` cookie (browser, set by `POST /admin/login`).
`auth._extract_bearer` falls back to the cookie when the header is
absent. CSRF is mitigated by `SameSite=Strict` on the cookie — browsers
refuse to attach it to cross-site POSTs.

The macOS client's menubar **Open Portal** button opens
`<server>/admin/login` for everyone — the same install ships to admins
and employees without a per-role configuration. The server's
`login_submit` redirect handles the role-based fork.

Templates use the `.html.j2` extension. Jinja2's default autoescape list
does **not** include this extension; `select_autoescape(enabled_extensions=("html.j2", "html", "j2"))`
is set explicitly so user-supplied fields (label, notes, error_class)
render escaped, closing the stored-XSS hole on the admin UI.

---

## Background maintenance and logging

`ops/background.py` hosts the lifespan's daemon loops. `daily_sweep_loop` runs every 24 h (in a worker thread): stale staging WAVs (>24 h, excluding WAVs of active jobs), stale chunked-upload dirs (>1 h), stale file-job output `.tmp` files, `sweep_transcripts` (below), and file-job retention (`sweep_old_meetings`, name kept) — finished (`done`/`failed`) job rows plus their `<id>.{txt,srt,vtt,json}` files older than `MEETING_JOB_RETENTION_DAYS` (default 90, 0 disables; pending/running and any job the runner still tracks as active are never touched; files are removed from each job's persisted `output_dir`, which must resolve inside `MEETING_OUTPUT_DIR`, and a job whose files fail to delete keeps its row for the next sweep). The same staging sweeps also run once at startup.

Logging: launchd cannot rotate `server.log` / `server.err.log` without sudo, so `ops/logging_setup.configure_file_logging` sends root and uvicorn loggers to a `RotatingFileHandler` at `settings.server_log_path` (default `~/Library/Logs/WisprAlt/wispralt.log`, 10 MB x 5 backups). The launchd files now only capture crash output and the one-off `NEW_API_KEY=` print on key rotation. `/admin/server-log/{job_id}` reads `wispralt.log` plus its rotated backups (`.1`-`.5`).

## Local database (`wispralt.db`)

Part 4 moves the Local-mode host onto **one SQLite file**, `data_dir/wispralt.db`
(`DATA_DIR` in `.env`, an absolute path; `outputs/` and `staging/` live beside it).
`wispralt.database.Database` is the only way in:

- a short-lived connection per call with `foreign_keys=ON`, `busy_timeout=5000`
  and `synchronous=NORMAL`; WAL is set once by `migrate()`;
- `read()` opens a `BEGIN` snapshot, `write()` a `BEGIN IMMEDIATE` transaction that
  commits on exit and rolls back if the body raises; no transaction spans inference,
  and async callers use `asyncio.to_thread`;
- `migrate()` creates the file at mode 0600 before the first connect, then applies
  `migrations/NNNN_*.sql` one statement at a time inside a single transaction
  (never `executescript`, which commits implicitly) and records each file's sha256
  in `schema_migrations`. An edited or missing applied file, or a DB newer than the
  code, raises `MigrationError` before anything is applied;
- `check()` backs readiness: `None`, `"db_error"` (any `sqlite3.Error`) or
  `"schema_outdated"`.

`0001_initial.sql` holds the full schema (users, api_keys, invites, usage_events,
dictations, jobs, weekly_insights, settings). Callers move onto it task by task
during Part 4; until then the sections below describe the current stores.

**Repositories** are classes over `Database`, one transaction per method:

- `accounts.store.AccountStore` splits identity from credentials: `users`
  (`role`, `kind`, `disabled_at`) and `api_keys` (`token_hash`, `revoked_at`).
  A key authenticates only while it is not revoked and its user is not disabled
  (`lookup_key`, one indexed join). `rotate_user_key` revokes the user's active
  keys, inserts one new key and clears `disabled_at` in a single transaction, so a
  failure leaves the old key valid. `create_first_admin` refuses
  (`AlreadyBootstrapped`) while an enabled admin exists.
- `accounts.service` is the only code that sees plaintext tokens: it generates a
  64-hex token, stores its sha256 and returns the plaintext once; it logs ids only.
- `usage.store.UsageStore` writes one `usage_events` row per `record` and serves
  the portal reads (overview tiles, top users, 14 UTC-day buckets, user totals,
  drilldown and CSV). The p50 is the median of the window's `duration_ms` values,
  computed in Python.

## Transcript persistence

Phase 1 of the portal "weekly insights" effort: every transcribed output
(dictation + file job) is persisted to the mini's SQLite so the future
weekly-insights cron has data to summarize. Capture is **unconditional and
transparent** — there is no client-visible toggle and no admin UI redaction.
Zero behavior change for end users.

### Where the data lives

Both tables live in `DATA_DIR/wispralt.db` (`migrations/0001_initial.sql`).

- **File-job transcripts** are stored on the `jobs` row: `transcript_text`,
  `word_count`, `client_app_version`, `user_id`. The derivation happens inside
  `FileJobRunner._run_pipeline_inner` right before `set_done`: joins
  `transcript["segments"][*].text`; falls back to reading
  `DATA_DIR/outputs/<job_id>.txt` if the joined string is empty.
- **Dictation transcripts** live in the `dictations` table. Dictation is
  request-response (no job row), so the route's success path fires-and-forgets
  an `asyncio.create_task` that calls the store via `asyncio.to_thread`. Tasks
  are held in `app.state.pending_persists` and drained with a 2-second budget
  at shutdown (Task 6 replaces this with a synchronous persist).

Both tables carry `user_id`, a real FK to `users`, so `/me/*` filters to one
user and `/admin/data` groups by it (`JobStore.stats_by_user`, one query).

### Privacy posture

- **No opt-in flag.** Deliberate product decision; do not re-introduce
  gating "to be safe."
- **90-day retention.** `JobStore.sweep_transcripts(days)` zeros
  `jobs.transcript_text` and deletes `dictations` rows older than
  `transcript_retention_days` (default 90). TTL is measured from
  `COALESCE(finished_at, created_at)` on jobs (the moment the transcript
  actually existed, not job submission) and from `created_at` on dictations.
  Sweep runs once at startup (`main.lifespan`) and on a daily 24 h asyncio
  timer task (`_daily_transcript_sweep`) so a long-uptime server eventually
  reaps.
- **Asymmetric word-count survival.** `jobs.word_count` survives past sweep
  because the row stays as audit history; `dictations` rows are deleted
  entirely. This is intentional.
- **File permissions.** `wispralt.db` is created 0600 (its `-wal`/`-shm`
  inherit the mode) and re-chmodded by every `migrate()`.
- **Empty / whitespace-only dictations are skipped.** Avoids polluting
  Phase 2 word counts with silence-only audio.
- **All transcripts stay on the mini.** Nothing is sent to OpenRouter
  except via the future weekly insights cron (Phase 2).

### Cloud-fallback dictations (removed in Part 2)

Plan A once captured dictations that went through the client's OpenRouter
audio fallback (a disk queue draining to a `/telemetry/*` ingest route).
The audio fallback, the queue and the ingest route were removed in Part 2.
Rows written back then keep `source='cloud_fallback'` and still render in
`/me/history`; no new ones are created.

### Client app version (`X-WisprAlt-Client-Version`)

The Swift client sets this header on every server-bound request
(`FileJobAPI.submitFile`, `ChunkedUploader.postJSON` +
`uploadOneChunk`, and `ServerClient.buildRequest` for `DictationAPI`).
Format: `"<CFBundleShortVersionString>+<CFBundleVersion>"`. The server reads
it at submit time and persists it on the row via `JobStore.create` (single-shot
file) or on `meta.json` (chunked init) so it survives both
worker crashes and mid-upload client upgrades. Missing → stored as NULL.

### What's deferred to Phase 2

- LLM weekly cron (launchd timer + OpenRouter summary client + a new
  `weekly_insights` table).
- Admin "Data" tab + per-employee self-view portal (`/me/insights`).
- The data-gate is "≥2 weeks of accumulated capture."

---

## Weekly insights

Phase 2 of the portal effort. The capture loop (transcript persistence above)
runs continuously; the **analysis loop** here turns the previous week's
captured transcripts into per-employee + team summaries every Sunday night.
The render loop (`Data portal` below) is what employees and admins see in the
browser.

Source: `server/src/wispralt/insights/{__init__.py, client.py, prompts.py, cron.py}`
and the `weekly_insights` table + 5 helpers in `jobs/store.py`.

### Schedule

The cron fires at **23:00 in `settings.insights_timezone`** (default
`America/Los_Angeles`) every Sunday. It's an **in-process asyncio task**
launched from the FastAPI lifespan — not a separate launchd plist. The
trade-off: in-process loses the process-isolation that a separate plist would
buy (a runaway insights pass could in principle starve the dictation loop)
but gains a warm OpenRouter client + reuse of the existing SQLite/`JobStore`
connection. The startup-catchup branch (next section) covers the
reboot-during-fire case, so the in-process path is effectively reliable in
practice; we accepted the trade.

`_seconds_until_next_fire(tz, *, iso_weekday, hour)` in `insights/cron.py`
computes the sleep interval via `datetime.now(ZoneInfo(...))` arithmetic,
which is DST-correct (`zoneinfo` handles the spring-forward / fall-back
delta automatically) — verified by hand against both the 2026 PST→PDT
and PDT→PST transitions. The parameterized form lets ops dry-runs and
the catchup path retarget specific weekdays/hours without code edits;
the back-compat alias `_seconds_until_sunday_23_local(tz)` is retained
for one release. The lifespan installs an `asyncio.Lock` on
`app.state.weekly_insights_lock`; `run_weekly_insights` short-circuits
with a warning if the lock is already held (catchup overlap → no
double-bill).

### Startup catchup

Gated behind `settings.insights_catchup_enabled` (defaults **False** — opt-in
only). When True, on lifespan startup the cron checks whether the most-recent
completed ISO week has a row in `weekly_insights` for any user **and** ended
less than 72 hours ago; if both conditions hold, it runs the insights pass
immediately rather than waiting for the next Sunday. The operator opts in via
`.env` only after verifying OpenRouter model availability + pricing — leaving
it False on a fresh deploy prevents an accidental Wednesday-after-deploy
charge.

### Cost control

A hard 30-day rolling cap (`settings.insights_max_30d_cost_usd`, default
**$8**) gates the cron at two seams:

1. **Pre-call check.** Before issuing any OpenRouter call, the cron reads
   `JobStore.rolling_insights_cost_usd(days=30)` and skips the entire run if
   the figure is already at or above the cap.
2. **Mid-run check.** Inside the per-user loop, an `accumulated_run_cost`
   counter tracks spend for the current invocation. After each call the cron
   re-checks `baseline_30d_cost + accumulated_run_cost ≥ BUDGET` and stops
   mid-run rather than blowing through the cap on the last few employees.

Cost is extracted from the OpenRouter response body's `usage.cost` field
(verified to exist on `x-ai/grok-4.3` responses on 2026-05-14 via Task 0).
If a future model strips the field, `InsightsClient` falls back to a
hardcoded `INSIGHTS_PRICING_PER_1K` rate-card keyed by model id; the
`_default` entry is intentionally set high so the fallback fails closed (we'd
rather skip a run than under-bill against a missing rate).

The OpenRouter 429 rate-limit response is surfaced as `RateLimitedError`
which aborts the entire current run; the next scheduled Sunday fire will
retry. We don't sleep-and-retry inside the loop — Sunday-to-Sunday cadence
gives more than enough recovery time.

### Data shapes

```sql
CREATE TABLE weekly_insights(
  iso_year INTEGER NOT NULL, iso_week INTEGER NOT NULL,
  scope_key TEXT NOT NULL,                       -- 'team' or 'user:<id>'
  user_id INTEGER REFERENCES users(id),           -- NULL for the team row
  insight_json TEXT NOT NULL, input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL,
  model TEXT NOT NULL, generated_at REAL NOT NULL,
  PRIMARY KEY (iso_year, iso_week, scope_key),
  CHECK (scope_key = CASE WHEN user_id IS NULL THEN 'team' ELSE 'user:' || user_id END));
```

The non-null `scope_key` closes the old duplicate-team-row hole (a nullable
column in the primary key never conflicted). `upsert_weekly_insight(user_id=…)`
derives the key and uses `ON CONFLICT (iso_year, iso_week, scope_key)
DO UPDATE` so a re-run for the same week overwrites the previous row
idempotently — useful for catchup, manual re-runs via the
`python -m wispralt.insights.cron --manual` entrypoint, and prompt
iteration during development.

Per-person `insight_json` schema:

```json
{
  "digest": ["3-bullet summary of the employee's week"],
  "action_items": [{"text": "ship migration", "owner": "Sarah", "eta": "Fri"}],
  "projects": [{"name": "Acme proposal", "pct_time": 0.40}],
  "decisions": ["we decided to..."],
  "blockers": ["mercury timeouts mentioned 6x"],
  "topics": [{"label": "billing", "weight": 0.3}],
  "filler_word_count": 47,
  "quotable_line": "the migration WILL ship on Friday"
}
```

Team `insight_json` schema:

```json
{
  "themes": [{"label": "client onboarding", "rank": 1, "wow_delta": "+2"}],
  "team_action_items": [...],
  "team_blockers": [...],
  "tool_mentions": {"Mercury": 6, "Cloudflare": 4},
  "knowledge_gaps": ["questions raised without follow-up"]
}
```

### Hallucination guard

`_scrub_hallucinations` in `insights/cron.py` is **two-tier**:

- **`quotable_line` — strict exact substring (case-insensitive).** Must appear
  verbatim in the week's transcript blob for that user, because the UI
  renders this field as a literal quote. If absent, the field is replaced
  with `null` rather than risk attributing made-up words to the employee.
- **`action_items` / `decisions` / `blockers` — 8-character sliding window.**
  Each item must share an 8-char substring with the transcript blob. This is
  paraphrase-tolerant (the LLM legitimately summarizes "we decided X" without
  copying the exact words) at the cost of false-positives on common phrases.
  The UI surfaces an **"AI-generated, may be inaccurate"** disclaimer on
  every card to set user expectations accordingly.

### Scope semantics

- **`scope = 'person'`** rows are emitted per employee that hit the
  data-floor gate: at least **5 dictations OR at least 1 file job** (history kind `file`) in
  the week. Users below the floor are skipped silently (no row written) to
  avoid charging for thin-data summaries.
- **The team row** (`scope_key = 'team'`, `user_id = NULL`) is one row per week. It's
  written **only when at least 2 employees had successful person-scope
  insights** — a one-person team aggregate would just be a re-rendering of
  that person's row.

---

## Data portal

The render loop. All three surfaces share the same base template + vendored
JS so a single design language covers admin + employee views.

Source: `server/src/wispralt/routes/{me.py, admin_data.py}` +
`server/src/wispralt/admin/templates/{me_login, me_insights, data,
_insight_cards, _stats_grid_partial}.html.j2`.

### Surfaces

- **Admin Data tab — `GET /admin/data`.** Admin-only (handler-level
  `require_admin`); people come from `AccountStore`. Renders the team `weekly_insights`
  card stack on top + a per-user leaderboard underneath. The optional
  `?user_id=N` query param drills into one employee using the same
  `_insight_cards.html.j2` macro layout the employee self-view renders, so
  the admin sees exactly what the employee sees.
- **Employee self-view — `GET /me/insights`.** Authenticated user only.
  Shows that user's `'person'`-scope insight (most recent week) plus a
  time-range stats grid (Today / 7d / 30d / 90d / 1y / All).
- **Employee login — `GET /me/login` / `POST /me/login`.** Token-paste form
  mirroring the admin login flow. On success it sets the **same**
  `wispralt_admin_token` cookie that admin login sets, but with `path="/"`
  so a single cookie scopes across both `/admin/*` and `/me/*` surfaces
  (the admin login handler was simultaneously broadened to `path="/"` so
  the cookie is interchangeable in both directions).

### HTMX partial swaps

The time-range tabs are wired through HTMX. Each surface route inspects the
`HX-Request: true` header (via the shared `web.htmx.is_htmx` dep): when
present, the handler returns a *body partial* — `_me_insights_body.html.j2`
for `/me/insights`, `_admin_data_body.html.j2` for `/admin/data` — which
includes BOTH the stats grid AND the insight cards. Each partial is
swapped into the page's `#insights-body` wrapper without a full reload, so
one range-tab click refreshes both the stats grid AND the insight cards
in a single round-trip. The Alpine `<nav>` tabs sit outside the swap
target so their `active` state survives the swap; an `htmx:historyRestore`
handler in `base.html.j2` dispatches a `range-changed` custom event so the
Alpine tabs re-sync from the URL on browser back/forward.

### Shared helpers

`web/templates_env.py` exports a single `Jinja2Templates` singleton (with
`select_autoescape` on `*.html.j2` / `*.html` / `*.j2`) used by
`routes/admin_ui.py`, `routes/admin_data.py`, and `routes/me.py` so the
escape policy + template dir stay in sync. `web/htmx.py` exports
`is_htmx(request)`. `insights/timewindow.py` exports
`last_full_iso_week` / `iso_week_epoch_bounds` / `epoch_for_range` —
the cron + both routes share one source of truth for week boundaries
and the Today/7d/30d/90d/1y/All range tabs. `auth.set_session_cookie`
/ `clear_session_cookie` / `set_csrf_cookie` / `verify_csrf` centralize
the `wispralt_admin_token` cookie issuance (path="/", samesite=strict,
secure, httponly, 8 h) AND the double-submit CSRF token used by both
`/admin/login` and `/me/login` forms (constant-time compare via
`hmac.compare_digest`). `JobStore.has_any_insight_for_week`,
`list_filler_counts_for_week`, and `compute_user_stats` are the public
read paths the routes use instead of reaching into the private `_exec`.

### CSS scoping

The frosted-glass card rules added to `base.html.j2` are scoped under the
`.data-page` class so the new visual treatment applies only to
`me_insights.html.j2` and `data.html.j2`. Existing admin pages (`/admin/`,
`/admin/users`, `/admin/usage`) inherit the old flat styling unchanged —
the scoping is a defensive choice so a regression in the new CSS can't
visually break the existing admin surfaces.

### Vendored JS

HTMX 2.0.4 (`/admin/static/htmx.min.js`, ~50 KB) and Alpine 3.14.8
(`/admin/static/alpine.min.js`, ~44 KB) are checked in to the repo and
served by FastAPI's `StaticFiles` mount at `/admin/static`. CDN delivery
was rejected for two reasons: (1) it would force a CSP carve-out for
`unpkg.com` (or similar), expanding the supply-chain surface; (2) the
mini's network egress isn't guaranteed — vendoring locally keeps the
portal functional during an internet outage at the office. Refresh
procedure (out-of-band): `curl -fsSL -o ...` from the upstream CDN,
verify SHA-256 against the published release, commit.

---

## Personal history (`/me/history`)

Plan A's headline employee surface. Every authenticated employee can browse
their own dictations + file transcriptions (including legacy meeting rows) in one
chronological list, filter by range / kind / search term, expand any row to
see the full transcript text, soft-delete with a one-click undo path, and
download an individual transcript as `.txt` or `.json`. Sits in the same
sidebar as `/me/insights` and inherits the data-portal frosted-glass
treatment.

Source: `server/src/wispralt/routes/me.py` (handlers) +
`server/src/wispralt/admin/templates/{me_history, _me_history_body,
_me_history_page, _me_history_row, _me_history_row_expanded}.html.j2`
(render). Design lineage:
`tmp/ready-plans/2026-05-15-plan-a-me-history-and-cloud-telemetry.md`.

### Two-leg union

A "history row" is either a dictation (from the `dictations` table) or a
file transcript (from the `jobs` table). Both legs carry `user_id`, so
per-user scoping is free. `JobStore.transcripts_in_range_filtered`
runs two parameterized SQL queries — one per leg — applies the range +
optional kind + optional `LIKE` search filter, ORDERs each by
`(created_at DESC, row_id DESC)`, then merge-sorts the two streams in
Python.

Filters live entirely server-side:

- `range` (default `30d`) — one of `today`, `7d`, `30d`, `90d`, `1y`, `all`,
  reusing `insights/timewindow.epoch_for_range` so the history page and the
  insights page agree on what "30d" means.
- `kind` (optional) — `dictation` or `file` (every `jobs` row; displayed as "File transcriptions"). `meeting` URLs from before Part 4 404. Empty string from the
  `<select>` element is normalized to None at the handler.
- `search` (optional) — minimum **3 characters** after `.strip()`; shorter
  strings are silently dropped to keep the `LIKE '%…%'` scan bounded.

### Per-leg cursor pagination

Each leg has its own opaque cursor (`dict_cursor`, `jobs_cursor`) so a page
that's heavy on dictations doesn't starve file transcriptions off the next page (and
vice versa). Cursors are `(created_at, row_id)` tuples base64-urlsafe-encoded
in the URL; malformed cursors decode to None and silently restart pagination.
The "Load more" HTMX button posts back both cursors and the server appends
the next 50 rows. Page size lives in `settings.history_page_size`
(default **50**).

`row_id` is included as a tie-breaker because two transcripts captured in
the same wall-clock second otherwise collide on `created_at` alone and one
of them gets dropped on the next page. Verification step 12 of the plan
covers this.

### Soft delete

`dictations.deleted_at` and `jobs.deleted_at` (REAL, NULL = live). Two
partial indexes keep the live-row scan fast:

```sql
CREATE INDEX dictations_user_created ON dictations(user_id, created_at) WHERE deleted_at IS NULL;
CREATE INDEX jobs_user_active ON jobs(user_id, created_at)
  WHERE deleted_at IS NULL AND transcript_text IS NOT NULL;
```

`DELETE /me/history/{kind}/{row_id}` flips `deleted_at = NOW()`; the row
disappears from `/me/history` and from every downstream read path that
respects the flag. `compute_user_stats` and `transcripts_in_range` were
audited to filter `deleted_at IS NULL` on both UNION legs so the
admin Data tab's word-count tiles, the weekly-insights cron's input, and
the personal stats grid all decrement after a delete. `POST
/me/history/{kind}/{row_id}/restore` clears the flag.

Hard deletion + cascade across already-generated `weekly_insights` rows is
out of scope for Plan A — the v1 contract is "I won't appear in *future*
insights and I'm hidden from my own history" rather than "I never
existed".

### CSRF + ownership

Both mutating routes (`DELETE`, `POST /restore`) accept the standard form
`csrf_token` field validated through the shared
`auth.verify_csrf`/`set_csrf_cookie` helpers (same double-submit pattern
as `/admin/login` and `/me/login`). Every read AND mutation re-checks
ownership: `get_history_row(user_id, kind, row_id)` filters on
`user_id` so a malicious or stale row_id from another user uniformly
returns **404** — we never leak existence to non-owners.

### HTMX render contract

Three template shapes, picked at the handler based on request kind:

| Trigger | Template | Notes |
|---|---|---|
| Fresh page load | `me_history.html.j2` | Full chrome (nav, filter bar, table shell + first page). |
| HTMX nav (range/kind/search change) | `_me_history_body.html.j2` | Body fragment swapped into `#history-body`. |
| HTMX Load-more | `_me_history_page.html.j2` | Just the next batch of `<tr>` rows + the updated Load-more button. |
| Row expand | `_me_history_row_expanded.html.j2` | Swaps the compact row in place; transcript text + per-row Delete/Download buttons. |
| Row collapse / restore | `_me_history_row.html.j2` | Compact row partial. |

Delete returns `<tr id="row-{kind}-{row_id}" hx-swap-oob="delete"></tr>`
so the row simply vanishes without a re-fetch. Downloads (`GET
/me/history/{kind}/{row_id}/download/{fmt}`) stream `text/plain` or
`application/json` with a `Content-Disposition: attachment;
filename="{kind}-{iso-date}-{row_id}.{fmt}"` header.

```
employee browser
   │  GET /me/history?range=30d
   ▼
me.py:me_history ──▶ JobStore.transcripts_in_range_filtered
                       │
                       ├─ leg A: dictations (deleted_at IS NULL)
                       └─ leg B: jobs       (deleted_at IS NULL)
                              │
                              ▼ merge-sort by created_at DESC, then row_id DESC
                      first 50 rows + next (dict_cursor, jobs_cursor)
                       │
                       ▼
            me_history.html.j2 (or body/page/row partial)
```

---

## Cloud-fallback telemetry sync (removed)

The client-side dictation fallback queue, the `/telemetry/*`
ingest route and `JobStore.insert_cloud_fallback_dictation` were removed in
Part 2 together with the OpenRouter audio fallback they served. The queue's
folder (`~/Library/Application Support/co.wispralt/cloud-fallback-queue/`)
is no longer read; see the root `CHANGELOG.md` for leftover cleanup. Rows
already ingested keep `source='cloud_fallback'`, and `/me/history` still
labels them so users can tell which dictations went through the old path.

---

## In-app updater (v0.5.0)

A lightweight in-app update checker runs alongside the still-disabled Sparkle
wrapper. The new module lives at
`client/WisprAlt/Update/UpdateChecker.swift` and is **deliberately not Sparkle**
— it reuses `install.sh` (the curl one-liner) as the canonical install path so
there is exactly one update mechanism to reason about.

- **Polling.** The checker fires 60 s after launch and then every 6 h,
  debounced via `Settings.shared.lastUpdateCheck` so a wake-from-sleep doesn't
  trigger an immediate re-poll. The request hits
  `https://api.github.com/repos/<owner>/<repo>/releases/latest` and compares the
  `tag_name` (minus the leading `v`) against the bundled
  `CFBundleShortVersionString` via a SemVer-aware numeric compare.
- **Badge.** When a newer release is found, the checker flips
  `Settings.shared.updateAvailable` (UserDefaults-backed) and calls
  `MenuBarController.shared.setUpdateBadge(visible: true)` — the latter
  composites a subtle orange dot onto the menubar mic icon. The badge is only
  rendered when `Settings.shared.serverURL != nil` so a half-configured install
  doesn't show a misleading update prompt.
- **Install now.** Settings → Advanced → Updates shows the current version, the
  latest tag, and an "Install now…" button. The button shells out to
  `Terminal.app` via `NSAppleScript` with the canonical curl install one-liner
  pre-typed; if Automation TCC is denied (no Terminal scripting permission)
  the command is dropped onto the clipboard with a "Paste this into Terminal"
  toast as a fallback so the user is never stuck.

Source: `client/WisprAlt/Update/UpdateChecker.swift` +
`client/WisprAlt/Storage/Settings.swift` (`updateAvailable`,
`lastUpdateCheck`) + `client/WisprAlt/App/MenuBarController.swift`
(`setUpdateBadge(visible:)`).

---

## User-facing client surfaces (v0.5.0)

Two menubar-popover additions surface the existing portal more clearly to
employees.

- **"Copy last dictation"** — pinned at the very top of the popover. Since
  Part 3 it reads the client's own `last.json` (see "Spool, Retry and last
  text"), in both modes; the client no longer calls
  `GET /me/dictations/last` (the server route still exists).
- **"Open My Dictations"** — opens
  `<serverURL>/me/login?next=/me/history` in the default browser. The
  `?next=` parameter (server-side open-redirect-guarded to relative paths
  under `/me/*` only) lets first-time employees land on their personal
  history page after token-paste instead of the default `/me/insights`
  landing — which was the discoverability gap raised against v0.4.x.

The other QuickActions buttons (Transcribe file…, Copy last custom
transcription, etc.) are unchanged from the v0.4.6 layout.

---

## Process Auto-Start

Three launchd entries keep both sides of WisprAlt alive across reboots, crashes, and logins.

```
Mac mini — launchd gui/<UID>
  │
  ├── co.wispralt.server  (~/Library/LaunchAgents/co.wispralt.server.plist)
  │     RunAtLoad: true
  │     KeepAlive: true
  │     → uvicorn on 127.0.0.1:8000
  │
  └── co.wispralt.cloudflared  (~/Library/LaunchAgents/co.wispralt.cloudflared.plist)
        RunAtLoad: true
        KeepAlive: {SuccessfulExit: false, NetworkState: true}
        ThrottleInterval: 10
        → cloudflared tunnel run --token-file ~/.config/wispralt/cloudflare-token
          (or --token <value> on cloudflared < 2025.4.0)

Client Mac — SMAppService (registered by AppDelegate at first launch)
  │
  └── co.wispralt.WisprAlt  (Login Items & Extensions entry)
        → /Applications/WisprAlt.app
        Appears in System Settings → General → Login Items & Extensions
        Configurable via in-app Settings → Launch at login toggle
```

Both Mac mini LaunchAgents run at user level (`gui/$UID`) — not system level — so they have access to the user's Keychain, home directory, and environment without requiring sudo. `EnvironmentVariables/PATH` is set explicitly in each plist because launchd provides a minimal PATH that may not include Homebrew.

The SMAppService entry on the client is registered via `SMAppService.mainApp.register()` and requires an Apple-issued code-signing identity (Apple Development or Developer ID). Ad-hoc signed builds cannot use SMAppService.
