# WisprAlt — Claude Code Project Rules

## Documentation discipline

Every code change must update all docs listed in [docs/OVERVIEW.md](docs/OVERVIEW.md). That file contains the file-to-doc map: it tells you which `docs/*.md` file is responsible for documenting each source file. Never leave documentation out of sync with implementation.

## Push policy

Never push to GitHub without explicit approval from the user. Always show exactly what will be pushed (branch, commits, diff summary) and ask for confirmation first. This applies to all branches and remotes — no exceptions.

## Slash command index

These commands are defined in `.claude/commands/` and can be run with `/command-name` inside Claude Code:

| Command | Purpose |
|---|---|
| `/setup-server` | Preflight checks, run `setup-server.sh`, persist printed client config |
| `/setup-client` | macOS 14+ check, download or build DMG, open each System Settings pane |
| `/test-connection` | `curl` `/healthz`, both `/readyz/*`, and a tiny-WAV roundtrip on `/transcribe/dictate` |
| `/docs-check` | Diff the file→doc map against last-edit timestamps to find stale docs |
| `/update-models` | Re-run `download-models.sh` then reload `co.wispralt.server.plist` via launchctl |
| `/verify-autostart` | Non-destructive reboot-survival smoke test for server, cloudflared, and client login-launch. |

> Note: employee-facing install is the `install.sh` curl one-liner (see `docs/INSTALL.md`). The legacy `/wispralt-setup` slash command has been removed. The `/wispralt-update` slash command in `~/.claude-dotfiles/commands/` remains as a developer convenience for in-place updates.

## Key conventions

- Repository pattern: database queries in repository functions, business logic in services, validation at route boundaries only.
- No generic `except Exception` — use typed errors.
- No model loading per dictation request; dictation models are resident at startup. Meeting models load lazily on the first meeting job (async batch — load cost is invisible).
- No Redis — SQLite-only job store.
- No server-side speaker rename — client-only, atomic local rewrite.
- Cloudflared tunnel token: stored in `~/.config/wispralt/cloudflare-token` (mode 0600)
  outside the repo. Read by the cloudflared LaunchAgent via `--token-file`. Never
  committed, never logged. The legacy `sudo cloudflared service install` flow is
  abandoned because its plist is broken on macOS 14/15. Rotation: see
  docs/DEPLOYMENT-NOTES.md.
- Secrets: `HF_TOKEN` and `WISPRALT_API_KEY` in `server/.env` (mode 0600). API key in client Keychain (`co.wispralt`). Never commit either.

@CLAUDE.local.md
