---
title: Architecture
---

# WisprAlt Architecture

## System Overview

WisprAlt is a two-component system: a native macOS menubar client running on your MacBook and a FastAPI server running on your always-on Mac mini, connected via a Cloudflare Tunnel.

```
┌─────────────────────────  Client (MacBook Air M4)  ──────────────────────────┐
│ FNKeyMonitor (CGEventTap, serial queue)                                       │
│   ├ hold ──▶ DictationRecorder  (AVAudioEngine, NO-OP if MeetingRecorder.on) │
│   └ 3-tap ▶ MeetingRecorder      (SCStream dual-channel + AlignedRingBuffer) │
│                ↓ 2-ch WAV                                                     │
│         ServerClient (URLSession, bearer, streaming multipart, progress)      │
│                ↓                                                              │
│         TextInjector (AX → clipboard) for dictation                           │
│         TranscriptStore (download + atomic local rewrites for renames)        │
│         SparkleController (auto-update via signed appcast)                    │
└──────────────────────────────────────┬────────────────────────────────────────┘
                                       │ HTTPS
                          ┌────────────▼────────────┐
                          │ Cloudflare Tunnel        │
                          │ transcribe.<user-domain> │ (token in cloudflared keychain only)
                          └────────────┬────────────┘
                                       │
┌────────────────────  Server (Mac mini M4 16GB) ──────────────────────────────┐
│ uvicorn 1 worker → FastAPI (lifespan: load models, run staging+orphan sweep) │
│   /healthz  /readyz/dictation  /readyz/meeting  /admin/rotate-key            │
│   /me  (GET, PATCH)   ──▶ JSON self-service identity (label, display_name)   │
│   /v1/audio/transcriptions ──▶ OpenAI-compat shim → ParakeetService          │
│       (sync, dictate-only, 25 MB cap, never invokes Mercury)                  │
│   /transcribe/dictate ──▶ ParakeetService (warm, single-thread executor)     │
│       └─ optional X-Smart-Format: true & raw_words ≥ 100 ──▶ Mercury 2       │
│             (OpenRouter, 1500ms hard timeout, fail-soft → raw on timeout,    │
│              error, or length-window safety check failure)                   │
│   /transcribe/meeting ──▶ enqueue → JobStore (SQLite, request_mode=meeting)   │
│   /transcribe/file?mode={file|meeting} ──▶ ffprobe → ffmpeg → JobStore         │
│                          ↓                                                    │
│                    asyncio.to_thread(run_pipeline)                            │
│                          ↓                                                    │
│       MeetingPipeline: mlx-whisper turbo (ANE) → Pyannote (MPS, meeting mode) │
│                          ↓                                                    │
│       output.py: JSON+SRT+VTT+TXT atomic write (tempfile in same dir)         │
│   /transcribe/meeting/{id}             ── poll (includes progress block)      │
│   /transcribe/meeting/{id}/download/{fmt}  ── stream                          │
│   /transcribe/meeting/{id}             ── DELETE (sets cancel_requested)      │
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
| **FNKeyMonitor** | `client/WisprAlt/Hotkeys/FNKeyMonitor.swift` | CGEventTap (`kCGSessionEventTap`, `.listenOnly`) on `flagsChanged | keyDown`. Hops to serial private queue immediately. Tracks FN-DOWN time; detects 300ms hold for dictation; detects triple-tap within 400ms window for meeting toggle. Clears `tapTimes` when hold is confirmed to prevent stale taps poisoning future triple-taps. |
| **DictationRecorder** | `client/WisprAlt/Capture/DictationRecorder.swift` | AVAudioEngine input tap that writes a native-format **Float32 PCM** WAV via `AVAudioFile.write(from:)`. Sample rate, channel count, and PCM format all match the tap's buffer exactly so AVAudioFile performs zero conversion — it streams the float bytes byte-for-byte to disk. Server (`audio.py`) resamples to 16 kHz and downmixes via `np.mean`. AVAudioConverter is intentionally avoided (default channel-mix sums channels rather than averaging, producing peak floats > 3.0). Int16 PCM output is also avoided (AVAudioFile's internal Float→Int16 converter applies a ~140x normalization that rail-clips the signal and destroys ASR accuracy). Tap callback dispatches each write onto a serial `ioQueue` to keep the realtime render thread free. Defensive in-tap clamp to ±0.95 catches any future regression of out-of-range floats. No-op if `MeetingRecorder.isActive == true`. |
| **MeetingRecorder** | `client/WisprAlt/Capture/MeetingRecorder.swift` | SCStream dual-channel capture: `captureMicrophone=true` (ch1) + `capturesAudio=true, excludesCurrentProcessAudio=true` (ch2). Both downsampled to 16kHz Float32 via stateful AVAudioConverter. `startPTS` locked with `os_unfair_lock`; `CMTimeSubtract` used (not float subtraction) to avoid float drift over multi-hour meetings. Feeds `AlignedRingBuffer`. The sink is **AVAssetWriter writing AAC m4a** (16 kHz stereo, 96 kbps; chosen to match AlignedRingBuffer's source rate — server downsamples to 16 kHz anyway, and ~10× smaller than the legacy WAV path). `appendSample` spin-waits on `AVAssetWriterInput.isReadyForMoreMediaData` per Apple's back-pressure contract (1 ms tick, 100 ms warn, 2 s ceiling) — calling `append` while not-ready silently drops samples and was the root cause of the meeting-audio "cutting out" symptom. Instantiates `AudioDeviceListener` before `stream.startCapture()`; posts `.meetingConfigChanged` on input-device change. On abort: tears down the listener, deletes the partial m4a, resets state to idle. |
| **AudioDeviceListener** | `client/WisprAlt/Capture/AudioDeviceListener.swift` | CoreAudio HAL listener for default-input-device changes (`kAudioHardwarePropertyDefaultInputDevice`). Uses a file-scope C function pointer (required by `AudioObjectAddPropertyListener` — cannot be stored on a Swift class instance). Context is heap-allocated via `Unmanaged.passRetained`; `deinit` calls `release()` exactly once and removes the listener. Posts `.meetingConfigChanged` via `DispatchQueue.main.async` so callers always receive the notification on the main thread. |
| **AlignedRingBuffer** | `client/WisprAlt/Capture/AlignedRingBuffer.swift` | Sample-position-keyed buffer (dictionary keyed by start-sample integer). `flushAligned()` returns aligned 2-channel chunks when both channels have data; pads lagging channel with silence when gap exceeds `GAP_TOLERANCE_MS` (200ms default). `padMissing(toEnd:)` force-flushes at stop time. |
| **TextInjector** | `client/WisprAlt/Inject/TextInjector.swift` | `@MainActor` strategy combinator. Captures `(FocusContext, AXUIElement?)` once via `captureFocus()` to close the TOCTOU window between security check and AX write. Refuses injection outright (no AX, no clipboard) when `WisprAltCore.shouldRefuseInjection(for:)` returns true (focused element subrole == `AXSecureTextField`); fires a 60-second-debounced local notification including the bundleID. Otherwise calls `AccessibilityInjector.tryInsertWith(element:text:)` and falls through to `ClipboardInjector` if AX cannot prove a value change (iMessages, Pane, every Electron app). |
| **AccessibilityInjector** | `client/WisprAlt/Inject/AccessibilityInjector.swift` | `tryInsertWith(element:text:)` accepts a pre-captured focused element from `TextInjector` (no internal focus walk → no TOCTOU). Sets a 250 ms `AXUIElementSetMessagingTimeout` to bound stalls on hung target apps, then `AXUIElementSetAttributeValue(kAXSelectedTextAttribute)` and reads `kAXValueAttribute` before/after. Returns true ONLY when the read-back proves a change — delegates the decision to the pure `WisprAltCore.didInjectionLand(...)` predicate (regression-pinned by 11 XCTest cases). The "empty before + write succeeded → assume success" heuristic is intentionally absent; that combination is the silent-no-op signature in Electron / custom NSTextView / iMessages compose. |
| **WisprAltCore** | `client/WisprAltCore/{InjectionPredicate,SecureFieldGate,FocusContext}.swift` | Pure-Swift library: `didInjectionLand(setSucceeded:beforeValue:afterValue:)`, `shouldRefuseInjection(for:)`, and the `Sendable, Equatable` `FocusContext` data type (`bundleID`, `pid`, `role`, `subrole`; `isSecureField` is derived from `subrole == "AXSecureTextField"` so callers can't construct an inconsistent context). No AppKit / ApplicationServices / Sparkle / resource dependencies — `Tests/WisprAltCoreTests` runs via `swift test` without invoking actool or pulling Sparkle into the test runner. |
| **ClipboardInjector** | `client/WisprAlt/Inject/ClipboardInjector.swift` | Saves all NSPasteboardItem types (skipping `dyn.*`), writes text, synthesizes Cmd+V (virtualKey 0x09, `.maskCommand` on both keyDown and keyUp), then restores clipboard if `changeCount == saved+1`. |
| **TranscriptStore** | `client/WisprAlt/Storage/TranscriptStore.swift` | File index of `~/Documents/WisprAlt/Meetings/`. Atomic local rewrite using `.{uuid}.tmp` + `replaceItemAt`. Creates `.transcriptWriteInProgress` sentinel before first replace, deletes after last; orphan sentinels on app launch trigger a partial-write revert. |
| **MenuBarController** | `client/WisprAlt/App/MenuBarController.swift` | State machine: idle → dictating → meeting-recording → uploading → processing → done. Enforces mic mutual exclusion. Renders the meeting-recording state via a custom NSImage composite (NSBezierPath red dot + bold "REC" attributed string) drawn into a single bitmap — a previous image+attributedTitle pair was character-wrapping in cramped menubars (R/E/C stacked vertically). Generates human-readable meeting filenames `EEE MMM d h.mma-h.mma.wav` (POSIX locale, periods not colons, no seconds — collision-suffix `(2)/(3)` if needed). |
| **MicEnumerator** | `client/WisprAlt/Audio/MicEnumerator.swift` | Stateless static helper bridging AVFoundation device discovery to CoreAudio HAL property reads. `availableInputs()` uses `[.external, .microphone]` (macOS 15+ — `.builtInMicrophone` is deprecated, subsumed by `.microphone`); dedup by uniqueID. `audioDeviceID(forUID:)` translates an AV `uniqueID` to an `AudioDeviceID` via `kAudioHardwarePropertyTranslateUIDToDevice` (using `withUnsafePointer(to: &cfUID)` and `qualifierSize = MemoryLayout<CFString?>.size`). `systemDefaultInputName()` exposes the current default's localized name for the picker label. Powers the SwiftUI Input Mic picker in `SettingsView`. |
| **AppDelegate** | `client/WisprAlt/App/AppDelegate.swift` | App lifecycle. Exposes `static weak var shared: AppDelegate?` for cross-controller access. On launch sets `shared`, runs a defensive cleanup of any `pendingMeetingDefaultInputUID` UserDefaults key left over from an earlier build (a previous iteration overrode the macOS system default input for meetings; we backed that out so meetings honor the system default). |
| **PermissionGate** | `client/WisprAlt/App/PermissionGate.swift` | Sequential 4-permission wizard: Accessibility → Input Monitoring → Microphone → Screen Recording. On macOS 14.4+, posts a blocking "Quit and Reopen Required" sheet after Input Monitoring is granted (`CGRequestListenEventAccess` returns `true` but real grant requires process restart). |
| **SparkleController** | `client/WisprAlt/Update/SparkleController.swift` | Sparkle 2 wrapper. Defers update sheet if `MeetingRecorder.isActive == true`. Auto-updates intentionally **disabled** in be720a1 — `startingUpdater: false` + `SUEnableAutomaticChecks=false` in Info.plist. Tier 1.5 distribution rides on `install.sh` (curl one-liner; see docs/INSTALL.md) for fresh installs and `/wispralt-update` (Claude Code) for in-place updates. |

### Server

| Component | File | Responsibility |
|---|---|---|
| **FastAPI app** | `server/src/wispralt_server/main.py` | Single uvicorn worker. Lifespan: validates `.env` permissions (warns if not 0600), loads Parakeet, runs `JobStore.recover_orphans()`, sweeps staging, calls `install_compat_shims()` (idempotent torch.load + HF kwarg patches), then `await meeting_runner.reenqueue_pending()` directly. **Meeting models are NOT loaded here** — `bootstrap_models` was removed; mlx-whisper + Pyannote load lazily on the first meeting job via `pipeline._ensure_models_loaded()`. Registers SIGTERM handler, mounts routers. |
| **ParakeetService** | `server/src/wispralt_server/dictate/parakeet.py` | Warm-resident `mlx-community/parakeet-tdt-0.6b-v2` (float32 — bfloat16 caused a `[matmul] (128,257) vs (514,51)` shape error on first inference). Single `ThreadPoolExecutor(max_workers=1)` serializes all inference (MLX is not thread-safe per model instance). Warmup JIT pass at startup. Defensive return-type handling: checks `hasattr(result, 'text')` vs list of `AlignedToken`. Tracks p50/p95 latency percentiles over last 100 calls. **Audio decode boundary**: `_sync_transcribe` wraps `soundfile.read` in `try/except (LibsndfileError, RuntimeError) → CorruptAudioError` so malformed uploads convert to HTTP 422 (via the route handler) instead of leaking as 500. |
| **MeetingRunner** | `server/src/wispralt_server/jobs/runner.py` | `asyncio.Semaphore(1)` enforces one meeting at a time. Dedicated `ThreadPoolExecutor(max_workers=1, thread_name_prefix="wispralt-meeting")` isolates meeting CPU work from the default asyncio thread pool. OOM guard: rejects if `psutil.virtual_memory().available < 2 GiB`. Staging WAV always cleaned up in `finally` block. `reenqueue_pending()` called at startup for jobs whose WAVs survived a restart. |
| **JobStore** | `server/src/wispralt_server/jobs/store.py` | SQLite WAL mode (`PRAGMA journal_mode=WAL; synchronous=NORMAL`). Thread-safe via `threading.Lock`. Job lifecycle: `pending → running → done | failed`. `recover_orphans()`: marks `running` jobs as `failed`; marks `pending` jobs with missing WAV as `failed`; leaves `pending` jobs with existing WAV for `reenqueue_pending()`. |
| **MeetingPipeline** | `server/src/wispralt_server/meeting/pipeline.py` | Orchestrates: `_ensure_models_loaded()` (lazy first-call load) → load channels → in-person detection (`silence.py`) → denoise no-op (`deepfilter.py` is a stub, see note below) → **mlx-whisper turbo** (`mlx_whisper_loader.py`; `word_timestamps=True` for meeting mode, `False` for file mode) → Pyannote MPS diarization (`diarize.py`, meeting mode only) → merge/label via `merge.assign_speakers_segments` (word-aware splits when word timestamps are present; largest-overlap fallback when not) → atomic output write (`output.py`). Emits `phase_start`/`phase_done` structured logs at every seam; threads `progress_cb`/`cancel_cb` from the runner into the loaders. Returns locked v3 transcript dict.

**Lazy model load (`_ensure_models_loaded`).** First meeting after server start pays a 5–30s one-time cost while mlx-whisper + Pyannote are loaded into RAM; subsequent meetings are zero-cost (warm). Single-flight is enforced by a module-level `threading.RLock` (`_load_lock`) with double-checked-locking around `_meeting_models_ready`; an `RLock` (not plain `Lock`) prevents deadlock if any future telemetry decorator wraps `load()` and re-enters from the same thread. The lazy loader also re-invokes `install_compat_shims()` (idempotent) to close the long-window race where any module imported between startup and first meeting could bind un-shimmed `torch.load` references. On partial failure (e.g. `_diarize_mod.load()` raises after `_mlx_mod.load()` succeeded) the except block calls `reset()` on each loader to null its module-level singletons, then `gc.collect()` as a hint, before re-raising — this prevents the next retry from double-allocating ~2.5–3 GB of mlx-whisper while the previous instance is still referenced. **Best-effort caveat:** dropping the Python reference does NOT guarantee immediate RAM reclaim — PyTorch holds C-level handles, MLX unified memory caches Metal backing pages, traceback frames retain locals until they unwind, and the OS allocator can keep slabs cached. `pipeline.evict_if_idle` calls `mx.metal.clear_cache()` + `mx.metal.reset_peak_memory()` to force MLX to release cached pages back to the unified pool (post-Phase 10; see `docs/CHANGELOG-2026-05-10.md`). The reset prevents reference accumulation across retries; OS-level reclaim is now bounded by the clear_cache call rather than indefinite. Observability snapshot is exposed via `pipeline.state() -> (warm, loading)` for `/readyz/meeting` and `/admin/metrics`. |
| **meeting/__init__** | `server/src/wispralt_server/meeting/__init__.py` | Package init that runs **two import-time compat shims** before any submodule loads checkpoints: (1) monkeypatches `torch.load` / `torch.serialization.load` to force `weights_only=False` (required for trusted HF checkpoints since PyTorch 2.6 default flipped to `weights_only=True`, blocking `omegaconf.ListConfig` and other pickled objects in pyannote); (2) intercepts `huggingface_hub.{hf_hub_download, snapshot_download}` to translate the removed `use_auth_token=` kwarg → `token=` (pyannote.audio 3.3.2 still calls the legacy name). |
| **RateLimitMiddleware** | `server/src/wispralt_server/middleware/rate_limit.py` | In-memory per-IP rolling-window limiter. Two windows: `/transcribe/dictate` 60 req/60s; `POST /transcribe/meeting` 4 req/3600s. Returns 429 with `Retry-After` header. |
| **staging / env_writer** | `server/src/wispralt_server/ops/` | `staging.py` manages the staging directory; startup sweep removes orphaned WAVs older than 24h. `env_writer.py` atomically rewrites `.env` key-value pairs via tempfile-in-same-dir + `os.replace`, preserving mode 0600. |

---

## Processing Modes

A single submission can take one of three paths depending on the **endpoint** and an explicit `request_mode` discriminator. Channel count is no longer conflated with intent — the "all Speaker 1" bug on long meeting recordings was caused by deriving `force_single_channel` from channel count rather than from request intent. The fix is one line in `runner._run_source`: `force_single = (request_mode == ProcessingMode.FILE)`.

| Mode | Endpoint | Channel handling | Diarization | Model | `word_timestamps` |
|---|---|---|---|---|---|
| **Dictation** | `POST /transcribe/dictate` | mono (server downmixes) | n/a | Parakeet TDT 0.6B v2 (MLX, resident) | n/a |
| **Meeting** | `POST /transcribe/file` w/ `mode=meeting`, or legacy `POST /transcribe/meeting` | mono or stereo; ffmpeg→2-ch canonical WAV | yes (Pyannote, MPS) | mlx-whisper large-v3-turbo | **`True`** — needed for word-aware speaker-boundary splits in `merge.assign_speakers_segments` |
| **File** | `POST /transcribe/file` (default `mode=file`) | any container; ffmpeg-decoded | **no** | mlx-whisper large-v3-turbo | `False` (≈20% faster; no word-level data emitted) |

The legacy `POST /transcribe/meeting` route persists `request_mode=meeting` on the row at submit time (`routes/meeting.py`); `routes/transcribe_file.py` takes `mode: ProcessingMode = Form(ProcessingMode.FILE)` and FastAPI auto-validates the enum (invalid → 422). `PendingUploadsQueue.swift` replays queued `.m4a` uploads with `mode=meeting` explicitly so an offline-queued meeting never gets reclassified as a file.

Meeting mode on a mono source still routes through the existing in-person branch in `pipeline.py`: ffmpeg produces a 2-channel canonical WAV with silent ch2, diarization runs on ch1, and `relabel_in_person` produces the final speaker labels.

---

## MLX Whisper

WhisperX (CTranslate2 CPU int8 + wav2vec2 alignment) was replaced wholesale by [`mlx-community/whisper-large-v3-turbo`](https://huggingface.co/mlx-community/whisper-large-v3-turbo) in `server/src/wispralt_server/meeting/mlx_whisper_loader.py`. The model runs on Apple Neural Engine via MLX unified memory.

| Item | Value |
|---|---|
| Model repo | `mlx-community/whisper-large-v3-turbo` |
| Loader | `mlx_whisper_loader.load()` — one-shot warmup with 1s of silence; idempotent via module-level `_loaded` flag |
| Inference | `mlx_whisper_loader.transcribe_channel(audio_16k, *, word_timestamps, progress_cb, cancel_cb)` — wraps `mlx_whisper.transcribe` with a `tqdm.auto.tqdm.update` monkeypatch for chunk progress + a 5s wall-clock fallback if tqdm.update never fires |
| Disk footprint | ~1.6 GB at `~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo/` |
| Spike result (M4 mini, 16 GB) | 105 min audio (`audio_duration_s=6341.4`) → `transcribe_s=740.26`, **realtime ratio 8.57×**; wall clock 12.3 min including ffmpeg + pyannote |
| Eviction | `mlx_whisper_loader.reset()` is **effectively a no-op** — MLX unified memory does not return promptly even after dropping references. The real eviction targets in `pipeline.evict_if_idle` remain Pyannote (PyTorch MPS) and DeepFilterNet (currently a no-op stub anyway) |
| Cancel semantics | `cancel_cb` is honored at chunk boundaries via the tqdm monkeypatch; mlx-whisper has no in-decode interrupt, so cancel mid-transcribe is **advisory** (see Honest Limitations below) |

The wav2vec2 alignment pass that WhisperX did is **dropped**. For meeting mode, mlx-whisper's own `word_timestamps=True` provides word-level data that `merge.assign_speakers_segments` consumes for speaker-boundary splits (coalesce consecutive same-speaker words; cut on speaker change). File mode skips word timestamps entirely (≈20% perf headroom; SRT/VTT/TXT formatters never accessed `seg["words"]`).

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
[<job_id>] phase_start name=diarize_load
[<job_id>] phase_done name=diarize
[<job_id>] phase_start name=merge
[<job_id>] phase_done name=output_write duration_ms=18
```

