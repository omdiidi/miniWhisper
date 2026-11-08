---
title: Server Setup
---

# Server Setup

The host is installed and run by the `wispralt` CLI: `./install.sh --role host`
from a clone (or `wispralt setup --role host`). [INSTALL.md](INSTALL.md) is the
step-by-step guide and command reference; this page covers the host-specific
details: models, remote access, the `.env` keys and day-to-day operation.

## Model weights (no Hugging Face token needed)

Both models are public on Hugging Face, so no account or token is needed:

- `mlx-community/parakeet-tdt-0.6b-v2` (~2.5 GB) for dictation, loaded at startup.
- `mlx-community/whisper-large-v3-turbo` (~1.6 GB) for file transcription, loaded lazily on the first file job and evicted when idle.

`wispralt setup` downloads both into `~/Library/Application Support/WisprAlt/models`
(the service's `HF_HOME`), verifies the sha256 of each weight file against the
Hub's metadata at the resolved commit, and records that commit in
`install-state.json`. `wispralt update` re-verifies them. Without network a
rerun keeps the already-verified copy.

## Remote access

The service binds `127.0.0.1:<port>` only. Other Macs reach it through
`tailscale serve` (simplest) or the optional Cloudflare tunnel below. Either
way, put the public URL in `SERVER_URL` and run `wispralt restart`.

### Cloudflare tunnel (optional)

#### Prerequisites

- A domain (or subdomain) whose DNS is managed by Cloudflare (any plan, including free).
- Access to the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/).

#### Get a tunnel token

1. Log in to the Cloudflare Zero Trust dashboard.
2. In the left sidebar, click **Networks → Tunnels**.
3. Click **Create a tunnel**.
4. Choose **Cloudflared** as the connector type.
5. Name your tunnel (e.g. `wispralt-host`).
6. On the **Install connector** page, select **macOS** → copy the tunnel **token** (a long base64 string). Do **not** copy the `cloudflared service install` command — that system-daemon path is broken on macOS 14/15+ (see [DEPLOYMENT-NOTES.md](DEPLOYMENT-NOTES.md)). `scripts/setup-cloudflared.sh` installs a **user-level LaunchAgent** instead.
7. Under **Public Hostname**, add a route:
   - **Subdomain**: `transcribe` (or your preferred subdomain)
   - **Domain**: your domain (e.g. `yourdomain.com`)
   - **Type**: `HTTP`; **URL**: `localhost:8000`
8. Click **Save hostname**.

Run `scripts/setup-cloudflared.sh` after `wispralt setup --role host`. The tunnel token is passed via stdin (silent read — not echoed). The script stores the token at `~/.config/wispralt/cloudflare-token` (mode 0600) and configures the cloudflared LaunchAgent to read it via `--token-file` (cloudflared ≥ 2025.4.0). On older cloudflared versions, the token is inlined in the plist (also mode 0600). See [DEPLOYMENT-NOTES.md](DEPLOYMENT-NOTES.md) for token rotation procedures for both paths.

> **Network security model:** FastAPI binds exclusively to `127.0.0.1`; only `tailscale` or `cloudflared` has external network access. Rate limiting reads `CF-Connecting-IP` from Cloudflare's authoritative header. If you change the bind address for LAN testing, set `TRUST_FORWARDED_HEADERS=false` in `.env` to avoid IP spoofing in rate limiting.

## Environment Variables

