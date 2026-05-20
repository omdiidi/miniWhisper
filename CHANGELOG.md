# Changelog

All notable changes to WisprAlt are documented here.

## [0.1.1] - 2026-05-01

### Fixed

- **install.sh** — pre-authorize `WisprAlt.app` and `security` CLI on
  the Keychain entry via `-T <bundle> -T /usr/bin/security`. Without
  this, the first time the app reads its API key from Keychain (different
  process from `security` CLI), macOS prompts for the login keychain
  password — a one-time-but-confusing UX paper cut. Caught during the
  Phase 4 friend's-mini test on 2026-05-01.
- **Docs** — env-var assignments in the curl one-liner now sit on the
  bash side of the pipe (`curl ... | WISPRALT_API_KEY=... WISPRALT_SERVER=... bash`).
  The previous form (`WISPRALT_API_KEY=... curl ... | bash`) put the env
  vars in curl's environment; the bash on the right of the pipe did not
  inherit them, so install.sh saw `WISPRALT_API_KEY` as unset and skipped
  Keychain provisioning. Empirically verified during Phase 4. Affected
  files: `README.md`, `docs/INSTALL.md`, `docs/SETUP-CLIENT.md`,
  `docs/DEPLOY-TEAM.md`, `docs/ADMIN.md`.

### Validated (Phase 4 friend's-mini test)

- **Cross-Apple-ID Gatekeeper.** Apple Development cert (free Personal
  Team, `Apple Development: zomid777@gmail.com`) launches on a
  non-Omid Mac without notarization. `SMAppService.mainApp.register()`
  Login Item registration succeeded — that operation requires Apple to
  validate the signing identity, so success confirms Gatekeeper
  acceptance. **No `$99/yr` Apple Developer Program enrollment needed
  for v1 distribution.**
- **`install.sh` end-to-end.** Preflight → release fetch → DMG download
  + SHA verify → mount + cp → quarantine xattr strip → Keychain write
  → UserDefaults write → app launch. All steps clean on macOS 26.4
  Tahoe.

## [0.5.0] - 2026-05-19

### Security

- **Role tightening on three admin routes.** `GET /admin/active`,
  `GET /admin/server-log/{job_id}`, and `GET /metrics` now require
  `require_admin` instead of `require_api_key`. Previously any authenticated
  bearer (including employee tokens) could read operator-level introspection.
  Employees are now scoped to `/me/*` for their own data. The client's "View
  server log" sheet handles the 403 with a friendly "Admin-only — ask your
  administrator" message rather than a raw error.

### Changed

- **`install.sh` is now bundle-ID-driven.** Discovery uses `mdfind` to
  enumerate every WisprAlt bundle anywhere on the filesystem (not just
  `/Applications/`), gracefully quits each one via AppleScript (with a
  narrow `pkill -f co.wispralt.WisprAlt` fallback), removes orphan bundles,
  and sweeps `~/Library/LaunchAgents/co.wispralt*.plist` before installing
  to the canonical `/Applications/WisprAlt.app` path. Closes the
  "broken old install left on employee's Mac" hole. Re-running the
  one-liner is still the idempotent update path.

### Added

#### Client

- **In-app update checker** (`client/WisprAlt/Update/UpdateChecker.swift`).
  Polls `releases/latest` 60 s after launch and every 6 h, debounced via
  `Settings.shared.lastUpdateCheck`. On a newer release, sets
  `Settings.shared.updateAvailable` and a subtle orange dot on the menubar
  icon via `MenuBarController.setUpdateBadge(visible:)` (only when
  `serverURL != nil`). Settings → Advanced → Updates shows the current vs
  latest version and an "Install now…" button that shells out to
  `Terminal.app` via `NSAppleScript` with the canonical curl one-liner;
  falls back to clipboard if Automation TCC is denied. Deliberately NOT
  Sparkle — reuses `install.sh` as the canonical install path. The
  pre-existing `SparkleController.swift` remains disabled and kept for
  reference.
- **"Copy last dictation"** menubar button pinned at the very top of the
  popover. Calls the new `LastDictationAPI.fetch()` namespace in
  `DictationAPI.swift` against `GET /me/dictations/last`, copies the
  returned text to the clipboard, and shows a "Copied" toast. Surfaces a
  friendly "No dictations yet" on 404 and a break-glass-admin message on
  403.
- **"Open My Dictations"** menubar button opens
  `<serverURL>/me/login?next=/me/history` so first-time employees land on
  their personal history page after token-paste instead of the default
  `/me/insights`.

#### Server