Phase names are the canonical keys used in `runner.PHASE_BUDGETS` / `PHASE_LABELS`: `queued → starting → ffprobe → ffmpeg_decode → transcribe_load → transcribe → diarize_load → diarize → merge → output_write → done|failed`. Diarize phases are skipped in file mode.

### Job-level progress on the poll response

`GET /transcribe/meeting/{id}` returns a `progress` block (and `serverFinishing: bool` if `cancel_requested=1` AND status is still `running`):

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
  "server_finishing": false
}
```

`chunk_index`/`total_chunks` are populated for the `transcribe` phase only (driven by the tqdm monkeypatch's `progress_cb`). The client renders `phase_label` as-is and shows chunk progress only when `phase == "transcribe"`.

### Admin endpoints

- `GET /admin/active` — rich projection of the currently in-flight job: `id, status, request_mode, mode, phase, phase_label, phase_elapsed_s, chunk_index, total_chunks, started_at, wav_path, audio_duration_s, attempts, cancel_requested, current_rss_mb`. Same auth dependency as `/admin/metrics`.
- `GET /admin/server-log/{job_id}` — plain-text response: the 100 lines bracketing the job's first and last appearance in `settings.server_log_path` (resolved from settings, NOT hardcoded). Includes intervening lines (ffmpeg stderr, pyannote warnings) for full-context diagnosis. The client's "View server log" sheet polls this every 5s.

---

## Honest Limitations

The cancel + per-phase-timeout story has **two real abort surfaces and three advisory ones**. This is documented up front so future debugging doesn't chase a non-existent kill primitive.

| Surface | Behavior |
|---|---|
| **`ffprobe` / `ffmpeg_decode` timeouts** | Real abort. Wrapped in `runner._phase` via `asyncio.wait_for` around a `run_in_executor` call. On timeout the executor task is cancelled and `set_failed` runs. |
| **`transcribe` / `diarize` timeouts** | **Advisory only.** A separate `_phase_watchdog` task reads `phase_started_at` every 5s and calls `set_failed` if elapsed > 2× budget. The executor thread keeps running. The semaphore (`async with self._semaphore:` around `_run_source`) is held by the awaited executor and **is not released until that thread returns naturally.** New submissions get a real `429`. The client UI surfaces this as a "Previous transcription still finishing on server" banner instead of a mysterious error. |
| **Cancel mid-upload** | Real abort. `URLSession.invalidateAndCancel()` on the client. |
| **Cancel mid-`ffmpeg_decode`** | Real abort. `staging.transcode_to_canonical_wav` runs ffmpeg as a `subprocess.Popen` and polls `jobs.cancel_requested` every 500ms; on True it `SIGTERM`s ffmpeg, cleans the `.partial` file, and raises `staging.StagingCancelled`. `_run_source` catches it, calls `set_failed(jid, "cancelled")`, exits the `async with` → semaphore released. |
| **Cancel mid-`transcribe`/`diarize`** | **Advisory only.** `cancel_requested=1` is written to the row; the client UI hides the job and shows the "Previous job finishing" banner; the executor thread keeps running until completion. Same semaphore constraint as the advisory timeout. |

When `_run_pipeline_inner`'s executor eventually returns, `set_done` is guarded by a status check — if the watchdog or a cancel has already written `failed`, the executor's result is discarded and a warning is logged.

### Networking

The Cloudflare Tunnel maps `transcribe.<user-domain>` → `http://127.0.0.1:8000` on the Mac mini. `cloudflared` runs as a **user-level LaunchAgent** (`~/Library/LaunchAgents/co.wispralt.cloudflared.plist`), not as a system service — the `sudo cloudflared service install` path is broken on macOS 14/15+. The tunnel token is stored at `~/.config/wispralt/cloudflare-token` (mode 0600); the LaunchAgent reads it via `--token-file` on cloudflared ≥ 2025.4.0, or has it inlined in the plist (mode 0600) on older versions. The setup script (`scripts/setup-cloudflared.sh`) reads the token via `read -r -s` and unsets the shell variable immediately after writing the file.

