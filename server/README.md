# WisprAlt Server

FastAPI-based transcription server for WisprAlt.

## Quick Start

From the repo root run `./install.sh --role host`. It installs this package as
the `wispralt` CLI (`uv tool install`, console script `wispralt.cli.main:main`)
and runs `wispralt setup`, which:

1. Checks the Mac and installs the Apple Silicon `[host]` extra
2. Writes `~/.config/wispralt/.env` (0600; only missing keys)
3. Downloads and sha256-verifies the models (Parakeet for dictation, MLX
   Whisper for file transcription; both public, no Hugging Face token needed)
4. Creates the database and the first admin (key shown once)
5. Registers the `co.wispralt.host` LaunchAgent and checks `/healthz`

The CLI code lives in `src/wispralt/cli/`. See `../docs/INSTALL.md` (command
reference) and `../docs/SETUP-SERVER.md` (host details).

Dependencies are locked in `uv.lock`; run `uv lock` here after changing `pyproject.toml`.

## Manual Start (development)

```bash
cd server
cp .env.example .env
# fill in .env values
chmod 600 .env
uv sync --extra host --extra dev
uv run --extra host uvicorn wispralt.main:app --port 8000 --workers 1
```

The server **must** run with `--workers 1` — multiple workers would load duplicate
model instances and exhaust the 16 GB unified memory budget.
