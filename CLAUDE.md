# WisprAlt — Claude Code Project Rules

## Documentation discipline

Every code change must update all docs listed in [docs/OVERVIEW.md](docs/OVERVIEW.md). That file contains the file-to-doc map: it tells you which `docs/*.md` file is responsible for documenting each source file. Never leave documentation out of sync with implementation.

## Push policy

Never push to GitHub without explicit approval from the user. Always show exactly what will be pushed (branch, commits, diff summary) and ask for confirmation first. This applies to all branches and remotes — no exceptions.

## Slash command index

These commands are defined in `.claude/commands/` and can be run with `/command-name` inside Claude Code:

| Command | Purpose |
|---|---|
| `/docs-check` | Diff the file→doc map against last-edit timestamps to find stale docs |

## Operating WisprAlt

Install from a clone with `./install.sh` (it installs the `wispralt` CLI and runs `wispralt setup`); then operate a Mac with `wispralt --help` (`status`, `doctor`, `start`/`stop`/`restart`, `users`, `keys`, `retention`, `backup`, `restore`, `update`, `uninstall`). Scripted and agent installs: `./install.sh --role host|client|both --non-interactive …` (see `docs/INSTALL.md`). For tests and throwaway installs set `WISPRALT_ROOT` and `WISPRALT_LABEL`; never run setup against the real host from a test.

## Key conventions

- Repository pattern: database queries in repository functions, business logic in services, validation at route boundaries only.
- No generic `except Exception` — use typed errors.
- No model loading per dictation request; dictation models are resident at startup. The file model (MLX Whisper) loads lazily on the first file job and is evicted when idle (async batch — load cost is invisible).
- No Redis and no Postgres: users, keys, usage events, file jobs and transcripts all live in the host's SQLite `DATA_DIR/wispralt.db`.
- Optional Cloudflare tunnel (`scripts/setup-cloudflared.sh`; `tailscale serve` is the simpler path): its token is stored in `~/.config/wispralt/cloudflare-token` (mode 0600)
  outside the repo and read by the cloudflared LaunchAgent via `--token-file`. Never
  committed, never logged. Rotation: see docs/DEPLOYMENT-NOTES.md.
- Client modes: Online (OpenAI, default) and Local (self-hosted server). Online never contacts the Local host; no OpenAI ↔ Local fallback. Testable client decisions live in `client/WisprAltCore` (Swift Testing, run by `scripts/test-client.sh`); app code only wires them.
- Client logs: an `Error` reaches a `Log` call only via `ErrorLog.describe`; never log transcript text, response bodies, server reasons, paths, URLs or keys (`scripts/check-log-privacy.py` enforces it).
- Secrets: the host config is `~/.config/wispralt/.env` (mode 0600; the optional `OPENROUTER_API_KEY` for weekly insights). There is no env admin key: API keys live hashed in `DATA_DIR/wispralt.db`, and a new key is shown once, on a terminal or in a new 0600 `--token-file` (`wispralt setup`, `wispralt keys rotate-admin`). Client keys in the Keychain only: Local `co.wispralt`, OpenAI `co.wispralt.openai`, OpenRouter `co.wispralt.openrouter` (client-side Mercury smart formatting). Never commit any of them.

@CLAUDE.local.md