Tunnel latency overhead: ~50–200ms same-region. The Cloudflare free tier has a community-reported body limit of approximately 100 MB; the server enforces `MAX_UPLOAD_BYTES` (default 2 GiB) at the application layer. A 90-minute 2-channel 16kHz Float32 WAV is approximately 460 MB; clients warn the user at 60 minutes.

### Cloud Fallback (dictation only)

When the Mac mini is offline, the Swift client falls back **directly** to OpenRouter's `openai/whisper-large-v3-turbo` (chat-completions audio) using an OpenRouter API key stored in the Keychain at `co.wispralt.openrouter`. No Worker, no proxy, no server-side code in the fallback path. The classifier (`ServerClient.isOfflineSignature`) trips ONLY on connect-refused errors, `URLError.networkConnectionLost`, `dnsLookupFailed`, `cannotFindHost`, or Cloudflare 502/522/523/524 with `CF-Ray` AND no `X-Request-Id`. Origin 5xx (FastAPI sets `X-Request-Id`) never trigger fallback. Meetings are queued locally instead — Whisper-only output cannot replace the mini's Pyannote diarization.

Spend protection lives in the OpenRouter dashboard's monthly cap (set per account, not per employee). If no OpenRouter key is set in the Keychain, the client surfaces the existing 503 dictation error toast — the rest of WisprAlt keeps working. Setup + threat model: [FALLBACK.md](FALLBACK.md).

