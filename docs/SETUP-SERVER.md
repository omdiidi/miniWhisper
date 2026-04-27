---
title: Server Setup
---

# Server Setup

This guide walks through installing the WisprAlt server on your Mac mini. The setup script handles everything end-to-end; this document explains each step and how to verify success.

---

## Prerequisites

- **macOS 13.0 or later** (macOS 14.0+ recommended for full feature parity with the client)
- **Apple Silicon M-series Mac** (M1/M2/M3/M4). MLX (Parakeet) and MPS (Pyannote) are Apple Silicon-only. An Intel Mac can serve as a development server but will be significantly slower.
- **Python 3.11** — exact version required. `python3.11 --version` must succeed.
- **Homebrew** — used to install `cloudflared`. Install at [brew.sh](https://brew.sh) if missing.
- **~8 GB free disk space** — model weights total ~5.6 GB; allow extra for staging WAVs.
- **A HuggingFace account** with accepted terms for two gated models (see below).
- **A domain on Cloudflare DNS** — the tunnel will serve at a subdomain you choose (e.g. `transcribe.yourdomain.com`).

> **Note on `uv.lock`**: not committed yet. After installing `uv` (`brew install uv`), run `cd server && uv lock` to generate it. Commit the lockfile to ensure reproducible installs in CI.

---

## HuggingFace Token and Gated Model Access

Pyannote speaker diarization requires accepting the model license agreements on HuggingFace before download.

### Step 1 — Create a HuggingFace account

Go to [huggingface.co/join](https://huggingface.co/join) if you do not have one.

### Step 2 — Accept terms for both gated models

You must accept terms on **both** model pages while logged in:

1. [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1) — click **Agree and access repository**.
2. [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0) — click **Agree and access repository**.

Accepting terms on one model is not sufficient; the pipeline downloads both.

### Step 3 — Generate an access token

1. Go to [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens).
2. Click **New token**.
3. Set **Type** to **Read** (write access is not needed).
4. Name it (e.g. `wispralt-server`) and click **Generate a token**.
5. Copy the token — you will paste it when `setup-server.sh` prompts for `HF_TOKEN`.

The setup script validates the token with `hf auth whoami` (the new HuggingFace CLI; the legacy `huggingface-cli` binary is deprecated upstream and removed from `huggingface_hub >= 0.30`) **and** performs a metadata fetch on both gated model URLs. If your token lacks gated access it will print the accept-terms URLs and exit cleanly.

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
| **1. Preflight checks** | macOS version ≥ 13; Python 3.11 present; `df -h` confirms ≥ 8 GB free disk; warns if Homebrew Redis is installed (not used, not required). |
| **2. Python environment** | `uv venv .venv` + `uv sync` inside `server/`. Installs all pinned dependencies from `pyproject.toml` including `torch==2.6.0`, `whisperx==3.4.0`, `pyannote.audio==3.3.2`, `parakeet-mlx==0.5.1`, plus `matplotlib>=3.8` (implicit dep of `pyannote.audio.utils.metric` — was missing previously and broke meeting bootstrap). DeepFilterNet was removed because `deepfilternet==0.5.6` pins `numpy<2.0` while `parakeet-mlx` requires `numpy>=2.2.5`; `meeting/deepfilter.py` is now a no-op stub. Test deps (`pytest`, `pytest-asyncio`, `httpx`) are installable via `uv sync --extra dev`. |
| **3. HuggingFace validation** | Prompts for `HF_TOKEN`. Runs `hf auth whoami` (new CLI) to confirm the token is valid. Fetches gated metadata for both pyannote models; prints accept-terms URLs and exits if access is denied. |
| **4. Model download** | Runs `download-models.sh`. Per-model size is echoed before download. Post-download size verification checks each weight file against expected byte ranges. Total: ~5.6 GB. |
| **5. API key generation** | Runs `generate-api-key.sh`: generates `secrets.token_hex(32)` (64 hex chars), writes `WISPRALT_API_KEY=<key>` to `server/.env`, runs `chmod 600 server/.env`. |
| **6. Cloudflare Tunnel setup** | Runs `setup-cloudflared.sh`: installs `cloudflared` via Homebrew if missing, prompts for your full `https://` tunnel URL (saved to `.env` as `SERVER_URL`), prompts silently for the tunnel token (`read -r -s`; token never echoed), stores token at `~/.config/wispralt/cloudflare-token` (mode 0600), generates `~/Library/LaunchAgents/co.wispralt.cloudflared.plist` (user-level, `RunAtLoad: true`, `KeepAlive`), bootstraps it via `launchctl bootstrap gui/$UID`, verifies with a retry loop. |
| **7. LaunchAgent registration** | Runs `server-launchd.sh install`: writes `~/Library/LaunchAgents/co.wispralt.server.plist` with `RunAtLoad: true` and `KeepAlive` set, bootstraps it with `launchctl bootstrap gui/$UID`, starts FastAPI on `http://127.0.0.1:8000`. The server starts automatically on every Mac mini reboot and restarts on crash. |
| **8. Health check** | Runs `doctor.sh` automatically (see Verification section below). |
| **9. Print client config** | Prints `SERVER_URL=...` and `API_KEY=...` for pasting into the client settings. |

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
- `GET /readyz/meeting` returns 200 (meeting models loaded + ≥ 2 GB RAM free).
- Model weight files on disk match expected sizes.
- `cloudflared` is listed in `launchctl list`.

A PASS on all checks confirms the server is production-ready.

---

## Environment Variables

All configuration is read from `server/.env` (mode 0600). The template is `server/.env.example`:

| Variable | Required | Default | Description |
|---|---|---|---|
| `HF_TOKEN` | Yes | — | HuggingFace read token for gated model downloads |
| `WISPRALT_API_KEY` | Yes | — | 64-char hex bearer token; generated by `generate-api-key.sh` |
| `SERVER_URL` | Yes | — | Full `https://` tunnel URL (e.g. `https://transcribe.yourdomain.com`) |
| `MEETING_OUTPUT_DIR` | Yes | — | Directory for completed transcript files (JSON/SRT/VTT/TXT) |
| `JOB_DB_PATH` | Yes | — | Path to SQLite job database file |
| `STAGING_DIR` | Yes | — | Temporary directory for in-flight WAV uploads |
| `SILENCE_THRESHOLD` | No | `0.002` | Per-frame RMS threshold for in-person mode detection |
| `MAX_UPLOAD_BYTES` | No | `2147483648` | Maximum upload size in bytes (default 2 GiB) |

Never commit `server/.env`. The `.gitignore` excludes it. The file must remain mode 0600 — the server warns loudly at startup if it is not (`config.py:verify_env_perms`).

### Configuration Knobs

| Variable | Default | When to change |
|---|---|---|
| `DICTATE_RATE_PER_MIN` | `60` | Lower if you want stricter per-IP rate limiting on the dictation endpoint |
| `MEETING_RATE_PER_HOUR` | `4` | Lower to reduce concurrency risk; raise if multiple users share one server |
| `TRUST_FORWARDED_HEADERS` | `true` | Set to `false` if you expose FastAPI directly without Cloudflare Tunnel (e.g. LAN testing) to avoid IP spoofing in rate limiting |

### Migration

On second startup, `jobs.db` is migrated to add the `attempts` column automatically (SQLite `ALTER TABLE`; idempotent — safe to restart multiple times).

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
3. Prompts before deleting model weights and meeting outputs.
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

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for detailed symptom/diagnosis/fix entries covering HuggingFace token errors, CTranslate2 build failures, Cloudflare body limits, disk full errors, and more.
