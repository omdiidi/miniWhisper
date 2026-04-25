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
│   /transcribe/dictate ──▶ ParakeetService (warm, single-thread executor)     │
│   /transcribe/meeting ──▶ enqueue → JobStore (SQLite)                        │
│                          ↓                                                    │
│                    asyncio.to_thread(run_pipeline)                            │
│                          ↓                                                    │
│       MeetingPipeline: DeepFilterNet → WhisperX (CPU int8) → Pyannote (MPS)  │
│                          ↓                                                    │
│       output.py: JSON+SRT+VTT+TXT atomic write (tempfile in same dir)         │
│   /transcribe/meeting/{id}             ── poll                                │
│   /transcribe/meeting/{id}/download/{fmt}  ── stream                          │
│   /transcribe/meeting/{id}             ── DELETE (cleanup staging)            │
└───────────────────────────────────────────────────────────────────────────────┘
```

The Cloudflare Tunnel is **outbound-only** from the Mac mini — no inbound port-forwarding, no firewall rule changes required. The tunnel token is read from stdin during setup and stored exclusively in the macOS system keychain by `cloudflared`; it is never written to `.env` or any plist.

---

## Components

### Client

| Component | File | Responsibility |
|---|---|---|
| **FNKeyMonitor** | `client/WisprAlt/Hotkeys/FNKeyMonitor.swift` | CGEventTap (`kCGSessionEventTap`, `.listenOnly`) on `flagsChanged | keyDown`. Hops to serial private queue immediately. Tracks FN-DOWN time; detects 300ms hold for dictation; detects triple-tap within 400ms window for meeting toggle. Clears `tapTimes` when hold is confirmed to prevent stale taps poisoning future triple-taps. |
| **DictationRecorder** | `client/WisprAlt/Capture/DictationRecorder.swift` | AVAudioEngine ring buffer, 16kHz mono Float32. No-op if `MeetingRecorder.isActive == true`. |
| **MeetingRecorder** | `client/WisprAlt/Capture/MeetingRecorder.swift` | SCStream dual-channel capture: `captureMicrophone=true` (ch1) + `capturesAudio=true, excludesCurrentProcessAudio=true` (ch2). Both downsampled to 16kHz Float32 via stateful AVAudioConverter. `startPTS` locked with `os_unfair_lock`; `CMTimeSubtract` used (not float subtraction) to avoid float drift over multi-hour meetings. Feeds `AlignedRingBuffer`. |
| **AlignedRingBuffer** | `client/WisprAlt/Capture/AlignedRingBuffer.swift` | Sample-position-keyed buffer (dictionary keyed by start-sample integer). `flushAligned()` returns aligned 2-channel chunks when both channels have data; pads lagging channel with silence when gap exceeds `GAP_TOLERANCE_MS` (200ms default). `padMissing(toEnd:)` force-flushes at stop time. |
| **TextInjector** | `client/WisprAlt/Inject/TextInjector.swift` | Strategy wrapper: tries `AccessibilityInjector` first, falls through to `ClipboardInjector` if AX returns success but value did not change (Electron silent-fail mode). |
| **AccessibilityInjector** | `client/WisprAlt/Inject/AccessibilityInjector.swift` | `AXUIElementSetAttributeValue(kAXSelectedTextAttribute)`. Reads `kAXValueAttribute` before and after to detect silent no-ops. Returns `false` if value unchanged, triggering clipboard fallback. |
| **ClipboardInjector** | `client/WisprAlt/Inject/ClipboardInjector.swift` | Saves all NSPasteboardItem types (skipping `dyn.*`), writes text, synthesizes Cmd+V (virtualKey 0x09, `.maskCommand` on both keyDown and keyUp), then restores clipboard if `changeCount == saved+1`. |
| **TranscriptStore** | `client/WisprAlt/Storage/TranscriptStore.swift` | File index of `~/Documents/WisprAlt/Meetings/`. Atomic local rewrite using `.{uuid}.tmp` + `replaceItemAt`. Creates `.transcriptWriteInProgress` sentinel before first replace, deletes after last; orphan sentinels on app launch trigger a partial-write revert. |
| **MenuBarController** | `client/WisprAlt/App/MenuBarController.swift` | State machine: idle → dictating → meeting-recording → uploading → processing → done. Enforces mic mutual exclusion. |
| **PermissionGate** | `client/WisprAlt/App/PermissionGate.swift` | Sequential 4-permission wizard: Accessibility → Input Monitoring → Microphone → Screen Recording. On macOS 14.4+, posts a blocking "Quit and Reopen Required" sheet after Input Monitoring is granted (`CGRequestListenEventAccess` returns `true` but real grant requires process restart). |
| **SparkleController** | `client/WisprAlt/Update/SparkleController.swift` | Sparkle 2 wrapper. Defers update sheet if `MeetingRecorder.isActive == true`. `SUAutomaticallyUpdate = NO`; user must confirm. |

### Server

| Component | File | Responsibility |
|---|---|---|
| **FastAPI app** | `server/src/wispralt_server/main.py` | Single uvicorn worker. Lifespan: validates `.env` permissions (warns if not 0600), loads Parakeet, runs `JobStore.recover_orphans()`, sweeps staging, registers SIGTERM handler, mounts routers. |
| **ParakeetService** | `server/src/wispralt_server/dictate/parakeet.py` | Warm-resident `mlx-community/parakeet-tdt-0.6b-v2` (bfloat16). Single `ThreadPoolExecutor(max_workers=1)` serializes all inference (MLX is not thread-safe per model instance). Warmup JIT pass at startup. Defensive return-type handling: checks `hasattr(result, 'text')` vs list of `AlignedToken`. Tracks p50/p95 latency percentiles over last 100 calls. |
| **MeetingRunner** | `server/src/wispralt_server/jobs/runner.py` | `asyncio.Semaphore(1)` enforces one meeting at a time. Dedicated `ThreadPoolExecutor(max_workers=1, thread_name_prefix="wispralt-meeting")` isolates meeting CPU work from the default asyncio thread pool. OOM guard: rejects if `psutil.virtual_memory().available < 2 GiB`. Staging WAV always cleaned up in `finally` block. `reenqueue_pending()` called at startup for jobs whose WAVs survived a restart. |
| **JobStore** | `server/src/wispralt_server/jobs/store.py` | SQLite WAL mode (`PRAGMA journal_mode=WAL; synchronous=NORMAL`). Thread-safe via `threading.Lock`. Job lifecycle: `pending → running → done | failed`. `recover_orphans()`: marks `running` jobs as `failed`; marks `pending` jobs with missing WAV as `failed`; leaves `pending` jobs with existing WAV for `reenqueue_pending()`. |
| **MeetingPipeline** | `server/src/wispralt_server/meeting/pipeline.py` | Orchestrates: load channels → in-person detection (`silence.py`) → DeepFilterNet (`deepfilter.py`) → WhisperX CPU int8 (`whisperx_loader.py`) → Pyannote MPS diarization (`diarize.py`) → merge/label (`merge.py`) → atomic output write (`output.py`). Returns locked v3 transcript dict. |
| **RateLimitMiddleware** | `server/src/wispralt_server/middleware/rate_limit.py` | In-memory per-IP rolling-window limiter. Two windows: `/transcribe/dictate` 60 req/60s; `POST /transcribe/meeting` 4 req/3600s. Returns 429 with `Retry-After` header. |
| **staging / env_writer** | `server/src/wispralt_server/ops/` | `staging.py` manages the staging directory; startup sweep removes orphaned WAVs older than 24h. `env_writer.py` atomically rewrites `.env` key-value pairs via tempfile-in-same-dir + `os.replace`, preserving mode 0600. |

### Networking

The Cloudflare Tunnel maps `transcribe.<user-domain>` → `http://127.0.0.1:8000` on the Mac mini. `cloudflared` runs as a launchd system service. Its persistent credential lives in the macOS system keychain; the setup script (`scripts/setup-cloudflared.sh`) discards the token from shell memory immediately after `sudo cloudflared service install <token>`.