### Custom Transcriptions (menubar file picker)

The menubar QuickActions popover (`UI/SettingsView.swift`) exposes a "Transcribe file…" button that lets the user pick any audio/video container and upload it for transcription. The client copies the source **as-is** (no transcoding) into a per-job folder under `~/Documents/WisprAlt/Custom Transcriptions/`, then POSTs to `/transcribe/file` with `mode=file` via `MeetingAPI.submitFile`. Output JSON/SRT/VTT/TXT lands beside the source. `CustomTranscriptionsStore.swift` manages the per-job folders; `LastTranscriptCaption.swift` powers a "Copy last custom transcription" button with a live mtime caption (folder DispatchSource watcher + 10s timer + `Notification.Name.wisprAltTranscriptWritten` observer; see `Util/TranscriptNotifications.swift`).

`MeetingAPI.swift` uses `URLSessionConfiguration` with `timeoutIntervalForRequest=300` and `request.timeoutInterval=6*60*60` so a 90-min upload over a slow uplink can complete without hitting the default 60s inactivity timeout.

### Large-file uploads (audio extraction + chunked upload)

Cloudflare's free / pro / business plans cap inbound request bodies at **100 MB**. A 1-hour stereo meeting WAV is ~460 MB, and a 3-hour AAC-encoded meeting m4a can easily clear 200 MB — both would 413 at the edge before reaching the origin. Two complementary strategies extend the supported range to 4 GB without touching Cloudflare's plan tier.

