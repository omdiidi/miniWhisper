# scripts/

Build, test and optional helper scripts. Installing and operating a host or
client is the `wispralt` CLI (`./install.sh` at the repo root, then
`wispralt --help`; see `docs/INSTALL.md`). The mini-era setup, launchd, doctor,
deploy, model-download and uninstall scripts were folded into it in Part 5.

## Script reference

### `setup-cloudflared.sh`
- Installs cloudflared via Homebrew if not present
- Removes any legacy `/Library/LaunchDaemons/com.cloudflare.cloudflared.plist` (the broken `sudo cloudflared service install` path on macOS 14/15)
- Optional (the host binds loopback; `tailscale serve` is the simpler path). Run it after `wispralt setup --role host`; it refuses when the host `.env` is missing
- Prompts for the full `https://` tunnel URL; persists as `SERVER_URL` in `~/.config/wispralt/.env` (then `wispralt restart`)
- Reads the Cloudflare Tunnel token via `read -s` (silent); persists it to `~/.config/wispralt/cloudflare-token` mode 0600 and unsets the shell variable immediately
- Generates a user-level LaunchAgent at `~/Library/LaunchAgents/co.wispralt.cloudflared.plist` with `RunAtLoad: true`, `KeepAlive: { SuccessfulExit: false, NetworkState: true }`, reading the token via `--token-file` on cloudflared ≥ 2025.4.0 (or inlined into a 0600 plist on older versions)
- Bootstraps via `launchctl bootstrap gui/$UID` (no sudo) — survives reboots
- Verifies tunnel via `curl $SERVER_URL/healthz`; explains HTTP 401 / 000 / 502 codes

### `build-client-local.sh`
Builds an Apple-Development-signed `client/build/WisprAlt.app`. `wispralt setup --role client` runs it and installs the result.

### `verify-openai-compat.sh`
Smoke test of the `/v1` OpenAI-compatible endpoints. `BASE_URL` defaults to the local host (`http://127.0.0.1:8000/v1`); `API_KEY` is required.

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
Builds the client and runs its Swift Testing suite (`swift build && swift test` in `client/`), then the log-privacy gate (`check-log-privacy.py --self-test`, then the scan). On a CommandLineTools-only Mac it adds the flags that find the CLT `Testing.framework`. Exports `WISPRALT_KEYCHAIN_TESTS=1` unless `CI` is set. See `docs/TESTING.md`.

### `check-log-privacy.py`
Python 3 scan of every `Log.info/debug/warning/error(` call in `client/WisprAlt` and `client/WisprAltCore` (multi-line calls joined; `//` and `///` comment lines skipped). Each `\(expr)` passes if it ends in `.count`, `.statusCode`, `.code` or `.rawValue` or is `ErrorLog.describe(…)`; otherwise it fails on transcript, error, URL, body, reason, file-name identifiers or `.path` / `.host` / `localizedDescription` / `absoluteString` / `errorDescription`, printing `file:line`. `--self-test` checks the checker against known leaks and known-safe lines. Exit 0 clean, 1 on any finding.

### `online-assumptions/run-all.sh`
**Costs real money (OpenAI).** Builds the `WisprAltProbe` developer tool and runs `preflight` (P5 Keychain read, P1 ten-minute clip report) and `suite-file` (every file model on a short clip, one direct m4a upload). The probe reads the key from the Keychain itself, enforces the spend caps and appends every call to `tmp/mission/part3/openai-spend.jsonl`. Exit 0 PASS / 1 FAIL / 2 BLOCKED. See `docs/TESTING.md`.
