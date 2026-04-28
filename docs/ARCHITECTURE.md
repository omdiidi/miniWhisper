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
│       └─ optional X-Smart-Format: true ──▶ Mercury 2 (OpenRouter, 250ms,     │
│             fail-soft → raw text on timeout/error)                           │
│   /transcribe/meeting ──▶ enqueue → JobStore (SQLite)                        │
│                          ↓                                                    │
│                    asyncio.to_thread(run_pipeline)                            │
│                          ↓                                                    │
│       MeetingPipeline: WhisperX (CPU int8) → Pyannote (MPS)                  │
│                          ↓                                                    │
│       output.py: JSON+SRT+VTT+TXT atomic write (tempfile in same dir)         │
│   /transcribe/meeting/{id}             ── poll                                │
│   /transcribe/meeting/{id}/download/{fmt}  ── stream                          │
│   /transcribe/meeting/{id}             ── DELETE (cleanup staging)            │
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
| **MeetingRecorder** | `client/WisprAlt/Capture/MeetingRecorder.swift` | SCStream dual-channel capture: `captureMicrophone=true` (ch1) + `capturesAudio=true, excludesCurrentProcessAudio=true` (ch2). Both downsampled to 16kHz Float32 via stateful AVAudioConverter. `startPTS` locked with `os_unfair_lock`; `CMTimeSubtract` used (not float subtraction) to avoid float drift over multi-hour meetings. Feeds `AlignedRingBuffer`. Instantiates `AudioDeviceListener` before `stream.startCapture()`; posts `.meetingConfigChanged` on input-device change. On abort: tears down the listener, deletes the partial WAV, resets state to idle. |
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
| **SparkleController** | `client/WisprAlt/Update/SparkleController.swift` | Sparkle 2 wrapper. Defers update sheet if `MeetingRecorder.isActive == true`. Auto-updates intentionally **disabled** in be720a1 — `startingUpdater: false` + `SUEnableAutomaticChecks=false` in Info.plist. Tier 1.5 distribution rides on `/wispralt-update`. |

### Server

| Component | File | Responsibility |
|---|---|---|
| **FastAPI app** | `server/src/wispralt_server/main.py` | Single uvicorn worker. Lifespan: validates `.env` permissions (warns if not 0600), loads Parakeet, runs `JobStore.recover_orphans()`, sweeps staging, registers SIGTERM handler, mounts routers. |
| **ParakeetService** | `server/src/wispralt_server/dictate/parakeet.py` | Warm-resident `mlx-community/parakeet-tdt-0.6b-v2` (float32 — bfloat16 caused a `[matmul] (128,257) vs (514,51)` shape error on first inference). Single `ThreadPoolExecutor(max_workers=1)` serializes all inference (MLX is not thread-safe per model instance). Warmup JIT pass at startup. Defensive return-type handling: checks `hasattr(result, 'text')` vs list of `AlignedToken`. Tracks p50/p95 latency percentiles over last 100 calls. **Audio decode boundary**: `_sync_transcribe` wraps `soundfile.read` in `try/except (LibsndfileError, RuntimeError) → CorruptAudioError` so malformed uploads convert to HTTP 422 (via the route handler) instead of leaking as 500. |
| **MeetingRunner** | `server/src/wispralt_server/jobs/runner.py` | `asyncio.Semaphore(1)` enforces one meeting at a time. Dedicated `ThreadPoolExecutor(max_workers=1, thread_name_prefix="wispralt-meeting")` isolates meeting CPU work from the default asyncio thread pool. OOM guard: rejects if `psutil.virtual_memory().available < 2 GiB`. Staging WAV always cleaned up in `finally` block. `reenqueue_pending()` called at startup for jobs whose WAVs survived a restart. |
| **JobStore** | `server/src/wispralt_server/jobs/store.py` | SQLite WAL mode (`PRAGMA journal_mode=WAL; synchronous=NORMAL`). Thread-safe via `threading.Lock`. Job lifecycle: `pending → running → done | failed`. `recover_orphans()`: marks `running` jobs as `failed`; marks `pending` jobs with missing WAV as `failed`; leaves `pending` jobs with existing WAV for `reenqueue_pending()`. |
| **MeetingPipeline** | `server/src/wispralt_server/meeting/pipeline.py` | Orchestrates: load channels → in-person detection (`silence.py`) → denoise no-op (`deepfilter.py` is a stub, see note below) → WhisperX CPU int8 (`whisperx_loader.py`) → Pyannote MPS diarization (`diarize.py`) → merge/label (`merge.py`) → atomic output write (`output.py`). Returns locked v3 transcript dict. |
| **meeting/__init__** | `server/src/wispralt_server/meeting/__init__.py` | Package init that runs **two import-time compat shims** before any submodule loads checkpoints: (1) monkeypatches `torch.load` / `torch.serialization.load` to force `weights_only=False` (required for trusted HF checkpoints since PyTorch 2.6 default flipped to `weights_only=True`, blocking `omegaconf.ListConfig` and other pickled objects in pyannote/WhisperX); (2) intercepts `huggingface_hub.{hf_hub_download, snapshot_download}` to translate the removed `use_auth_token=` kwarg → `token=` (pyannote.audio 3.3.2 still calls the legacy name). |
| **RateLimitMiddleware** | `server/src/wispralt_server/middleware/rate_limit.py` | In-memory per-IP rolling-window limiter. Two windows: `/transcribe/dictate` 60 req/60s; `POST /transcribe/meeting` 4 req/3600s. Returns 429 with `Retry-After` header. |
| **staging / env_writer** | `server/src/wispralt_server/ops/` | `staging.py` manages the staging directory; startup sweep removes orphaned WAVs older than 24h. `env_writer.py` atomically rewrites `.env` key-value pairs via tempfile-in-same-dir + `os.replace`, preserving mode 0600. |

### Networking

The Cloudflare Tunnel maps `transcribe.<user-domain>` → `http://127.0.0.1:8000` on the Mac mini. `cloudflared` runs as a **user-level LaunchAgent** (`~/Library/LaunchAgents/co.wispralt.cloudflared.plist`), not as a system service — the `sudo cloudflared service install` path is broken on macOS 14/15+. The tunnel token is stored at `~/.config/wispralt/cloudflare-token` (mode 0600); the LaunchAgent reads it via `--token-file` on cloudflared ≥ 2025.4.0, or has it inlined in the plist (mode 0600) on older versions. The setup script (`scripts/setup-cloudflared.sh`) reads the token via `read -r -s` and unsets the shell variable immediately after writing the file.

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
| Parakeet TDT 0.6B v2 (MLX float32) | ~3.5–4.0 GB unified (≈2× bf16) |
| WhisperX CrisperWhisper (CTranslate2, CPU int8) | ~1.5 GB |
| Pyannote 3.1 (PyTorch, MPS) | ~1.0 GB |
| Python / FastAPI process | ~0.5 GB |
| **Total resident** | **~6.5–7.0 GB** |

The Mac mini M4 16 GB configuration leaves ~8.7 GB for the OS and other processes. `MeetingRunner.submit_or_429()` rejects a new meeting job if `psutil.virtual_memory().available < 2 GiB`.

**Disk guard:** `staging.stream_to_staging()` checks that free disk is at least 1.5x the upload size before accepting a file. `GET /metrics` reports `disk.free_gb` and `disk.staging_count` for monitoring.

**Device matrix:** MLX for Parakeet (Apple Neural Engine / GPU unified memory). CTranslate2 has no MPS support — WhisperX runs on CPU with `compute_type="int8"`. Pyannote supports MPS and runs on `torch.device("mps")` for diarization.

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
