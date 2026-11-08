# AGENTS.md

Instructions for coding agents working in this repository.

- **Rules:** [CLAUDE.md](CLAUDE.md) holds the project rules and conventions; they apply to
  every agent, not only Claude Code.
- **Codebase map:** [docs/OVERVIEW.md](docs/OVERVIEW.md) lists every source file and the doc
  that covers it. A code change updates those docs in the same change.
- **Tests:** `bash scripts/test-client.sh` (client build, Swift tests, log-privacy gate) and the
  server suite as CI runs it (`.github/workflows/ci.yml`; see [docs/TESTING.md](docs/TESTING.md)).
- **Installs:** never run `wispralt setup` against a real host from a test; set `WISPRALT_ROOT`
  and `WISPRALT_LABEL` for throwaway installs ([docs/INSTALL.md](docs/INSTALL.md)).
