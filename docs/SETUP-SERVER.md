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
| **4. API key generation** | Runs `generate-api-key.sh`: generates `secrets.token_hex(32)` (64 hex chars), writes `WISPRALT_API_KEY=<key>` to `server/.env`, runs `chmod 600 server/.env`. |
| **5. Cloudflare Tunnel setup** | Runs `setup-cloudflared.sh`: installs `cloudflared` via Homebrew if missing, prompts for your full `https://` tunnel URL (saved to `.env` as `SERVER_URL`), prompts silently for the tunnel token (`read -r -s`; token never echoed), stores token at `~/.config/wispralt/cloudflare-token` (mode 0600), generates `~/Library/LaunchAgents/co.wispralt.cloudflared.plist` (user-level, `RunAtLoad: true`, `KeepAlive`), bootstraps it via `launchctl bootstrap gui/$UID`, verifies with a retry loop. |
| **6. LaunchAgent registration** | Runs `server-launchd.sh install`: writes `~/Library/LaunchAgents/co.wispralt.server.plist` with `RunAtLoad: true` and `KeepAlive` set, bootstraps it with `launchctl bootstrap gui/$UID`, starts FastAPI on `http://127.0.0.1:8000`. The server starts automatically on every Mac mini reboot and restarts on crash. |
| **7. Health check** | Runs `doctor.sh` automatically (see Verification section below). |
| **8. Print client config** | Prints `SERVER_URL=...` and `API_KEY=...` for pasting into the client settings. |

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
- A silent WAV round-trips through `POST /transcribe/dictate`.
- `/metrics` returns data.
- The file model (MLX Whisper) has no readiness probe: it loads lazily on the first file job and is evicted when idle.
- `cloudflared` is listed in `launchctl list`.

A PASS on all checks confirms the server is production-ready.

---

## Environment Variables

All configuration is read from `server/.env` (mode 0600). The template is `server/.env.example`:

| Variable | Required | Default | Description |
|---|---|---|---|
| `WISPRALT_API_KEY` | Yes | — | 64-char hex bearer token; generated by `generate-api-key.sh` |
| `SERVER_URL` | Yes | — | Full `https://` tunnel URL (e.g. `https://transcribe.yourdomain.com`) |
| `MEETING_OUTPUT_DIR` | Yes | — | Directory for completed file-job transcript files (JSON/SRT/VTT/TXT); the name is kept from the meeting era |
| `JOB_DB_PATH` | Yes | — | Path to SQLite job database file |
| `STAGING_DIR` | Yes | — | Temporary directory for in-flight WAV uploads |
| `SUPABASE_DATABASE_URL` | No | — | Postgres URL for the `wispralt` schema (multi-token auth + usage events). When unset, server falls back to env-var break-glass admin only. See [DEPLOY-TEAM.md](DEPLOY-TEAM.md) for the connection-string format and the schema-bootstrap migration. |
| `OPENROUTER_API_KEY` | No | — | Enables the **Smart formatting** toggle. When set, `/transcribe/dictate` requests with `X-Smart-Format: true` AND at least `SMART_FORMAT_MIN_WORDS` words are post-processed by OpenRouter Mercury 2 (~$0.0001 per cleanup, ~250ms added wall-clock, fail-soft on timeout/error). Cleanup includes punctuation, casing, filler removal, and bullet-list formatting; meaning preserved. When unset, the header is silently a no-op and the toggle in the macOS client appears to do nothing. Get a key at https://openrouter.ai/keys. |
| `SMART_FORMAT_MIN_WORDS` | No | `80` | Minimum word count for smart-formatting to engage. Below this the call short-circuits to raw Parakeet output. Targets long-form dictations (LLM prompts, notes) where the cleanup is actually visible; short utterances aren't worth the LLM round-trip. |
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

#### Postgres schema migrations

The `wispralt` schema in Supabase is versioned via `wispralt.schema_version`. Apply each migration once on initial setup, and run new migrations whenever you upgrade the server. Migrations live in `server/migrations/`:

| Version | File | Adds |
|---|---|---|
| v1 | `2026-04-27-v1-wispralt-schema.sql` | `wispralt.users` + `wispralt.usage_events` + `wispralt.schema_version` |
| v2 | `2026-04-27-v2-display-name.sql` | `display_name TEXT NULL` column on `wispralt.users` (CHECK 1–40 chars, no control chars) |

Apply v2 by either pasting it into Supabase Studio → SQL Editor → Run, or via the Supabase MCP:

```text
mcp__supabase__apply_migration   # paste contents of 2026-04-27-v2-display-name.sql
```

Verify both versions are recorded:

```sql
SELECT version, applied_at, notes FROM wispralt.schema_version ORDER BY version;
-- Expect rows for version 1 and 2.
```

The migration is idempotent (`ADD COLUMN IF NOT EXISTS` + `ON CONFLICT DO NOTHING` on the version row), so re-running is safe.

### Optional: enable smart formatting

Smart formatting (server-side cleanup via OpenRouter Mercury 2 — punctuation, casing, filler removal, and bullet-list formatting on dictations of at least 100 words; meaning preserved) is opt-in. To turn it on:

1. Get an OpenRouter key at https://openrouter.ai/keys. Pricing is roughly $0.0001 per cleanup; for a single employee dictating heavily, expect under $1/month.
2. Add `OPENROUTER_API_KEY=<key>` to `server/.env` (mode 0600 — never commit).
3. Restart the server (`bash scripts/server-launchd.sh stop && bash scripts/server-launchd.sh start`).

When the key is present, employees with the **Smart formatting** toggle ON in the macOS client get cleaned text. When the key is absent, the toggle silently does nothing — there is no client-visible error.

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

Rotate the API key without restarting the server:

```bash
# Read current key from .env
CURRENT_KEY=$(grep WISPRALT_API_KEY server/.env | cut -d= -f2)

# Rotate; new key is printed to server.log only (never in response body)
curl -X POST \
  -H "Authorization: Bearer $CURRENT_KEY" \
  https://transcribe.yourdomain.com/admin/rotate-key
```

The response body is `{"rotated": true}`. The new key is written to `server.log` and to `~/Library/Application Support/WisprAlt/.last-rotation-key` (mode 0600):

```bash
# Retrieve the new key
tail -20 ~/Library/Logs/WisprAlt/server.log | grep NEW_API_KEY
# or
cat ~/Library/Application\ Support/WisprAlt/.last-rotation-key
```

Update your client settings with the new key immediately; the old key is invalid the moment rotation completes.

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