**Step 0 — Audio extraction (`client/WisprAlt/Capture/AudioExtractor.swift`).** Before deciding which upload path to take, the client passes the user-picked URL to `AudioExtractor.extractAudioIfVideo(_:)`. If the asset has BOTH a video and an audio track, it runs `AVAssetExportSession` with `AVAssetExportPresetPassthrough` and writes just the audio track to a temp `.m4a` (or `.mp4` fallback when the source codec isn't AAC-compatible). A 200 MB MP4 typically becomes a ~15 MB audio file — often dropping under the chunking threshold entirely and letting the existing single-shot `/transcribe/file` path serve the request. The extractor never throws: any probe / export failure returns the original URL so the upload still proceeds.

**Step 1 — Chunked upload (`client/WisprAlt/Server/ChunkedUploader.swift` ↔ `server/src/wispralt_server/routes/transcribe_file.py`).** If `uploadSize > 50 MiB`, the client switches from `MeetingAPI.submitFile` (multipart single-shot) to `ChunkedUploader.upload`. The three-step wire protocol is:

1. `POST /transcribe/file/chunked/init` with JSON `{mode, total_bytes, chunk_count, original_filename}` → server returns `{upload_id, chunk_size}` (22-char `secrets.token_urlsafe(16)`) and creates `staging/chunked/<upload_id>/meta.json` recording the caller's `user.id`.
2. `POST /transcribe/file/chunked/<upload_id>/<chunk_index>` with raw bytes (`application/octet-stream`, NOT multipart) for each 50 MiB chunk. The body is streamed via `request.stream()` to `chunk-NNNN.part.tmp`, then atomically renamed on success. `Content-Length` is mandatory and validated against bytes-written (corrupt chunks → 400). The path declares `{chunk_index:int}` — Starlette's `:int` converter constrains this segment to digits so `/finalize` cannot match the chunk route. Without this constraint, Starlette's registration-order matching sent every `/finalize` request into the wildcard chunk handler with `chunk_index="finalize"`; Pydantic int-coerce then produced a RequestValidationError → 422 with `{"detail":[…]}`, which the client surfaced as the misleading "appears incomplete or corrupted" banner.
3. `POST /transcribe/file/chunked/<upload_id>/finalize` with an empty body. Server verifies all chunks present + `Σ(part.size) == total_bytes`, concatenates in `run_in_executor` (deletes each `.part` as it copies to halve peak disk), then hands the assembled file to the SAME `MeetingRunner.submit_source_or_429` that the single-shot route uses. Returns `{job_id, status: "pending"}` — from here the existing poll/download/delete loop is unchanged.

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

### Meeting Upload Latency

A 1-hour dual-channel 16kHz Float32 WAV is approximately 460 MB. At 100 Mbps symmetric: ~37 seconds upload alone. `RecordingIndicatorView` shows three explicit states with progress: **Uploading (%)** via `URLSession` upload-progress callbacks, then **Processing** (server pipeline), then **Done**.

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
    └── POST /transcribe/meeting        (legacy; persists request_mode=meeting)
        POST /transcribe/file            (mode=file|meeting; ffprobe + ffmpeg in worker)
            └── MeetingRunner.submit_or_429() / submit_source_or_429()
                    ├── Check Semaphore locked → 429 immediately (non-blocking)
                    ├── Check available RAM < 2 GiB → 429 immediately
                    └── asyncio.create_task(_run())
                            └── async with asyncio.Semaphore(1)
                                    └── loop.run_in_executor(
                                            MeetingRunner._executor,  ← dedicated 1-worker pool
                                            meeting_pipeline.transcribe_meeting
                                        )
```

The FastAPI event loop is never blocked by inference work. When a meeting is active, `app.state.meeting_active_flag` is `True`. `GET /readyz/dictation` adds `X-Dictation-Degraded: true` to signal that dictation inference may be slower due to unified memory pressure.

---

## Memory and Resource Model

| Component | Resident Memory |
|---|---|
| Parakeet TDT 0.6B v2 (MLX float32) | ~3.5–4.0 GB unified (≈2× bf16) |
| mlx-whisper large-v3-turbo (MLX, ANE/unified memory) | ~2.5–3.0 GB |
| Pyannote 3.1 (PyTorch, MPS) | ~1.0 GB |
| Python / FastAPI process | ~0.5 GB |
| **Total resident (warm)** | **~7.5–8.5 GB** |
| **Peak observed across the 12-run matrix** | **< 14 GB** (gate criterion for Phase 10) |

The Mac mini M4 16 GB configuration leaves ~8.7 GB for the OS and other processes. `MeetingRunner.submit_or_429()` rejects a new meeting job if `psutil.virtual_memory().available < 2 GiB`.

**Cold-start footprint:** before any meeting/file job has run, only Parakeet + Python/FastAPI are resident (~4.0–4.5 GB). mlx-whisper + Pyannote are loaded lazily on the first meeting/file job (see `MeetingPipeline._ensure_models_loaded`), reclaiming ~3.5 GB until that point. Once warm, models stay resident for the process lifetime; `pipeline.evict_if_idle` targets Pyannote and DeepFilterNet — `mlx_whisper_loader.reset()` is effectively a no-op because MLX unified memory does not return promptly after dropping references.

**Disk guard:** `staging.stream_to_staging()` checks that free disk is at least 1.5x the upload size before accepting a file. `GET /metrics` reports `disk.free_gb` and `disk.staging_count` for monitoring.

**Device matrix:** MLX for Parakeet **and mlx-whisper** (Apple Neural Engine / GPU unified memory). Pyannote supports MPS and runs on `torch.device("mps")` for diarization. The CTranslate2 CPU-int8 path that powered WhisperX has been retired.

**Denoise note:** `meeting/deepfilter.py` is currently a **no-op stub** — `get_df()` returns `None` and `deepfilter()` returns the audio unchanged. DeepFilterNet 3 was removed because it pins `numpy<2.0` while `parakeet-mlx` requires `numpy>=2.2.5`. The stub keeps function signatures stable so callers don't need conditional branches; re-introducing a numpy-2-compatible denoiser is tracked as future work.

---

## Failure Handling

### SIGTERM Handler

Registered in `main.py` lifespan. On SIGTERM:

1. Sets `app.state.shutting_down = True`.
2. Calls `job_store.fail_all_running("server shutdown")` to mark in-flight jobs as failed.
3. Calls `sys.exit(0)` so launchd's `ExitTimeout=15` applies cleanly.

The client's `GET /transcribe/meeting/{job_id}` poll sees `status: "failed"` and surfaces the error to the user.

### Orphan Recovery (`recover_orphans`)

Called once at startup (before the event loop accepts requests). Policy from `jobs/store.py:157`:

- `running` → `failed` (server restarted mid-job; job is dead)
- `pending` + WAV file exists → left as `pending`; `MeetingRunner.reenqueue_pending()` re-submits it
- `pending` + WAV file missing → `failed` (staging file disappeared between crashes)

### Staging Sweep

`ops/staging.sweep_old()` is called at startup and removes staging WAV files older than 24 hours that are no longer referenced by a pending or running job.

---

## Authentication

`/healthz`, `/readyz/*`, and `/admin/login` are intentionally **unauthenticated** so Kubernetes-style probes, Cloudflare health checks, external monitoring, and the admin login form can be reached without API credentials. The probes expose only a ready-flag boolean, free RAM in MB, and a `meeting_active` indicator — no user data, no audio, no model output.

Every other route requires `Authorization: Bearer <token>`. The legacy single-key compare path was replaced in `2026-04-27-team-distribution` by the multi-token flow described next; `secrets.compare_digest` no longer appears on the request hot path.

### Auth (multi-token)

```
Bearer <plaintext>
   │
   ▼
sha256(plaintext) ─────────────► token_hash (hex)
   │
   ▼
TokenCache.get(token_hash)        ← LRU 256 entries × 60s TTL, in-process
   │ hit
   ├──► User(id, label, role) ──► request.state.user
   │
   │ miss
   ▼
asyncpg pool ─► SELECT id, label, role
                FROM wispralt.users
               WHERE token_hash = $1
                 AND revoked_at IS NULL
   │
   │ row found
   ├──► TokenCache.put → User → request.state.user
   │
   │ row missing OR pool=None OR PostgresError
   ▼
break-glass branch
   IF token_hash == app.state.break_glass_token_hash:
       User(id=-1, label="break-glass-admin", role="admin")
   ELSE:
       401 Invalid bearer / 503 Auth temporarily unavailable
```

Source: `server/src/wispralt_server/auth.py` +
`server/src/wispralt_server/users/{cache.py,store.py}`.

Key invariants:

- **`request.state.user` is populated for every authenticated request.** The observability middleware reads it to attribute usage events; `routes/admin_ui.py:require_admin` reads it to gate role.
- **The break-glass branch only fires when Postgres is unreachable** OR when the row genuinely is missing. On first boot the lifespan seeds a real `wispralt.users` row whose `token_hash` matches the env-var hash, so 99% of break-glass calls take the normal Postgres path and produce attributable usage events. The `id=-1` sentinel is reserved for the case where the pool is `None` (Postgres degraded).
- **Pool watcher (added 2026-05-02).** The lifespan starts a background task that probes `app.state.db_pool` with `SELECT 1` (2s timeout) every 10 seconds. On failure it calls `db.recreate_pool()`, swaps `app.state.db_pool`, and restarts the usage drainer against the fresh pool. Without this, a transient Supabase blip leaves `db_pool=None` forever and every authenticated request returns 503 until a manual `launchctl kickstart` — the failure mode that bit production on 2026-05-02. Recovery latency is bounded by the 10s probe interval plus the asyncpg create_pool round-trip.
- **Cookie fallback:** `_extract_bearer` falls back to the `wispralt_admin_token` cookie when no `Authorization` header is present. Set by `POST /admin/login` for browser navigation; `HttpOnly`, `Secure`, `SameSite=Strict`, `max_age=8h`. CSRF is mitigated by `SameSite=Strict`.

#### `wispralt.users` columns

| Column         | Type        | Notes                                                                                |
|----------------|-------------|--------------------------------------------------------------------------------------|
| `id`           | bigserial   | Primary key                                                                          |
| `label`        | text        | Operator-visible identifier (typically the employee email or canonical handle)        |
| `display_name` | text NULL   | Self-managed friendly name, edited by the user via `PATCH /me`. 1–40 chars, no control chars (CHECK constraint mirrors `MAX_DISPLAY_NAME_LEN` in `constants.py`). NULL until the user fills in the first-launch sheet. Added by migration `2026-04-27-v2-display-name.sql`. |
| `role`         | text        | `'admin'` or `'employee'` (CHECK)                                                    |
| `token_hash`   | text        | sha256(plaintext token), partial-indexed `WHERE revoked_at IS NULL`                   |
| `created_at`   | timestamptz | Set at mint time                                                                     |
| `revoked_at`   | timestamptz | NULL while active; set on revoke                                                     |

The admin UI's user list renders `display_name (label)` when both are populated, falling back to `label` alone when `display_name IS NULL`. See [ADMIN.md](ADMIN.md).

### Usage event tracking

```
                    request hot path                         background
                                                             drainer
┌─ middleware/observability.dispatch ─────────┐    ┌────────────────────────┐
│ after call_next:                              │    │ usage.writer.drain_loop│
│   if user.id >= 0 and route in TRACKED_ROUTES │    │  (asyncio task)        │
│      and method == POST:                      │    │                        │
│        observability.usage_queue.offer(       │    │  while True:           │
│          UsageEvent(user_id, ts, kind,        │    │    e = await q.get()   │
│            status, duration_ms, bytes_in,     │    │    batch.append(e)     │
│            request_id))                       │    │    if len >= 50 OR     │
└──────────────────┬───────────────────────────┘    │       1s elapsed:      │
                   │ asyncio.Queue (maxsize=1000)    │      _flush(pool, batch)
                   ▼                                  │      batch = []        │
       UsageEventQueue (bounded)                      └──────────┬─────────────┘
       full → drop oldest, log WARNING                           │
                                                                  ▼
                                                  asyncpg ── INSERT INTO
                                                            wispralt.usage_events
                                                            (executemany, in
                                                             explicit transaction)

                                                  on FK violation:
                                                  filter survivors (user_id>=0)
                                                  retry in fresh transaction
```

Source:
`server/src/wispralt_server/usage/{events.py,queue.py,writer.py}` +
`observability.py` (singleton) + `main.py` (drainer task lifecycle).

Why fire-and-forget:

- The dictation hot path is sub-200ms; a Postgres write per request would
  add ~10–40ms of unified-memory contention.
- Bounded queue caps unbounded memory growth if Postgres falls behind:
  on overflow the oldest event is dropped and a WARNING is logged.
- FK-violation retry is critical: a `ForeignKeyViolationError` aborts the
  entire transaction, so we explicitly wrap in `async with conn.transaction():`,
  filter `user_id >= 0` survivors on retry, and re-execute. Without the
  explicit transaction the post-error connection would be in an aborted
  state and the retry would hit `InFailedSQLTransactionError`.
- Cancelled-on-shutdown: lifespan cancels the drainer task and awaits it;
  `CancelledError` triggers a final batch flush before the task exits.

Tracked routes: `transcribe/dictate`, `transcribe/meeting`, and
`v1/audio/transcriptions`, **POST only** — status-poll GETs are
excluded so a client polling every 5s doesn't multiply the event volume
by 60. The OpenAI-compat shim records its events with
`kind = "v1_dictate"` so admins can split native-client traffic from
third-party API traffic without parsing the `route` column. The current
admin overview tiles (Dictations 24h/7d/30d) sum across all `kind`
values; query `usage_events` directly with `WHERE kind = 'v1_dictate'`
to isolate API traffic.

### Admin UI

```
┌────── /admin (FastAPI APIRouter) ─────────────────┐
│                                                     │
│  public_router  ─► /admin/login (GET, POST)        │  no auth
│                                                     │
│  me_router      ─► /admin/me                       │  Depends=[require_api_key,
│                                                     │           _require_db_pool]
│                                                     │  any role — admin or employee
│                                                     │
│  authed_router  ─► everything else                  │  Depends=[require_admin,
│      /admin/                                        │           _require_db_pool]
│      /admin/users  /admin/users/{id}               │
│      /admin/users/{id}/mint  …/revoke              │
│      /admin/usage  /admin/usage.csv                 │
└─────────────────────────────────────────────────────┘
```

Source: `server/src/wispralt_server/routes/admin_ui.py` +
`server/src/wispralt_server/admin/templates/*.html.j2`.

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
- **`authed_router`** has `dependencies=[Depends(require_admin),
  Depends(_require_db_pool)]`, so a browser hitting `/admin/` without a
  valid admin token gets 401/403, and a request hitting it while
  Postgres is degraded gets 503 — never an `AttributeError` on
  `app.state.db_pool`.

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

### Legacy single-key shim

`auth.current_key()` and `auth.set_current_key()` are retained as thin
shims for `routes/admin.py:rotate_key`, the last-resort tool for
rotating the env-var token while Postgres is unreachable. Once all
rotation moves through the admin UI's mint flow, this endpoint can be
removed.

### Memory budget impact

The asyncpg pool adds ~1 MB per resident connection. Pool sized
`min_size=1, max_size=10`, so the steady-state footprint is ~1–10 MB
on top of the existing ~6.5–7.0 GB resident model memory. The drainer
holds at most one connection during the `executemany` window; auth
lookups and admin UI requests compete for the other nine. Comfortable
headroom for ≤10 employees.

---

## Speaker Rename

Speaker rename is **client-side only**. There is no server `PATCH /speakers` endpoint. The client (`TranscriptDocument.swift`):

1. Loads the local `.json` file.
2. Calls `renameSpeaker(raw:to:)`: updates `display_name` in the `speakers` table and rewrites the `speaker` field in every matching segment. Throws `.speakerNameConflict` if the new name collides with an existing speaker's `display_name`.
3. Writes all four formats atomically: each file is written to a `.{uuid}.tmp` file in the same directory, then `replaceItemAt` replaces the original.
4. A `.transcriptWriteInProgress` sentinel file is created before the first replace and deleted after the last. On app launch, orphan sentinels trigger a revert (delete partial outputs, keep originals).

This is fully offline-capable and requires no network connectivity.

---

## Transcript persistence

Phase 1 of the portal "weekly insights" effort: every transcribed output
(dictation + meeting + file) is persisted to the mini's SQLite so the future
weekly-insights cron has data to summarize. Capture is **unconditional and
transparent** — there is no client-visible toggle and no admin UI redaction.
Zero behavior change for end users.

### Where the data lives

- **Meeting + file transcripts** are stored on the existing `jobs` row.
  Four new columns added by an idempotent `ALTER TABLE` in `JobStore.__init__`:
  `transcript_text`, `word_count`, `client_app_version`, `api_key_id`. The
  derivation happens inside `MeetingRunner._run_pipeline_inner` right before
  `set_done`: joins `transcript["segments"][*].text`; falls back to reading
  `<output_dir>/<job_id>.txt` if the joined string is empty.
- **Dictation transcripts** live in a separate `dictations` table created in
  the same `__init__`. Dictation is request-response (no job row), so the
  route's normal success path fires-and-forgets an `asyncio.create_task` that
  calls `JobStore.insert_dictation` via `asyncio.to_thread` (the `_exec`
  lock is sync, so we must hop off the event loop). Tasks are held in
  `app.state.pending_persists` to defeat the documented `asyncio.create_task`
  GC race, and drained with a 2-second budget at shutdown.

Both tables carry `api_key_id`, the existing per-user FK to `wispralt.users`.
This makes the Phase 2 per-employee filter trivial: `/me/insights` will filter
to one `api_key_id`; `/admin/data` will union all of them.

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
- **File permissions.** `jobs.db`, `jobs.db-wal`, and `jobs.db-shm` are
  `chmod 0600` on every startup so a restored backup is auto-tightened.
  When the mode actually changes, the new mode is logged at INFO.
- **Break-glass admin (user.id < 0) is skipped everywhere.** No orphaned
  `-1` rows ever appear in either table.
- **Empty / whitespace-only dictations are skipped.** Avoids polluting
  Phase 2 word counts with silence-only audio.
- **All transcripts stay on the mini.** Nothing is sent to OpenRouter
  except via the future weekly insights cron (Phase 2).

### Known gap: cloud-fallback dictations

When the mini is offline, the Swift client transcribes via OpenRouter directly
(see "Cloud Fallback" above) and never sends those transcripts to the server.
Phase 1 does not capture them. Phase 2 may add an optional best-effort
`/telemetry/cloud-dictation` endpoint the client calls once the mini reappears,
but that's deferred.

### Client app version (`X-WisprAlt-Client-Version`)

The Swift client sets this header on every server-bound request
(`MeetingAPI.submit`, `MeetingAPI.submitFile`, `ChunkedUploader.postJSON` +
`uploadOneChunk`, and `ServerClient.buildRequest` for `DictationAPI`).
Format: `"<CFBundleShortVersionString>+<CFBundleVersion>"`. The server reads
it at submit time and persists it on the row via `JobStore.create` (single-shot
file + legacy meeting) or on `meta.json` (chunked init) so it survives both
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

Source: `server/src/wispralt_server/insights/{__init__.py, client.py, prompts.py, cron.py}`
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
CREATE TABLE weekly_insights (
    iso_year     INTEGER NOT NULL,
    iso_week     INTEGER NOT NULL,
    scope        TEXT NOT NULL,         -- 'person' or 'team'
    api_key_id   INTEGER,                -- NULL for team scope
    insight_json TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd     REAL NOT NULL,
    model        TEXT NOT NULL,         -- e.g. 'x-ai/grok-4.3'
    generated_at REAL NOT NULL,
    PRIMARY KEY (iso_year, iso_week, scope, api_key_id)
);
CREATE INDEX idx_weekly_insights_generated ON weekly_insights(generated_at);
```

`upsert_weekly_insight` uses `ON CONFLICT (iso_year, iso_week, scope, api_key_id)
DO UPDATE` so a re-run for the same week overwrites the previous row
idempotently — useful for catchup, manual re-runs via the
`python -m wispralt_server.insights.cron --manual` entrypoint, and prompt
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
  data-floor gate: at least **5 dictations OR at least 1 meeting/file** in
  the week. Users below the floor are skipped silently (no row written) to
  avoid charging for thin-data summaries.
- **`scope = 'team'`** is one row per week with `api_key_id = NULL`. It's
  written **only when at least 2 employees had successful person-scope
  insights** — a one-person team aggregate would just be a re-rendering of
  that person's row.

---

## Data portal

The render loop. All three surfaces share the same base template + vendored
JS so a single design language covers admin + employee views.

Source: `server/src/wispralt_server/routes/{me.py, admin_data.py}` +
`server/src/wispralt_server/admin/templates/{me_login, me_insights, data,
_insight_cards, _stats_grid_partial}.html.j2`.

### Surfaces

- **Admin Data tab — `GET /admin/data`.** Admin-only (router-level
  `require_admin` + `_require_db_pool` deps). Renders the team `weekly_insights`
  card stack on top + a per-user leaderboard underneath. The optional
  `?user_id=N` query param drills into one employee using the same
  `_insight_cards.html.j2` macro layout the employee self-view renders, so
  the admin sees exactly what the employee sees.
- **Employee self-view — `GET /me/insights`.** Authenticated user only.
  Shows that user's `'person'`-scope insight (most recent week) plus a
  time-range stats grid (Today / 7d / 30d / 90d / 1y / All). Break-glass
  admin (`user.id < 0`) is 303'd to `/admin/data` because break-glass
  identities have no per-user transcript history to summarize.
- **Employee login — `GET /me/login` / `POST /me/login`.** Token-paste form
  mirroring the admin login flow. On success it sets the **same**
  `wispralt_admin_token` cookie that admin login sets, but with `path="/"`
  so a single cookie scopes across both `/admin/*` and `/me/*` surfaces
  (the admin login handler was simultaneously broadened to `path="/"` so
  the cookie is interchangeable in both directions). Break-glass tokens are
  rejected at this surface with an explanatory error — the break-glass
  identity is intended for emergency admin access, not employee self-view.

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