- **`GET /me/dictations/last`** (`server/src/wispralt_server/routes/me.py`).
  Returns the caller's most recent non-deleted dictation as JSON:
  `{id, text, created_at}` on 200, 404 on empty, 403 for the break-glass
  admin. Backed by a new `JobStore.get_most_recent_dictation(api_key_id)`
  repository function — single-row SELECT filtered by `deleted_at IS NULL`
  AND `text != ''`, ordered `created_at DESC, id DESC`, `CAST(id AS TEXT)`
  for the Swift `String` decode.
- **`POST /me/login` honors `?next=`** — open-redirect-guarded to relative
  paths under `/me/*` only. Powers the "Open My Dictations" deep-link.

### Deployment notes

- **Mini redeploy required.** The three admin-route role swaps + the
  `/me/dictations/last` route + the `/me/login?next=` parameter all live
  server-side. Standard `scripts/deploy-server.sh` flow.
- **Client cdhash changes.** The Update/UpdateChecker.swift addition and
  the menubar layout shuffle (Copy last dictation + Open My Dictations
  buttons) produce a new cdhash, which triggers macOS TCC re-prompts on
  first run after install. Expected.

## [Unreleased]

### Added

#### Install

- **`install.sh`** — curl-pipe-bash one-liner installer. Replaces the
  `/wispralt-setup` Claude Code slash command. Pure bash + native macOS
  tools; no homebrew, no `gh`, no sudo. Idempotent (re-runs serve as the
  update path). See [docs/INSTALL.md](docs/INSTALL.md).
- **`docs/INSTALL.md`** — canonical install guide for employees.

#### Server

- **OpenAI-compatible `/v1/audio/transcriptions` endpoint** (`server/src/wispralt_server/routes/v1_transcriptions.py`). Drop-in replacement for any client that talks to the OpenAI Audio API: set `OPENAI_BASE_URL=https://<your-server>/v1` and `OPENAI_API_KEY=<wispralt-token>` and existing OpenAI SDKs (Python, Node, curl) work without further changes. Sync, dictate-only, 25 MB cap, raw output (smart formatting deliberately not applied — third parties expect raw model output). Returns OpenAI-shaped error envelopes with `request_id` for support correlation. See `docs/INTEGRATION-GUIDE.md`.
- **JSON `GET /me` and `PATCH /me`** (`server/src/wispralt_server/routes/me.py`). Self-service identity for any authenticated user. Returns/updates `display_name` (1–40 chars, no control chars). Mirrors the SQL CHECK constraint added by the v2 migration.
- **Smart-formatting hook on `/transcribe/dictate`** via the new `X-Smart-Format` header (accepts `true` / `1` / `yes` case-insensitive). Header-gated, fail-soft against a 250ms OpenRouter Mercury 2 budget. Adds new response field `smart_formatted: bool`. Requires `OPENROUTER_API_KEY` in `.env`; if unset, the header is silently a no-op. Cleanup via `server/src/wispralt_server/smart_format/mercury_client.py`.
- **OpenAI error-envelope middleware** (`server/src/wispralt_server/middleware/openai_errors.py`). Translates HTTPExceptions on `/v1/*` paths into the OpenAI-shaped `{error: {message, type, param, code, request_id}}` envelope so the FastAPI default Pydantic-shape errors don't leak to OpenAI clients.
- **Migration v2** (`server/migrations/2026-04-27-v2-display-name.sql`): `ALTER TABLE wispralt.users ADD COLUMN display_name TEXT NULL` with CHECK constraint enforcing 1–40 chars and no control characters. Idempotent (`IF NOT EXISTS`, `ON CONFLICT DO NOTHING`).
- **Shared constants module** (`server/src/wispralt_server/constants.py`): `MAX_DISPLAY_NAME_LEN`, `OPENAI_COMPAT_SIZE_CAP`. Single source of truth for limits that span SQL, Pydantic, and route handlers.

#### Client

- **Smart-formatting toggle** in Settings popover. Default OFF. When ON, the macOS client sets `X-Smart-Format: true` on every dictation. ~250ms added wall-clock; fail-soft (no error if server has no OpenRouter key — just returns raw text).
- **Identity section** in Settings — read/write `display_name` via `PATCH /me`. Validation matches the server's 1–40 char / no-control-char rule.
- **First-launch display-name sheet** (`client/WisprAlt/UI/DisplayNameSheet.swift` + `FirstLaunchCoordinator.swift`). On first launch after install, calls `GET /me`; if `display_name` is `null`, presents the sheet. Skippable (leaves it `null` until edited later).
- **`MeAPI` Swift client** (`client/WisprAlt/Server/MeAPI.swift`) — typed `GET /me` and `PATCH /me` wrappers used by the Identity section and the first-launch coordinator.
- **App icon** — real brand mark (dark mic + chat bubble) visible in Finder Get Info, Launchpad, Spotlight, and notifications. Generated by `scripts/build-icon.sh` into `client/WisprAlt/Resources/Assets.xcassets/AppIcon.appiconset/` (10 PNGs + `Contents.json`).

