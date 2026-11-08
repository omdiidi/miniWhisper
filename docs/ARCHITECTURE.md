---
title: Architecture
---

# WisprAlt Architecture

## System Overview

WisprAlt has two modes. In **Online** mode (the default) the native macOS menubar client sends audio straight to OpenAI with the user's own key (Keychain `co.wispralt.openai`); no server is involved. In **Local** mode the client sends audio to a self-hosted FastAPI host on an always-on Apple Silicon Mac, reached through `tailscale serve` or an optional Cloudflare Tunnel. The diagram below shows Local mode.

```
┌─────────────────────────  Client (macOS menubar)  ────────────────────────────┐
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
| **DictationRecorder** | `client/WisprAlt/Capture/DictationRecorder.swift` | AVAudioEngine input tap that writes a native-format **Float32 PCM** CAF to the spool (`~/Library/Application Support/WisprAlt/spool/<id>.caf`; CAF stays readable after a crash) via `AVAudioFile.write(from:)`. Sample rate, channel count, and PCM format all match the tap's buffer exactly so AVAudioFile performs zero conversion — it streams the float bytes byte-for-byte to disk. Server (`audio.py`) resamples to 16 kHz and downmixes via `np.mean`. AVAudioConverter is intentionally avoided (default channel-mix sums channels rather than averaging, producing peak floats > 3.0). Int16 PCM output is also avoided (AVAudioFile's internal Float→Int16 converter applies a ~140x normalization that rail-clips the signal and destroys ASR accuracy). Tap callback dispatches each write onto a serial `ioQueue` to keep the realtime render thread free. Defensive in-tap clamp to ±0.95 catches any future regression of out-of-range floats. Streaming `chunkBuffer` memory: appended without exact-size `reserveCapacity` (geometric growth), cuts use `removeSubrange`, guarded by `chunkLock` (tap section and a `stop()` fence serialize on it), capped at ~120 s of pending audio by shipping it as a chunk (pure silence is dropped, speech never is), and taken (consumed) by `stop()`. `start(dictationId:endpoint:maxSeconds:frameSink:)`: the first line is `guard !slot.isActive else { return false }` (Core `CaptureSlot` replaces `isRecording` and the public `streamSession`); a `DictationStreamSession` is created **only for a Local endpoint** (Online never streams) and captured by the tap closure; nothing in `start()` aborts a session; `slot.begin(session)` is the last step, so every throw leaves the slot free. a live model's `frameSink` gets a per-dictation `MonoResampler` (24 kHz) fed on a serial `frameQueue` (the tap only copies and dispatches). `stop()` fences the tap (new buffers are dropped) and drains `ioQueue` (every already-queued write lands, so the spool holds the same tail the live sink got), `frameQueue` (then flushes the resampler tail to the sink) and `chunkEncodeQueue`, closes the file, copies every per-dictation value into locals, then `slot.end()` (the next `start()` may run from there) and returns `RecorderOutput { recording: Recording, local: LocalCapture? }`; on a throw it aborts the session it took; the spool file is never deleted by the recorder (the dictation's `SpoolDisposition` owns it). A Local capture is re-encoded to native-rate Int16 (`LocalCapture.make`); a failed re-encode throws `.encodeFailed` (no Float32 fallback). The per-dictation cap (`maxSeconds`: 600 s, 420 s for `gpt-realtime-whisper`) drops further tap audio and posts `.dictationMaxDurationReached` once with `userInfo["seconds"]` (the UI runs the FN-up path and names the limit). A device change records `engineStartFailed(NSError -11)`; `stop()` returns what was recorded with `interruptedByDeviceChange` (toast "Audio device changed — transcribed what was recorded"). |
| **TextInjector** | `client/WisprAlt/Inject/TextInjector.swift` | `@MainActor` strategy combinator. Captures `(FocusContext, AXUIElement?)` once via `captureFocus()` to close the TOCTOU window between security check and AX write. Refuses injection outright (no AX, no clipboard) when `WisprAltCore.shouldRefuseInjection(for:)` returns true (focused element subrole == `AXSecureTextField`); fires a 60-second-debounced local notification including the bundleID. Otherwise calls `AccessibilityInjector.tryInsertWith(element:text:)` and falls through to `ClipboardInjector` if AX cannot prove a value change (iMessages, Pane, every Electron app). Returns Core `InjectOutcome`: `.insertedVerified` (AX; clipboard untouched), `.pastedUnverified` (Cmd+V; the text stays on the clipboard), `.refusedSecureField` (nothing typed or copied), `.noTarget` (no frontmost app; copied only). |
| **AccessibilityInjector** | `client/WisprAlt/Inject/AccessibilityInjector.swift` | `tryInsertWith(element:text:)` accepts a pre-captured focused element from `TextInjector` (no internal focus walk → no TOCTOU). Sets a 250 ms `AXUIElementSetMessagingTimeout` to bound stalls on hung target apps, then `AXUIElementSetAttributeValue(kAXSelectedTextAttribute)` and reads `kAXValueAttribute` before/after. Returns true ONLY when the read-back proves a change — delegates the decision to the pure `WisprAltCore.didInjectionLand(...)` predicate (regression-pinned by 11 Swift Testing cases). The "empty before + write succeeded → assume success" heuristic is intentionally absent; that combination is the silent-no-op signature in Electron / custom NSTextView / iMessages compose. |
| **WisprAltCore** | `client/WisprAltCore/` (`InjectionPredicate`, `SecureFieldGate`, `FocusContext`, `FileJobContract`, `TranscriptsFolderDefault`, `KeychainItem`, `AppMode`, `OpenAIModels`, `OpenAIErrors`, `OpenAIRequests`, `Errors`, `Deadline`, `Audio/{MonoResampler,Mono16kWAV,ChunkPlanner}`, `Dictation/{DictationProvider,CaptureSlot,LocalFinalizeRoute,OpenAIFileProvider,SpoolDisposition,RetryEntries,LastText}`, `Mercury/{MercuryPolicy,MercuryClient}`) | Pure-Swift library holding every testable decision (see "Online mode" below): `didInjectionLand(setSucceeded:beforeValue:afterValue:)`, `shouldRefuseInjection(for:)`, `FileJobContract.cancelledByUserReason` (the server's cancel reason, a silent end), `TranscriptsFolderDefault.resolve(stored:docs:exists:)` (transcripts-folder default), and the `Sendable, Equatable` `FocusContext` data type (`bundleID`, `pid`, `role`, `subrole`; `isSecureField` is derived from `subrole == "AXSecureTextField"` so callers can't construct an inconsistent context). No AppKit / ApplicationServices / Sparkle / resource dependencies — `Tests/WisprAltCoreTests` (Swift Testing) runs via `scripts/test-client.sh` / `swift test` without invoking actool or pulling Sparkle into the test runner. |
| **ClipboardInjector** | `client/WisprAlt/Inject/ClipboardInjector.swift` | `injectViaCmdV` writes the text and synthesizes Cmd+V (virtualKey 0x09, `.maskCommand` on both keyDown and keyUp); `copyOnly` writes it only. **Clipboard rule:** the pasteboard is never restored, because a Cmd+V paste cannot be verified and restoring could erase the only copy of a dictation that did not land. |
| **MenuBarController** | `client/WisprAlt/App/MenuBarController.swift` | Owns the activity state (main thread): `currentSession: DictationSession?` (set only after `recorder.start` returns true; the only "dictation in progress" check), `localUploadPhase` (Core `LocalUploadPhase`: `.none/.converting/.uploading/.polling`, mirrored to `RecordingState`), `localDoneFlash`, `onlineJobTask`, and the `pollGeneration` fence. The icon `mode` is **computed** (`ActivityState.icon(…)`, `Mode` = Core `ActivityIcon`) and never written or branched on. `isBusy` (`currentSession != nil \|\| dictationsInFlight > 0 \|\| fileJobsInFlight > 0`) is the single busy signal: the update relaunch refuses while busy. `beginRun` increments `dictationsInFlight` in the same main-actor turn that clears `currentSession`; the submit Task's `defer` decrements it. Only one file job runs at a time (`transcribePickedFile` ignores a second pick, then branches on the mode: Online → `onlineJobTask`, Local → `activeJobTask`; see "Online file jobs"). `applyModeEvent` / `applyModeSwitch(to:)` run Core `LaunchActions` (see "Online mode"). |
| **FileJobAPI** | `client/WisprAlt/Server/FileJobAPI.swift` | Poll / download / cancel for `/transcribe/file/{id}`. `cancel` returns the HTTP status; the caller cancels its poll Task, clears `activeJobID` and leaves the processing icon before the request; on 202 or 204 it clears `serverFinishingJobID` only if that still names the cancelled job. A later `failed: cancelled by user` poll result ends silently; any other `failed` clears `activeJobID` and its UserDefaults key (never resumed), while a poll timeout or transport error keeps the key so the next launch resumes. |
| **MicEnumerator** | `client/WisprAlt/Audio/MicEnumerator.swift` | Stateless static helper bridging AVFoundation device discovery to CoreAudio HAL property reads. `availableInputs()` uses `[.external, .microphone]` (macOS 15+ — `.builtInMicrophone` is deprecated, subsumed by `.microphone`); dedup by uniqueID. `audioDeviceID(forUID:)` translates an AV `uniqueID` to an `AudioDeviceID` via `kAudioHardwarePropertyTranslateUIDToDevice` (using `withUnsafePointer(to: &cfUID)` and `qualifierSize = MemoryLayout<CFString?>.size`). `systemDefaultInputName()` exposes the current default's localized name for the picker label. Powers the SwiftUI Input Mic picker in `SettingsView`. |
| **AppDelegate** | `client/WisprAlt/App/AppDelegate.swift` | App lifecycle. Exposes `static weak var shared: AppDelegate?` for cross-controller access. Sweeps leftover `wispralt-online-*` temp dirs (`OnlineFileJob.sweepLeftovers`), recovers last session's spool into Retry (`recoverSpoolAtLaunch`; nothing is sent), warms the OpenAI key cache, then runs `menuBarController.applyModeEvent(.launch)`: in Local mode with a server URL + key it resumes a saved Local file job and fetches `/me` once; in Online it contacts neither. **Invite links**: `Info.plist` registers the `wispralt` URL scheme; `application(_:open:)` (a link that arrives before launch finishes is held until then) parses it with Core `InviteLink.parse` (refused unless the `host` passes `LocalURLPolicy`), shows an NSAlert with the server's host, and on **Join** sends `InviteLink.redeemRequest` (JSON `POST <host>/invites/redeem` with `device_label` = the Mac's name), then `KeychainHelper.setAPIKey(token)` (`co.wispralt`), `Settings.serverURL = host` (the link's host, not the response's) and `applyModeSwitch(to: .local)`. Failures show `InviteError.message`; logs carry only `InviteError.logDescription`, never the token or the key. |
| **PermissionGate** | `client/WisprAlt/App/PermissionGate.swift` | Sequential 3-permission wizard: Accessibility → Input Monitoring → Microphone. On macOS 14.4+, posts a blocking "Quit and Reopen Required" sheet after Input Monitoring is granted (`CGRequestListenEventAccess` returns `true` but real grant requires process restart). |
| **SparkleController** | `client/WisprAlt/Update/SparkleController.swift` | Sparkle 2 wrapper. Defers the update sheet while `MenuBarController.isBusy`. Auto-updates intentionally **disabled** in be720a1 — `startingUpdater: false` + `SUEnableAutomaticChecks=false` in Info.plist. Fresh installs: clone + `./install.sh`; updates: `wispralt update --client` (see docs/INSTALL.md). |

### Online mode (OpenAI direct)

Two modes, picked in the popover: **Online (OpenAI)** and **Local server** (`Settings.mode`; absent → `TranscriptionMode.default` = `.online`, on fresh installs and upgrades alike). A change applies to the next dictation.

- **FN press** (`dictationStart`, all synchronous): `DictationSnapshot` freezes `{id (lowercase UUID), mode, kind, model, endpoint}`; `RouteKind.decide(mode:modelKind:keyPresent:)` gives `.openAIFile`, `.openAIRealtime` (a live model), `.local`, or `.needKey` (Online with no key: toast, the popover opens at the OpenAI key field, nothing records). Online reads only the OpenAI key (`OpenAIKey.current()`: memory cache, else a Keychain re-read, so a key added from Terminal works on the next press); Local reads only the Local server. `ProviderFactory.make` builds one fresh `OpenAIFileProvider`, or for a live model an `OpenAIRealtimeProvider` with a `gpt-transcribe` `OpenAIFileProvider` as its recovery (nil for Local). `recorder.start(dictationId:endpoint:maxSeconds:frameSink:)` gets the endpoint only for `.local`, so Online never streams to the Local host, and a `frameSink` only for a live model; right after it returns `true`, `dictationStart` calls `sink.start()`, which opens the Realtime socket. `false` → "Still saving the last dictation — press again".
- **FN-up / cap / device change** → `beginRun` → `runDictation`: `recorder.stop()` → `DictationSession.route(local:)` → `.openAIFile(p)`: `Mono16kWAV` (16 kHz mono Int16, channels averaged by `MonoResampler`) → one `POST https://api.openai.com/v1/audio/transcriptions` (`audio.wav`, `json`, Bearer key; never `language`), one retry on 429/5xx/URLError, deadline `max(30, 10 + 0.5 × duration)` s via `withDeadline` (the timeout cancels the `URLSessionTask`, it never waits on the work); `.local(capture)`: `LocalServerProvider().finish(capture:)` → `DictationAPI.transcribe(capture:)` (`LocalFinalizeRoute`: stream finalize when a chunk went out, else whole file). Non-empty text is saved to `last.json` (Core `LastText`) **before** delivery, then `TextInjector.inject` returns an `InjectOutcome`, and `SpoolDisposition.decide(outcome, delivery:)` keeps or deletes the audio (see "Spool, Retry and last text"). The log line carries only `chars=`, `model=`, `mode=`, `saved=`, `truncated=`.
- **Live models (Realtime API)**: `gpt-live-transcribe` (live default) and `gpt-realtime-whisper` ($0.017/min each, about 3.8× `gpt-transcribe`). `start()` opens `wss://api.openai.com/v1/realtime?intent=transcription` (`URLSessionWebSocketTransport`, Bearer key; no `model` query, no `OpenAI-Beta`) and sends `session.update` (`type: transcription`, `audio/pcm` 24 kHz, `transcription.model`, `turn_detection: null`) first. While FN is held the recorder's tap copies each buffer to a serial `frameQueue`, where one `MonoResampler` per dictation makes 24 kHz mono and calls `append`; the provider sends 200 ms `input_audio_buffer.append` messages (clamped PCM16 LE, base64) from one sender task, queued in order until the socket is open (at most 10 s; overflow or a failed open marks `.notConnected`; the spool keeps every frame regardless). `stop()` drains `frameQueue` and flushes the resampler. `finish`: drain (5 s deadline), < 100 ms of audio → `.emptyRecording` (no commit), else wait in the ordered event stream for the `session.updated` ack (an `error` there ends the run with no commit; a socket that closes first is `.notConnected`), then `input_audio_buffer.commit` and wait for that item's `conversation.item.input_audio_transcription.completed` (matched by `item_id`) under `max(15, 5 + 0.25 × duration)` s whose timeout closes the socket. An `error` event (also one answering `session.update`) or `…transcription.failed` throws `ProviderError.realtime(type:)` with the sanitized code. `RealtimeRecovery.shouldRecover`: `.notConnected`, `URLError`, `.deadlineExceeded`, `server_error`, transient HTTP → the same spooled recording goes once through `gpt-transcribe` (`modelUsed = "gpt-transcribe"` in the log line); never for empty audio, a cancel, `invalid_api_key`, `insufficient_quota`, `model_not_found`. The socket is closed on every path. Deltas are decoded but not shown (no text while speaking). Caps: `gpt-realtime-whisper` stops at 420 s (2,000 documented output tokens, A1), `gpt-live-transcribe` at 600 s. Measured latency (P4) is in [TESTING.md](TESTING.md#real-api-assumption-gates-openai).
- **Smart formatting (Mercury, both modes)**: when `Settings.smartFormatting` is on, `MercuryStep.apply` runs after transcription and before the last-text save and delivery (also on a Retry). Core `MercuryClient` POSTs the text (never audio) to `https://openrouter.ai/api/v1/chat/completions` (`inception/mercury-2`, Bearer from Keychain `co.wispralt.openrouter` via `OpenRouterKey`, `X-Title: WisprAlt`) under a 2.5 s `withDeadline` whose timeout cancels the task; `MercuryPolicy` (prompt verbatim from the server, ≥ 20 words, 0.70–1.10 word-count window, `extractText` for every content shape) decides. Fail-soft: `DictationResult.formatting` is `.applied`, `.skipped("short" / "no key")` or `.failed("timeout" / "ratio" / "http" / "network" / "empty")` with the raw text; the popover shows it under the toggle. The client no longer sends `smart_format` / `X-Smart-Format`; server Mercury is removed in Part 4.
- **Errors**: `ErrorLog.describe` / `ErrorLog.outcome` are the only ways an `Error` reaches a log or a toast. `LogSafeError` conformances: `DictationRecorder.DictationError`, `ServerError` (status / kind only, never the body), `FileJobError` (never the server reason), `ProviderError`, `KeychainItemError`. An empty recording stays silent; `.deviceChanged` toasts "Audio device changed — press FN again".
- **Mode switch** (`applyModeSwitch(to:)` → `LaunchActions.decide(.switched(to:), state)`): to Online cancels only the Local poll task (`pollGeneration += 1`, `activeJobTask` cancelled; no server DELETE; the saved `wispralt.activeJobID` stays); to Local resumes polling a saved job only when no poller and no Online job runs; `/me` at most once per launch. Neither touches `currentSession`, so a recording finishes on its FN-press route. The picker is disabled while `localUploadPhase` is `.converting` / `.uploading` ("Finish or cancel the upload first").
- **Poll fence**: `transcribePickedFile` and `resumeInFlightJobIfNeeded` bump `pollGeneration` and pass the captured `gen` down (`handlePickedFile` → `processCustomTranscriptionUpload` → `runFileTranscriptionJob`, and `resumePollingForJob`). Every state write there (phase, done flash, `activeJobID` + defaults, `serverFinishingJobID`, progress fields, notifications, the `defer` that clears `activeJobTask`) runs only if `PollFence.mayWrite(isCancelled:captured:current:)`. `cancelActiveTranscription` bumps the generation first.
- **Popover status + model picker**: under the Mode picker a status line reads `Online · <model> · key saved|key missing` (Keychain presence only, no network) or `Local · <host> · reachable|unreachable` (`/healthz` runs only when the popover opens in Local or on a switch to Local, never in Online), plus `transcribing N` from `recordingState.dictationsInFlight` (mirrored by `MenuBarController.dictationsInFlight`'s `didSet`; not an icon state). Advanced → **OpenAI Model** lists every `OpenAIModels.all` row with `displayPrice` (`$0.0045/min`; `≈` for token-billed estimates) and `retireTag` (`retires Feb 26, 2027`), the caption "Files always use a file model. Dictation stops at N min for this model.", and the live caption "Live models stream audio while you hold FN and give the text when you release it (no text while speaking). They cost about 3.8× gpt-transcribe."; it writes `Settings.openAIModel`, read at the next FN press. A dictation failing with `FailureKind.modelNotFound` resets the picker to `gpt-transcribe` (`OpenAIModels.defaultFileModelID`, also for a live model) and toasts.
- **Online hides server-only UI**: connection / Test Connection, Server URL, API key, Identity, Open Portal, Open My Dictations, the job Log button. "Transcribe file…" works in both modes (Online caption: "Online saves TXT + JSON. Subtitles (SRT/VTT) need Local mode."); "Copy last dictation" reads the local `last.json` in both modes.

### Online file jobs

`transcribePickedFile` branches on the mode at pick time, before any task starts, so the Local path's job folder and source copy never happen in Online. Online → `startOnlineFileJob` runs `OnlineFileJob` (`App/OnlineFileJob.swift`) in `onlineJobTask`:

- **Isolation.** Counted in `fileJobsInFlight` (busy signal; one file job at a time). No mode switch cancels it, and it never touches `localUploadPhase`, `activeJobID` or the Local host. Its `defer` (`onlineJobDidEnd`, one main-actor turn) clears `onlineJobTask` and `recordingState.onlineJobID`, then runs `LaunchActions.decide(.onlineJobEnded, state)`, so a saved Local job resumes once the Online job ends if the mode is Local by then. Cancel cancels only the Online task (no fence bump, no server DELETE, `activeJobID` untouched).
- **Source and temp files.** The pick is read in place (never copied; security scope held for the job). Every temp file lives in one `temporaryDirectory/wispralt-online-<id>/`, removed by `defer`; `AppDelegate` sweeps leftover `wispralt-online-*` dirs at launch. A job interrupted by quit is lost; the user re-picks the file.
- **Upload plan.** The file model is `OpenAIModels.fileModel(for: Settings.openAIModel)`; its cap is `dictationCapSeconds` (600 s). `OnlineUploadPlan.decide`: mp3 / mp4 / m4a / wav / webm / ogg / flac / mpeg / mpga **and** ≤ 25,000,000 bytes **and** a known duration ≤ the cap → one direct POST with the file's own name and content type. Otherwise a video goes through `AudioExtractor.extract(strict: true)` into the job dir (`AudioExtractPolicy`: no audio track → "This video has no usable audio track"; export failure → "Could not extract audio"; an extraction that is not smaller is still used) and the plan runs again on the extracted file. Still not direct → decode: `Mono16kWAV.rmsProfile` (100 ms windows) → `ChunkPlanner.plan` (each cut at the quietest window in the last 20 s before the cap, never within 1 s of the end) → one 16 kHz mono WAV per chunk via `Mono16kWAV.make(range:)` → `OpenAIFileProvider.transcribe` with `prompt` = the previous chunk's last 200 characters (when the model supports it). A file `AVAudioFile` cannot open (e.g. a passthrough `audio.mp4` with non-AAC audio) → "This file type needs Local mode". Progress goes to `recordingState.chunkIndex` / `totalChunks`.
- **Output, all or nothing.** Only on success: `<stem>.txt` and `<stem>.json` (`OnlineTranscript`, see TRANSCRIPT-FORMAT.md) are written into `wispralt-online-<id>/out/`, then `FileJobContract.commitOutputs` creates the `Custom Transcriptions/<stem>__<timestamp>/` folder and moves both in; any throw removes the new folder. A failure or cancel leaves nothing in the transcripts folder. Online writes TXT + JSON only (OpenAI returns no timestamps); subtitles need Local.

### Spool, Retry and last text

Files under `~/Library/Application Support/WisprAlt/` (folder 0700, files 0600, at rest protected by FileVault):

- `spool/<id>.caf` + `spool/<id>.json` (`SpoolSidecar {mode, model}`): the sidecar is written synchronously by `SpoolFiles.writeSidecarSync` in `dictationStart` **before** `recorder.start`, so audio on disk always knows its route (a failed write toasts "Could not save audio" and nothing records; a failed start removes it).
- After FN-up every file operation runs on the `SpoolStore` actor, serially. `SpoolDisposition.decide` (Core, table-tested): empty recording → delete, no toast; empty text from a real recording → keep + "No speech detected. Audio saved under Retry."; text delivered → delete (+ "No app to paste into — the text is on the clipboard" for `.noTarget`; a secure-field refusal adds nothing, `TextInjector` shows its own notice); possibly truncated → keep; a Local 200 with `"saved": false` → the text is delivered **and** the audio kept; text that never reached delivery → keep; any failure → keep, except the terminal Local `.rejected` (410 `dictation_deleted` / 409 `dedup_conflict`) → delete, never retried. A cancelled run arrives as `.failed(.other)` (kept).
- `keep` converts the spool to `retry/<id>.wav` (16 kHz mono via `Mono16kWAV`; the raw `.caf` is moved if conversion fails; if both fail the spool file and sidecar stay in `spool/` for the next launch, never deleted), moves the sidecar and applies `RetryEntries.plan`: newest 2, 7 days, no 0 s entries, no orphan sidecars; a 3rd entry drops the oldest with "Oldest saved recording discarded".
- **Launch**: the `spool/` ids are snapshotted in `applicationDidFinishLaunching` (later dictations have new ids); each `.caf` with duration > 0 goes to Retry, the rest and orphan sidecars are deleted; "N saved recordings from last session — see Retry". Nothing is sent.
- **Retry N** (popover): per entry "age · duration · mode" and `RetryEntries.actions(sidecar:currentMode:)`: a valid sidecar of the current mode → Retry, Export…, Discard; missing / corrupt → Export…, Discard; the other mode → Export…, Discard + "Switch to Local to retry" (or Online). Retry: Online → `OpenAIFileProvider(fileModel(for: sidecar.model))`; Local → `LocalServerProvider.retry` → `DictationAPI.wholeFile` to the **current** host with `X-Client-Dedup-Id: <id>` (the entry's dictation id). The result goes to `last.json` and the clipboard first; only then `SpoolStore.retryDelivered` deletes the entry (`RetryEntries.dropAfterRetry`): kept on a Local `saved: false` or an empty transcript ("No speech detected; kept under Retry."). A terminal 410/409 deletes it; any other error keeps it. Export… copies through `AtomicFile.copy` (temp file beside the destination, then `rename`), so a failed export never removes an existing file.
- **Last text**: `last.json` (`LastText {text, at, mode, modelUsed}`, atomic 0600 write via `PrivateFile.write`) is read by "Copy last dictation" in both modes and expires after 7 days. It replaced the server `GET /me/dictations/last` call (`LastDictationAPI` removed).

### Server

| Component | File | Responsibility |
|---|---|---|
| **FastAPI app** | `server/src/wispralt/main.py` | Single uvicorn worker. Lifespan: requires `DATA_DIR`, migrates `wispralt.db`, validates `.env` permissions (warns if not 0600), runs `JobStore.recover_orphans()` BEFORE the staging sweep, loads Parakeet, then `await file_runner.reenqueue_pending()`. On exit it sets `shutting_down` and awaits `file_runner.shutdown()` first, before any loop cancel or store release. **The file model is NOT loaded here** — MLX Whisper loads lazily on the first file job via `files.pipeline._ensure_models_loaded()` and the eviction loop unloads it when idle. Registers SIGTERM handler, mounts routers. |
| **ParakeetService** | `server/src/wispralt/dictate/parakeet.py` | Warm-resident `mlx-community/parakeet-tdt-0.6b-v2` (float32 — bfloat16 caused a `[matmul] (128,257) vs (514,51)` shape error on first inference). Single `ThreadPoolExecutor(max_workers=1)` serializes all inference (MLX is not thread-safe per model instance). Warmup JIT pass at startup. Defensive return-type handling: checks `hasattr(result, 'text')` vs list of `AlignedToken`. Tracks p50/p95 latency percentiles over last 100 calls. **Audio decode boundary**: `_sync_transcribe` wraps `soundfile.read` in `try/except (LibsndfileError, RuntimeError) → CorruptAudioError` so malformed uploads convert to HTTP 422 (via the route handler) instead of leaking as 500. |
| **FileJobRunner** | `server/src/wispralt/jobs/runner.py` | `asyncio.Semaphore(1)` serialises execution; admission is decided by the set of spawned-but-unfinished worker tasks (`_workers`), so a second submit is refused even before the first worker has acquired the semaphore. Dedicated `ThreadPoolExecutor(max_workers=1, thread_name_prefix="wispralt-files")` isolates file-job work from the default asyncio thread pool. Busy or low-memory (< 2 GiB) submissions raise `FileJobInProgressError` with `code` `file_job_busy` / `insufficient_memory`, which the routes turn into a coded 429. `_run_source` owns the single `set_running` per attempt and re-checks `cancel_requested` right after the decode (a cancel during the decode ends the job before the model loads); `_fail(jid, reason)` records `cancelled by user` instead of any other reason (phase timeout, watchdog, exception) when a cancel was requested; `set_running` and `set_done` are conditional (`AND cancel_requested=0`), so a cancelled job can neither start nor finish `done` — it ends `failed: cancelled by user` with its outputs removed. The exception path also removes partial outputs. Staging files are cleaned up when the job ends, except when shutdown interrupted it (`CancelledError`, or `shutting_down` and the row still `running`): the input is kept and `requeue_interrupted` puts the row back to `pending` with its attempt refunded. `FileJobRunner(store, app.state)`; `shutdown()` cancels and awaits the workers, then `executor.shutdown(wait=False, cancel_futures=True)`. `reenqueue_pending()` called at startup for jobs whose inputs survived a restart (`MAX_ATTEMPTS = 3`). |
| **JobStore** | `server/src/wispralt/jobs/store.py` | `JobStore(db, data_dir)` on the one SQLite DB (`Database`: connection per call, `BEGIN IMMEDIATE` writes), so it is safe from any thread. `user_id` is a real FK to `users`; `wav_path` is stored relative to `data_dir` and returned absolute; outputs always live in `DATA_DIR/outputs`. Job lifecycle: `pending → running → done | failed`. `recover_orphans()` — see Failure Handling. |
| **files.pipeline** | `server/src/wispralt/files/pipeline.py` | `transcribe_file(...)`, holding `_load_lock` for the whole job: `_ensure_models_loaded()` (lazy first-call load, inside the `try` so a failed warmup still stamps the idle clock and releases the cache) → read the canonical WAV and average any channels to mono → resample to 16 kHz → **mlx-whisper turbo** (`mlx_whisper_loader.py`, `word_timestamps=False` hard-coded) → label every segment "Speaker 1" (`segments.py`) → atomic output write (`output.py`). Emits `phase_start`/`phase_done` structured logs at every seam; threads `progress_cb`/`cancel_cb` from the runner into the loader. Returns the v3 transcript dict (`mode="single"`, `model` lists the ASR model only). |
| **RateLimitMiddleware** | `server/src/wispralt/middleware/rate_limit.py` | In-memory per-IP rolling-window limiter. Two windows: `/transcribe/dictate` 60 req/60s; `/readyz/*` + `/healthz` 120 req/60s. Returns 429 with `Retry-After` header and no `code` field. File jobs are bounded by the runner, not the limiter. |
| **staging / env_writer** | `server/src/wispralt/ops/` | `staging.py` manages the staging directory; startup sweep removes orphaned WAVs older than 24h. `env_writer.py` atomically rewrites `.env` key-value pairs via tempfile-in-same-dir + `os.replace`, preserving mode 0600. |

---

**Lazy load and idle eviction.** The first file job after start (or after an eviction) pays a one-time load of MLX Whisper. Single-flight is enforced by a module-level `threading.RLock` (`_load_lock`): `transcribe_file` holds it for the whole job (`_ensure_models_loaded` re-enters it), so `evict_if_idle`'s non-blocking acquire skips while any job runs, and no job can pass the load check and then lose its model. `ops/background.eviction_loop` calls `pipeline.evict_if_idle(settings.file_model_idle_eviction_seconds)` every minute: when the model is warm, no job holds the lock and it has been idle past the threshold, it calls `_mlx_mod.reset()` (clears `mlx_whisper.transcribe.ModelHolder`), `gc.collect()` and `_mlx_mod.release_cache()` (`mx.clear_cache()` + `mx.reset_peak_memory()`; also run after every job), then runs a memory-delta self-check that increments `eviction_failures_total` and logs CRITICAL if the MLX pages did not come back. Observability snapshot: `pipeline.state() -> (warm, loading)` and `idle_seconds()`, surfaced on `/metrics` under the `meeting` key (name kept until a later schema pass).

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
| Eviction | `mlx_whisper_loader.reset()` clears `mlx_whisper.transcribe.ModelHolder`; `pipeline.evict_if_idle` follows it with `mlx_whisper_loader.release_cache()` so the unified-memory pages return to the OS (see Lazy load and idle eviction above) |
| Label / imports | `mlx_whisper_loader.MODEL_ID` is the one model label (the transcript `model` block reads it). `mlx`, `mlx_whisper` and `parakeet_mlx` are imported lazily and live in the `[host]` extra (`uv sync --extra host`), so `wispralt.main` imports without them; `ParakeetService(load_fn=…)` lets tests run dictation without MLX |
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
and use the per-user `/me/*` routes (`/me`, `/me/insights`, `/me/history`)
for their own data instead. The client's "View server
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

When `_run_pipeline_inner`'s executor eventually returns, `set_done` is conditional (`WHERE status='running' AND cancel_requested=0`) and returns whether a row changed. If it refuses (watchdog already wrote `failed`, or a cancel was requested), the outputs are removed, a cancelled job is recorded as `failed: cancelled by user`, and `transcript_text` stays NULL. On the DELETE side the status is re-read after setting the flag, so a job that finished in between has its outputs removed (204) rather than left `done` after a 202; a watchdog-`failed` job whose executor is still running (`runner.active_job_id`) gets 202, not an output removal under the running thread. A finished job's DELETE removes only its outputs and sets `outputs_removed_at`: the row and text stay for `/me/history` and retention, and `/transcribe/file/*` treats it as gone.

### Networking

The Cloudflare Tunnel maps `transcribe.<user-domain>` → `http://127.0.0.1:8000` on the Mac mini. `cloudflared` runs as a **user-level LaunchAgent** (`~/Library/LaunchAgents/co.wispralt.cloudflared.plist`), not as a system service — the `sudo cloudflared service install` path is broken on macOS 14/15+. The tunnel token is stored at `~/.config/wispralt/cloudflare-token` (mode 0600); the LaunchAgent reads it via `--token-file` on cloudflared ≥ 2025.4.0, or has it inlined in the plist (mode 0600) on older versions. The setup script (`scripts/setup-cloudflared.sh`) reads the token via `read -r -s` and unsets the shell variable immediately after writing the file.

Tunnel latency overhead: ~50–200ms same-region. The Cloudflare free tier has a community-reported body limit of approximately 100 MB; the server enforces `MAX_UPLOAD_BYTES` (default 2 GiB) at the application layer. Files over 50 MiB go through the chunked upload path below, so the edge cap never applies to them.

### Offline behaviour (no cloud fallback)

There is no cloud fallback for audio. When the Mac mini is offline, dictation retries the origin once (`DictationAPI`: origin, wait, origin; `X-Client-Dedup-Id` keeps a retry from double-persisting), then surfaces the existing dictation error toast. File jobs fail with the usual upload error; nothing is queued for later. `ServerClient.isOfflineSignature` still classifies offline errors for that retry decision. The OpenRouter Keychain item (`co.wispralt.openrouter`) is kept for Mercury smart formatting; it is not used for audio.

### Custom Transcriptions (menubar file picker)

The menubar QuickActions popover (`UI/SettingsView.swift`) exposes a "Transcribe file…" button that lets the user pick any audio/video container and upload it for transcription. This section is the **Local** path; Online is described under "Online file jobs". The client copies the source **as-is** (no transcoding) into a per-job folder under `<transcripts folder>/Custom Transcriptions/` (default `~/Documents/WisprAlt/Transcripts/Custom Transcriptions/`, or `Meetings/` when that folder already existed), then POSTs to `/transcribe/file` via `FileJobAPI.submitFile`. Output JSON/SRT/VTT/TXT lands beside the source. `CustomTranscriptionsStore.swift` manages the per-job folders; `LastTranscriptCaption.swift` powers a "Copy last custom transcription" button with a live mtime caption (folder DispatchSource watcher + 10s timer + `Notification.Name.wisprAltTranscriptWritten` observer; see `Util/TranscriptNotifications.swift`).

`FileJobAPI.swift` uses `URLSessionConfiguration` with `timeoutIntervalForRequest=300` and `request.timeoutInterval=6*60*60` so a 90-min upload over a slow uplink can complete without hitting the default 60s inactivity timeout.

### Large-file uploads (audio extraction + chunked upload)

Cloudflare's free / pro / business plans cap inbound request bodies at **100 MB**. A 1-hour stereo WAV is ~460 MB, and a 3-hour AAC m4a can easily clear 200 MB — both would 413 at the edge before reaching the origin. Two complementary strategies extend the supported range to 4 GB without touching Cloudflare's plan tier.

**Step 0 — Audio extraction (`client/WisprAlt/Capture/AudioExtractor.swift`).** Before deciding which upload path to take, the client passes the user-picked URL to `AudioExtractor.extractAudioIfVideo(_:)`. If the asset has BOTH a video and an audio track, it runs `AVAssetExportSession` with `AVAssetExportPresetPassthrough` and writes just the audio track to a temp `.m4a` (or `.mp4` fallback when the source codec isn't AAC-compatible). A 200 MB MP4 typically becomes a ~15 MB audio file — often dropping under the chunking threshold entirely and letting the existing single-shot `/transcribe/file` path serve the request. The extractor never throws: any probe / export failure returns the original URL so the upload still proceeds.

**Step 1 — Chunked upload (`client/WisprAlt/Server/ChunkedUploader.swift` ↔ `server/src/wispralt/routes/transcribe_file.py`).** If `uploadSize > 50 MiB`, the client switches from `FileJobAPI.submitFile` (multipart single-shot) to `ChunkedUploader.upload`. The three-step wire protocol is:

1. `POST /transcribe/file/chunked/init` with JSON `{total_bytes, chunk_count, original_filename}` → server returns `{upload_id, chunk_size}` (22-char `secrets.token_urlsafe(16)`) and creates `staging/chunked/<upload_id>/meta.json` recording the caller's `user.id`.
2. `POST /transcribe/file/chunked/<upload_id>/<chunk_index>` with raw bytes (`application/octet-stream`, NOT multipart) for each 50 MiB chunk. The body is streamed via `request.stream()` to `chunk-NNNN.part.tmp`, then atomically renamed on success. `Content-Length` is mandatory and validated against bytes-written (corrupt chunks → 400). The path declares `{chunk_index:int}` — Starlette's `:int` converter constrains this segment to digits so `/finalize` cannot match the chunk route. Without this constraint, Starlette's registration-order matching sent every `/finalize` request into the wildcard chunk handler with `chunk_index="finalize"`; Pydantic int-coerce then produced a RequestValidationError → 422 with `{"detail":[…]}`, which the client surfaced as the misleading "appears incomplete or corrupted" banner.
3. `POST /transcribe/file/chunked/<upload_id>/finalize` with an empty body. Server verifies all chunks present + `Σ(part.size) == total_bytes`, concatenates in `run_in_executor`, checks the assembled size, then hands the assembled file to the SAME `FileJobRunner.submit_source_or_429` that the single-shot route uses. The `.part` files are deleted only once the runner has accepted the job: a concat I/O error removes just the partial output and a busy 429 just the assembled copy, so finalize can be retried without re-uploading (the disk gate already requires room for one more copy). Finalize is serialized per upload id (an in-process claim set, taken before the first await; the server is one process): a concurrent second finalize gets 409 instead of racing the first on the same output path. Returns `{job_id, status: "pending"}` — from here the existing poll/download/delete loop is unchanged.

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
- Part 4 contract (server: `dedup.py`; client: `LocalRequests`): a 200 with `"saved": false` is delivered as usual and surfaces as `DictationResult.serverSaved == false` (`SpoolDisposition` keeps that audio and its Retry entry); 410 `{"error":"dictation_deleted"}` and 409 `{"error":"dedup_conflict"}` are terminal (`LocalDictationError`, `FailureKind.rejected`): no whole-file fallback, no retry, and the audio and Retry entry are discarded. A Local Retry reuses the entry's dictation id as `X-Client-Dedup-Id`. A 409/410 with a `detail` body (e.g. session not open) is not terminal.
- **Server side (Part 4, `dedup.py`).** The header is canonical: RFC-4122 shape, lowercased, 422 if malformed; required on chunk and finalize, optional on `/transcribe/dictate` (third-party callers). Whole-file and finalize hold an in-process **claim** (`DedupLocks`: one refcounted `asyncio.Lock` per id, keyed by the id alone, across users) across replay check → inference → save, so concurrent requests with one id run inference once; one process serves the DB, so an in-process lock suffices.
- **Replay before work.** `JobStore.find_dictation(id, user)` runs before the session lookup and inference: same user → 200 with the stored text and `"replayed": true` (no usage row: `request.state.dedup_replay`); another user → 409 `{"error":"dedup_conflict"}`; the owner soft-deleted it → 410 `{"error":"dictation_deleted"}` (no inference, no row; re-running would resurrect deleted text). A chunk for a stored id returns 202 `finalized: true` without opening a session. A session remembers its dedup id; a chunk or finalize with another id → 409.
- **Synchronous save.** `JobStore.insert_dictation` is one `BEGIN IMMEDIATE` select-or-insert (the global `UNIQUE(client_dedup_id)` stays as the collision detector), so at most one row exists per id whichever path wins. A `sqlite3.Error` still returns the text with `"saved": false`, bumps `dictation_save_failures_total` (in `/metrics` and `/readyz/db`) and logs only the error type. The fire-and-forget persist and its shutdown drain are gone.
- **Blank text.** Neither path stores blank text (nothing to keep; both report `"saved": true`). A retried `/transcribe/dictate` simply re-runs inference on its audio; a streamed session cannot (its partials are consumed), so the session records the blank result (`StreamingSession.blank_text`) and a retried finalize with the same id replays it (`"replayed": true`) rather than hitting 410 `Session is finalized`. In-memory only: a server restart in between gives the retry a 404, which the client's whole-file fallback covers.
- When `X-Client-Dedup-Id` is absent (third-party callers on `/transcribe/dictate`, and `/v1/audio/transcriptions`, which stores no row) there is no claim or replay and the column stays NULL.

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
| Dedup-id persistence | `jobs/store.py` — `dictations.client_dedup_id UNIQUE`; `find_dictation` / `insert_dictation` (atomic select-or-insert) |
| Dedup contract | `dedup.py` — header parsing, `DedupLocks` claim, `replay_or_none`, `persist`; used by `routes/dictate.py` and `routes/dictate_stream.py` |

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

The SIGTERM handler (registered in the lifespan when running on the main thread) only sets `app.state.shutting_down = True` and raises SIGINT, which uvicorn's own handler turns into its graceful shutdown; it no longer fails running jobs. The lifespan shutdown then:

1. sets `shutting_down` and awaits `file_runner.shutdown()` FIRST, while the DB is open: each worker is cancelled; a job it interrupts keeps its staged input (WAV or source) and `requeue_interrupted` sets the row back to `pending` with the attempt `set_running` took refunded, so a deploy never spends an attempt;
2. cancels the daemon loops and streaming sessions, closes the insights client;
3. drops the stores (`app.state.db` etc.);
4. releases the server lock (`<data_dir>.lock`) last.

A worker that finished (`done`/`failed`) during shutdown cleans up its input as usual. The executor thread itself cannot be cancelled: an ffmpeg decode stops at its next cancel poll, an mlx-whisper call runs to its end. `file_runner.shutdown()` waits up to `EXECUTOR_DRAIN_S` (10 s) for it; if the thread is still running, the lifespan calls `ops.backup.pin_until_exit`, which keeps the server lock held until the process really exits (Python joins the thread at exit), so a restore can never swap `DATA_DIR` under it.

A job's staged input (source or WAV) is deleted only once its row reads back `done` / `failed`. If marking it failed hits a DB error, the row stays `running` with its input, and the next start's `recover_orphans` requeues it.

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
- **First admin:** `wispralt setup --role host` creates it and hands over the key once (terminal or a new `--token-file`); `wispralt keys rotate-admin` replaces a lost admin key (`python -m wispralt.bootstrap` remains as a TTY-only shim over the same code). There is no env admin key and no break-glass path.
- **`request.state.user`** is set for every authenticated request; the usage middleware reads it.
- **Cookie fallback:** `require_api_key` falls back to the `wispralt_admin_token` cookie when no Bearer header is present (`/v1` never does). Set by `POST /admin/login` / `POST /me/login`; `HttpOnly`, `Secure`, `SameSite=Strict`, `max_age=8h`. `auth.cookie_secure(request)` drops `Secure` (session and CSRF cookies) only for plain http to a loopback host (`localhost`, `127.0.0.0/8`, `::1`), so the setup URL `http://127.0.0.1:8000/admin` logs in on Safari; callers that pass no `request` stay Secure.

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

Tracked requests are POSTs only, classified by `usage.events.kind_for_path`: `dictate` = one whole dictation (`/transcribe/dictate` or a streamed `…/finalize`), `dictate_chunk` = one streamed chunk, `file` = one file job (`/transcribe/file` or the chunked `…/finalize`), `file_chunk` = chunked init / pieces, `v1_dictate` (`/v1/audio/transcriptions`), `v1_translations`. So `dictate` latency and error rates are per dictation; the measured stats (`insights/measured.py`) count only the whole-request kinds (`WHOLE_REQUEST_KINDS`). The admin overview's Dictations 24h/7d/30d tiles count `dictations` rows (`JobStore.dictation_counts`), not requests; the person page's tiles still count that user's `usage_events` requests. Since Part 6 each row also records the authenticating `key_id`, which gives every device its own "last seen" and drives the active/recent client list on `/admin/system`.

### Admin UI

```
┌────── /admin (FastAPI APIRouter) ─────────────────┐
│                                                     │
│  public_router  ─► /admin/login, /admin/setup      │  no auth (setup gated)
│                                                     │
│  me_router      ─► /admin/me                       │  Depends=[require_api_key]
│                                                     │  any role — admin or employee
│                                                     │
│  authed_router  ─► everything else                  │  Depends=[require_admin]
│      /admin/  /admin/people[/new|/{id}]             │
│      /admin/people/{id}/rotate …/disable           │
│      /admin/keys/{key_id}/revoke   (one device)     │
│      /admin/integrations[/new|/{id}/rotate|revoke]  │
│      /admin/usage  /admin/usage.csv                 │
│  admin_invites ─► /admin/invites[/{id}/revoke]      │  require_admin
│  admin_system  ─► /admin/system[/snapshot]          │  require_admin
│  admin_data    ─► /admin/data[/runs…] /records      │  require_admin per route
│                   /admin/settings                   │
└─────────────────────────────────────────────────────┘
   public: POST /invites/redeem (admin_invites.public_router)
```

Source: `server/src/wispralt/routes/{admin_ui,admin_invites,admin_system,admin_data}.py`
+ `server/src/wispralt/admin/templates/*.html.j2` + `admin/static/portal.css`.

Three-router pattern (be720a1 split out `me_router`):

- **`public_router`** carries `/admin/login` (GET form + POST submit).
  Must be reachable without auth, otherwise neither role has a way to
  acquire the session cookie. **The login form accepts ANY valid
  token** — admin and employee — and redirects by role on success: admin
  → `/admin/`, employee → `/admin/me`.
- **`me_router`** carries `/admin/me`, gated by `require_api_key` (NOT
  `require_admin`). Admins are 303'd to `/admin/`. Employees see their
  own person page without admin actions; `base.html.j2` shows the Admin nav
  group only for admins (employees get My history / My insights / My usage).
- **`authed_router`** has `dependencies=[Depends(require_admin)]`, so a
  browser hitting `/admin/` without a valid admin token gets 401/403.
  Every page reads `AccountStore` / `UsageStore` in a worker thread.

Auth model: `Authorization: Bearer ...` (curl/Postman) or
`wispralt_admin_token` cookie (browser, set by `POST /admin/login`).
`auth._extract_bearer` falls back to the cookie when there is no Bearer
header. Cookie-authenticated mutations are guarded by `auth.require_csrf`,
a dependency that declares `require_api_key` as its own sub-dependency (so
auth always resolves first and records `request.state.auth_via`) and skips
the token only when `auth_via == "bearer"`; otherwise the `X-CSRF-Token`
header or `csrf_token` form field must pass `auth.verify_csrf`, or 403. The
token is **bound to the session**: `auth.session_csrf_token(session)` =
HMAC-SHA256 keyed by the (HttpOnly, secret) session token, so a sibling
same-site origin that plants a parent-domain `wispralt_csrf` cookie still
cannot forge it (an unsigned double-submit cookie could simply be
overwritten). This works over loopback http, where a `__Host-` cookie would
not (it needs Secure). With no session cookie (the login and setup forms
only) the token must equal the `wispralt_csrf` cookie (double-submit). Every admin mutation takes it as a parameter through
`require_admin_csrf`; `PATCH /me` and the `/me/history` mutations through
`require_csrf` beside `forbid_integration_kind`. Pages that render those
forms embed `auth.csrf_token_for(request)` (`admin_ui._form_page`): stable
for the session, so full pages and partials agree and nothing rotates. `SameSite=Strict` stays as a
second layer.

The macOS client's menubar **Open Portal** button opens
`<server>/admin/login` for everyone — the same install ships to admins
and employees without a per-role configuration. The server's
`login_submit` redirect handles the role-based fork.

Templates use the `.html.j2` extension. Jinja2's default autoescape list
does **not** include this extension; `select_autoescape(enabled_extensions=("html.j2", "html", "j2"))`
is set explicitly so user-supplied fields (label, notes, error_class)
render escaped, closing the stored-XSS hole on the admin UI.

Escaping does not protect a value inside an inline handler or an Alpine/HTMX
expression (`onsubmit`, `x-data`, `@click`, `:class`, `hx-on`): the browser decodes
`&#39;` back to `'` before evaluating it. So no template interpolates `{{ … }}` into
such an attribute; values go in a `data-*` attribute and the expression reads
`$el.dataset.*` / `this.dataset.*`. The routes also validate `range` with
`timewindow.normalize_range` (unknown → the page default). `test_portal_render.py`
fails on any template that breaks this rule.

**Part 6 portal rules.** One stylesheet (`admin/static/portal.css`: CSS variables,
light/dark via `prefers-color-scheme`, sidebar → top strip under 768 px, tables scroll
inside `.table-wrap`) and the `_ui.html.j2` macros; no `<style>` blocks, no build
step, no CDN. Every page that shows a secret (a new key, an invite, the first-admin
key) sends `Cache-Control: no-store` and renders it through `secret_once`. HTMX
partials that poll or swap (`/admin/system/snapshot`, `/admin/data/runs/current`,
range tabs) never mint or rotate the CSRF cookie, so forms already on the page keep
working. **First-admin setup**: while no enabled admin exists the lifespan writes
`data_dir/setup-code` (0600, 128-bit) and logs its path; `/admin/setup` needs CSRF and
that content (constant-time compare), then calls `bootstrap_first_admin` (race-safe:
`AlreadyBootstrapped` → 404) and deletes the file. `request.client.host` is loopback
for every tunnelled request, so it is never used as an auth signal. **Invites**: a
`secrets.token_urlsafe(32)` token stored as sha256 with a 10-minute TTL;
`POST /invites/redeem` validates and redeems in one `BEGIN IMMEDIATE` (so concurrent
redeems issue one key), answers every failure with the same `400 invite_invalid`, and
is rate-limited per client IP.

---

## Operating the host (`wispralt` CLI)

`wispralt` (console script `wispralt.cli.main:main`, installed by `uv tool install <clone>/server`) installs, runs and maintains a host and/or client; [INSTALL.md](INSTALL.md) is the command reference. It wraps the server's own functions (accounts, retention, `ops.backup`) and never re-implements them.

- **Layout.** `cli/main.py` (argparse + dispatch), `paths.py` (`Paths`, `InstallState`, `Ctx`, `.env` merge), `system.py` (`Runner`, `http_get`, `Deps` — the only `subprocess`/`execv` seam), `launchd.py`, `host.py`, `client.py`, `ops.py`.
- **Paths.** `WISPRALT_ROOT` (default `$HOME`) re-roots the config dir `~/.config/wispralt/` (`.env` and `install-state.json`, both 0600), the data dir `~/Library/Application Support/WisprAlt/host`, `HF_HOME` `…/WisprAlt/models`, logs `~/Library/Logs/WisprAlt/` and backups `~/WisprAlt-Backups/`. `--data-dir`/`--backup-dir` override and are recorded. The LaunchAgent plist always goes in `$HOME/Library/LaunchAgents`; `WISPRALT_LABEL` overrides its label (tests, Part 8 E2E).
- **State.** `install-state.json` records `{version, source_dir, role, label, port, paths, models, artifacts, data, retention_prompted, last_run}`, written atomically. `artifacts` are what a plain `uninstall` removes; `data` only goes with `--purge-data`; backups are never removed by the CLI.
- **Setup is idempotent.** `setup` adds only absent `.env` keys, re-verifies the models (sha256 of the LFS files against the Hub's metadata at the resolved commit; nothing is downloaded when the cache matches), never creates a second admin or rotates a key, and asks the retention question once. A missing `[host]` extra is installed with `uv tool install --force` and the CLI `execv`s the fresh entry point, so new code is never half-imported. The first admin's key is handed over right after it is minted (terminal or `--token-file`), before the warm-up and health wait.
- **Service.** A per-user LaunchAgent `co.wispralt.host` (`gui/$UID`) runs uvicorn from the tool venv on `127.0.0.1:<port>` with the config dir as its working directory; remote access goes through `tailscale serve` or the optional tunnel. `stop` is proven by taking `server_lock` (bootout returns before teardown ends); `restart` reloads only when the rendered plist changed. A mini-era `co.wispralt.server` agent is booted out and its plist moved to `config/legacy/`.
- **Status and doctor.** `status` reports allocated sizes (`st_blocks`, so APFS clones are not double-counted), backups, `*.pre-restore-*` copies and retention. `doctor` is read-only (`mode=ro` integrity check, Keychain presence without reading secrets); only `--warmup` loads the dictation model, so a running host never pays its RAM twice.
- **Offline operations.** `backup`, `restore` and `update` run inside `service_stopped`: boot the agent out, prove it stopped by taking `server_lock`, run the Part 4 function (`ops.backup.backup` / `restore`), and start the agent again in `finally` if it had been loaded. `restore` keeps the old tree at `<data_dir>.pre-restore-<ts>`; the CLI never deletes those or any backup.
- **Update and uninstall.** `update` pulls (`--ff-only`, refusing a dirty clone), stops the service, backs up, reinstalls the tool and `execv`s its second phase in the new code, which re-verifies models, restarts the agent and waits for `/healthz` + `/readyz/db` (migrations run in the lifespan). `uninstall` removes only the recorded `artifacts` (and only under the install root or LaunchAgents), with `uv tool uninstall` last; `--purge-data` backs up first (a files-only copy when there is no DB) and then deletes only what WisprAlt owns in the data dir (`wispralt.db*`, `setup-code`, `outputs/`, `staging/`; the dir only if then empty). A phase-2 failure still tries to start the service. Explicit `--server-url` / `--port` on a setup rerun update `SERVER_URL` (`paths.set_values`); defaults never clobber.
- **Exit codes.** 0 ok, 1 failed, 2 usage error or missing non-interactive answer, 3 doctor failures. `--non-interactive` never prompts; `--json` is machine output.
- The CLI never imports `wispralt.config` (its module-level `Settings()` needs a `.env`): it takes the data dir from `install-state.json`, and the service reads the config `.env` from its launchd `WorkingDirectory`.

## Background maintenance and logging

`ops/background.py` hosts the lifespan's daemon loops. `daily_sweep_loop` runs every 24 h (in a worker thread): stale staging WAVs (>24 h, excluding WAVs of active jobs), stale chunked-upload dirs (>1 h), stale file-job output `.tmp` files, and `run_retention` (see "Retention" below). The staging sweeps and `run_retention` also run once at startup.

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
dictations, jobs, weekly_insights, settings). `0002_portal.sql` (Part 6) adds the
full-text indexes, `insight_runs`, `usage_events.key_id` and the AI settings seeds;
`0003_import_runs.sql` (Part 7) adds `import_runs`. The runner applies files in sorted
order, so numbers may arrive out of merge order.

**Full-text search (Part 6, `0002_portal.sql`).** `dictations_fts` is an
*external-content* FTS5 table over `dictations.text` (`content_rowid='id'`, a stable
`INTEGER PRIMARY KEY`). `jobs_fts(text, job_id UNINDEXED)` is a *regular* FTS5 table:
`jobs.id` is TEXT, and implicit rowids can change under `VACUUM` / `VACUUM INTO` (the
backup path), so the job id is stored as a column. Tokenizer `unicode61
remove_diacritics 2` (`cafe` finds `café`). Six triggers keep both exact for every
writer (routes, retention purge, the Part 7 importer, restore): insert; update of the
text column guarded by `WHEN old IS NOT new` (jobs get `transcript_text` late, NULL →
text, and may go back to NULL); delete. An external-content delete must pass the
**old** text (`INSERT INTO dictations_fts(dictations_fts, rowid, text)
VALUES('delete', old.id, old.text)`) or the index is corrupted; the tests run
`'integrity-check'` after every kind of mutation. Soft delete leaves the index entry:
every query joins back to the source row and filters `deleted_at IS NULL`, so restore
needs no reindex. Deleting from `jobs_fts` by the UNINDEXED `job_id` scans `jobs_fts`;
at file-job volume (thousands of rows) that is fine, and a retention purge of N jobs
costs N such scans offline. Queries never pass user input raw to `MATCH`
(`jobs.store.fts_query`: each token quoted, ANDed, last token a prefix, 200 chars / 12
tokens). The owner is bound in SQL: `search_history(user_id, q)` for `/me`, a separate
admin-only `search_history_team` — there is no "all users" wildcard. Snippets come back
raw with `\x02…\x03` sentinels and are rendered by `web.templates_env.highlight`
(escape first, then `<mark>`). A SQLite build without FTS5 fails the migration with
`MigrationError("SQLite lacks FTS5")`.

**Server lock.** The lifespan takes `fcntl.flock(LOCK_EX | LOCK_NB)` on the
sibling `<data_dir>.lock` (`ops.backup.server_lock`) before it opens the DB and
releases it last; a second server on the same `DATA_DIR` refuses to start
(`ServerRunning`). The lock lives beside, not inside, `DATA_DIR`, so a restore's
directory swap never moves it. `lock_path` resolves `DATA_DIR` first, so a
symlinked `DATA_DIR` and its target share one lock. (In WAL mode an idle server holds no SQLite lock,
so no SQLite probe can prove the server is stopped; only this flock can.)

**Offline backup and restore** (`ops/backup.py`; the Part 5 CLI stops the service,
runs them, and restarts it). Both take the server lock non-blocking and raise
`ServerRunning` while a server runs, and both preflight disk first: the estimated
bytes × 1.1 must fit in the target parent's free space, else `InsufficientSpace("need
X GB, have Y GB")` before anything is written.

- `backup(data_dir, dest)` refuses a `dest` that exists or overlaps `DATA_DIR`
  (`BackupRefused`); takes a `VACUUM INTO` snapshot and switches it to
  `journal_mode=DELETE` (so no `-wal`/`-shm` appears beside it); copies `outputs/`
  and `staging/` (regular files only); writes `manifest.json` (relative POSIX paths,
  sha256, bytes, per-table counts, `schema_version`). Dirs 0700, files 0600,
  whatever the umask.
- `restore(backup_dir, data_dir)` refuses (`RestoreRefused`, nothing changed) a
  backup inside `DATA_DIR`, a symlink anywhere in it, a manifest path that is
  absolute, has `..` or leaves the backup, any file not in the manifest (e.g. a
  stray `wispralt.db-wal`), a missing database entry, a hash or size mismatch, or a
  failed `integrity_check` (run on the staged copy opened `?mode=ro&immutable=1`, so
  the backup is never written). Files are copied into a sibling `.restore-<ts>`
  (0700/0600), `DATA_DIR` is moved aside to `.pre-restore-<ts>` and the staged tree
  takes its place; a failed swap puts the original back. It returns the aside path,
  which is never deleted (a refusal removes only its staged copy).

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

### History import writer (Part 7)

`0003_import_runs.sql` adds `import_runs` (one row per source per run: `status`
running/done/failed/blocked, `checkpoint`, `counts_json`). Numbering skips `0002`,
which Part 6 owns: `migrate()` applies pending files in sorted order and does not
need contiguous numbers (a DB that got `0003` first simply applies `0002` later).

`wispralt.imports` is the one generic writer for imported history.
`insert_rows(con, table, rows, source)` runs inside the caller's `write()` (so a
batch and its `import_runs` checkpoint commit together); `import_rows(db, …)` wraps
it in its own transaction. Tables and columns come from a fixed allowlist
(`users`, `usage_events`, `dictations`, `jobs`; never `api_keys`/`invites`), every
row needs a `source_id`, and bad input raises `ValueError` before anything is
written. Each row runs in its own SAVEPOINT with
`ON CONFLICT(source, source_id) WHERE source IS NOT NULL DO NOTHING RETURNING id`:
it is inserted, a duplicate (its existing id is read in the same transaction), or
a `RowError` naming the `source_id` and the failed constraint, while the rest of
the batch commits. `link_existing` stamps an existing row (the owner's local admin)
with a `(source, source_id)` once, idempotently. A real import holds the offline
`<data_dir>.lock` via `ops.backup.server_lock` (through `importer/lock.py`'s
re-entrant `offline`), so it refuses while the server runs; `python -m
wispralt.importer` takes it *before* migrating and holds it through the import, and
a dry run never migrates.

**Supabase export (read-only).** `importer/supabase_export.py` copies the old
Postgres history (`users` without `token_hash`, `usage_events`, `fallback_events`)
into `~/WisprAlt-Backups/supabase-snapshot-YYYY-MM-DD[-N]/` (dir 0700, files 0600):
one canonical-JSON line per row in `<table>.jsonl`, plus `counts.json` per table
(`source_count`, `exported`, `columns`, `max_id`, `sha256` of the JSONL bytes,
`exported_at`). Supabase is never written: `PsqlSource` only sends allowlisted SELECTs through
`psql -X -q -At -v ON_ERROR_STOP=1` with `PGOPTIONS=-c default_transaction_read_only=on`;
table names are fixed, columns come from `information_schema` and are
identifier-checked, and `_check` refuses any statement with a write verb. Column
discovery is a catalog read (`BEGIN TRANSACTION READ ONLY; <SELECT>; COMMIT;`); all
**data** then comes from ONE psql session in ONE `BEGIN TRANSACTION ISOLATION LEVEL
REPEATABLE READ READ ONLY` transaction, so every table is the same consistent
snapshot. The
connection string (env `WISPRALT_IMPORT_PG_URL`, else Keychain item
`co.wispralt.import-pg`) reaches `psql` only as `PG*` env vars. Per table that session prints a
`count(*)`/`max(id)` header line, then one `row_to_json` line per row; the parser
demands exactly that shape and the rows must match the header, else
`ImportIntegrityError` (no retry: a snapshot cannot drift). The output is held in
memory, fine for this history's size. A fresh snapshot dir is created per run
(`-2`, `-3`, … on the same day); files are created with `O_EXCL`, a failed write's
`.part` file is kept, and nothing is ever deleted or overwritten. An unreachable or paused project raises `ExportBlocked`
and leaves `BLOCKED.json`; nothing ever calls a Supabase management API or
unpauses it. `JsonPagesSource(dir)` reads `<table>.json` arrays saved by hand from
the same SELECTs (read-only MCP fallback).

**Snapshot import.** `importer/supabase_import.py` verifies every `<table>.jsonl`
against `counts.json` (sha256 and line count) before anything is written, then maps
each row into exactly one bucket — new, duplicate, linked, malformed or unmatched —
and checks `source_count` equals their sum per table (fail closed). Users get new
local ids (`source='supabase.users'`, `source_id` = the Postgres id), `revoked_at`
becomes `disabled_at`, and no `api_keys` are created (people rejoin by invite);
`last_seen_at` and the fallback `provider`/`provider_request_id`/`cost_micro_usd`
stay in the snapshot only. The **owner** — the one enabled Supabase admin — is
*linked* to the one enabled local admin (`link_existing`) instead of inserted; the
mapping must be confirmed (`--yes` only when unambiguous, or `--owner-map PG=LOCAL`),
an earlier link counts as confirmed, and with no local admin the import refuses
until `wispralt setup --role host` has created one. `usage_events` keep their kind
(`meeting` → `file`), `fallback_events` land in `usage_events` as `fallback_<kind>`,
and `user_id` is remapped through the imported users (unknown → unmatched). Rows go
in batches of 500, each committed with its `import_runs` checkpoint, so a crashed
run resumes the same run row; a first run with any duplicate, or a write that does
not account for every planned row, marks the run `failed`. A dry run only reads and
prints the counts and the owner proposal.

**Transcript files.** `importer/files.py` imports TXT/JSON/SRT/VTT transcripts as
`jobs` rows (`source='files'`, `status='done'`, `mode='file'`, `wav_path=''`, with
`estimated=1` when the time is a file mtime); see
[TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md#importing-old-transcript-files-part-7).
Each group's files are copied to `.import-<id><ext>.part` temps in `outputs/` and
renamed to `<id><ext>` (fresh uuid names) *before* the batch and its checkpoint
commit, so a committed row always has its outputs; a duplicate, a row error, a
failed rename or an uncommitted batch removes only those copies. A crash between
rename and commit leaves unreferenced `<uuid>.*` copies; the retry makes new ones. The first-run
duplicate abort does not apply here: the same file in two folders is an expected
duplicate.

**Entry point.** `wispralt.importer` exports `run_export`, `run_snapshot_import` and
`run_file_import` (what the Part 5 `wispralt import …` CLI will wrap);
`python -m wispralt.importer export | snapshot DIR | files PATH… --owner ID` is the
interim operator command (`--dry-run`, `--yes`, `--owner-map PG=LOCAL`; exit codes
0 ok, 1 error, 2 refused, 3 BLOCKED, 4 integrity). A real import needs `--yes` or an
interactive confirmation after the dry-run report. Runbook:
[ADMIN.md](ADMIN.md#importing-old-history-supabase--transcript-files).

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
  request-response (no job row); the route saves the row synchronously
  (`dedup.persist` → `JobStore.insert_dictation` in a thread) before it
  responds, and the response says `"saved": true|false` (see Idempotency).

Both tables carry `user_id`, a real FK to `users`, so `/me/*` filters to one
user and `/admin/data` groups by it (`JobStore.stats_by_user`, one query).

### Privacy posture

- **No opt-in flag.** Deliberate product decision; do not re-introduce
  gating "to be safe."
- **Retention: keep forever by default.** One DB setting, `settings.retention_days`
  (seeded `'0'` by `0001`; a missing row also reads 0 = forever; DESIGN §2.1).
  `JobStore.retention_days()` / `set_retention_days(n)` / `retention_preview(n) →
  {"dictations", "jobs"}` (what a purge would delete now; the Part 5 CLI shows it
  before setting). When `n > 0`, `background.run_retention` (startup + daily)
  calls `purge_dictations(n)` (by `created_at`) and `purge_expired_jobs`: finished
  (`done`/`failed`) job rows older than `n` days by `COALESCE(finished_at,
  created_at)` plus their `<id>.{txt,srt,vtt,json}` in `outputs/`. Pending/running
  jobs and any job the runner still tracks as active are never touched; an id whose
  output path would leave `outputs/` is skipped (files untouched), and a job whose
  files fail to delete keeps its row for the next run. Imported rows (`source IS NOT
  NULL`) and `usage_events` are never purged.
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

The analysis loop: it turns a week of captured transcripts into per-person and team
summaries. Since Part 6 it is **opt-in** and every AI claim is tied to the records it
came from; the numbers people see are **measured** from the database, never asked of
the model.

Source: `server/src/wispralt/insights/{client,prompts,sources,measured,settings,cron,timewindow}.py`
and `jobs/store.py` (`weekly_insights`, `insight_runs`, refs, measured reads).

### Measured vs AI

| | Measured (`insights/measured.py`) | AI interpretation (`insights/cron.py` + `sources.py`) |
|---|---|---|
| Source | DB rows: `dictations`, `jobs`, `usage_events` | OpenRouter, only when enabled |
| When | Every page view, any range | Weekly (scheduled) or a run/backfill from the portal |
| What | counts, words, active days, file audio minutes, filler words (regex `um/uh/like/you know`), time saved (**estimate**, 40 wpm), dictation latency p50/p95, error rate, 8-week trend, hour of day | digest, action items, decisions, blockers, projects, topics, quotable line; team: themes, action items, blockers, decisions, knowledge gaps |
| Numbers | yes | **none**: `pct_time`, `filler_word_count`, `wow_delta`, `tool_mentions` were removed |

Measured counts come from `dictations` rows (a streamed dictation is one row however
many chunk requests it took); `usage_events` only feeds latency and error rates.
`dictations.duration_ms` is inference time, so audio minutes cover file jobs
(`jobs.audio_duration_s`) only. Weeks, trend buckets and the hour chart use the
insights timezone (DB setting, else `INSIGHTS_TIMEZONE`, else the host zone from
`/etc/localtime`, else UTC).

### Source refs (insight schema v2)

1. **Assemble.** Each record goes into the prompt behind a bracketed ref: `[d:12]`
   (dictation 12) or `[f:<job id>]`, newest first, capped at `insights_input_word_cap`
   words. `sources.assemble` returns the set of refs actually placed in the prompt.
2. **Parse.** The reply is parsed with pydantic models (`extra='ignore'`). A wrong
   shape (`sources` not a list, a non-object) is an `InsightShapeError` and counts as
   a person failure; bad JSON likewise.
3. **Validate.** Each item keeps only refs from the prompt set and is dropped when none
   remain, so an invented ref, or another person's record (it was never in this
   person's prompt), can never survive. `quotable_line` is kept only when its text is an
   exact substring of the record it cites. The team pass receives the validated person
   insights of the week (saved earlier or new in this run) and its items may cite only
   refs those insights cite.
4. **Render.** `sources.build_view` re-checks refs at view time
   (`JobStore.refs_exist(refs, viewer, is_admin)`): refs to deleted records, or to
   records the viewer may not open, are hidden, and an item left with none is hidden.
   Each ref links to `/me/history/{kind}/{id}` (own) or `/admin/records/{kind}/{id}`
   (admin). A **stale** notice appears when records of that week were created after
   `generated_at` (`count_records_after`); a topic also present in the previous 4 weeks'
   stored insights shows "seen N weeks" (`recent_topic_labels`, computed, not asked).
   Rows without `schema_version: 2` render a "regenerate" notice (no converter: the old
   host was wiped and Part 7 imports no insights).

The prompts carry no ids beyond the refs (no `user_id`, no `api_key_id`). Logs carry
ids, error classes and lengths, never transcript or response text.

### Enablement, scheduler and runs

AI settings live in the DB `settings` table (`insights/settings.py`): `insights_enabled`
(effective only together with `insights_disclosure_ack_at`), `insights_model`,
`insights_cap_30d_usd` (default 8), `insights_timezone`. They are edited at
`/admin/settings`; enabling requires acknowledging the disclosure that transcript text
will be sent to OpenRouter. The OpenRouter key comes only from the host `.env` (`~/.config/wispralt/.env`). Readers
fail closed (no store or a DB error = off).

`scheduler_loop` (an in-process asyncio task from the lifespan) checks once an hour
(first after 60 s) whether the **last full ISO week** is due: AI enabled, a client
present, no `done` scheduled run for that week, and no scheduled attempt in the last 6
hours. This replaces the old Sunday-23:00 sleep and the opt-in startup catch-up: a
missed week self-heals at the next tick, and sleep or DST cannot skip it. Older weeks
run only by an explicit backfill.

`run_weeks(app, weeks, trigger, force)` runs weeks one after another under
`app.state.weekly_insights_lock` (a held lock → the call returns None; nothing
overlaps or double-bills). That lock is per process, so the run row is also the
**DB run lock**: `JobStore.start_insight_run` checks for another `running` row and
inserts its own in one `BEGIN IMMEDIATE` transaction, and returns None while one exists
(`run_weeks` → None), so the server and a CLI run never both spend the same remaining
budget. A `running` row older than `cron.RUN_LOCK_STALE_S` (2 h) is a crashed run and no
longer counts; a cancelled run (shutdown) closes its row `aborted` / `cancelled`. Each
week writes an `insight_runs` row:

```sql
CREATE TABLE insight_runs(
  id INTEGER PRIMARY KEY, trigger TEXT NOT NULL CHECK (trigger IN ('scheduled','manual','backfill')),
  iso_year INTEGER NOT NULL, iso_week INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running','done','failed','aborted','skipped')),
  people_total INTEGER NOT NULL DEFAULT 0, people_done INTEGER NOT NULL DEFAULT 0,
  failures_json TEXT NOT NULL DEFAULT '[]',   -- [{user_id, error_class}] only
  estimated_usd REAL, cost_usd REAL NOT NULL DEFAULT 0, error_class TEXT,
  started_at REAL NOT NULL, finished_at REAL);
```

Progress is updated after every person; the portal polls it every 2 s. A run ends
`done`; `failed` (`all_people_failed`; `some_people_failed`, when any person call failed:
the team pass is then skipped, since a team insight missing someone would be saved as
complete and never revisited; `team_openrouter_error` / `team_unusable` when the team
call errors or is unusable twice; or a crash with its exception class); `aborted`
(`budget`, `rate_limit` or `cancelled`); or `skipped` (`ai_disabled`, `no_client`). A
failed or aborted scheduled run is not `done`, so the scheduler retries it. Without
`force` a week is restart-safe (people who already have a row are skipped, so nobody is
paid for twice) but their saved insights still go into the team prompt; the team pass
runs when the week has no team row, anyone new was analysed, or with `force`, and no
person failed. `force` regenerates everyone.

### Cost control

- The **30-day cap** is checked before **every** OpenRouter call (person and team),
  against the 30-day spend at the start of the run plus this run's spend **plus that
  call's reserved worst case** (`cron.call_reserve_usd`: prompt tokens counted
  generously, the larger of 1.35/word and 1 per 3 chars, plus a full `max_tokens`
  reply, at the rate card via `client.worst_case_cost_usd`). A call that could cross
  the cap is never made.
  `rolling_insights_cost_usd` takes the larger of the stored insights' cost and the
  runs' recorded spend (a forced rerun overwrites a row's cost; runs also count replies
  that were billed but unusable).
- Cost comes from the response's `usage.cost`, else the `INSIGHTS_PRICING_PER_1K` rate
  card (`_default` is deliberately high, so an unknown model fails closed).
- A 429 (`RateLimitedError`) aborts the run; the next due tick (after 6 h) retries.
- Before any portal-started run the admin sees an **estimate**: words actually sent
  (per qualifying person, capped) × 1.35 tokens/word ÷ 1000 × the model's input rate.

### Data shapes

`weekly_insights` keeps one row per `(iso_year, iso_week, scope_key)` (`'team'` or
`'user:<id>'`; `upsert_weekly_insight` overwrites). Person `insight_json` (v2):

```json
{
  "schema_version": 2,
  "digest": [{"text": "Planned the Q4 launch", "sources": ["d:12"]}],
  "action_items": [{"text": "ship the migration", "owner": "self", "sources": ["f:3f2a…"]}],
  "decisions": [{"text": "go with vendor B", "sources": ["d:14"]}],
  "blockers": [{"text": "waiting on legal", "sources": ["d:13"]}],
  "projects": [{"name": "Acme proposal", "sources": ["d:12", "d:15"]}],
  "topics": [{"label": "billing", "sources": ["d:12"]}],
  "quotable_line": {"text": "the migration ships on Friday", "source": "d:12"}
}
```

Team: `{schema_version: 2, themes: [{label, sources}], team_action_items: [{text, owner,
sources}], team_blockers, decisions, knowledge_gaps: [{text, sources}]}`.

### Scope semantics

- A person row is written for each active person with at least **5 dictations or 1 file
  transcription** that week; below that they are skipped (no call, no charge).
- The team row is written when at least one person insight succeeded in the run.

---

## Data portal

The render loop: one shell (`base.html.j2` + `portal.css` + `_ui.html.j2`) for admins
and employees.

### Surfaces

- **Admin Data — `GET /admin/data`.** Measured (team, or `?user_id=N`), the AI panel,
  the per-person table and the AI runs panel. Admin-only.
- **My insights — `GET /me/insights`.** The caller's Measured stats and their AI panel.
- **Records — `GET /admin/records`** (admin) and **My history — `GET /me/history`**
  (own rows only): see "Personal history" below.
- **Login — `/admin/login`, `/me/login`.** Both set the same `wispralt_admin_token`
  cookie with `path="/"`, so one sign-in covers `/admin/*` and `/me/*`.

### HTMX partial swaps

Range tabs swap `#insights-body` with `_me_insights_body.html.j2` or
`_admin_data_body.html.j2` (Measured + AI panel) when `HX-Request: true`; the partial
branch never mints a CSRF cookie. The Alpine tabs sit outside the swap target; an
`htmx:historyRestore` handler in `base.html.j2` re-syncs them on back/forward. The runs
list (`_runs_list.html.j2`) re-polls itself every 2 s only while a run is going.

### Shared helpers

`web/templates_env.py` (one `Jinja2Templates`, autoescape on `.html.j2`, the `highlight`
and `ts` filters), `web/htmx.py` (`is_htmx`), `web/records.py` (time window, row
decoration), `insights/timewindow.py` (ISO weeks, range tabs, `host_timezone`), `auth`
cookie + CSRF helpers.

### Vendored JS

HTMX 2.0.4 (`/admin/static/htmx.min.js`) and Alpine 3.14.8 (`/admin/static/alpine.min.js`)
are checked in and served by the `/admin/static` `StaticFiles` mount with SRI hashes.
CDN delivery was rejected: it would widen the supply-chain surface and the portal must
work during an internet outage. Refresh out-of-band: download, verify the SHA-256
against the release, commit.

---

## Personal history (`/me/history`)

Plan A's headline employee surface. Every authenticated employee can browse
their own dictations + file transcriptions (including legacy meeting rows) in one
chronological list, filter by range / kind, full-text search it (Part 6), expand any row to
see the full transcript text, soft-delete with a one-click undo path, and
download an individual transcript as `.txt` or `.json`. Sits in the same
sidebar as `/me/insights`.

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
optional kind filter, ORDERs each by
`(created_at DESC, row_id DESC)`, then merge-sorts the two streams in
Python.

Filters live entirely server-side:

- `range` (default `30d`) — one of `today`, `7d`, `30d`, `90d`, `1y`, `all`,
  reusing `insights/timewindow.epoch_for_range` so the history page and the
  insights page agree on what "30d" means.
- `kind` (optional) — `dictation` or `file` (every `jobs` row; displayed as "File transcriptions"). `meeting` URLs from before Part 4 404. Empty string from the
  `<select>` element is normalized to None at the handler.
- `q` (optional, Part 6) — 2+ characters after `.strip()` switch the page to
  **full-text search**: `JobStore.search_history(user_id, q, since, until, kind,
  limit, offset)` over the FTS5 indexes (see "Local database → Full-text search"),
  newest first, offset paging, snippets highlighted via `|highlight`. Shorter input
  shows the plain list. The old `LIKE` filter is gone.
- `since` / `until` (optional, unix seconds) — override the range tab; the Measured
  tiles link here with them.

### Per-leg cursor pagination

Each leg has its own opaque cursor (`dict_cursor`, `jobs_cursor`) so a page
that's heavy on dictations doesn't starve file transcriptions off the next page (and
vice versa). Cursors are `(created_at, row_id)` tuples base64-urlsafe-encoded
in the URL; malformed cursors decode to None and silently restart pagination.
The "Load more" HTMX button posts back both cursors and the server appends
the next 50 rows. Each leg fetches `limit + 1` rows and the two are merged in
order; a leg's next cursor is the last row of that leg actually shown (its
incoming cursor if it showed none), so a row the page limit cut appears on the
next page. Both cursors are empty once nothing is left. Page size lives in `settings.history_page_size`
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

Both mutating routes (`DELETE`, `POST /restore`) take the
`auth.require_csrf` dependency: the `csrf_token` form field (or
`X-CSRF-Token` header) must pass `auth.verify_csrf` (the session-bound token the page render
embedded, see Auth above); a Bearer-authenticated
request needs no token. Every read AND mutation re-checks
ownership: `get_history_row(user_id, kind, row_id)` (=
`get_record_scoped(kind, row_id, viewer_id, is_admin=False)`) filters on
`user_id` in SQL, so a malicious or stale row_id from another user uniformly
returns **404** — we never leak existence to non-owners. Admins read any record only
through `/admin/records/{kind}/{id}` (`is_admin=True`).

### HTMX render contract

Three template shapes, picked at the handler based on request kind:

| Trigger | Template | Notes |
|---|---|---|
| Fresh page load | `me_history.html.j2` | Full chrome (nav, filter bar, table shell + first page). |
| HTMX nav (range/kind/search change) | `_me_history_body.html.j2` | Body fragment swapped into `#history-body`. |
| HTMX Load-more | `_me_history_page.html.j2` | Just the next batch of `<tr>` rows + the updated Load-more button (`_load_more.html.j2`: cursors, or `offset` in search mode; `hx-vals` via `|tojson`). |
| Record link (no `HX-Request`) | `record_detail.html.j2` | Full page for one record; the target of AI source links. |
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

## In-app updater

A lightweight in-app update checker runs alongside the still-disabled Sparkle
wrapper. It lives at `client/WisprAlt/Update/UpdateChecker.swift` and is
**deliberately not Sparkle**: it only notices a release; installing is
`wispralt update --client`.

- **Polling.** The checker fires 60 s after launch and then every 6 h,
  debounced via `Settings.shared.lastUpdateCheck` so a wake-from-sleep doesn't
  trigger an immediate re-poll. The request hits
  `https://api.github.com/repos/<owner>/<repo>/releases/latest` and compares the
  `tag_name` (minus the leading `v`) against the bundled
  `CFBundleShortVersionString` via a SemVer-aware numeric compare.
  `<owner>/<repo>` is the bundle's `WisprAltReleaseRepo` key, which
  `scripts/build-client-local.sh` stamps from the clone's GitHub `origin`
  (`WISPRALT_RELEASE_REPO` overrides it). Without the key the check is skipped.
- **Badge.** When a newer release is found, the checker sets
  `Settings.shared.updateAvailable` (UserDefaults-backed) and calls
  `MenuBarController.shared.setUpdateBadge(visible: true)`, which composites a
  subtle orange dot onto the menubar mic icon.
- **Update.** Settings → Advanced → Updates shows the new version and a
  **Copy update command** button that puts `wispralt update --client` on the
  clipboard, to paste into Terminal (the app quits while it is reinstalled).

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
  `GET /me/dictations/last` (the server route was removed in Part 4).
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
Host — launchd gui/<UID>
  │
  ├── co.wispralt.host  (~/Library/LaunchAgents/co.wispralt.host.plist; written by `wispralt setup`)
  │     RunAtLoad: true
  │     KeepAlive: {SuccessfulExit: false}
  │     ThrottleInterval: 30
  │     → uvicorn on 127.0.0.1:<port> (default 8000), cwd ~/.config/wispralt
  │
  └── co.wispralt.cloudflared  (optional; ~/Library/LaunchAgents/co.wispralt.cloudflared.plist)
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

Both host LaunchAgents run at user level (`gui/$UID`) — not system level — so they have access to the user's Keychain, home directory, and environment without requiring sudo. `EnvironmentVariables/PATH` is set explicitly in each plist because launchd provides a minimal PATH that may not include Homebrew.

The SMAppService entry on the client is registered via `SMAppService.mainApp.register()` and requires an Apple-issued code-signing identity (Apple Development or Developer ID). Ad-hoc signed builds cannot use SMAppService.
