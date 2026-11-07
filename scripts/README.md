# scripts/

One-step setup and operational scripts for WisprAlt.

## Typical run order (server setup)

```
git clone <repo> && cd wisprflowALT
./scripts/setup-server.sh          # runs all steps below automatically
```

`setup-server.sh` calls each script in order. You can also run them individually:

| Order | Script | Purpose |
|-------|--------|---------|
| 1 | `download-models.sh` | Download ~2.7 GB of model weights from Hugging Face (public, no token) |
| 2 | `generate-api-key.sh` | Generate `openssl rand -hex 32` key; write to `server/.env` (mode 0600) |
| 3 | `setup-cloudflared.sh` | Install cloudflared; configure HTTPS tunnel; persist `SERVER_URL` to `.env` |
| 4 | `server-launchd.sh install` | Register FastAPI server as a macOS LaunchAgent |
| 5 | `doctor.sh` | Verify all checks pass; run dictation round-trip |

## Script reference

### `setup-server.sh`
End-to-end orchestrator. Checks macOS ≥ 13.0, Python 3.11, Homebrew. Runs all
sub-scripts in order. Prints `SERVER_URL` + `API_KEY` at the end for pasting into
the client.

### `download-models.sh`
- Pre-flight: requires ≥ 4 GB free on `$HOME`
- Downloads (public repos, no token): Parakeet (~1.2 GB) for dictation and mlx-whisper large-v3-turbo (~1.6 GB) for file transcription; each download retries 3 times with a 5 s backoff
- Reports total HF cache size after completion

### `generate-api-key.sh`
Idempotent. Generates `openssl rand -hex 32`. Replaces existing `WISPRALT_API_KEY`
line in `server/.env` or appends it. Always sets `server/.env` to mode 0600.
Echoes the generated key once for copy-paste into the client.

### `setup-cloudflared.sh`
- Installs cloudflared via Homebrew if not present
- Removes any legacy `/Library/LaunchDaemons/com.cloudflare.cloudflared.plist` (the broken `sudo cloudflared service install` path on macOS 14/15)
- Prompts for the full `https://` tunnel URL; persists as `SERVER_URL` in `.env`
- Reads the Cloudflare Tunnel token via `read -s` (silent); persists it to `~/.config/wispralt/cloudflare-token` mode 0600 and unsets the shell variable immediately
- Generates a user-level LaunchAgent at `~/Library/LaunchAgents/co.wispralt.cloudflared.plist` with `RunAtLoad: true`, `KeepAlive: { SuccessfulExit: false, NetworkState: true }`, reading the token via `--token-file` on cloudflared ≥ 2025.4.0 (or inlined into a 0600 plist on older versions)
- Bootstraps via `launchctl bootstrap gui/$UID` (no sudo) — survives reboots
- Verifies tunnel via `curl $SERVER_URL/healthz`; explains HTTP 401 / 000 / 502 codes

### `server-launchd.sh`
Manages the `co.wispralt.server` LaunchAgent (FastAPI via uvicorn).

Subcommands: `install`, `start`, `stop`, `status`, `uninstall`

The generated plist includes:
- `ExitTimeOut: 15` — gives SIGTERM handler 15 s before SIGKILL
- `ThrottleInterval: 30` — prevents rapid restart loops
- `KeepAlive.SuccessfulExit: false` — restarts on crash, not on clean shutdown
- No secrets in the plist; `server/.env` is loaded by Pydantic Settings at startup

### `doctor.sh`
Health-check suite. Checks:
1. `server/.env` mode is 0600, owner is `$USER`
2. cloudflared service status
3. Disk free on `$HOME` (warns if < 4 GB)
4. `STAGING_DIR` and `MEETING_OUTPUT_DIR` on the same filesystem
5. `/healthz` — polls up to 60 s
6. `/readyz/dictation` — polls up to 60 s (Parakeet model load)
7. Dictation round-trip: generates a silent WAV with Python + POSTs to `/transcribe/dictate`
8. `/metrics` — pretty-prints JSON

The file-transcription model (MLX Whisper) loads lazily on the first file job and is evicted when idle, so doctor has no readiness check for it.

### `server-uninstall.sh`
Prompts before each destructive action:
- Unloads LaunchAgent
- Uninstalls cloudflared service
- Removes `server/.venv/`
- Removes `~/Library/Logs/WisprAlt/`
- Removes `~/Library/Application Support/WisprAlt/` (DB + staging + outputs)

HF model cache and `server/.env` are kept by default.

### `build-client.sh`
CI-ready script to build, sign, notarize, and staple the macOS client DMG.

Usage:
```bash
export APPLE_ID="you@example.com"
export APP_SPECIFIC_PASSWORD="xxxx-xxxx-xxxx-xxxx"
export TEAM_ID="XXXXXXXXXX"
./scripts/build-client.sh "Developer ID Application: Your Name (TEAMID)"
```

Optional: set `SPARKLE_ED_PRIVATE_KEY` to a key file path to generate an
appcast snippet at `client/build/appcast-snippet.xml`.

### `test-client.sh`
Builds the client and runs its Swift Testing suite (`swift build && swift test` in `client/`). On a CommandLineTools-only Mac it adds the flags that find the CLT `Testing.framework`. Exports `WISPRALT_KEYCHAIN_TESTS=1` unless `CI` is set. See `docs/TESTING.md`.

### `uninstall-client.sh`
Run on the **client** Mac (not the server). Removes:
1. Quits the running WisprAlt app
2. `~/Documents/WisprAlt/` — local transcripts (with confirmation)
3. UserDefaults domain `co.wispralt.WisprAlt`
4. Keychain item for service `co.wispralt`
5. `/Applications/WisprAlt.app` — moved to Trash via Finder