All configuration is read from `~/.config/wispralt/.env` (mode 0600; the
LaunchAgent's working directory). `wispralt setup` creates it and only ever adds
missing keys. The server reads `.env` from its working directory and nowhere else
(real environment variables override it), and the startup permission check
(`ops.env_writer.find_env_path`) looks at that same file; a dev run started
elsewhere reads `./.env`. `server/.env.example` lists every key:

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATA_DIR` | Yes | — | Absolute path of the data root that holds `wispralt.db`, `outputs/` and `staging/`; `wispralt setup` writes `$HOME/Library/Application Support/WisprAlt/host` (or `--data-dir`). An empty or relative value is rejected. The server never creates it (setup does) and refuses to start if it is missing. |
| `SERVER_URL` | Yes | — | The URL clients use. Setup writes `http://127.0.0.1:<port>`; set your `tailscale serve` or tunnel URL here (or pass `--server-url`; on a rerun an explicit `--server-url` replaces it, and `--port` moves a loopback URL to the new port) and `wispralt restart` |
| `SERVER_LOG_PATH` | No | `~/Library/Logs/WisprAlt/wispralt.log` | The rotating application log; setup writes it so a re-rooted install logs inside its root |
| `OPENROUTER_API_KEY` | No | — | Needed for the optional weekly AI insights (OpenRouter). AI also stays off until an admin enables it at `/admin/settings` after the disclosure; the model, 30-day cap and timezone are set there too. When unset, no insights run starts. Server smart formatting (Mercury) was removed in Part 4. Get a key at https://openrouter.ai/keys. |
| `INSIGHTS_TIMEZONE` | No | host zone, else `UTC` | Default IANA timezone for insight weeks and charts when none is set at `/admin/settings`. (Part 6 removed `INSIGHTS_MAX_30D_COST_USD`, `INSIGHTS_SCHEDULE_*` and `INSIGHTS_CATCHUP_ENABLED`: the cap is a portal setting and the scheduler is an hourly due-check.) |
| `FILE_MODEL_IDLE_EVICTION_SECONDS` | No | `60` | Seconds the file model (MLX Whisper) may sit idle before it is evicted to reclaim memory |
| `MAX_UPLOAD_BYTES` | No | `2147483648` | Maximum upload size in bytes (default 2 GiB) |

The file is outside the repo and must remain mode 0600 — the server warns loudly at startup if it is not (`config.py:verify_env_perms`).

### Configuration Knobs

| Variable | Default | When to change |
|---|---|---|
| `DICTATE_RATE_PER_MIN` | `60` | Lower if you want stricter per-IP rate limiting on the dictation endpoint |
| `TRUST_FORWARDED_HEADERS` | `true` | Set to `false` if you expose FastAPI directly without Cloudflare Tunnel (e.g. LAN testing) to avoid IP spoofing in rate limiting |

### Migration

Jobs, dictations and insights now live in `DATA_DIR/wispralt.db` too (outputs in `DATA_DIR/outputs`, staged uploads in `DATA_DIR/staging`); `MEETING_OUTPUT_DIR`, `JOB_DB_PATH` and `STAGING_DIR` are gone and ignored if still in an old `.env`. There is no `jobs.db` upgrade path (the old host was wiped).

#### Local database migrations

Users, API keys and usage events live in `DATA_DIR/wispralt.db` (SQLite). The server applies the numbered migrations in `server/src/wispralt/migrations/` at startup (and `wispralt setup` does so before creating the first admin); each is recorded with its checksum in `schema_migrations`. There is no Supabase/Postgres step. `/readyz/db` returns 503 `schema_outdated` if a migration is missing.

---

## Daily Operations

| Task | Command |
|---|---|
| Is it healthy? | `wispralt status`, `wispralt doctor` |
| Logs | `tail -f ~/Library/Logs/WisprAlt/wispralt.log` (crash output: `server.err.log`) |
| Restart / stop / start | `wispralt restart`, `wispralt stop`, `wispralt start` |
| Update code and models | `wispralt update` |
| Lost admin key | `wispralt keys rotate-admin` (see [ADMIN.md](ADMIN.md)) |
| Backup / restore | `wispralt backup`, `wispralt restore DIR` |
| Uninstall | `wispralt uninstall` (keeps data), `--purge-data` (backs up, then deletes it) |

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md): it maps every `wispralt doctor` check to its fix and covers model download errors, Cloudflare body limits, disk full errors, and more.
