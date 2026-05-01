# WisprAlt

A self-hosted, privacy-first replacement for Wispr Flow. Sub-400ms dictation and speaker-diarized meeting transcription running entirely on hardware you own — no cloud subscription, no audio leaving your network. Powered by Parakeet TDT 0.6B v2 (MLX) for dictation and WhisperX + Pyannote 3.1 for meetings.

![Demo](docs/demo.gif)

## What it is

WisprAlt is a two-component system:

- **Server** (`server/`): A FastAPI service you run on your always-on Mac mini. It exposes two transcription endpoints: a low-latency dictation endpoint powered by Parakeet TDT 0.6B v2 (MLX, ~80–200ms warm inference) and a meeting transcription endpoint that runs DeepFilterNet noise reduction, WhisperX with CrisperWhisper, and Pyannote 3.1 speaker diarization. The server is reachable from anywhere via a Cloudflare Tunnel — no port forwarding, no inbound firewall rules, no cloud subscription.

- **Client** (`client/`): A native macOS menubar app (signed, notarized) that listens for your FN key. Hold FN to dictate; release to inject the transcribed text at the cursor. Triple-tap FN within 400ms to start or stop a dual-channel meeting recording that captures both your microphone and system audio. Speaker rename happens entirely on-device — no server round-trip, works offline.

Your audio never leaves your own infrastructure. The server runs on hardware you own; the Cloudflare Tunnel is an outbound-only connection.

## Architecture

```
MacBook (client) ──FN hold──▶ Dictation Recorder ──WAV──▶ /transcribe/dictate
                                                                    │
                                                            Parakeet TDT 0.6B (MLX)
                                                                    │
                                                            text ──▶ AX inject / clipboard

MacBook (client) ──FN 3-tap─▶ Meeting Recorder (SCStream dual-channel)
                                        │ 2-ch 16kHz WAV
                               /transcribe/meeting ──▶ JobStore (SQLite)
                                        │
                              DeepFilterNet → WhisperX (CPU) → Pyannote (MPS)
                                        │
                              JSON + SRT + VTT + TXT ──▶ client download + local rename
```

The server runs a single uvicorn worker process. All models are resident in memory (~7.3 GB combined). Dictation is serialized through a single-thread executor (MLX is not thread-safe). Meeting jobs are limited to one concurrent job via an asyncio semaphore.

For the full architecture diagram and latency budget, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Features

- **Sub-400ms dictation** — hold FN, speak, release; text injected at cursor
- **Dual-channel meeting recording** — triple-tap FN to capture mic + system audio simultaneously via SCStream
- **Speaker diarization** — Pyannote 3.1 with WhisperX word alignment; "You" / "Other" labels in remote mode; "Speaker N" labels in in-person mode
- **In-person mode auto-detection** — frame-based RMS silence check on system audio channel
- **Offline speaker rename** — rename speakers locally without any server round-trip; atomic file rewrite
- **Four output formats** — JSON (with word-level timestamps), SRT, VTT, TXT
- **Noise reduction** — DeepFilterNet 3 applied before transcription
- **Privacy-first** — your audio goes to your own Mac mini, not a cloud API
- **Free** — all models are open-source; no per-minute API fees
- **Auto-updates** — Sparkle 2 EdDSA-signed appcast; never during an active meeting
- **Cloudflare Tunnel** — access your Mac mini from anywhere; outbound-only, no port forwarding

## Comparison

| Feature | WisprAlt | Wispr Flow |
|---|---|---|
| **Privacy** | Audio stays on your hardware | Audio sent to cloud |
| **Cost** | Free (after hardware) | $15/month |
| **Dictation latency** | ~250–400ms p50 | ~200–500ms (varies) |
| **Meeting recording** | Yes — dual-channel, diarized | No |
| **Speaker rename** | Yes — offline, atomic | N/A |
| **Platforms** | macOS 14+ | macOS, iOS, Windows |
| **Setup** | ~20 minutes (one script) | Instant (SaaS) |

## Status

**Alpha** — core dictation and meeting transcription are implemented and functional. This is a polished GitHub showcase project; production use is at your own discretion. Issues and pull requests welcome.

## Quickstart — Server

> Prerequisites: macOS 13+, Python 3.11, ~8 GB free disk, Homebrew, a HuggingFace account with gated-model access accepted for `pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0`.

```bash
git clone https://github.com/yourusername/wisprflowALT.git
cd wisprflowALT
./scripts/setup-server.sh
```

The script handles everything: Python venv, model downloads (~5.6 GB), API key generation, Cloudflare Tunnel setup (token read from stdin — never written to disk), and launchd registration. When it finishes it prints a one-liner you paste into the client.

Full walkthrough: [docs/SETUP-SERVER.md](docs/SETUP-SERVER.md)

## Quickstart — Client (employees)

Open Terminal on the Mac you want to dictate from, paste this, hit enter:

```bash
curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh \
  | WISPRALT_API_KEY=sk_xxx WISPRALT_SERVER=https://transcribe.integrateapi.ai bash
```

The app opens, walks you through 4 macOS permissions (Accessibility, Input Monitoring, Microphone, Screen Recording), and you're ready to dictate.

Hold FN to dictate. Release to inject text at the cursor. Triple-tap FN quickly to start a meeting recording.

See [docs/INSTALL.md](docs/INSTALL.md) for the full install guide and troubleshooting.

Building from source (developers only): [docs/SETUP-CLIENT.md](docs/SETUP-CLIENT.md).

## Docs

| Document | Description |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, latency budget, model residency |
| [docs/SETUP-SERVER.md](docs/SETUP-SERVER.md) | Full server setup walkthrough |
| [docs/SETUP-CLIENT.md](docs/SETUP-CLIENT.md) | Client install, permissions, and config |
| [docs/API.md](docs/API.md) | HTTP endpoint reference |
| [docs/TRANSCRIPT-FORMAT.md](docs/TRANSCRIPT-FORMAT.md) | Transcript JSON schema, SRT/VTT/TXT formats |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common issues and fixes |
| [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) | CI secrets, Sparkle key management, PR workflow |

## License

MIT — see [LICENSE](LICENSE).

## Links

- [Documentation](docs/)
- [Issues](../../issues)
- [Contributing](docs/CONTRIBUTING.md)
