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
3. Model weight download (~2.7 GB total: Parakeet for dictation, MLX Whisper
   for file transcription; both public, no Hugging Face token needed)
4. API-key generation and `.env` permissions hardening
5. Cloudflare Tunnel installation
6. LaunchAgent registration (auto-starts on login)
7. End-to-end health check via `doctor.sh`

For a detailed walkthrough see `../docs/SETUP-SERVER.md`.

**Note on `uv.lock`**: not committed yet. After installing `uv` (`brew install uv`), run `cd server && uv lock` to generate it. Commit the lockfile to ensure reproducible installs in CI.

## Manual Start (development)

```bash
cd server
cp .env.example .env
# fill in .env values
chmod 600 .env
uv sync
uv run uvicorn wispralt.main:app --port 8000 --workers 1
```

The server **must** run with `--workers 1` — multiple workers would load duplicate
model instances and exhaust the 16 GB unified memory budget.
