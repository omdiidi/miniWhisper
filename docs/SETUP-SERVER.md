---
title: Server Setup
---

# Server Setup

This guide walks through installing the WisprAlt server on your Mac mini. The setup script handles everything end-to-end; this document explains each step and how to verify success.

---

## Prerequisites

- **macOS 13.0 or later** (macOS 14.0+ recommended for full feature parity with the client)
- **Apple Silicon M-series Mac** (M1/M2/M3/M4). MLX (Parakeet and MLX Whisper) is Apple Silicon-only. An Intel Mac can serve as a development server but will be significantly slower.
- **Python 3.11** — exact version required. `python3.11 --version` must succeed.
- **Homebrew** — used to install `cloudflared`. Install at [brew.sh](https://brew.sh) if missing.
- **~4 GB free disk space** — model weights total ~2.7 GB; allow extra for staging uploads.
- **A domain on Cloudflare DNS** — the tunnel will serve at a subdomain you choose (e.g. `transcribe.yourdomain.com`).

> **Note on `uv.lock`**: not committed yet. After installing `uv` (`brew install uv`), run `cd server && uv lock` to generate it. Commit the lockfile to ensure reproducible installs in CI.

---

## Model weights (no Hugging Face token needed)

Both models are public on Hugging Face, so no account or token is needed:

- `mlx-community/parakeet-tdt-0.6b-v2` (~1.2 GB) for dictation, loaded at startup.
- `mlx-community/whisper-large-v3-turbo` (~1.6 GB) for file transcription, loaded lazily on the first file job and evicted when idle.

`setup-server.sh` fetches both through `download-models.sh` with the `hf` CLI from the server venv.

---

## Cloudflare Tunnel Setup

### Prerequisites

- A domain (or subdomain) whose DNS is managed by Cloudflare (any plan, including free).
- Access to the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/).

### Get a tunnel token

1. Log in to the Cloudflare Zero Trust dashboard.
2. In the left sidebar, click **Networks → Tunnels**.
3. Click **Create a tunnel**.
4. Choose **Cloudflared** as the connector type.
5. Name your tunnel (e.g. `wispralt-mini`).
6. On the **Install connector** page, select **macOS** → copy the tunnel **token** (a long base64 string). Do **not** copy the `cloudflared service install` command — that system-daemon path is broken on macOS 14/15+ (see [DEPLOYMENT-NOTES.md](DEPLOYMENT-NOTES.md)). The setup script installs a **user-level LaunchAgent** instead.
7. Under **Public Hostname**, add a route:
   - **Subdomain**: `transcribe` (or your preferred subdomain)
   - **Domain**: your domain (e.g. `yourdomain.com`)
   - **Type**: `HTTP`; **URL**: `localhost:8000`
8. Click **Save hostname**.

The tunnel token will be passed to `setup-cloudflared.sh` via stdin (silent read — not echoed). The script stores the token at `~/.config/wispralt/cloudflare-token` (mode 0600) and configures the cloudflared LaunchAgent to read it via `--token-file` (cloudflared ≥ 2025.4.0). On older cloudflared versions, the token is inlined in the plist (also mode 0600). See [DEPLOYMENT-NOTES.md](DEPLOYMENT-NOTES.md) for token rotation procedures for both paths.

> **Network security model:** FastAPI binds exclusively to `127.0.0.1:8000`; only `cloudflared` has external network access. Rate limiting reads `CF-Connecting-IP` from Cloudflare's authoritative header. If you change the bind address for LAN testing, set `TRUST_FORWARDED_HEADERS=false` in `.env` to avoid IP spoofing in rate limiting. Rate limiting reads `CF-Connecting-IP` from Cloudflare's authoritative header; if you bypass the tunnel for testing, the rate limiter falls back to direct client IP.

---

## Installation

Clone the repository on your Mac mini and run the setup script:

```bash
git clone https://github.com/yourusername/wisprflowALT.git
cd wisprflowALT
bash scripts/setup-server.sh
```

The script is interactive and will prompt you at each step. Keep the terminal visible; it prints the client config one-liner at the end.

---

## What `setup-server.sh` Does

The script runs these phases in order:

| Phase | What it does |
|---|---|
| **1. Preflight checks** | macOS version ≥ 13; Python 3.11 present; `df -h` confirms ≥ 4 GB free disk; warns if Homebrew Redis is installed (not used, not required). |
| **2. Python environment** | `uv venv .venv` + `uv sync` inside `server/`. Installs all pinned dependencies from `pyproject.toml` including `parakeet-mlx==0.5.1`, `mlx-whisper==0.4.2` (torch arrives only transitively through it) and `huggingface_hub>=1.12,<1.13`. The diarization stack (Pyannote, its torch audio companion, matplotlib) and the DeepFilterNet stub were removed with the meeting pipeline. **WhisperX/ctranslate2/faster-whisper were removed in Phase 10 (2026-05-10);** mlx-whisper replaces them and runs on Apple Silicon GPU. Test deps (`pytest`, `pytest-asyncio`, `httpx`) are installable via `uv sync --extra dev`. |
| **3. Model download** | Runs `download-models.sh`: Parakeet + MLX Whisper, public repos, 3 attempts per model. Per-model size is echoed before download. Total: ~2.7 GB. |
| **4. `.env` + first admin** | Copies `.env.example` to `server/.env` (0600) if missing and writes `DATA_DIR="$HOME/Library/Application Support/WisprAlt"` when the key is absent or empty (a relative value aborts setup). After the model download it runs `python -m wispralt.bootstrap --label owner` from `server/` in your terminal: this creates `DATA_DIR` (0700) and `wispralt.db`, creates the first admin and prints the **client config once** (server URL + admin token). The token is never written to disk; copy it then. It refuses a non-terminal stdout so no log captures it. |
| **5. Cloudflare Tunnel setup** | Runs `setup-cloudflared.sh`: installs `cloudflared` via Homebrew if missing, prompts for your full `https://` tunnel URL (saved to `.env` as `SERVER_URL`), prompts silently for the tunnel token (`read -r -s`; token never echoed), stores token at `~/.config/wispralt/cloudflare-token` (mode 0600), generates `~/Library/LaunchAgents/co.wispralt.cloudflared.plist` (user-level, `RunAtLoad: true`, `KeepAlive`), bootstraps it via `launchctl bootstrap gui/$UID`, verifies with a retry loop. |
| **6. LaunchAgent registration** | Runs `server-launchd.sh install`: writes `~/Library/LaunchAgents/co.wispralt.server.plist` with `RunAtLoad: true` and `KeepAlive` set, bootstraps it with `launchctl bootstrap gui/$UID`, starts FastAPI on `http://127.0.0.1:8000`. The server starts automatically on every Mac mini reboot and restarts on crash. |
| **7. Health check** | Runs `doctor.sh` automatically (see Verification section below). |
| **8. Client config** | Reminds you to enter the server URL and the token printed by the bootstrap step; setup itself never prints a key. |

---

## Verification

Run `doctor.sh` at any time to verify the server is healthy:

```bash
bash scripts/doctor.sh
```

`doctor.sh` checks:

- `server/.env` file mode is `0600` and owned by the current user.
- `GET /healthz` returns 200.
- `GET /readyz/dictation` returns 200 (Parakeet loaded).
- `GET /readyz/db` returns 200 (local SQLite DB answers, schema current).
- An enabled admin exists (`python -m wispralt.bootstrap --check`).
- Doctor needs no API key: every probe is unauthenticated and it never reads or prints a token.
- The file model (MLX Whisper) has no readiness probe: it loads lazily on the first file job and is evicted when idle.
- `cloudflared` is listed in `launchctl list`.

A PASS on all checks confirms the server is production-ready.

---

## Environment Variables

