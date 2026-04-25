# WisprAlt — Codebase Overview & File-to-Doc Map

This file is the single source of truth for which documentation file covers each source file. Every code change must consult this map and update the listed doc.

## Root files

| File | Covers |
|---|---|
| `README.md` | Top-level project pitch, architecture diagram, quickstart links |
| `CLAUDE.md` | Claude Code project rules, slash command index, key conventions |
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
| `server/src/wispralt_server/auth.py` | [API.md](API.md) — bearer auth, key rotation |
| `server/src/wispralt_server/audio.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — audio decode/resample pipeline |
| `server/src/wispralt_server/main.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — startup lifecycle, route mounting |
| `server/src/wispralt_server/dictate/parakeet.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — Parakeet service, warm load, single-thread executor |
| `server/src/wispralt_server/routes/dictate.py` | [API.md](API.md) — `/transcribe/dictate` endpoint |
| `server/src/wispralt_server/routes/health.py` | [API.md](API.md) — `/healthz`, `/readyz/dictation`, `/readyz/meeting` |
| `server/src/wispralt_server/routes/admin.py` | [API.md](API.md) — `/admin/rotate-key` |
| `server/src/wispralt_server/routes/meeting.py` | [API.md](API.md) — meeting POST/GET/download/DELETE endpoints |
| `server/src/wispralt_server/meeting/silence.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — in-person mode detection |
| `server/src/wispralt_server/meeting/deepfilter.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — noise reduction pipeline |
| `server/src/wispralt_server/meeting/whisperx_loader.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — WhisperX/CrisperWhisper singleton |
| `server/src/wispralt_server/meeting/diarize.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — Pyannote diarization, MPS device |
| `server/src/wispralt_server/meeting/merge.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — segment merging, speaker labeling |
| `server/src/wispralt_server/meeting/output.py` | [TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md) — atomic output write, SRT/VTT/TXT formats |
| `server/src/wispralt_server/meeting/pipeline.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — full meeting pipeline orchestration |
| `server/src/wispralt_server/jobs/store.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — SQLite job store, orphan recovery |
| `server/src/wispralt_server/jobs/runner.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — asyncio.to_thread runner, semaphore |
| `server/src/wispralt_server/ops/staging.py` | [ARCHITECTURE.md](ARCHITECTURE.md) — staging area management |
| `server/src/wispralt_server/ops/env_writer.py` | [SETUP-SERVER.md](SETUP-SERVER.md) — atomic .env rewrite, key rotation |
| `server/src/wispralt_server/middleware/rate_limit.py` | [ARCHITECTURE.md](ARCHITECTURE.md), [API.md](API.md) — per-IP rate limiting middleware |

## Client (`client/`)

| File | Covered by |
|---|---|
| `client/Package.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — macOS 14.0 target, Sparkle 2 dependency |
| `client/README.md` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — client build and run |
| `client/WisprAlt/Info.plist` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — permission usage descriptions, Sparkle config |
| `client/WisprAlt/WisprAlt.entitlements` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — required entitlements |
| `client/WisprAlt/App/AppDelegate.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — app lifecycle |
| `client/WisprAlt/App/MenuBarController.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — state machine, mic exclusion |
| `client/WisprAlt/App/PermissionGate.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — 4-permission wizard, 14.4+ restart |
| `client/WisprAlt/Hotkeys/FNKeyMonitor.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — FN key state machine |
| `client/WisprAlt/Hotkeys/HotkeyEvents.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — delegate protocol |
| `client/WisprAlt/Capture/DictationRecorder.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — AVAudioEngine dictation |
| `client/WisprAlt/Capture/MeetingRecorder.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — SCStream dual-channel capture |
| `client/WisprAlt/Capture/AlignedRingBuffer.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — sample-aligned ring buffer |
| `client/WisprAlt/Capture/AudioFormat.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — format conversion, CMSampleBuffer→AVAudioPCMBuffer |
| `client/WisprAlt/Server/ServerClient.swift` | [API.md](API.md) — URLSession, multipart upload, progress |
| `client/WisprAlt/Server/DictationAPI.swift` | [API.md](API.md) — dictation client |
| `client/WisprAlt/Server/MeetingAPI.swift` | [API.md](API.md) — meeting submit/poll/download/delete |
| `client/WisprAlt/Server/ServerError.swift` | [API.md](API.md) — typed errors |
| `client/WisprAlt/Inject/TextInjector.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — injection strategy |
| `client/WisprAlt/Inject/AccessibilityInjector.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — AX injection with read-back |
| `client/WisprAlt/Inject/ClipboardInjector.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — clipboard fallback |
| `client/WisprAlt/Storage/Settings.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — UserDefaults keys |
| `client/WisprAlt/Storage/KeychainHelper.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — API key in Keychain |
| `client/WisprAlt/Storage/TranscriptStore.swift` | [TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md) — local file index, atomic rewrites |
| `client/WisprAlt/Storage/TranscriptDocument.swift` | [TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md) — JSON model, speaker rename |
| `client/WisprAlt/Update/SparkleController.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — auto-update via Sparkle 2 |
| `client/WisprAlt/UI/SettingsView.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — settings UI |
| `client/WisprAlt/UI/PermissionsView.swift` | [SETUP-CLIENT.md](SETUP-CLIENT.md) — permissions UI |
| `client/WisprAlt/UI/TranscriptListView.swift` | [TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md) — transcript list |
| `client/WisprAlt/UI/TranscriptDetailView.swift` | [TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md) — rename UI, offline |
| `client/WisprAlt/UI/RecordingIndicatorView.swift` | [ARCHITECTURE.md](ARCHITECTURE.md) — uploading/processing/done states |
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
| `scripts/build-client.sh` | [SETUP-CLIENT.md](SETUP-CLIENT.md) |
| `scripts/uninstall-client.sh` | [SETUP-CLIENT.md](SETUP-CLIENT.md) |

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
