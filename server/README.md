# WisprAlt Server

FastAPI-based transcription server for WisprAlt.

## Quick Start

Run the one-step setup script from the repo root:

```bash
./scripts/setup-server.sh
```

The script handles:

1. Python 3.11 and `uv` availability checks
2. Virtual-environment creation and dependency installation
3. HuggingFace token validation and gated-model acceptance
4. Model weight download (~5.6 GB total)
5. API-key generation and `.env` permissions hardening
6. Cloudflare Tunnel installation
7. LaunchAgent registration (auto-starts on login)
8. End-to-end health check via `doctor.sh`

For a detailed walkthrough see `../docs/SETUP-SERVER.md`.

**Note on `uv.lock`**: not committed yet. After installing `uv` (`brew install uv`), run `cd server && uv lock` to generate it. Commit the lockfile to ensure reproducible installs in CI.

## Manual Start (development)

```bash
cd server
cp .env.example .env
# fill in .env values
chmod 600 .env
uv sync
uv run uvicorn wispralt_server.main:app --port 8000 --workers 1
```

The server **must** run with `--workers 1` — multiple workers would load duplicate
model instances and exhaust the 16 GB unified memory budget.