All configuration is read from `server/.env` (mode 0600). The template is `server/.env.example`:

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATA_DIR` | Yes | — | Absolute path of the data root that holds `wispralt.db`, `outputs/` and `staging/`; `setup-server.sh` writes `$HOME/Library/Application Support/WisprAlt`. An empty or relative value is rejected. The server never creates it (the bootstrap does) and refuses to start if it is missing. |
| `SERVER_URL` | Yes | — | Full `https://` tunnel URL (e.g. `https://transcribe.yourdomain.com`) |
| `MEETING_OUTPUT_DIR` | Yes | — | Directory for completed file-job transcript files (JSON/SRT/VTT/TXT); the name is kept from the meeting era |
| `JOB_DB_PATH` | Yes | — | Path to SQLite job database file |
| `STAGING_DIR` | Yes | — | Temporary directory for in-flight WAV uploads |
| `OPENROUTER_API_KEY` | No | — | Enables the weekly insights cron (Sunday-night LLM summary via OpenRouter). When unset, the cron is a no-op. Server smart formatting (Mercury) was removed in Part 4. Get a key at https://openrouter.ai/keys. |
| `FILE_MODEL_IDLE_EVICTION_SECONDS` | No | `60` | Seconds the file model (MLX Whisper) may sit idle before it is evicted to reclaim memory |
| `MAX_UPLOAD_BYTES` | No | `2147483648` | Maximum upload size in bytes (default 2 GiB) |

Never commit `server/.env`. The `.gitignore` excludes it. The file must remain mode 0600 — the server warns loudly at startup if it is not (`config.py:verify_env_perms`).

### Configuration Knobs

| Variable | Default | When to change |
|---|---|---|
| `DICTATE_RATE_PER_MIN` | `60` | Lower if you want stricter per-IP rate limiting on the dictation endpoint |
| `TRUST_FORWARDED_HEADERS` | `true` | Set to `false` if you expose FastAPI directly without Cloudflare Tunnel (e.g. LAN testing) to avoid IP spoofing in rate limiting |

### Migration

On second startup, `jobs.db` is migrated to add the `attempts` column automatically (SQLite `ALTER TABLE`; idempotent — safe to restart multiple times).

#### Local database migrations

Users, API keys and usage events live in `DATA_DIR/wispralt.db` (SQLite). The server applies the numbered migrations in `server/src/wispralt/migrations/` at startup (and `wispralt.bootstrap` does so before creating the first admin); each is recorded with its checksum in `schema_migrations`. There is no Supabase/Postgres step. `/readyz/db` returns 503 `schema_outdated` if a migration is missing.

---

## Daily Operations

### Viewing Logs

LaunchAgent stdout/stderr are written to `~/Library/Logs/WisprAlt/`:

```bash
tail -f ~/Library/Logs/WisprAlt/server.log
tail -f ~/Library/Logs/WisprAlt/server.error.log
```

Log format: `YYYY-MM-DD HH:MM:SS LEVEL    module_name — message`

### Restarting the Server

```bash
bash scripts/server-launchd.sh stop
bash scripts/server-launchd.sh start
```

Or use `launchctl` directly:

```bash
launchctl bootout gui/$UID ~/Library/LaunchAgents/co.wispralt.server.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/co.wispralt.server.plist
```

### Updating Models

Re-run the model download script then reload the server:

```bash
bash scripts/download-models.sh
bash scripts/server-launchd.sh stop
bash scripts/server-launchd.sh start
```

From Claude Code, use the `/update-models` slash command to run this automatically.

### Key Rotation

There is no env admin key. Employee and integration keys are rotated from the admin UI (`/admin/users/{id}/mint`, `/admin/keys/{id}/rotate`). A lost **admin** key is replaced in a terminal on the server:

```bash
cd server && .venv/bin/python -m wispralt.bootstrap --rotate-admin   # --user-id N if several admins
```

It revokes that admin's keys and prints the new token once (terminal only). `python -m wispralt.bootstrap --check` exits 0 when an enabled admin exists (`doctor.sh` uses it).

---

## Uninstall

```bash
bash scripts/server-uninstall.sh
```

The uninstall script:

1. Unloads and removes the `co.wispralt.server` LaunchAgent.
2. Removes the Python virtual environment (`server/.venv`).
3. Prompts before deleting logs and application data (job DB, staging, file-job outputs); the model cache is kept.
4. Does **not** uninstall `cloudflared` — manage that separately if desired.

To remove the cloudflared user LaunchAgent and binary:

```bash
launchctl bootout gui/$UID/co.wispralt.cloudflared 2>/dev/null || true
rm -f ~/Library/LaunchAgents/co.wispralt.cloudflared.plist
rm -f ~/.config/wispralt/cloudflare-token
brew uninstall cloudflared
```

---

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for detailed symptom/diagnosis/fix entries covering model download errors, Cloudflare body limits, disk full errors, and more.
