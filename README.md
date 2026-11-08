# WisprAlt

Hold-to-talk dictation and audio/video file transcription for macOS: hold FN, speak,
release, and the text appears at the cursor. A menubar app, plus an optional
self-hosted transcription server for teams that want audio to stay on their own hardware.

## Two modes

The app's popover has a **Mode** picker:

- **Online (OpenAI)**, the default: audio goes straight from your Mac to OpenAI
  (`gpt-transcribe` by default; other models and prices under Advanced) with your own
  API key. No server needed. File transcripts are saved as TXT + JSON.
- **Local (self-hosted)**: audio goes to a WisprAlt host you run on an Apple Silicon
  Mac: Parakeet TDT 0.6B (MLX) for dictation, mlx-whisper large-v3-turbo for files,
  a SQLite database, a web portal for people, keys, usage and history, and an
  [OpenAI-compatible `/v1` API](docs/OPENAI-COMPAT.md). Audio never leaves your
  network. File transcripts are saved as JSON, SRT, VTT and TXT.

Optional smart formatting (Mercury via OpenRouter) runs in the app in either mode and
sends text only.

## Install

macOS 14+. A host also needs Apple Silicon and about 6 GB of free disk.

```bash
git clone <repo-url> wispralt
cd wispralt
./install.sh                      # asks for the role, or:
./install.sh --role client        # the menubar app only (Online by default)
./install.sh --role host          # the Local server
./install.sh --role both          # a host plus the app, already connected
```

`install.sh` installs Homebrew (if missing), `ffmpeg`, `uv` and the `wispralt` CLI,
then runs `wispralt setup --role …`. It never uses `sudo` and is safe to rerun.
Afterwards `wispralt status`, `doctor`, `backup`, `update` and `uninstall` manage the
Mac; `wispralt --help` lists everything. Full guide: [docs/INSTALL.md](docs/INSTALL.md).

The app walks you through three macOS permissions (Accessibility, Input Monitoring,
Microphone).

### OpenAI key (Online mode)

The key lives only in the macOS Keychain. Paste it in the app's Settings > Advanced,
or add it from Terminal (it prompts for the key):

```bash
security add-generic-password -U -s co.wispralt.openai -a default -w
```

Use a project-scoped key with a monthly budget cap. See
[docs/SETUP-CLIENT.md](docs/SETUP-CLIENT.md#mode-online-openai-or-local-server).

## Docs

| Document | Description |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Install and the `wispralt` command reference |
| [docs/SETUP-CLIENT.md](docs/SETUP-CLIENT.md) | The app: modes, keys, permissions, building from source |
| [docs/SETUP-SERVER.md](docs/SETUP-SERVER.md) | Host details: models, remote access, `.env` |
| [docs/ADMIN.md](docs/ADMIN.md) | The portal: people, invites, integrations, insights, importing history |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, latency budget, model residency |
| [docs/API.md](docs/API.md) | HTTP endpoint reference |
| [docs/OPENAI-COMPAT.md](docs/OPENAI-COMPAT.md) | OpenAI Whisper API drop-in (`/v1/*`) |
| [docs/INTEGRATION-GUIDE.md](docs/INTEGRATION-GUIDE.md) | Third-party integration quickstart |
| [docs/TRANSCRIPT-FORMAT.md](docs/TRANSCRIPT-FORMAT.md) | Transcript JSON schema, SRT/VTT/TXT |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | `wispralt doctor` checks and common fixes |
| [docs/TESTING.md](docs/TESTING.md) | Running the test suites |
| [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) | PR workflow, CI secrets |
| [docs/OVERVIEW.md](docs/OVERVIEW.md) | Codebase map: which doc covers each file |

## Status

Alpha. Issues and pull requests welcome.

## License

MIT; see [LICENSE](LICENSE).