Tunnel latency overhead: ~50–200ms same-region. The Cloudflare free tier has a community-reported body limit of approximately 100 MB; the server enforces `MAX_UPLOAD_BYTES` (default 2 GiB) at the application layer. A 90-minute 2-channel 16kHz Float32 WAV is approximately 460 MB; clients warn the user at 60 minutes.

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
    └── POST /transcribe/meeting
            └── MeetingRunner.submit_or_429()
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
| Parakeet TDT 0.6B v2 (MLX bfloat16) | ~1.8–2.2 GB unified |
| WhisperX CrisperWhisper (CTranslate2, CPU int8) | ~1.5 GB |
| Pyannote 3.1 (PyTorch, MPS) | ~1.0 GB |
| DeepFilterNet 3 | ~0.5 GB |
| Python / FastAPI process | ~0.5 GB |
| **Total resident** | **~7.3 GB** |

The Mac mini M4 16 GB configuration leaves ~8.7 GB for the OS and other processes. `MeetingRunner.submit_or_429()` rejects a new meeting job if `psutil.virtual_memory().available < 2 GiB`.

**Disk guard:** `staging.stream_to_staging()` checks that free disk is at least 1.5x the upload size before accepting a file. `GET /metrics` reports `disk.free_gb` and `disk.staging_count` for monitoring.

