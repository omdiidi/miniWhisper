# Plan: WisprAlt — Self-Hosted Wispr Flow Replacement (v3)

**Brief:** `./tmp/briefs/2026-04-24-wispralt.md`
**Confidence:** 9.5/10 for one-pass AI implementation. Revised after **five** independent reviewer passes — all 30 issues from R1/R2/R3 + 30 NEW issues from R4/R5 incorporated.

---

## Goal

Build a two-component system:

1. **`server/`** — FastAPI on the user's M4 Mac mini (16GB), reachable via Cloudflare Tunnel on a subdomain of an existing Cloudflare-managed domain. Two endpoints:
   - `POST /transcribe/dictate` — Parakeet TDT 0.6B v2 (MLX) for sub-200ms English dictation.
   - `POST /transcribe/meeting` — DeepFilterNet 3 + WhisperX (`nyrahealth/faster_CrisperWhisper`) + Pyannote 3.1 diarization. Background-processed via `asyncio.to_thread` + SQLite job store (no Redis). Polled by client.

2. **`client/`** — Swift native macOS menubar app (signed `.app`, notarized DMG):
   - Hold FN to record dictation; release to send → server → inject text at cursor.
   - Triple-tap FN within 400ms to start/stop dual-channel meeting recording (mic on ch1 via `captureMicrophone`, system audio on ch2 via `capturesAudio`).
   - Auto-detect "in-person mode" (channel 2 RMS-silent) and let server use single-channel diarization with "Speaker N" labels.
   - **Speaker rename happens entirely client-side** (atomic local file rewrite) — no server round-trip.
   - Deep-link permission flows for the four required permissions; mid-flow restart for Input Monitoring on macOS 14.4+.
   - Mic mutual exclusion: if meeting recording is active, dictation hold is a no-op.

3. **`docs/`** — README, ARCHITECTURE, SETUP-CLIENT, SETUP-SERVER, API, TRANSCRIPT-FORMAT (with locked-in JSON schema), TROUBLESHOOTING, OVERVIEW (file→doc map), CONTRIBUTING (CI signing secrets).

4. **`.claude/commands/`** — `/setup-server`, `/setup-client`, `/test-connection`, `/docs-check`, `/update-models` so a fresh checkout is fully driven from Claude Code.

5. **`scripts/`** — One-step setup. Server: install Python deps, validate HF token, download ~5.6GB weights with progress + integrity checks, generate API key, install cloudflared (token via stdin, never stored to disk), persist `SERVER_URL`, register launchd. Client: build/sign/notarize/staple DMG.

## Why

- User pays $15/mo for Wispr Flow. Privacy-cloud model. Has an always-on M4 mini with spare capacity.
- Dictation latency budget <500ms p50 + meeting transcription with speaker diarization beats Wispr Flow on functionality, privacy, and cost.
- Polished GitHub showcase project; setup driven via Claude Code in one conversation.

## What — User-Visible Behavior

### Dictation flow
1. Hold FN. Indicator turns red.
2. Mic captured via AVAudioEngine ring buffer (16kHz mono Float32). **No-op if `MeetingRecorder.isActive == true`.**
3. Release FN → send WAV to `/transcribe/dictate` with `Authorization: Bearer <key>`.
4. Server transcribes via warm Parakeet (~80–200ms).
5. Client injects text: AXUIElement first (with read-back verification), clipboard+Cmd+V fallback (rich-clipboard preserved with `changeCount` guard).
6. p50 round-trip: ~250–400ms.

### Meeting flow
1. Triple-tap FN within 400ms (or click menubar Record). Indicator pulses.
2. `SCStream` captures `captureMicrophone=true` (ch1) + `capturesAudio=true, excludesCurrentProcessAudio=true` (ch2). Both downsampled to 16kHz Float32, written to single 2-channel non-interleaved WAV via `AlignedRingBuffer` (sample-position-keyed, gap-padded with silence on sleep/wake).
3. Triple-tap again to stop. Client UI moves through three explicit states: **uploading** → **processing** → **done**, each with progress where applicable.
4. WAV uploaded to `/transcribe/meeting` (HTTP/1.1 streaming with `Content-Length`; 90-min recording cap, warning at 60min). Server returns `{job_id, status: "pending"}`, persists job in SQLite.
5. Server worker (in-process, `asyncio.to_thread`):
   - Detects in-person mode: frame-based RMS check on ch2 (configurable `SILENCE_THRESHOLD`, default 0.002 over 100ms frames, 90% rule).
   - In-person → single-channel WhisperX + Pyannote on ch1 → "Speaker 1, 2, ...".
   - Remote → per-channel WhisperX, Pyannote on ch2 only, merge by timestamp → "You" + "Other (1, 2, ...)".
6. Client polls `GET /transcribe/meeting/{job_id}` every 5s. On `done`, downloads JSON+SRT+VTT+TXT to `~/Documents/WisprAlt/Meetings/YYYY-MM-DD_HHMM_<title>.{ext}`. Calls `DELETE /transcribe/meeting/{job_id}` to clean up server staging. Notifies.
7. Speaker rename: open transcript in menubar, edit names → client rewrites local JSON+SRT+VTT+TXT atomically (`tempfile in same dir → os.replace`). No server call. Works offline.