#### Docs

- **New `docs/INTEGRATION-GUIDE.md`** — third-party drop-in setup guide for the `/v1` endpoint with Python, Node, curl, and Swift examples; supported parameters and limits; auth failure shape; the smart-formatting-via-direct-API extension; troubleshooting; and an explicit list of differences from upstream OpenAI.
- **`OVERVIEW.md` file-to-doc map** updated with all new files (server routes, smart_format module, migration v2, client `MeAPI`/`DisplayNameSheet`/`FirstLaunchCoordinator`, asset catalog, `build-icon.sh`, `INTEGRATION-GUIDE.md`, `CHANGELOG.md`).
- **`API.md`** gains `/v1/audio/transcriptions`, `GET /me`, and `PATCH /me` reference sections; `/transcribe/dictate` documents the new `X-Smart-Format` header and `smart_formatted` response field.
- **`ARCHITECTURE.md`** updates the system diagram with `/v1` and the Mercury smart-format hook, adds a `wispralt.users` column table including `display_name`, and notes that `/v1` events are tracked with `kind = "v1_dictate"`.
- **`ADMIN.md`** documents the `display_name (label)` admin UI rendering, the `OPENROUTER_API_KEY` env var, and how `v1_dictate` events fold into the existing per-user dictation tiles.
- **`SETUP-CLIENT.md`** adds Smart formatting, Your Name, and App Icon subsections.
- **`SETUP-SERVER.md`** adds `OPENROUTER_API_KEY` to the env table, a Postgres schema-migrations subsection covering v1 + v2, and an "Optional: enable smart formatting" step.

### Removed

- **`~/.claude-dotfiles/commands/wispralt-setup.md`** — obsolete; replaced
  by `install.sh`. The `/wispralt-update` slash command remains as a
  developer convenience.

### Changed

- Admin user list now shows `display_name (label)` when both are populated; falls back to `label` alone otherwise. Self-service via `PATCH /me` — admins do not edit other users' display names from the UI.
- `/transcribe/dictate` response schema gains a stable `smart_formatted: bool` field. Always present, `false` when no cleanup happened (header absent, server lacks `OPENROUTER_API_KEY`, Mercury timed out, etc.).
- Tracked usage routes now include `v1/audio/transcriptions` in addition to `transcribe/dictate` and `transcribe/meeting`. POST-only, as before.
- **Smart-formatting threshold raised from 20 → 100 words** and the cleanup contract loosened. Previously Mercury 2 was invoked above 20 words and could only fix punctuation/casing under a strict word-multiset equality safety check that rejected any added/removed/substituted word. Now: short-circuit below `SMART_FORMAT_MIN_WORDS` (default 100, env-configurable), and above the threshold Mercury may also remove fillers ("um", "uh", "you know" when filler), collapse repeated words, fix obvious mid-utterance corrections, and add bullet-list formatting where the speaker is enumerating. Meaning preservation is still required (no rephrasing, no summarization, no new content, no Markdown literals). The strict multiset safety check is replaced with a length-window check (cleaned word count must be 0.7×–1.10× of raw); the strong system prompt is now the primary guard. The `_word_multiset`, `_canonicalize`, `_is_safe_cleanup`, and `_CONTRACTION_EXPANSIONS` helpers in `mercury_client.py` are deleted. `test_mercury_safety.py` replaces `TestIsSafeCleanup` with `TestWordCount` + `TestLengthWindow`; `TestExtractText` is unchanged.

### Deployment notes

- **Postgres**: apply migration v2 (`server/migrations/2026-04-27-v2-display-name.sql`) before deploying the new server build, or `PATCH /me` will fail with a missing-column error. The migration is idempotent and can be applied via Supabase Studio or `mcp__supabase__apply_migration`.
- **OpenRouter key (optional)**: add `OPENROUTER_API_KEY=<key>` to `server/.env` (mode 0600) if you want the smart-formatting toggle to actually do anything. Without it, the toggle silently returns raw output.
- **Client cdhash**: rebuilding the client with the icon asset catalog produces a new cdhash, which triggers macOS TCC re-prompts on first run after install. Expected.

### Not yet implemented

- `scripts/measure-dictation-latency.sh` — referenced as a future helper for timing dictation round-trips against a fixture WAV. Listed in `OVERVIEW.md` as a placeholder; the script itself is not committed yet.

## [Prior]

See git log up to commit `5427e3d`.