**Device matrix:** MLX for Parakeet (Apple Neural Engine / GPU unified memory). CTranslate2 has no MPS support — WhisperX runs on CPU with `compute_type="int8"`. Pyannote supports MPS and runs on `torch.device("mps")` for diarization. DeepFilterNet requires 48kHz input; `deepfilter.py` resamples 16k→48k→enhance→16k.

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

All `/transcribe/*`, `/admin/*`, and `/readyz/*` endpoints require `Authorization: Bearer <WISPRALT_API_KEY>`. `auth.py` uses `secrets.compare_digest` for constant-time comparison (prevents timing oracle attacks). The authorization header check (line 56–61 in `auth.py`) strips the `Bearer ` prefix before comparing.

The API key is a 64-character hex string (`secrets.token_hex(32)`). It lives in `server/.env` (mode 0600, owner = current user) and in memory as a module-level variable guarded by `threading.Lock` for hot-swap during key rotation.

**Key rotation** (`POST /admin/rotate-key`): generates a new key, atomically rewrites `.env` via `env_writer.rewrite_env_var` (tempfile-in-same-dir + `os.replace`, mode 0600 preserved), writes the new key to `~/Library/Application Support/WisprAlt/.last-rotation-key` (mode 0600, written with `os.O_CREAT | os.O_WRONLY | os.O_TRUNC` at mode 0o600), prints `NEW_API_KEY=<key>` to stdout (captured by launchd to `server.log`), and hot-swaps the in-memory key. The new key is **never** in the response body — response is `{"rotated": true}`.

---

## Speaker Rename

Speaker rename is **client-side only**. There is no server `PATCH /speakers` endpoint. The client (`TranscriptDocument.swift`):

1. Loads the local `.json` file.
2. Calls `renameSpeaker(raw:to:)`: updates `display_name` in the `speakers` table and rewrites the `speaker` field in every matching segment. Throws `.speakerNameConflict` if the new name collides with an existing speaker's `display_name`.
3. Writes all four formats atomically: each file is written to a `.{uuid}.tmp` file in the same directory, then `replaceItemAt` replaces the original.
4. A `.transcriptWriteInProgress` sentinel file is created before the first replace and deleted after the last. On app launch, orphan sentinels trigger a revert (delete partial outputs, keep originals).

This is fully offline-capable and requires no network connectivity.
