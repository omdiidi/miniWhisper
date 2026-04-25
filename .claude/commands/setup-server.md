---
description: Bootstrap the WisprAlt FastAPI server on a Mac mini (M-series, 16GB+) with Cloudflare Tunnel, model weights, launchd, and a generated API key.
---

# /setup-server

End-to-end interactive setup of the WisprAlt server.

## Pre-flight checks

Run these and STOP with an actionable error if any fail:

1. `sw_vers -productVersion` — require ≥ 13.0.
2. `python3.11 --version` — require 3.11.x. If missing, instruct: `brew install python@3.11`.
3. `brew --version` — if missing, instruct user to install Homebrew first (`https://brew.sh`).
4. `command -v cloudflared` — informational; `setup-cloudflared.sh` will install it if missing.
5. `command -v uv` — if missing, instruct: `brew install uv`.

## Brief the user on environment variables

Read `server/.env.example` and explain each variable to the user in your own words. The two they must supply manually are:

- **HF_TOKEN** — required. They must:
  1. Create a HuggingFace account at https://huggingface.co.
  2. Accept terms on https://huggingface.co/pyannote/speaker-diarization-3.1
  3. Accept terms on https://huggingface.co/pyannote/segmentation-3.0
  4. Generate a read token at https://huggingface.co/settings/tokens
- **Cloudflare Tunnel token** — required. They get this from the Cloudflare Zero Trust dashboard → Networks → Tunnels → Create a tunnel → save the install command (the long string after `cloudflared service install`).

## Run the orchestrator

`bash scripts/setup-server.sh`

The script handles: ffmpeg install, `uv sync`, model downloads (~5.6GB, with retries and per-model progress), API key generation (chmod 600), Cloudflare Tunnel registration (token via stdin, never written to .env), launchd registration with `ExitTimeOut=15`, and a final `doctor.sh` health check.

## After completion

The script prints a config snippet ending with `SERVER_URL=...` and `API_KEY=...`. **Save this to `tmp/client-config.txt`** (gitignored — already covered by `.gitignore`).

Tell the user:

> Server is up. To configure your client device, run `/setup-client` on that Mac.

If anything in the script failed, point them at `docs/TROUBLESHOOTING.md` and the relevant section.

## Manual escape hatches

If the user wants to skip parts:
- Models already downloaded → `SKIP_MODELS=1 bash scripts/setup-server.sh`
- Skip Cloudflare (LAN-only experimentation) → not recommended; document but warn.

## Never

- Do not push to GitHub without explicit user approval (per global rule).
- Do not paste the API key in chat history beyond the line that prints it once.
