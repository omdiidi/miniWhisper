# WisprAlt

A self-hosted, privacy-first replacement for Wispr Flow. Sub-400ms dictation and audio/video file transcription running entirely on hardware you own — no cloud subscription, no audio leaving your network. Powered by Parakeet TDT 0.6B v2 (MLX) for dictation and mlx-whisper large-v3-turbo for files.

**OpenAI-compatible**: point any Whisper-API client at `https://transcribe.integrateapi.ai/v1`. See [docs/OPENAI-COMPAT.md](docs/OPENAI-COMPAT.md).

**v0.4.0 (May 2026)** adds opt-in **streaming dictation** — long dictations transcribe audio chunks in parallel with recording so only a short tail remains on FN release. Off by default; enable via Settings → "Streaming dictation (experimental — faster for long dictations)". Falls back to the standard path on any failure. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#streaming-dictation-opt-in-experimental).

![Demo](docs/demo.gif)

## Two modes

The macOS client has a **Mode** picker in its popover:

- **Online (OpenAI)** — the default. Dictation and file transcription go straight from your Mac to OpenAI (`gpt-transcribe` by default, $0.0045/min; other models with prices under Advanced), using your own key from the macOS Keychain. No server needed. Files are saved as TXT + JSON.
- **Local server** — everything goes to the self-hosted server described below, and audio never leaves your infrastructure. Files are saved as JSON, SRT, VTT and TXT.

Online sends nothing to your Local server. Optional smart formatting (Mercury via OpenRouter) runs in the client in both modes and sends text only. See [docs/SETUP-CLIENT.md](docs/SETUP-CLIENT.md#mode-online-openai-or-local-server).

## What it is

WisprAlt is a two-component system:

- **Server** (`server/`): A FastAPI service you run on your always-on Mac mini. It exposes two transcription endpoints: a low-latency dictation endpoint powered by Parakeet TDT 0.6B v2 (MLX, ~80–200ms warm inference) and a file transcription endpoint (any audio/video container) that runs mlx-whisper large-v3-turbo (Apple Silicon GPU) as a background job. The server is reachable from anywhere via a Cloudflare Tunnel — no port forwarding, no inbound firewall rules, no cloud subscription. See [docs/CHANGELOG-2026-05-10.md](docs/CHANGELOG-2026-05-10.md) for the WhisperX → mlx-whisper swap history.

- **Client** (`client/`): A native macOS menubar app (signed, notarized) that listens for your FN key. Hold FN to dictate; release to inject the transcribed text at the cursor. "Transcribe file…" in the menubar uploads an audio or video file and saves the transcript locally.

Your audio never leaves your own infrastructure. The server runs on hardware you own; the Cloudflare Tunnel is an outbound-only connection.

## Architecture

```
MacBook (client) ──FN hold──▶ Dictation Recorder ──WAV──▶ /transcribe/dictate
                                                                    │
                                                            Parakeet TDT 0.6B (MLX)
                                                                    │
                                                            text ──▶ AX inject / clipboard

MacBook (client) ──"Transcribe file…"──▶ audio/video file
                                        │ upload (chunked when large)
                               /transcribe/file ──▶ JobStore (SQLite)
                                        │
                              ffmpeg → mlx-whisper turbo (MLX, mono)
                                        │
                              JSON + SRT + VTT + TXT ──▶ /transcribe/file/{id}/download/{fmt}
```

The server runs a single uvicorn worker process. The dictation model (Parakeet) is resident in memory; the file model (MLX Whisper) loads lazily on the first file job and is evicted when idle. Dictation is serialized through a single-thread executor (MLX is not thread-safe). File jobs are limited to one concurrent job.

For the full architecture diagram and latency budget, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Features

- **Sub-400ms dictation** — hold FN, speak, release; text injected at cursor
- **File transcription** — any audio/video container (m4a, mp3, mp4, mov, wav, …); large files upload in chunks; cancel any time
- **Output formats** — Local: JSON (segment timestamps), SRT, VTT, TXT; Online: TXT + JSON
- **Privacy-first** — in Local mode your audio goes to your own Mac mini, not a cloud API; logs never carry transcript text
- **Free** — all models are open-source; no per-minute API fees
- **Auto-updates** — never while a dictation or file job is in flight
- **Cloudflare Tunnel** — access your Mac mini from anywhere; outbound-only, no port forwarding

## Comparison

| Feature | WisprAlt | Wispr Flow |
|---|---|---|
| **Privacy** | Audio stays on your hardware | Audio sent to cloud |
| **Cost** | Free (after hardware) | $15/month |
| **Dictation latency** | ~250–400ms p50 | ~200–500ms (varies) |
| **File transcription** | Yes — any audio/video file | No |
| **Platforms** | macOS 14+ | macOS, iOS, Windows |
| **Setup** | one command (`./install.sh`) | Instant (SaaS) |

## Status

**Alpha** — core dictation and file transcription are implemented and functional. This is a polished GitHub showcase project; production use is at your own discretion. Issues and pull requests welcome.

## Quickstart

> Prerequisites: macOS 14+. The host (the local transcription server) needs Apple Silicon and ~6 GB free disk. No Hugging Face account or token is needed (both models are public).

```bash
git clone <repo-url> wispralt
cd wispralt
./install.sh            # choose host, client or both
```

`install.sh` installs Homebrew (if missing), `ffmpeg`, `uv` and the `wispralt` CLI, then runs `wispralt setup`: models (verified), database, first admin, LaunchAgent and the menubar app. It is safe to rerun and never uses `sudo`. After that, `wispralt status`, `wispralt doctor`, `wispralt backup` and `wispralt update` run the Mac; `wispralt --help` lists every command.

The app walks you through 3 macOS permissions (Accessibility, Input Monitoring, Microphone), and you're ready to dictate.

Hold FN to dictate. Release to inject text at the cursor. Use "Transcribe file…" in the menubar to transcribe an audio or video file.

See [docs/INSTALL.md](docs/INSTALL.md) for the full install guide, the command reference and scripted (agent) installs.

Building from source (developers only): [docs/SETUP-CLIENT.md](docs/SETUP-CLIENT.md).

## Docs

| Document | Description |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, latency budget, model residency |
| [docs/INSTALL.md](docs/INSTALL.md) | Install and the `wispralt` command reference |
| [docs/SETUP-SERVER.md](docs/SETUP-SERVER.md) | Host details: models, remote access, `.env` |
| [docs/SETUP-CLIENT.md](docs/SETUP-CLIENT.md) | Client install, permissions, and config |
| [docs/API.md](docs/API.md) | HTTP endpoint reference |
| [docs/OPENAI-COMPAT.md](docs/OPENAI-COMPAT.md) | OpenAI Whisper API drop-in (`/v1/*`) — canonical reference |
| [docs/INTEGRATION-GUIDE.md](docs/INTEGRATION-GUIDE.md) | Third-party integration quickstart (Python / Node / curl) |
| [docs/TRANSCRIPT-FORMAT.md](docs/TRANSCRIPT-FORMAT.md) | Transcript JSON schema, SRT/VTT/TXT formats |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common issues and fixes |
| [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) | CI secrets, Sparkle key management, PR workflow |

## License

MIT — see [LICENSE](LICENSE).

## Links

- [Documentation](docs/)
- [Issues](../../issues)
- [Contributing](docs/CONTRIBUTING.md)