### Setup flow (server)
1. `git clone` on Mac mini, run `./scripts/setup-server.sh`.
2. Script: macOS 13+ check → Python 3.11 check → `df -h` requires ≥8GB free → Homebrew Redis NOT installed (we don't use it) → `uv venv` + `uv sync` → prompt for HF_TOKEN, validate with `huggingface-cli whoami` AND gated metadata fetch on both `pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0` with copy-paste accept-terms URLs on failure → `download-models.sh` (per-model size echoes, post-download size verification) → `generate-api-key.sh` (32-byte hex, `chmod 600 server/.env`) → `setup-cloudflared.sh` (interactive: prompt for full subdomain URL, prompt for tunnel token via stdin, runs `sudo cloudflared service install`, **discards token from memory immediately**, persists chosen `SERVER_URL` to `.env`) → `server-launchd.sh` (single LaunchAgent for FastAPI; tunnel-token stored separately by `cloudflared` itself in system keychain) → `doctor.sh` (verifies `/healthz`, `/readyz/dictation`, `/readyz/meeting`, `.env` mode is `0600`, owner is current user, models on disk match expected sizes) → prints client-config one-liner.

### Setup flow (client)
1. Download signed/notarized DMG from GitHub Releases.
2. Drag to Applications. First launch: sequential 4-permission wizard (Accessibility → Input Monitoring → Microphone → Screen Recording). After Input Monitoring grant, on macOS 14.4+ a "Quit and Reopen Required" sheet blocks until app restart.
3. Paste server config one-liner → done.

---

### Success Criteria

- [ ] `/transcribe/dictate` round-trip on LAN: <250ms p50, <400ms p95 for 10s clips after warmup.
- [ ] Triple-tap FN: 0 false positives during fn+arrow / fn+delete / fn+F-keys normal use.
- [ ] Meeting capture: valid 2-channel 16kHz Float32 WAV, channel-aligned to <2 samples drift over 1h, gaps from sleep/wake padded with silence.
- [ ] In-person mode auto-detect flips when ch2 RMS <`SILENCE_THRESHOLD` for 90%+ of 100ms frames (threshold configurable).
- [ ] Server holds Parakeet, WhisperX, Pyannote (MPS), DeepFilterNet, alignment model resident; ~7.3GB combined; no per-request loading.
- [ ] `/readyz/dictation` and `/readyz/meeting` reflect actual model state independently.
- [ ] Cloudflare Tunnel + bearer auth: 401 on missing/wrong, 200 on correct.
- [ ] Tunnel token never persisted to `.env` or LaunchAgent plist; lives only in cloudflared's system keychain entry.
- [ ] All four permissions detected at launch; "Quit and Reopen" sheet blocks on macOS 14.4+ Input Monitoring grant.
- [ ] Mic mutual exclusion: holding FN during active meeting recording is a no-op (with toast/log).
- [ ] Speaker rename works fully offline; atomic file rewrite preserves the original on failure.
- [ ] `/setup-server` slash command runs end-to-end on a fresh Mac mini in one Claude conversation.
- [ ] All `docs/*.md` listed in `OVERVIEW.md` updated alongside their code (file→doc map).
- [ ] DMG: signed Developer ID, hardened-runtime, notarized, stapled. CI workflow uses non-keychain notarytool path.
- [ ] Meeting upload UI shows three explicit states (uploading with %, processing, done).
- [ ] No `[NEEDS CLARIFICATION]` markers remain.

---

## All Needed Context

### Locked decisions (resolved from v1 Open Clarifications)

| Decision | v2 resolution |
|---|---|
| Sparkle auto-updates? | **Yes, included.** Adds Sparkle 2 SPM dep, EdDSA-signed appcast hosted on GitHub Pages. |
| Apple Developer Program? | **Required.** $99/yr. Plan assumes user has or will enroll. CI workflow uses notarytool with `--apple-id/--password/--team-id` env vars from GH secrets, NOT `--keychain-profile`. |
| macOS minimum target | **macOS 14.0+** for `captureMicrophone` single-clock-domain. Version-gated `#available(macOS 14.4, *)` for Input Monitoring restart UX. |
| Redis required? | **No — dropped.** Use SQLite-only `JobStore` + `asyncio.to_thread`. Single-user Mac mini does not need Redis. Trade: server restart kills in-flight jobs (resubmit acceptable). |
| Diarize ch1 in remote mode? | **No.** All ch1 = "You". |
| Speaker rename location | **Client-side only.** Atomic local rewrite. No server `PATCH`. Works offline. |

### Locked Transcript JSON Schema

```jsonc
{
  "job_id": "string (uuid4)",
  "mode": "remote" | "in_person",
  "created_at": "ISO 8601 string",
  "duration_s": "number",
  "language": "en",
  "model": {
    "transcription": "nyrahealth/faster_CrisperWhisper",
    "diarization": "pyannote/speaker-diarization-3.1",
    "denoise": "deepfilternet-3"
  },
  "segments": [
    {
      "start": 0.0,                  // seconds
      "end": 3.42,
      "channel": 1 | 2 | null,       // null for in-person mode
      "speaker": "You",              // canonical label after any client renames
      "speaker_raw": "SPEAKER_00",   // pyannote raw label, never overwritten
      "text": "string",
      "words": [
        { "word": "Let",   "start": 0.00, "end": 0.18, "score": 0.99 }
      ],
      "overlap": false               // true if simultaneous with another segment
    }
  ],
  "speakers": {                      // canonical name table; client rename rewrites this
    "You":      { "raw": ["mic"],          "channel": 1 },
    "Other":    { "raw": ["SPEAKER_00"],   "channel": 2 },
    "Other 2":  { "raw": ["SPEAKER_01"],   "channel": 2 }
  }
}
```

The `speaker_raw` field is preserved even after rename so client can map raw pyannote labels back to user names if a recording is reprocessed. The `speakers` table is the single source of truth for current display names.

### Documentation & References

```yaml
# === Server: Parakeet (dictation) ===
- url: https://github.com/senstella/parakeet-mlx
  why: parakeet-mlx==0.5.1 reference
- url: https://www.mintlify.com/senstella/parakeet-mlx/advanced/low-level-api
  why: Low-level get_logmel + model.generate path; in-memory bytes (high-level transcribe() is path-only).
- url: https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v2
  why: Canonical model ID. v3 is multilingual + worse on English.
- critical: |
    model.generate may return EITHER a single Hypothesis OR a list of AlignedToken. Defensive
    extraction: if hasattr(result, 'text'): text = result.text else: text = "".join(t.text for t in result).
    MLX inference is NOT thread-safe per model instance — serialize via single-thread executor.
    First-call kernel JIT adds 300ms-2s; warmup pass at startup is mandatory.
    Resident memory: ~1.8-2.2 GB unified.

# === Server: WhisperX + CrisperWhisper + Pyannote + DeepFilterNet ===
- url: https://github.com/m-bain/whisperX
  why: pin whisperx==3.8.5 (3.8.2/3.8.3/3.7.3/3.3.2 + several 3.1.x YANKED)
- url: https://github.com/nyrahealth/CrisperWhisper
  why: Use faster-whisper variant `nyrahealth/faster_CrisperWhisper` (CTranslate2 backend)
- url: https://huggingface.co/pyannote/speaker-diarization-3.1
  why: GATED — accept terms at speaker-diarization-3.1 AND segmentation-3.0. Validate at setup time.
- url: https://github.com/Rikorose/DeepFilterNet
  why: pin deepfilternet==0.5.6. Requires 48kHz input.
- critical: |
    torch==2.6.0 (torchaudio 2.7+ removes AudioMetaData → pyannote breaks).
    librosa>=0.11.0 required (0.10.x needs numpy<2; we want numpy 2.x for parakeet).
    CTranslate2 has NO MPS — WhisperX must run on CPU (compute_type="int8" recommended).
    Pyannote DOES support MPS via pipeline.to(torch.device("mps")).
    Pyannote 3.3.2 returns Annotation, not DataFrame — convert via itertracks.
    Pyannote crashes on audio <2s — guard.
    DeepFilterNet wants exactly 48kHz — resample 16k→48k→enhance→16k.

# === Server: FastAPI (no arq, no Redis) ===
- url: https://fastapi.tiangolo.com/async/
  why: Single uvicorn worker; long jobs use asyncio.to_thread (CPU work in default thread pool).
- critical: |
    Single-process: no Redis, no separate worker. SQLite JobStore tracks state; jobs survive
    crashes via at-startup recovery sweep that marks orphaned 'running' jobs as 'failed'.

# === Server: Cloudflare Tunnel ===
- url: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/as-a-service/macos/
  why: brew install cloudflared && sudo cloudflared service install <TOKEN>
- critical: |
    Tunnel token granted via STDIN to `cloudflared service install`, then discarded.
    cloudflared stores its persistent credential in the macOS system keychain — we don't.
    Tunnel is OUTBOUND-ONLY — no inbound port forwarding, no risk to existing site on same domain.
    Latency overhead 50-200ms same-region. Document Cloudflare tunnel free-tier body limits in
    docs/TROUBLESHOOTING.md (community-reported ~100MB; verify; otherwise enforce 90-min recording cap).

# === Client: ScreenCaptureKit dual-channel (macOS 14+) ===
- url: https://developer.apple.com/documentation/screencapturekit/scstreamconfiguration/capturemicrophone
  why: Single SCStream emits .audio + .microphone with synchronized PTS (single clock domain).
- url: https://nonstrict.eu/blog/2024/handling-audio-capture-gaps-on-macos/
  why: Sleep/wake creates PTS gaps; pad with silence to maintain alignment.
- url: https://developer.apple.com/documentation/technotes/tn3136-avaudioconverter-performing-sample-rate-conversions
  why: Stateful AVAudioConverter, .noDataNow handling.
- critical: |
    width=2, height=2, minimumFrameInterval=(1,1) to minimize unused video CPU.
    SCStream emits silent buffers when system audio silent → enables in-person detection.
    Use CMTimeSubtract (NOT .seconds float math) and per-channel running sample counters
    to avoid float drift over multi-hour meetings.
    macOS 14.4+ has separate "System Audio Recording" sub-permission distinct from full Screen Recording.

# === Client: FN-key detection ===
- url: https://developer.apple.com/documentation/coregraphics/cgeventflags
  why: kCGEventFlagsChanged + .maskSecondaryFn (0x800000). Keycode 63 raw.
- url: https://github.com/lwouis/alt-tab-macos/blob/master/src/logic/events/KeyboardEvents.swift
  why: Reference CGEventTap + flagsChanged.
- critical: |
    Tap level: kCGSessionEventTap with .listenOnly (don't block normal FN+key combos).
    Mask: flagsChanged | keyDown.
    Tap-time recorded from FN-DOWN, not FN-UP, to handle slow releases.
    Clear tapTimes when hold confirmed (prevents stale taps poisoning future triple-taps).
    State mutation must happen on a serial private queue, NOT directly from the CGEventTap thread,
    to avoid races with the 300ms hold timer running on .main.
    macOS 14.4+ CGRequestListenEventAccess returns true but real grant requires process restart.

# === Client: Text injection ===
- url: https://levelup.gitconnected.com/swift-macos-insert-text-to-other-active-applications-two-ways-9e2d712ae293
  why: AXUIElement primary; clipboard+Cmd+V fallback.
- url: https://github.com/p0deje/Maccy/blob/master/Maccy/Clipboard.swift
  why: Rich clipboard save-restore via NSPasteboardItem types; skip dyn.* prefixed types.
- critical: |
    AXUIElement may return .success and silently no-op (Electron). Read kAXValueAttribute back
    to verify; if unchanged, fall through to clipboard.
    On clipboard restore, only write back if pb.changeCount == saved+1 (don't stomp user copy).
    Cmd+V virtualKey 0x09; flags = .maskCommand on both keyDown and keyUp.

# === Client: Code signing & CI ===
- url: https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution
  why: Apple notarytool flow.
- url: https://www.frr.dev/posts/macos-notarization-guide-linter/
  why: End-to-end DMG sign + notarize + staple.
- critical: |
    --options runtime mandatory for notarization.
    com.apple.security.automation.apple-events entitlement required for AX text injection.
    com.apple.security.network.client required for HTTPS uploads.
    CI: notarytool submit --apple-id $APPLE_ID --password $APP_SPECIFIC_PASSWORD --team-id $TEAM_ID
    (NOT --keychain-profile — keychain doesn't persist in CI).
    DEVELOPER_ID_APP passed as GH secret, validated non-empty at top of build-client.sh.

# === Repo conventions ===
- file: /Users/omidzahrai/Desktop/CODEBASES/TOOLS/.claude/rules/backend-patterns.md
  why: Repository pattern, service layer, schema validation at boundaries, typed errors.
- file: /Users/omidzahrai/.claude/CLAUDE.md
  why: Documentation Discipline (every code change updates docs); never push without approval.
```

### Current Codebase Tree

```
wisprflowALT/
├── .git/
└── tmp/
    ├── briefs/2026-04-24-wispralt.md
    └── ready-plans/2026-04-24-wispralt-implementation.md
```

### Desired Codebase Tree

```
wisprflowALT/
├── README.md                                                    ← NEW
├── LICENSE                                                      ← NEW (MIT)
├── .gitignore .editorconfig                                     ← NEW
│
├── server/                                                      ← NEW (FastAPI)
│   ├── pyproject.toml                                           ← NEW
│   ├── uv.lock                                                  ← NEW
│   ├── .env.example                                             ← NEW (HF_TOKEN, WISPRALT_API_KEY, SERVER_URL, paths, SILENCE_THRESHOLD)
│   ├── README.md                                                ← NEW
│   ├── src/wispralt_server/
│   │   ├── __init__.py main.py                                  ← NEW (FastAPI lifespan, route mount, startup orphan-job sweep)
│   │   ├── config.py                                            ← NEW (Pydantic Settings; SILENCE_THRESHOLD configurable; .env mode 600 verify)
│   │   ├── auth.py                                              ← NEW (Bearer + secrets.compare_digest; rotation endpoint)
│   │   ├── audio.py                                             ← NEW (decode/resample/split — pure data access)
│   │   ├── dictate/parakeet.py                                  ← NEW (warm load + single-thread executor; defensive return-type handling)
│   │   ├── meeting/
│   │   │   ├── pipeline.py                                      ← NEW (orchestration; service layer)
│   │   │   ├── deepfilter.py                                    ← NEW (16k↔48k around enhance)
│   │   │   ├── whisperx_loader.py                               ← NEW (CrisperWhisper + align singletons)
│   │   │   ├── diarize.py                                       ← NEW (Pyannote on MPS; <2s guard; Annotation→DataFrame)
│   │   │   ├── merge.py                                         ← NEW (chronological merge; speaker label maps; overlap flag)
│   │   │   ├── silence.py                                       ← NEW (frame-based RMS; 100ms frames; threshold from config)
│   │   │   └── output.py                                        ← NEW (JSON/SRT/VTT/TXT; tempfile in same dir; os.replace)
│   │   ├── jobs/
│   │   │   ├── store.py                                         ← NEW (SQLite repo: Job table; recover-orphans on startup)
│   │   │   └── runner.py                                        ← NEW (in-process asyncio.to_thread runner; pipeline invocation)
│   │   ├── routes/
│   │   │   ├── dictate.py                                       ← NEW
│   │   │   ├── meeting.py                                       ← NEW (POST + GET + DELETE; NO PATCH/speakers)
│   │   │   ├── admin.py                                         ← NEW (POST /admin/rotate-key)
│   │   │   └── health.py                                        ← NEW (/healthz, /readyz/dictation, /readyz/meeting)
│   │   └── ops/
│   │       ├── staging.py                                       ← NEW (manage /tmp/wispralt; startup sweep; per-job cleanup)
│   │       └── notarytool_secrets.py                            ← N/A (server-side, ignore)
│
├── client/                                                      ← NEW (Swift)
│   ├── Package.swift                                            ← NEW (macOS 14, Sparkle 2 dep)
│   ├── README.md                                                ← NEW
│   ├── WisprAlt.xcodeproj/                                      ← NEW
│   ├── WisprAlt/
│   │   ├── WisprAltApp.swift Info.plist WisprAlt.entitlements   ← NEW
│   │   ├── App/
│   │   │   ├── AppDelegate.swift                                ← NEW
│   │   │   ├── MenuBarController.swift                          ← NEW (mic-exclusion logic)
│   │   │   └── PermissionGate.swift                             ← NEW (4-step wizard; macOS 14.4+ restart sheet)
│   │   ├── Hotkeys/
│   │   │   ├── FNKeyMonitor.swift                               ← NEW (serial-queue state; tap=DOWN time; clear taps on hold)
│   │   │   └── HotkeyEvents.swift                               ← NEW (delegate proto)
│   │   ├── Capture/
│   │   │   ├── DictationRecorder.swift                          ← NEW (AVAudioEngine; no-op when meeting active)
│   │   │   ├── MeetingRecorder.swift                            ← NEW (SCStream dual-channel; CMTimeSubtract; gap padding)
│   │   │   ├── AlignedRingBuffer.swift                          ← NEW (sample-position-keyed; flushAligned; padMissing)
│   │   │   └── AudioFormat.swift                                ← NEW (canonical formats, CMSampleBuffer→AVAudioPCMBuffer)
│   │   ├── Server/
│   │   │   ├── ServerClient.swift                               ← NEW (URLSession; multipart streaming upload with progress; retry-on-reset)
│   │   │   ├── DictationAPI.swift                               ← NEW
│   │   │   ├── MeetingAPI.swift                                 ← NEW (submit/poll/download/delete; NO renameSpeakers)
│   │   │   └── ServerError.swift                                ← NEW
│   │   ├── Inject/
│   │   │   ├── TextInjector.swift                               ← NEW
│   │   │   ├── AccessibilityInjector.swift                      ← NEW (read-back verify)
│   │   │   └── ClipboardInjector.swift                          ← NEW (Maccy-style)
│   │   ├── Storage/
│   │   │   ├── Settings.swift                                   ← NEW (UserDefaults: serverURL, paths, hotkey config)
│   │   │   ├── KeychainHelper.swift                             ← NEW (API key in Keychain)
│   │   │   ├── TranscriptStore.swift                            ← NEW (file index + atomic local rewrites)
│   │   │   └── TranscriptDocument.swift                         ← NEW (JSON model; client-side rename logic)
│   │   ├── Update/
│   │   │   └── SparkleController.swift                          ← NEW (Sparkle 2 wrapper)
│   │   ├── UI/
│   │   │   ├── SettingsView.swift                               ← NEW (server URL, API key, paths, Test connection)
│   │   │   ├── PermissionsView.swift                            ← NEW
│   │   │   ├── TranscriptListView.swift                         ← NEW
│   │   │   ├── TranscriptDetailView.swift                       ← NEW (rename UI; offline-capable)
│   │   │   └── RecordingIndicatorView.swift                     ← NEW (uploading/processing/done states)
│   │   └── Util/
│   │       ├── Logger.swift                                     ← NEW
│   │       └── Notifications.swift                              ← NEW
│
├── scripts/                                                     ← NEW
│   ├── setup-server.sh                                          ← NEW
│   ├── setup-cloudflared.sh                                     ← NEW (token via stdin; never persisted; SERVER_URL → .env)
│   ├── download-models.sh                                       ← NEW (df check; per-model echoes; HF gated validation; size verify)
│   ├── generate-api-key.sh                                      ← NEW (chmod 600 .env after write)
│   ├── server-launchd.sh                                        ← NEW (single LaunchAgent for FastAPI; no Redis plist)
│   ├── server-uninstall.sh                                      ← NEW
│   ├── doctor.sh                                                ← NEW (.env mode/owner check; both /readyz; size verify)
│   └── build-client.sh                                          ← NEW (DEVELOPER_ID_APP arg; CI-safe notarytool)
│
├── docs/                                                        ← NEW
│   ├── README.md ARCHITECTURE.md SETUP-SERVER.md SETUP-CLIENT.md
│   ├── API.md TRANSCRIPT-FORMAT.md TROUBLESHOOTING.md
│   ├── OVERVIEW.md (file→doc map)
│   └── CONTRIBUTING.md (CI signing secrets list)
│
├── CLAUDE.md                                                    ← NEW
│
├── .claude/
│   ├── commands/
│   │   ├── setup-server.md setup-client.md test-connection.md
│   │   ├── docs-check.md update-models.md
│   └── settings.json                                            ← NEW (allowlist common commands)
│
├── .github/
│   ├── workflows/build-client.yml                               ← NEW (signs with notarytool env-var path)
│   └── ISSUE_TEMPLATE/bug_report.md
│
└── tmp/...
```

---

## Architecture Overview

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

### Latency Budget (dictation)

| Stage | Budget |
|---|---|
| FN release detection | <5ms |
| Mic buffer finalize | ~30ms |
| WAV encode in-memory | <5ms |
| Upload to CF edge | ~30-80ms |
| CF→Mini tunnel | ~50-150ms |
| Parakeet warm inference | ~80-200ms |
| Response back | ~50-150ms |
| AX/Cmd+V injection | <10ms |
| **p50 total** | **~250-400ms** |

### Latency for meeting upload (separate UI state)

A 1h dual-channel 16k Float32 WAV ≈ 460MB. At 100Mbps symmetric: ~37s upload alone. The `RecordingIndicatorView` shows distinct **uploading (with %)** state through `URLSession` upload-progress callbacks before transitioning to **processing**.

---

## Key Pseudocode

### 1. Parakeet warm-load + defensive return-type handling

```python
# parakeet.py
import asyncio, io, secrets
from concurrent.futures import ThreadPoolExecutor
import mlx.core as mx, numpy as np, soundfile as sf, librosa
from parakeet_mlx import from_pretrained, DecodingConfig
from parakeet_mlx.audio import get_logmel

MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v2"
TARGET_SR = 16_000; MIN_SAMPLES = 1_600

class ParakeetService:
    def __init__(self):
        self.model = None
        self._exec = ThreadPoolExecutor(max_workers=1)  # SERIALIZE inference
        self.ready = False

    def load(self):
        self.model = from_pretrained(MODEL_ID, dtype=mx.bfloat16)
        # Warmup — Metal kernel JIT
        dummy = mx.zeros((TARGET_SR // 2,), dtype=mx.bfloat16)
        mel = get_logmel(dummy, self.model.preprocessor_config)
        result = self.model.generate(mel, decoding_config=DecodingConfig())
        mx.eval(result)
        self.ready = True

    def _extract_text(self, result) -> str:
        # CRITICAL: defensive — parakeet-mlx returns either Hypothesis with .text
        #   OR list of AlignedToken with .text per token. Handle both.
        if hasattr(result, "text"):
            return result.text.strip()
        if isinstance(result, list) and result and hasattr(result[0], "text"):
            return "".join(t.text for t in result).strip()
        return ""

    def _sync(self, audio_bytes: bytes) -> str:
        audio_np, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=False)
        if audio_np.ndim == 2: audio_np = audio_np.mean(axis=1)
        if sr != TARGET_SR:    audio_np = librosa.resample(audio_np, orig_sr=sr, target_sr=TARGET_SR)
        if len(audio_np) < MIN_SAMPLES: return ""
        audio_mlx = mx.array(audio_np, dtype=mx.bfloat16)
        mel = get_logmel(audio_mlx, self.model.preprocessor_config)
        result = self.model.generate(mel, decoding_config=DecodingConfig())
        mx.eval(result)
        return self._extract_text(result)

    async def transcribe(self, audio_bytes: bytes) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._exec, self._sync, audio_bytes)
```

### 2. FN-key state machine (serial-queue safe; tap-time from DOWN)

```swift
// FNKeyMonitor.swift
final class FNKeyMonitor {
    weak var delegate: FNKeyEventsDelegate?
    private let q = DispatchQueue(label: "co.wispralt.fn", qos: .userInteractive)
    private var fnDownTime: Double?
    private var otherKeyDuringFN = false
    private var holdTimer: DispatchWorkItem?
    private var tapTimes: [Double] = []     // store DOWN times
    private let holdThreshold = 0.30
    private let tripleWindow = 0.40

    // CGEventTap callback (NOT main, NOT q): hop to q immediately.
    func handleEvent(type: CGEventType, event: CGEvent) {
        let t = machToSeconds(event.timestamp)
        let isFn = event.flags.contains(.maskSecondaryFn)
        q.async { [self] in
            switch type {
            case .flagsChanged:
                if isFn { onFnDown(at: t) } else { onFnUp(at: t) }
            case .keyDown:
                if fnDownTime != nil {
                    otherKeyDuringFN = true
                    holdTimer?.cancel()
                }
            default: break
            }
        }
    }

    private func onFnDown(at t: Double) {
        fnDownTime = t; otherKeyDuringFN = false
        let item = DispatchWorkItem { [weak self] in
            self?.q.async {
                guard let s = self, s.fnDownTime != nil, !s.otherKeyDuringFN else { return }
                s.tapTimes.removeAll()  // CRITICAL: prevent stale taps from poisoning future triple-tap
                DispatchQueue.main.async { s.delegate?.dictationStart() }
            }
        }
        holdTimer = item
        DispatchQueue.main.asyncAfter(deadline: .now() + holdThreshold, execute: item)
    }

    private func onFnUp(at t: Double) {
        holdTimer?.cancel(); holdTimer = nil
        guard let down = fnDownTime else { return }
        let dur = t - down
        fnDownTime = nil
        if otherKeyDuringFN { return }                     // fn-modifier combo
        if dur >= holdThreshold {
            DispatchQueue.main.async { self.delegate?.dictationStop() }
        } else {
            // Tap — record DOWN time, not UP, to handle slow releases
            recordTap(at: down)
        }
    }

    private func recordTap(at downTime: Double) {
        tapTimes.append(downTime)
        let now = downTime
        tapTimes = tapTimes.filter { now - $0 < tripleWindow }
        if tapTimes.count >= 3 {
            tapTimes.removeAll()
            DispatchQueue.main.async { self.delegate?.toggleMeetingRecording() }
        }
    }
}
```

CGEventTap is `kCGSessionEventTap` + `.listenOnly`; mask `flagsChanged | keyDown`.

### 3. MeetingRecorder with AlignedRingBuffer + race-safe startPTS

```swift
// MeetingRecorder.swift
final class MeetingRecorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private(set) var isActive = false                       // queried by DictationRecorder
    private var stream: SCStream?
    private var stereoFile: AVAudioFile?
    private var aligned = AlignedRingBuffer()               // see below
    private var startPTS: CMTime?
    private let ptsLock = os.os_unfair_lock_s()
    private var micSampleCount: Int64 = 0                   // running counter, NOT pts arithmetic
    private var sysSampleCount: Int64 = 0
    private let stereoFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32, sampleRate: 16000,
        channels: 2, interleaved: false)!

    func start(to url: URL) async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.excludesCurrentProcessAudio = true
        cfg.sampleRate = 48000
        cfg.channelCount = 1
        if #available(macOS 14.0, *) { cfg.captureMicrophone = true }
        cfg.width = 2; cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        let filter = SCContentFilter(display: content.displays[0],
                                      excludingApplications: [], exceptingWindows: [])
        stream = SCStream(filter: filter, configuration: cfg, delegate: self)
        try stream?.addStreamOutput(self, type: .audio,        sampleHandlerQueue: .global())
        if #available(macOS 14.0, *) {
            try stream?.addStreamOutput(self, type: .microphone, sampleHandlerQueue: .global())
        }
        stereoFile = try AVAudioFile(forWriting: url, settings: stereoFormat.settings,
            commonFormat: .pcmFormatFloat32, interleaved: false)
        try await stream?.startCapture()
        isActive = true
    }

    func stream(_ s: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        let pts = CMSampleBufferGetPresentationTimeStamp(sb)
        // CRITICAL: lock startPTS assignment — both .audio and .microphone callbacks race here
        var localStart: CMTime
        os_unfair_lock_lock(&ptsLock)
        if startPTS == nil { startPTS = pts }
        localStart = startPTS!
        os_unfair_lock_unlock(&ptsLock)

        // CRITICAL: use CMTimeSubtract (not .seconds float subtract) to avoid float drift on long recordings
        let offsetSamples = Int(CMTimeGetSeconds(CMTimeSubtract(pts, localStart)) * 16000.0)

        guard let buf16k = AudioFormat.convertCMSampleBufferTo16kMono(sb) else { return }

        switch type {
        case .audio:
            aligned.append(buf16k, atSamplePos: offsetSamples, channel: .system)
        case .microphone:
            aligned.append(buf16k, atSamplePos: offsetSamples, channel: .mic)
        default: break
        }
        flushAligned()
    }

    private func flushAligned() {
        // Drain any positions where both channels have data; pad missing with silence.
        while let frame = aligned.flushAligned() {
            try? stereoFile?.write(from: frame)              // 2-channel non-interleaved Float32
        }
    }

    func stop() async throws -> URL {
        try await stream?.stopCapture()
        aligned.padMissing(toEnd: true)                      // pad lagging channel with silence
        flushAligned()
        stereoFile = nil; isActive = false
        return outputURL
    }
}
```

### 4. AlignedRingBuffer (named deliverable, full interface)

```swift
// AlignedRingBuffer.swift
enum AudioChannel { case mic, system }

final class AlignedRingBuffer {
    // Internally: Dictionary<Int /* startSample */, AVAudioPCMBuffer> per channel.
    // Frames are flushed when both channels have data covering up through some position;
    // missing channel data is filled with silence.

    func append(_ buf: AVAudioPCMBuffer, atSamplePos: Int, channel: AudioChannel) {
        // Insert into per-channel pending map; coalesce adjacent buffers.
    }

    /// Returns next aligned 2-channel chunk if available (smallest of mic/system head), else nil.
    /// Pads the lagging channel with silence ONLY if the gap exceeds GAP_TOLERANCE_MS (default 200).
    func flushAligned() -> AVAudioPCMBuffer? {
        // Algorithm:
        // 1. Find min position across both channels' next-available chunks.
        // 2. Up to that position, take overlapping samples from both; pad missing with zeros.
        // 3. Return as 2-channel non-interleaved AVAudioPCMBuffer (ch[0]=mic, ch[1]=system).
    }

    /// Force-flush all remaining buffered data, padding the lagging channel with silence.
    func padMissing(toEnd: Bool) { /* … */ }
}
```

### 5. Server meeting pipeline (SQLite-only, no Redis)

```python
# pipeline.py
def transcribe_meeting(wav_path: str, output_dir: str, job_id: str,
                       silence_threshold: float) -> dict:
    ch1, ch2, sr = load_channels(wav_path)
    in_person = is_silent_robust(ch2, sr, threshold=silence_threshold,
                                  frame_ms=100, silent_fraction=0.90)
    ch1_clean = deepfilter(ch1, sr)
    ch1_result = whisperx_transcribe(ch1_clean, sr)  # CPU, int8
    if in_person:
        diar = pyannote_diarize(ch1_clean, sr, max_speakers=8)  # MPS
        if diar.empty:
            segments = label_all(ch1_result, "Speaker 1", channel=None, raw_speakers=["mic"])
        else:
            ch1_diar = whisperx.assign_word_speakers(annotation_to_df(diar), ch1_result)
            segments = relabel_in_person(ch1_diar, channel=None)
    else:
        ch2_clean = deepfilter(ch2, sr)
        ch2_result = whisperx_transcribe(ch2_clean, sr)
        diar = pyannote_diarize(ch2_clean, sr, max_speakers=5)
        ch2_diar = whisperx.assign_word_speakers(annotation_to_df(diar), ch2_result) if not diar.empty else ch2_result
        seg_a = label_all(ch1_result, "You", channel=1, raw_speakers=["mic"])
        seg_b = label_others(ch2_diar, base="Other", channel=2)
        segments = merge_two_channels(seg_a, seg_b)
    transcript = build_transcript_object(job_id, "in_person" if in_person else "remote",
                                         segments, model_meta=MODEL_META)
    write_outputs_atomic(transcript, output_dir, job_id)  # tempfile in SAME dir → os.replace
    return transcript
```

### 6. JobStore (SQLite repository pattern, orphan recovery)

```python
# jobs/store.py
import sqlite3, json, time, uuid
class JobStore:
    def __init__(self, db_path: str):
        self.con = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self.con.execute("""CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, status TEXT, mode TEXT,
          created_at REAL, started_at REAL, finished_at REAL,
          error TEXT, output_dir TEXT, wav_path TEXT)""")
    def create(self, wav_path: str) -> str:
        jid = str(uuid.uuid4())
        self.con.execute("INSERT INTO jobs(id,status,created_at,wav_path) VALUES(?,?,?,?)",
                         (jid, "pending", time.time(), wav_path))
        return jid
    def recover_orphans(self):
        # Mark jobs that were 'running' at last shutdown as 'failed' so client polling completes.
        self.con.execute("UPDATE jobs SET status='failed', error='server restart' "
                         "WHERE status IN ('pending','running')")
    # set_running, set_done, set_error, get, ...
```

### 7. Bearer auth + key rotation

```python
# auth.py
import os, secrets, threading
_lock = threading.Lock()
_API_KEY = os.environ["WISPRALT_API_KEY"]

def current_key() -> str:
    with _lock: return _API_KEY

def require_api_key(request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "): raise HTTPException(401, "Missing bearer")
    if not secrets.compare_digest(auth.removeprefix("Bearer ").strip(), current_key()):
        raise HTTPException(401, "Invalid token")

# routes/admin.py
@router.post("/admin/rotate-key", dependencies=[Depends(require_api_key)])
def rotate_key():
    new_key = secrets.token_hex(32)
    # Atomically rewrite .env (same dir, os.replace, mode 600 preserved)
    rewrite_env_var(".env", "WISPRALT_API_KEY", new_key)
    global _API_KEY
    with _lock: _API_KEY = new_key
    return {"new_key": new_key}  # printed once, never logged
```

### 8. Text injection — AX with read-back verify

```swift
// AccessibilityInjector.swift
func tryInsert(_ text: String) -> Bool {
    guard AXIsProcessTrusted() else { return false }
    let sys = AXUIElementCreateSystemWide()
    var focused: CFTypeRef?
    guard AXUIElementCopyAttributeValue(sys, kAXFocusedUIElementAttribute as CFString, &focused) == .success,
          let el = focused else { return false }
    let ax = el as! AXUIElement

    // Snapshot existing value to detect silent no-op (Electron failure mode)
    var before: CFTypeRef?
    AXUIElementCopyAttributeValue(ax, kAXValueAttribute as CFString, &before)
    let beforeStr = (before as? String) ?? ""

    let r = AXUIElementSetAttributeValue(ax, kAXSelectedTextAttribute as CFString, text as CFTypeRef)
    if r != .success { return false }

    // Read-back: did anything actually change?
    var after: CFTypeRef?
    AXUIElementCopyAttributeValue(ax, kAXValueAttribute as CFString, &after)
    let afterStr = (after as? String) ?? ""
    return afterStr != beforeStr
}
```

### 9. Client-side speaker rename (atomic, offline-capable)

```swift
// TranscriptDocument.swift
struct TranscriptDocument: Codable {
    var job_id: String; var mode: String; var created_at: String
    var duration_s: Double; var segments: [Segment]; var speakers: [String: SpeakerInfo]

    mutating func renameSpeaker(from oldName: String, to newName: String) {
        guard speakers[oldName] != nil else { return }
        for i in segments.indices where segments[i].speaker == oldName {
            segments[i].speaker = newName
        }
        speakers[newName] = speakers.removeValue(forKey: oldName)
    }
}

// TranscriptStore.swift
func renameSpeaker(in jobID: String, from old: String, to new: String) throws {
    let base = meetingsDir.appendingPathComponent(jobID)
    var doc = try JSONDecoder().decode(TranscriptDocument.self,
                                        from: Data(contentsOf: base.appendingPathExtension("json")))
    doc.renameSpeaker(from: old, to: new)
    try writeAtomic(json: doc, to: base.appendingPathExtension("json"))
    try writeAtomic(srt: doc.toSRT(),  to: base.appendingPathExtension("srt"))
    try writeAtomic(vtt: doc.toVTT(),  to: base.appendingPathExtension("vtt"))
    try writeAtomic(txt: doc.toTXT(),  to: base.appendingPathExtension("txt"))
}

private func writeAtomic<T>(_ data: Data, to url: URL) throws {
    let tmp = url.deletingLastPathComponent()
        .appendingPathComponent(".\(UUID().uuidString).tmp")  // SAME directory — APFS rename is atomic
    try data.write(to: tmp, options: .atomic)
    try FileManager.default.replaceItemAt(url, withItemAt: tmp)
}
```

VTT writer uses `<v Speaker>text</v>` voice tags so playback tools render the speaker label natively. SRT keeps the `Speaker: text` convention documented in `docs/TRANSCRIPT-FORMAT.md`.

---

## Tasks (in implementation order)

> [DOC] tasks MUST update the listed `docs/*.md` in the same change.

### Phase 0 — Repo skeleton (1h)

1. [x] CREATE `.gitignore`, `LICENSE` (MIT), `.editorconfig`, `README.md` placeholder, `CLAUDE.md`. [DOC]
2. [x] CREATE `docs/OVERVIEW.md` with file→doc map (per global doc rule). [DOC]
3. [x] CREATE empty trees: `server/`, `client/`, `scripts/`, `docs/`, `.claude/commands/`, `.github/workflows/`.
   Note: `.claude/commands/.gitkeep` could not be written (sandbox restriction on `.claude/` directory). The directory exists. A `touch` from the user's terminal will add the `.gitkeep` if needed.

### Phase 1 — Server: foundation & dictation (4h)

4. CREATE `server/pyproject.toml` with **locked** versions:
   - `python = "^3.11"`
   - `parakeet-mlx==0.5.1`, `mlx>=0.22.1,<0.32.0`
   - `whisperx==3.8.5`, `pyannote.audio==3.3.2`, `deepfilternet==0.5.6`
   - `torch==2.6.0`, `torchaudio==2.6.0` (CPU index in `[tool.uv.sources]`)
   - `fastapi==0.115.0`, `uvicorn[standard]==0.30.0`, `python-multipart==0.0.9`
   - `pydantic==2.9`, `pydantic-settings==2.5`
   - `soundfile==0.12.1`, `librosa>=0.11.0,<0.12.0` (numpy 2 compat), `numpy>=2.0,<2.3`
   - `huggingface_hub>=0.30.2`
   - **NO** `arq`, **NO** `redis`
   - dev: `ruff==0.6.9`, `pyright==1.1.380`
5. CREATE `server/.env.example`: `HF_TOKEN`, `WISPRALT_API_KEY`, `SERVER_URL`, `MEETING_OUTPUT_DIR`, `JOB_DB_PATH`, `STAGING_DIR`, `SILENCE_THRESHOLD=0.002`, `MAX_UPLOAD_BYTES=2147483648`. **No `CLOUDFLARE_TUNNEL_TOKEN`.**
6. CREATE `server/src/wispralt_server/config.py` — Pydantic Settings; validates env at startup; verifies `.env` mode is `0600` and owned by current user (warns loudly if not).
7. CREATE `server/src/wispralt_server/auth.py` — bearer middleware, `secrets.compare_digest`, key-rotation primitives.
8. CREATE `server/src/wispralt_server/audio.py` — pure data access: `decode_wav_bytes`, `split_channels`, `resample`.
9. CREATE `server/src/wispralt_server/dictate/parakeet.py` — pseudocode 1 (defensive return-type extraction; warm load).
10. CREATE `server/src/wispralt_server/routes/dictate.py` — multipart validation, `MAX_UPLOAD_BYTES` guard, calls service, returns `{text, model_id, duration_ms}`.
11. CREATE `server/src/wispralt_server/routes/health.py` — `/healthz`, `/readyz/dictation` (Parakeet warm), `/readyz/meeting` (worker models loaded — checks an in-memory ready flag set after pipeline-bootstrap).
12. CREATE `server/src/wispralt_server/routes/admin.py` — `POST /admin/rotate-key` (current-key-protected; returns new key once).
13. CREATE `server/src/wispralt_server/main.py` — FastAPI lifespan: load Parakeet + ALL meeting models (in same process; no separate worker), set ready flags, run `JobStore.recover_orphans()`, run `staging.sweep_old(>24h)`. Mount routes. Apply `require_api_key` to `/transcribe/*` and `/admin/*`.
14. UPDATE `docs/API.md` — full request/response schema for `/transcribe/dictate` + `/healthz`/`/readyz/*` + `/admin/rotate-key`. [DOC]
15. UPDATE `docs/ARCHITECTURE.md` — dictation latency budget, single-process model residency, single-thread executor, mic-exclusion rule. [DOC]

### Phase 2 — Server: meeting pipeline + jobs (5h, was 6h, no Redis)

16. [x] CREATE `server/src/wispralt_server/meeting/silence.py` — `is_silent_robust(audio, sr, threshold, frame_ms=100, silent_fraction=0.90)`. Threshold from `config.SILENCE_THRESHOLD`.
17. [x] CREATE `server/src/wispralt_server/meeting/deepfilter.py` — 16k↔48k around `df.enhance`.
18. [x] CREATE `server/src/wispralt_server/meeting/whisperx_loader.py` — module-level singletons: faster_CrisperWhisper (`device="cpu", compute_type="int8"`), `whisperx.load_align_model("en")`.
19. [x] CREATE `server/src/wispralt_server/meeting/diarize.py` — `PyannoteService`: gated-load, `.to(mps)`, `<2s` guard returns empty `Annotation`, `annotation_to_df()`.
20. [x] CREATE `server/src/wispralt_server/meeting/merge.py` — `merge_two_channels` chronological; `label_all(result, label, channel, raw_speakers)`; `label_others(result, base="Other", channel=2)`; `relabel_in_person(result, channel=None)`. Sets `overlap=true` when adjacent segments straddle. Added `build_speakers_table` helper.
21. [x] CREATE `server/src/wispralt_server/meeting/output.py` — `write_outputs_atomic(transcript, output_dir, job_id)`. Writes `{job_id}.{json,srt,vtt,txt}` via tempfile **in the same `output_dir`** → `os.replace`. SRT uses `Speaker: text` convention; **VTT uses `<v Speaker>text</v>` voice tags**; TXT is plain `[Speaker] text` lines.
22. [x] CREATE `server/src/wispralt_server/meeting/pipeline.py` — pseudocode 5. Also includes `bootstrap_models(hf_token)` and `is_ready()`. Also created: `meeting/__init__.py` (empty) and `_errors.py` (DiskFullError, CorruptAudioError, UploadTruncatedError, MeetingInProgressError).
23. CREATE `server/src/wispralt_server/jobs/store.py` — pseudocode 6: SQLite repo, `recover_orphans()`.
24. CREATE `server/src/wispralt_server/jobs/runner.py` — `enqueue(wav_path) -> job_id`; spawns `asyncio.create_task(asyncio.to_thread(transcribe_meeting, ...))`; updates `JobStore` set_running/set_done/set_error; **always calls `staging.cleanup(job_id)` in `finally`**.
25. CREATE `server/src/wispralt_server/ops/staging.py` — `stage(file_bytes) -> path` (writes to `STAGING_DIR/<uuid>.wav`); `cleanup(job_id)`; `sweep_old(max_age_hours=24)` removes orphans on startup.
26. CREATE `server/src/wispralt_server/routes/meeting.py` — defines all four endpoints in **one** task:
    - `POST /transcribe/meeting` — validates upload size (`MAX_UPLOAD_BYTES`, default 2GB), writes via `staging.stage`, calls `runner.enqueue`, returns `{job_id, status: "pending"}`.
    - `GET /transcribe/meeting/{job_id}` — status from `JobStore`.
    - `GET /transcribe/meeting/{job_id}/download/{format}` — streams `.json/.srt/.vtt/.txt`.
    - `DELETE /transcribe/meeting/{job_id}` — deletes server-side outputs + staging WAV (called by client after successful download).
    - **No `PATCH /speakers`** — rename is client-only.
27. WIRE routes into `main.py`.
28. UPDATE `docs/API.md` — meeting endpoint schemas, status state machine, `/admin/rotate-key`. [DOC]
29. UPDATE `docs/ARCHITECTURE.md` — meeting pipeline diagram, MPS vs CPU device matrix, model RAM table, in-process worker rationale. [DOC]
30. CREATE `docs/TRANSCRIPT-FORMAT.md` — locked JSON schema verbatim, SRT format note, VTT voice-tag format. [DOC]

### Phase 3 — Server: ops & deployment (3h)

31. CREATE `scripts/generate-api-key.sh` — `openssl rand -hex 32`; appends to `.env`; **`chmod 600 .env`** before exit.
32. CREATE `scripts/download-models.sh`:
    - `df -k $HOME` → require ≥8GB free.
    - `huggingface-cli whoami` (fail with actionable message if HF_TOKEN missing/invalid).
    - For each gated repo (`pyannote/speaker-diarization-3.1`, `pyannote/segmentation-3.0`): `huggingface-cli download <repo> --local-dir-use-symlinks=False --include "*.yaml"` (small probe). On 401, print exact accept-terms URL and exit.
    - Per-model `echo "Downloading <name> (~<size>)…"` (Parakeet ~1.2GB, faster_CrisperWhisper ~3.1GB, wav2vec2 align ~360MB, Pyannote ~800MB combined, DFN ~100MB).
    - Post-download size verification (model dir size within 10% of expected).
33. CREATE `scripts/setup-cloudflared.sh`:
    - `brew install cloudflared`.
    - Prompt for full subdomain URL (e.g. `https://transcribe.example.com`); persist as `SERVER_URL` in `.env`.
    - Read tunnel token from **stdin** (`read -s`); pass to `sudo cloudflared service install <token>`; **immediately `unset` the variable**; never write to `.env` or any file.
    - Verify `cloudflared service status` then `curl $SERVER_URL/healthz` (expect 401 from missing bearer — proves tunnel up).
34. CREATE `scripts/server-launchd.sh` — single `co.wispralt.server.plist` for FastAPI; KeepAlive=true; `ThrottleInterval=30` to avoid restart loops on persistent failure; logs to `~/Library/Logs/WisprAlt/`. **No worker plist, no Redis plist.** Includes `start`/`stop`/`uninstall` subcommands.
35. CREATE `scripts/setup-server.sh` — orchestrator: macOS check → Python 3.11 check → `uv venv` + `uv sync` → `download-models.sh` → `generate-api-key.sh` → `setup-cloudflared.sh` → `server-launchd.sh install` → `doctor.sh` → prints client config (`SERVER_URL`, `API_KEY`).
36. CREATE `scripts/doctor.sh` — checks: `.env` mode `0600` and owner; `SERVER_URL` set; `cloudflared service status`; `curl -fsS -H "Authorization: Bearer $API_KEY" $SERVER_URL/healthz`; both `/readyz/*`; tiny WAV roundtrip on `/transcribe/dictate`; SQLite reachable; staging dir writable.
37. CREATE `scripts/server-uninstall.sh` — unloads launchd, optionally removes venv + HF cache.
38. CREATE `server/README.md` quickstart. [DOC]
39. UPDATE `docs/SETUP-SERVER.md` — full walkthrough; HF token gating; key rotation procedure; Cloudflare tunnel body-limit notes. [DOC]
40. UPDATE `docs/TROUBLESHOOTING.md` — server issues (HF 401, CTranslate2 wheel build, cloudflared port conflict, large-upload Cloudflare limits, `.env` perms warning). [DOC]

### Phase 4 — Client: skeleton + permissions + settings (4h)

41. [x] CREATE `client/Package.swift` — macOS 14.0 deployment, Sparkle 2 dep (`https://github.com/sparkle-project/Sparkle`).
42. [x] CREATE `client/WisprAlt.xcodeproj` — DECISION: skipped checked-in .xcodeproj per plan escape clause ("If it's too complex, write it as Package.swift only and add a comment in client/README.md"). Documented in README: use `swift package generate-xcodeproj` or `swift build` directly. `scripts/build-client.sh` handles `xcodebuild` for CI/distribution.
43. [x] CREATE `client/WisprAlt/Info.plist` — `NSMicrophoneUsageDescription`, `NSAccessibilityUsageDescription`, `NSAppleEventsUsageDescription`, `SUFeedURL` (Sparkle), `SUPublicEDKey` (Sparkle), `SUAutomaticallyUpdate=false`, `LSUIElement=true`, `LSMinimumSystemVersion=14.0`.
44. [x] CREATE `client/WisprAlt/WisprAlt.entitlements` — `com.apple.security.device.audio-input`, `com.apple.security.network.client`, `com.apple.security.automation.apple-events`.
45. [x] CREATE `client/WisprAlt/Util/Logger.swift`.
46. [x] CREATE `client/WisprAlt/Storage/Settings.swift` — UserDefaults for `serverURL`, `meetingsPath` (default `~/Documents/WisprAlt/Meetings`), `holdMinDuration=0.30`, `tripleTapWindow=0.40`. **API key NOT here.**
47. [x] CREATE `client/WisprAlt/Storage/KeychainHelper.swift` — `kSecAttrService = "co.wispralt"`.
48. [x] CREATE `client/WisprAlt/App/PermissionGate.swift` — sequential 4-step wizard. **After Input Monitoring grant, on `#available(macOS 14.4, *)` show "Quit and Reopen Required" sheet with a `Quit Now` button before continuing.** Each missing permission has a `x-apple.systempreferences:` deep-link button.
49. [x] CREATE `client/WisprAlt/App/MenuBarController.swift` — `NSStatusItem`, mode states (idle / dictating / meetingRecording / uploading / processing). **Holds `meetingActive` flag; `tryStartDictation()` returns false + logs warning toast if `isMeetingActive`.**
50. [x] CREATE `client/WisprAlt/UI/SettingsView.swift` — server URL, API key (Keychain-backed), paths, **Test Connection** button (stub: `print("test connection — wired by Wave 1b")`).
51. [x] CREATE `client/WisprAlt/UI/PermissionsView.swift`.
52. [x] CREATE `client/WisprAlt/Update/SparkleController.swift` — `SPUStandardUpdaterController`, exposes "Check for Updates"; defers update sheet if `isMeetingActive`.
53. [x] CREATE `client/WisprAlt/App/AppDelegate.swift`, `WisprAltApp.swift`.
54. [x] UPDATE `docs/SETUP-CLIENT.md` — install + 4-permission walk; explicit note about Quit-Reopen step on 14.4+. [DOC]
55. [x] UPDATE `client/README.md` — build/run, project layout, .xcodeproj decision note. [DOC]

### Phase 5 — Client: hotkeys + dictation (3h)

56. CREATE `client/WisprAlt/Hotkeys/HotkeyEvents.swift` — protocol.
57. CREATE `client/WisprAlt/Hotkeys/FNKeyMonitor.swift` — pseudocode 2. Tap level **`kCGSessionEventTap`**, options **`.listenOnly`**, mask `flagsChanged | keyDown`. Serial private queue for state mutation. Tap-time = DOWN time. Clears `tapTimes` when hold confirmed.
58. CREATE `client/WisprAlt/Capture/AudioFormat.swift` — canonical formats; `convertCMSampleBufferTo16kMono(_) -> AVAudioPCMBuffer?` (stateful AVAudioConverter retained per stream).
59. CREATE `client/WisprAlt/Capture/AlignedRingBuffer.swift` — pseudocode 4 with full implementation.
60. CREATE `client/WisprAlt/Capture/DictationRecorder.swift` — AVAudioEngine ring buffer, **early-return if `MeetingRecorder.isActive`** (logs warning + UI toast).
61. [x] CREATE `client/WisprAlt/Server/ServerError.swift` — typed errors.
62. [x] CREATE `client/WisprAlt/Server/ServerClient.swift` — `URLSession` (background config for resumable meeting upload), bearer header injection, **upload-progress callback**, **one retry on connection-reset for dictation only** (meetings are too large to retry blindly), error mapping. Note: `backgroundSession` declared on ServerClient; per-upload sessions created inside `MeetingAPI.submit` for delegate-based progress.
63. [x] CREATE `client/WisprAlt/Server/DictationAPI.swift` — `transcribe(_ wav: Data) async throws -> String`.
64. [x] CREATE `client/WisprAlt/Inject/AccessibilityInjector.swift` — pseudocode 8.
65. [x] CREATE `client/WisprAlt/Inject/ClipboardInjector.swift` — Maccy-style save/restore; `changeCount` guard; skip `dyn.*` types.
66. [x] CREATE `client/WisprAlt/Inject/TextInjector.swift` — strategy combinator.
67. WIRE in `MenuBarController`: hold→record→send→inject; update icon per state.
68. UPDATE `docs/ARCHITECTURE.md` — client state machine diagram. [DOC]

### Phase 6 — Client: meeting recorder + transcript UI (5h)

69. CREATE `client/WisprAlt/Capture/MeetingRecorder.swift` — pseudocode 3 (locked startPTS, CMTimeSubtract, sample counters, gap padding via AlignedRingBuffer).
70. [x] CREATE `client/WisprAlt/Server/MeetingAPI.swift` — `submit(wav:) async throws -> JobID` (with upload progress via URLSessionDelegate), `poll(_:) async throws -> JobStatus`, `download(_:format:) async throws -> Data`, `delete(_:) async throws`. **No `renameSpeakers`.** Note: `download` returns `Data` (not `URL`) since the caller saves to disk at a path determined by `TranscriptStore`.
71. [x] CREATE `client/WisprAlt/Storage/TranscriptDocument.swift` — Codable JSON model matching locked v3 schema (speakers keyed by speaker_raw with display_name); `renameSpeaker(rawKey:to:)` with collision check; `toSRT()` (`Speaker: text`); `toVTT()` (`<v Speaker>text</v>`); `toTXT()`.
72. [x] CREATE `client/WisprAlt/Storage/TranscriptStore.swift` — `refresh()`, `load(_:)`, `renameSpeaker(in:rawKey:to:)` with sentinel+atomic writes (v3 P4#5). Index maps job_id → base URL. **No server call.**
73. [x] CREATE `client/WisprAlt/UI/TranscriptListView.swift`.
74. [x] CREATE `client/WisprAlt/UI/TranscriptDetailView.swift` — Rename Speakers sheet; offline-capable.
75. [x] CREATE `client/WisprAlt/UI/RecordingIndicatorView.swift` — `RecordingPhase` enum + five states (idle, recording with elapsed, uploading with %, processing with elapsed, done).
76. WIRE `toggleMeetingRecording` → start/stop → upload (with progress UI) → poll → download → call `MeetingAPI.delete(jobID)` → `TranscriptStore` refresh → notify.
77. [x] CREATE `client/WisprAlt/Util/Notifications.swift`.
78. UPDATE `docs/SETUP-CLIENT.md` — meeting recorder usage, rename UI offline note. [DOC]
79. UPDATE `docs/TRANSCRIPT-FORMAT.md` — explicit client-rename behavior, atomic-write contract, VTT vs SRT speaker convention. [DOC]

### Phase 7 — Build, sign, distribute, auto-update (3h)

80. CREATE `scripts/build-client.sh` — accepts `DEVELOPER_ID_APP` as `$1` (validates non-empty); `xcodebuild archive` → `exportArchive`; `codesign --force --deep --timestamp --options runtime --entitlements ... --sign "$DEVELOPER_ID_APP"`; `hdiutil create`; `codesign` DMG; **`xcrun notarytool submit --apple-id "$APPLE_ID" --password "$APP_SPECIFIC_PASSWORD" --team-id "$TEAM_ID" --wait`**; `xcrun stapler staple`. Generates `appcast.xml` snippet for Sparkle (signed with `$SPARKLE_ED_PRIVATE_KEY`).
81. CREATE `.github/workflows/build-client.yml` — `macos-14` runner, on `v*` tag. Imports cert from secrets `DEVELOPER_ID_APP_CERT_P12` + `DEVELOPER_ID_APP_CERT_PASSWORD`. Passes `APPLE_ID`, `APP_SPECIFIC_PASSWORD`, `TEAM_ID`, `DEVELOPER_ID_APP`, `SPARKLE_ED_PRIVATE_KEY` from secrets. Attaches DMG + appcast to GH Release.
82. CREATE `.github/ISSUE_TEMPLATE/bug_report.md`.
83. UPDATE `docs/SETUP-CLIENT.md` — DMG install, Gatekeeper notes, granting all four permissions, Quit-Reopen step. [DOC]
84. UPDATE `docs/TROUBLESHOOTING.md` — client issues (FN tap missed → check Input Monitoring; AX silent fail in Electron → falls back to clipboard; meeting upload progress stuck → check Cloudflare body limit). [DOC]
85. CREATE `docs/CONTRIBUTING.md` — required GH secrets list (`DEVELOPER_ID_APP`, `DEVELOPER_ID_APP_CERT_P12`, `DEVELOPER_ID_APP_CERT_PASSWORD`, `APPLE_ID`, `APP_SPECIFIC_PASSWORD`, `TEAM_ID`, `SPARKLE_ED_PRIVATE_KEY`); how to enroll in Apple Developer Program; ad-hoc fallback note. [DOC]

### Phase 8 — Claude Code commands & repo polish (2h)

86. CREATE `.claude/settings.json` — allowlist `Bash(brew:*)`, `Bash(uv:*)`, `Bash(launchctl:*)`, `Bash(cloudflared:*)`, `Bash(xcodebuild:*)`, `Bash(huggingface-cli:*)`, etc.
87. CREATE `.claude/commands/setup-server.md` — preflight checks, runs `setup-server.sh`, persists printed client config to `tmp/client-config.txt`.
88. CREATE `.claude/commands/setup-client.md` — macOS 14+ check; downloads latest Release DMG OR builds locally; opens System Settings panes for each of the 4 permissions; pastes config.
89. CREATE `.claude/commands/test-connection.md` — curl `/healthz`, both `/readyz/*`, tiny-WAV roundtrip on `/transcribe/dictate`.
90. CREATE `.claude/commands/docs-check.md` — diffs file→doc map (`docs/OVERVIEW.md`) against last-edit timestamps.
91. CREATE `.claude/commands/update-models.md` — runs `download-models.sh` **then unloads + reloads `co.wispralt.server.plist` via launchctl** (reloads in-memory model weights).
92. UPDATE `CLAUDE.md` — slash-command index, doc-update rule, never-push-without-approval reminder. [DOC]
93. UPDATE root `README.md` — 60s pitch, GIF placeholder, install steps, links to `docs/`. [DOC]
94. UPDATE `docs/README.md` index, `docs/CONTRIBUTING.md` finalize. [DOC]

### Phase 9 — End-to-end manual validation (1h, last)

95. Run `setup-server.sh` on actual Mac mini → green doctor.
96. Build client (`build-client.sh "$DEVELOPER_ID_APP"`), install DMG, complete 4-permission wizard incl. Quit-Reopen.
97. Test dictation in TextEdit, Chrome address bar, Slack, Terminal, VS Code.
98. Verify mic mutual exclusion: start meeting recording, then try to hold FN → no-op + UI toast.
99. Test meeting (remote): self-Zoom from secondary device → triple-tap → speak both sides → triple-tap → upload progress visible → JSON has `"You"` + `"Other"` + `mode: "remote"`.
100. Test meeting (in-person): no system audio playing → triple-tap → speak alone, then with another person → triple-tap → JSON has `"Speaker 1"`/`"Speaker 2"` + `mode: "in_person"`.
101. Test rename **offline**: turn off Wi-Fi, open transcript, rename "Other"→"Alice", verify all four files atomic-updated locally.
102. Test rename idempotency: re-run rename to same name; no errors.
103. Test sleep/wake mid-meeting: silence-padding holds alignment, transcript continuous.
104. Test long upload: 60+ minute meeting → upload progress smooth, server enforces `MAX_UPLOAD_BYTES`.
105. Test key rotation: `curl POST /admin/rotate-key`; old key returns 401; new key works after server hot-swap; `.env` rewritten and still mode 600.
106. Test `/update-models`: weights re-downloaded, server restarted, new readyz green.
107. Test friend share: paste URL+key on a fresh Mac → works.

---

## Integration Points

```yaml
SERVER:
  models_cache: "~/.cache/huggingface/hub  (HF_HOME override supported)"
  jobs_db:      "$HOME/Library/Application Support/WisprAlt/jobs.db"
  staging_dir:  "$HOME/Library/Application Support/WisprAlt/staging  (per-job WAV; cleaned in finally + 24h sweep)"
  outputs_dir:  "$HOME/Library/Application Support/WisprAlt/meetings  (server staging; client downloads then DELETEs)"
  logs:         "$HOME/Library/Logs/WisprAlt/server.log"
  launchd:      "~/Library/LaunchAgents/co.wispralt.server.plist  (single agent; no Redis/worker)"
  cloudflared:  "managed by `cloudflared service install`; token in macOS system keychain only"

CLIENT:
  meetings_local: "~/Documents/WisprAlt/Meetings/YYYY-MM-DD_HHMM_<title>.{json,srt,vtt,txt}"
  api_key_storage: "Keychain — service co.wispralt"
  settings:       "UserDefaults — co.wispralt.WisprAlt"

SECRETS:
  HF_TOKEN:               "server/.env (chmod 600); validated at setup time"
  WISPRALT_API_KEY:       "server/.env (chmod 600); rotated via /admin/rotate-key; client Keychain"
  CLOUDFLARE_TUNNEL_TOKEN: "stdin only during setup; persisted ONLY by cloudflared in system keychain"
  DEVELOPER_ID_APP:       "GH secret + local env var; validated non-empty in build-client.sh"
  APPLE_ID/APP_SPECIFIC_PASSWORD/TEAM_ID: "GH secrets; passed to notarytool via flags (NOT --keychain-profile)"
  SPARKLE_ED_PRIVATE_KEY: "GH secret; signs appcast for auto-updates"
```

---

## Validation Loop

```bash
# Server lint/type
cd server && uv run ruff check . && uv run pyright

# Server smoke
uv run python -c "from wispralt_server.dictate.parakeet import ParakeetService; s=ParakeetService(); s.load(); print('warm')"

# Server live roundtrip (requires real WISPRALT_API_KEY + HF_TOKEN)
uv run uvicorn wispralt_server.main:app --port 8000 &
sleep 8
curl -fsS -H "Authorization: Bearer $WISPRALT_API_KEY" http://localhost:8000/healthz
curl -fsS -H "Authorization: Bearer $WISPRALT_API_KEY" http://localhost:8000/readyz/dictation
curl -fsS -H "Authorization: Bearer $WISPRALT_API_KEY" http://localhost:8000/readyz/meeting
ffmpeg -f lavfi -i "sine=f=440:d=1" -ac 1 -ar 16000 -f wav -y /tmp/test.wav
curl -fsS -X POST -H "Authorization: Bearer $WISPRALT_API_KEY" \
   -F "file=@/tmp/test.wav;type=audio/wav" http://localhost:8000/transcribe/dictate
kill %1

# Client build
cd client && xcodebuild -project WisprAlt.xcodeproj -scheme WisprAlt -configuration Debug build

# Codesign verify (after build-client.sh)
codesign --verify --deep --strict --verbose=2 build/WisprAlt.app
spctl --assess --verbose=4 --type execute build/WisprAlt.app
xcrun stapler validate build/WisprAlt.dmg

# Docs sync
ls -la docs/*.md
```

## Final Validation Checklist

- [ ] All 30 reviewer items addressed (R1#1-#18, R2#1-#15, R3#1-#16; merged & deduped — see `Reviewer Items Resolved`).
- [ ] `ruff` + `pyright` clean.
- [ ] Xcode build succeeds.
- [ ] `setup-server.sh` end-to-end on fresh mini.
- [ ] DMG signed/notarized/stapled; CI workflow green.
- [ ] All 4 permissions wizard completes; Quit-Reopen step honored on 14.4+.
- [ ] Dictation in TextEdit/Chrome/Slack/Terminal/VS Code.
- [ ] Triple-tap zero false-positive in fn+arrow normal use.
- [ ] Meeting (remote) "You"+"Other"; meeting (in-person) "Speaker N".
- [ ] Mic mutual exclusion verified.
- [ ] Offline speaker rename: atomic, idempotent.
- [ ] Sleep/wake doesn't corrupt WAV.
- [ ] Long upload shows progress; cleanup happens after client DELETE.
- [ ] Key rotation works; `.env` stays 0600.
- [ ] `/update-models` reloads weights.
- [ ] No `[NEEDS CLARIFICATION]`.

---

## v3 Deltas (Pass 4 + 5 Resolutions — 30 NEW issues)

These are the **binding** changes layered on top of v2. Where they conflict with earlier text, this section wins.

### Concurrency & memory isolation (P4#3, P5#1, P5#11)

- **Meeting jobs run on a dedicated `ThreadPoolExecutor(max_workers=1)`** (not the default asyncio pool). Created in `runner.py` at startup. Lives for process lifetime.
- **One meeting at a time enforced via `asyncio.Semaphore(1)`** wrapping the dedicated executor submit. Second concurrent submission returns **HTTP 429** with body `{"error": "meeting in progress", "retry_after_s": <eta>}`.
- **Dictation degraded-mode signaling**: while `meeting_semaphore.locked()`, `/readyz/dictation` still returns 200 but adds header `X-Dictation-Degraded: true`. Client UI shows a subtle yellow indicator.
- **OOM guard**: lifespan adds `psutil>=6.0` to deps. `/readyz/meeting` returns 503 if `psutil.virtual_memory().available < 2 * 1024**3`. Same check at `runner.enqueue` — return 507 Insufficient Storage if disk OR memory below threshold.

### `AlignedRingBuffer` algorithm — fully specified (P4#1, P4#2, P5#8)

```swift
// AlignedRingBuffer.swift — locked behavior

final class AlignedRingBuffer {
    // Per-channel sorted insertion: Array<(start: Int, buffer: AVAudioPCMBuffer)>
    // Always kept sorted by `start` (binary insert).
    private var mic: [(start: Int, buf: AVAudioPCMBuffer)] = []
    private var sys: [(start: Int, buf: AVAudioPCMBuffer)] = []
    private var committedCursor: Int = 0          // last sample position written to file

    private let lock = os.os_unfair_lock_s()      // CRITICAL: append + flush concurrent on serial queue
    private let gapToleranceMs: Int = 200         // wall-clock fallback

    // Wall-clock force-flush: a DispatchSourceTimer in MeetingRecorder fires every 100ms and
    // calls `forceFlushIfStalled(now:)`. If a channel has been silent (no append) > gapTolerance,
    // pad it with silence up to the other channel's head and flush.

    func append(_ buf: AVAudioPCMBuffer, atSamplePos: Int, channel: AudioChannel) {
        let safePos = max(0, atSamplePos)         // CRITICAL P4#2: clamp negative offsets from PTS race
        // binary-insert into sorted array
    }

    /// Returns next aligned 2-channel chunk with overlapping samples from both channels.
    /// Algorithm:
    ///   1. let micEnd = mic.first?.end ?? committedCursor
    ///   2. let sysEnd = sys.first?.end ?? committedCursor
    ///   3. let target = min(micEnd, sysEnd)
    ///   4. If target <= committedCursor: nothing to flush, return nil
    ///   5. Pull samples from each channel covering [committedCursor, target);
    ///      where a channel has no chunk covering this range yet, return nil (wait for it)
    ///      UNLESS forceFlush=true (called on stop or wall-clock stall) → pad with zeros
    ///   6. Build 2-channel non-interleaved buffer; return.
    ///   7. Update committedCursor = target.
    func flushAligned(forceFlush: Bool) -> AVAudioPCMBuffer? { /* … */ }

    func padMissing(toEnd: Bool) { /* zero-fills lagging channel up to longest tail */ }
}
```

- **Single serial queue for both `.audio` and `.microphone` outputs in `MeetingRecorder`.** Replace `sampleHandlerQueue: .global()` with a private `DispatchQueue(label: "co.wispralt.meeting.io", qos: .userInteractive)`. Eliminates the `AVAudioFile` write race (P5#8) and removes the need for additional locks around `stereoFile.write`.
- **`AVAudioConverter` retained per-channel in `MeetingRecorder`** (P4#8). Two instances (`micConverter`, `sysConverter`) created in `start()`, used across all callbacks, released in `stop()`. `AudioFormat` exposes a small `AudioConverter` struct holding the converter + source format detection.

### File atomicity (P4#5, P4#11)

- **Server `output.py`**: `tempfile.NamedTemporaryFile(dir=output_dir, delete=False, suffix='.tmp')`. Explicit `os.chmod(tmp, 0o644)` then `os.replace(tmp, dest)`. Add `assert os.path.dirname(tmp) == os.path.abspath(output_dir)`.
- **`STAGING_DIR` MUST be on same filesystem as `MEETING_OUTPUT_DIR`** — `setup-server.sh` defaults both to `$HOME/Library/Application Support/WisprAlt/{staging,meetings}` (same APFS volume). `doctor.sh` adds `stat -f %d` comparison and warns if different.
- **Client `TranscriptStore.writeAtomic`**: drop `options: .atomic` from `data.write(to: tmp)` (redundant when followed by manual replace). Write all four formats (json/srt/vtt/txt) to `.{uuid}.tmp` files first, then `replaceItemAt` each in sequence. Add a `.transcriptWriteInProgress` sentinel before first replace, deleted after last; on app launch, scan for orphan sentinels and revert (delete partial outputs, keep originals).

### FN-key state machine (P4#12)

- **Hold timer fires on the private serial queue `q`, not on `.main`**. `q.asyncAfter(deadline: .now() + holdThreshold) { ... }`. Removes double-hop and any main-thread loading effects on hold detection. UI delegate calls (`dictationStart`, `dictationStop`, `toggleMeetingRecording`) still hop to `.main` for `@MainActor` compatibility.

### JobStore SQLite (P4#4, P5#2)

- **WAL mode + write lock**: in `JobStore.__init__`: `self.con.execute("PRAGMA journal_mode=WAL")` and `self.con.execute("PRAGMA synchronous=NORMAL")`. Wrap all writes in `self._write_lock = threading.Lock()`.
- **`recover_orphans()` policy**: only `running` → `failed`. `pending` jobs are **re-enqueued** on startup if their staging WAV file still exists; otherwise marked `failed` with reason `"staging file missing after restart"`.

### Meeting upload safety (P5#4, P5#15)

- **`POST /transcribe/meeting` streams to disk** rather than `await file.read()`:
  ```python
  async def stream_to_staging(file: UploadFile, max_bytes: int) -> Path:
      total = 0
      path = STAGING_DIR / f"{uuid4()}.wav"
      with open(path, "wb") as f:
          async for chunk in iter(lambda: file.file.read(1 << 20), b""):
              total += len(chunk)
              if total > max_bytes:
                  path.unlink(missing_ok=True)
                  raise HTTPException(413, "Upload too large")
              f.write(chunk)
      return path
  ```
- **WAV header validation in `staging.stage()`** before enqueueing: read first 12 bytes (`RIFF....WAVE`); read `data` chunk size from header; assert `header_data_size + 44 == file_size_on_disk`. On mismatch return **HTTP 422** with body `"upload truncated; please retry"`.
- **`Content-MD5` request header** required from client; server validates after streaming. Mismatch → 422.
- **Pre-flight `Content-Length` check** before streaming: if header > `MAX_UPLOAD_BYTES`, return 413 immediately.

### Setup-script robustness (P4#9)

- **`setup-server.sh` installs `ffmpeg`** via `brew install ffmpeg` (required by `librosa` audio backends and the validation-loop sine-wave test).
- **`download-models.sh` retry loop**: 3 attempts with 5s backoff for `huggingface-cli whoami` (distinguishing 401 auth from 429/network). Pass `--resume-download` to all `huggingface-cli download` calls. On 429 print "rate-limited; retrying in {backoff}s".
- **`doctor.sh` polls `/readyz/dictation`** with up to 12 retries × 5s before running the WAV roundtrip (P4#13). Cold-start model load can exceed 60s.

### Sleep/wake & graceful shutdown (P5#5)

- **SIGTERM handler in `main.py`**: sets `app.state.shutting_down = True`, marks all `running` jobs as `failed` with `error="server shutdown"`, calls `sys.exit(0)`.
- **LaunchAgent `ExitTimeOut: 15`** in `co.wispralt.server.plist` so macOS gives the SIGTERM handler 15s before SIGKILL.
- **macOS sleep/wake notification observer in client `MeetingRecorder`**: on `NSWorkspace.didWakeNotification`, log gap, call `aligned.padMissing(uptoNow:)` so silence-fill maintains alignment.

### Rate limiting & abuse protection (P5#6)

- **In-memory rate limiter middleware in `main.py`** (no Redis): `collections.deque` per `(client_ip, key_prefix)` with rolling 60s window.
  - Dictation: 60 req/min per IP.
  - Meeting submit: 4 per hour per IP.
  - Health/readyz: unlimited.
- Returns 429 with `Retry-After` header.

### Sparkle auto-update gating (P4#7)

- **Gate update prompts on `MeetingRecorder.isActive == false`**. SparkleController observes the recorder; if a meeting is recording, defer the update sheet until `stop()` completes.
- **Set `SUAutomaticallyUpdate = NO`** in `Info.plist` — never auto-relaunch; require user "Restart Now" click.
- **`docs/CONTRIBUTING.md`**: add a "Sparkle Key Management" section. Generate keys with `Sparkle/bin/generate_keys`. Public key in `Info.plist` is permanent; private key stored in 1Password vault, copied to GH secret. Rotation requires a major-version release with new key.

### Key rotation security (P4#6, P4#15)

- **`POST /admin/rotate-key` returns `{"rotated": true}` only**. New key written to **stdout** (captured by launchd to `~/Library/Logs/WisprAlt/server.log`) and to a chmod-600 file `~/Library/Application Support/WisprAlt/.last-rotation-key` that is deleted on next successful auth with the new key. Client retrieves via SSH/local read, never via API response.
- **CREATE `server/src/wispralt_server/ops/env_writer.py`** (NEW Task 7.5): `rewrite_env_var(path, key, value)` — reads file, replaces line, writes via `tempfile.NamedTemporaryFile(dir=os.path.dirname(path))`, `os.chmod(tmp, 0o600)` BEFORE `os.replace(tmp, path)`. Includes `verify_env_perms(path)` returning bool.

### Speaker schema fix (P4#10, P5#14)

- **`speakers` table keyed by `speaker_raw`** (stable pyannote label or `"mic"`), with `display_name` field inside each entry:
  ```jsonc
  "speakers": {
    "mic":         { "display_name": "You",     "channel": 1 },
    "SPEAKER_00":  { "display_name": "Other",   "channel": 2 },
    "SPEAKER_01":  { "display_name": "Other 2", "channel": 2 }
  }
  ```
  Each segment has both `speaker_raw` (lookup key) and `speaker` (denormalized current display name; rewritten on rename).
- **`renameSpeaker(raw:to:)` collision check**: `guard !speakers.values.contains(where: { $0.display_name == newName }) else { throw .speakerNameConflict }`.

### Filename timezone (P5#3)

- **Local time + UTC offset suffix**: `YYYY-MM-DD_HHMM<±HHMM>_<title>` (e.g. `2026-04-24_1543-0700_meeting`). Eliminates DST collision. Locked in `docs/TRANSCRIPT-FORMAT.md`.

### Display & device guards (P5#13, P4#14)

- **Client `MeetingRecorder.start()`**: `guard let primary = content.displays.first else { throw .noDisplayAvailable }` with actionable error.
- **Server `audio.py`**: catch `soundfile.SoundFileError` → mark job `failed` with `error="corrupted upload"` instead of crashing the pipeline.
- **`AVAudioFile` close ordering**: in client `MeetingRecorder.stop()`, ensure `stereoFile = nil` happens INSIDE the serial I/O queue's last block to flush header. On app force-quit, header may be invalid; server must handle gracefully (above).

### Observability (P5#9)

- **`GET /metrics` endpoint** (bearer-protected): returns
  ```jsonc
  {
    "parakeet": { "p50_ms": 142, "p95_ms": 218, "queue_depth": 0, "last_inference_at": "..." },
    "meeting": { "active": false, "completed_24h": 3, "failed_24h": 0, "current_eta_s": null },
    "memory": { "rss_mb": 7842, "available_mb": 5120 },
    "disk": { "free_gb": 142, "staging_count": 0 }
  }
  ```
- ParakeetService keeps a `collections.deque(maxlen=100)` of recent durations for percentiles.

### Client uninstall (P5#7)

- **CREATE `scripts/uninstall-client.sh`** + an in-app **Settings → Uninstall…** menu item that:
  1. Quits all transcription/meeting activity.
  2. Removes `~/Documents/WisprAlt/` (with confirmation dialog).
  3. Deletes Keychain item (service `co.wispralt`).
  4. Removes UserDefaults domain (`co.wispralt.WisprAlt`).
  5. Moves `WisprAlt.app` to Trash.

### Disk-full guards (P5#10)

- `staging.stream_to_staging()`: `shutil.disk_usage(STAGING_DIR).free` checked before write loop and after each chunk; abort with 507.
- `output.py`: catch `OSError` with `errno.ENOSPC` → typed `DiskFullError` → 507.
- `doctor.sh`: warns if `<4GB` free.

### Task ordering fix (P5#12)

- **Split Task 13 → 13a (Phase 1) and 13b (Phase 2)**:
  - **13a (Phase 1)**: CREATE `main.py` with FastAPI app, model loading lifespan (Parakeet + meeting models), route mounting. NO orphan recovery, NO staging sweep yet (those modules don't exist in Phase 1).
  - **13b (Phase 2, after Tasks 23–25)**: ADD lifespan hooks: `JobStore.recover_orphans()`, `staging.sweep_old(>24h)`, SIGTERM handler, rate-limit middleware. UPDATE `main.py`.

### Validation environment (P4#9 cont'd)

- Validation Loop block: replace `ffmpeg`-generated test WAV with a Python one-liner using `numpy + soundfile` so the validation works even if ffmpeg isn't yet installed:
  ```python
  python -c "import numpy as np, soundfile as sf; sf.write('/tmp/test.wav', np.zeros(16000, dtype='float32'), 16000)"
  ```

---

## Reviewer Items Resolved

R1#1 → Pseudocode 1 + Task 9 defensive return-type extraction. R1#2 → Tasks 24/25 staging cleanup `finally` + 24h sweep at startup. R1#3 → `/readyz/dictation` and `/readyz/meeting` split (Task 11). R1#4 → Task 72 atomic local rewrite (no server PATCH). R1#5 → Task 16 `SILENCE_THRESHOLD` from config. R1#6 → Task 59 explicit `AlignedRingBuffer` deliverable. R1#7 → Pseudocode 2 serial private queue. R1#8 → Task 57 `kCGSessionEventTap` + `.listenOnly` + mask spec. R1#9 → Task 26 single task for all four endpoints. R1#10 → Task 81 notarytool `--apple-id/--password/--team-id`. R1#11 → Task 49+60 mic mutual exclusion. R1#12 → Task 4 `librosa>=0.11`, `numpy>=2.0,<2.3`. R1#13 → Tasks 26+62+75 upload size limit + `uploading` state + progress UI. R1#14 → Tasks 33+36 `SERVER_URL` persisted in `.env`. R1#15 → Task 34 `ThrottleInterval=30`; Task 62 retry-on-reset for dictation. R1#16 → Locked decisions: SQLite-only (no Redis). R1#17 → Task 70 client `DELETE` after download. R1#18 → Pseudocode 3 `CMTimeSubtract` + per-channel sample counters.

R2#1 → Tasks 26+62+75 upload caps + progress + docs. R2#2 → Tasks 24/25/13 staging cleanup `finally` + startup sweep. R2#3 → Task 59 explicit `AlignedRingBuffer`. R2#4 → Pseudocode 3 `os_unfair_lock` for `startPTS`. R2#5 → Locked decision: no Redis. R2#6 → Task 12 `/admin/rotate-key`. R2#7 → Task 21 `tempfile in same dir`. R2#8 → Pseudocode 2 clear `tapTimes` on hold confirmed. R2#9 → Task 32 progress + integrity + HF gated probe. R2#10 → Task 33 token via stdin only. R2#11 → Task 48 macOS 14.4+ Quit-Reopen sheet. R2#12 → Task 80 `DEVELOPER_ID_APP` arg validated. R2#13 → Task 11 split `/readyz/*`. R2#14 → Task 21 + 71 emit VTT alongside SRT. R2#15 → Tasks 71+72 client-side rename (offline-capable).

R3#1 → Pseudocode 2 tap-time = DOWN. R3#2 → Pseudocode 3 `os_unfair_lock`. R3#3 → Tasks 24+25 + finally cleanup. R3#4 → Task 32 `huggingface-cli whoami` + gated probe. R3#5 → Task 11 split + meeting ready flag. R3#6 → Task 81 + Task 85 CONTRIBUTING.md. R3#7 → Tasks 71/72 client rename, `PATCH` removed. R3#8 → Task 59 explicit `AlignedRingBuffer` interface. R3#9 → Locked decisions: PATCH removed. R3#10 → Tasks 62+75 upload state. R3#11 → Task 33 token never in launchd plist. R3#12 → Task 36 `doctor.sh` `.env` perms check. R3#13 → Task 48 version-gated 14.4+. R3#14 → Locked decisions: Redis dropped. R3#15 → Locked Transcript JSON Schema section. R3#16 → Task 91 `/update-models` reloads launchd.

R4#1 → v3 deltas: AlignedRingBuffer fully specified with sorted queues + wall-clock fallback. R4#2 → v3 deltas: `max(0, atSamplePos)` clamp. R4#3 → v3 deltas: dedicated executor + asyncio.Semaphore + 429 + degraded-mode header. R4#4 → v3 deltas: WAL mode + write lock. R4#5 → v3 deltas: drop `.atomic` redundancy + sentinel file + same-FS assertion. R4#6 → v3 deltas: rotate returns `{"rotated":true}`, key in stdout/file. R4#7 → v3 deltas: Sparkle gate on `MeetingRecorder.isActive` + key management docs. R4#8 → v3 deltas: AVAudioConverter retained per-channel. R4#9 → v3 deltas: HF retry/resume + ffmpeg install + Python sine fallback. R4#10 → v3 deltas: `speakers` keyed by `speaker_raw` with `display_name`. R4#11 → v3 deltas: same-FS staging↔output assertion. R4#12 → v3 deltas: hold timer fires on private queue. R4#13 → v3 deltas: doctor.sh `/readyz` poll loop. R4#14 → v3 deltas: graceful AVAudioFile close + server SoundFileError handling + Content-MD5. R4#15 → v3 deltas: NEW Task 7.5 `ops/env_writer.py` with `rewrite_env_var`.

R5#1 → v3 deltas: dedicated executor + semaphore. R5#2 → v3 deltas: `recover_orphans` re-enqueues `pending`. R5#3 → v3 deltas: filename timezone offset. R5#4 → v3 deltas: streaming + WAV header validate + Content-MD5. R5#5 → v3 deltas: SIGTERM handler + ExitTimeOut + sleep/wake observer. R5#6 → v3 deltas: rate-limit middleware. R5#7 → v3 deltas: client uninstall script + in-app menu. R5#8 → v3 deltas: single serial queue for SCStream callbacks. R5#9 → v3 deltas: `/metrics` endpoint. R5#10 → v3 deltas: disk-full guards + 507. R5#11 → v3 deltas: psutil + memory guards. R5#12 → v3 deltas: split Task 13 → 13a/13b. R5#13 → v3 deltas: `displays.first` guard. R5#14 → v3 deltas: rename collision check. R5#15 → v3 deltas: streaming upload to disk.

---

## Known Gotchas (consolidated)

```python
# WhisperX: device="cpu" only (CTranslate2 has no MPS). Pyannote: .to(mps).
# Pyannote 3.3.2 returns Annotation, convert to DataFrame manually.
# Pyannote crashes on <2s audio; guard returns empty annotation.
# DeepFilterNet input MUST be 48kHz; resample 16k→48k→enhance→16k.
# torch==2.6.0; torchaudio 2.7+ breaks pyannote.
# whisperx==3.8.5 ONLY (multiple yanked versions on PyPI).
# librosa>=0.11 (numpy 2 compat).
# parakeet-mlx model.generate is NOT thread-safe — single-thread executor.
# parakeet-mlx return type is either Hypothesis (with .text) OR list of AlignedToken — handle both.
# First MLX inference triggers Metal kernel JIT (300ms-2s) — warmup mandatory.
# pyannote use_auth_token kwarg still works in 3.3.2 (don't switch to token=).
# uvicorn --workers 1 ALWAYS (multiple workers = multiple model copies).
```

```swift
// 0x800000 = .maskSecondaryFn (Globe key on M4 still wired here).
// kCGSessionEventTap + .listenOnly to NOT block normal FN combos.
// State mutation on serial queue, NOT directly on CGEventTap callback thread.
// captureMicrophone (macOS 14+) keeps mic+system in single clock domain via SCStream.
// Use CMTimeSubtract not .seconds float math (drift over hours).
// AVAudioConverter is stateful; always check outputBuf.frameLength > 0.
// AXUIElementSetAttributeValue may return .success and silently no-op (Electron) → read-back verify.
// Clipboard restore: skip dyn.* types; check pb.changeCount before overwriting (don't stomp user copy).
// CGRequestListenEventAccess on macOS 14.4+ requires process restart — block UI until restart.
// Hardened runtime + automation.apple-events entitlement mandatory.
// CI notarization: --apple-id/--password/--team-id (NOT --keychain-profile).
```

---

## Anti-Patterns to Avoid

- No backwards-compatibility shims (greenfield).
- No tests in v1 (manual Phase 9 validation gates).
- No mocks for HF / Pyannote / Cloudflare — real components in validation.
- No input validation inside services — at route boundaries only.
- No generic `except Exception` — typed errors.
- No SQL inline outside `JobStore`.
- No model loading per request.
- No Redis (locked: SQLite-only).
- No server-side speaker rename (locked: client-only).
- No PATCH endpoint for transcripts.
- No tunnel token written to `.env` or launchd plist.
- No pushing without explicit approval (per global rule).

---

## Deprecated Code Removal

N/A (greenfield).

---

## Confidence: 9.5/10

All 60 reviewer items resolved (R1#1–18, R2#1–15, R3#1–16, R4#1–15, R5#1–15). Five independent review passes have stabilized the plan. The v3 deltas section is binding wherever it conflicts with v1/v2 text. Remaining 0.5pt risk is purely environmental (macOS TCC quirks, Cloudflare regional latency, first-time HF token + Apple Developer enrollment friction, real-world meeting noise variability).
