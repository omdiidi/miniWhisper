# Brief: Team-distribution + multi-tenant + admin UI for WisprAlt

## Why

Omid wants to roll WisprAlt out to his small team (his employees) without
turning it into App-Store-grade distribution. Goal: he texts an employee an
API key, the employee opens Claude Code, says "set this up", and a few
minutes later they're dictating. The slash commands already live in his
shared dotfiles repo, which all employees keep in sync — so the install +
update commands are reachable from any employee's machine without cloning a
new repo first.

Bigger picture (out of scope this round, but informs the architecture
choices): this becomes a CRM-tied product over time. The user/usage table
and admin dashboard built now should be easy to expand into per-customer
account tiers, billing-relevant usage data, and integrations with the
existing CRM. Build it shaped right; don't build the actual CRM piece yet.

## Context

### Current state on `origin/main` (HEAD `05eb1fc`)

- Server is single-tenant: one `WISPRALT_API_KEY` in `server/.env`,
  validated by `server/src/wispralt_server/auth.py:require_api_key`.
- Bearer comparison via `secrets.compare_digest`; rotation route
  `POST /admin/rotate-key` (only one key, hot-swapped).
- `/metrics` exposes per-route counters + per-route p50/p95/p99 (recent
  5-min window with low-traffic fallback) + `process_uptime_seconds`.
- No per-user tracking; no admin UI; no usage history beyond in-memory
  rolling deques.
- SQLite already used for the meeting-job store
  (`server/src/wispralt_server/jobs/store.py`) — same pattern fits a
  `users` + `usage_events` table.
- Server is at `https://transcribe.integrateapi.ai`, fronted by Cloudflare
  Tunnel → `127.0.0.1:8080` on the Mac mini. Anything mounted at `/admin/*`
  is reachable through that hostname.
- The macOS client (`client/WisprAlt/`) already has a settings pane
  (`UI/SettingsView.swift`) that takes a server URL + API key. The
  permission gate (`App/PermissionGate.swift`) walks through the 4 macOS
  permissions on first launch.
- The existing slash commands `/setup-server` and `/setup-client` live in
  the project's `.claude/commands/` and are project-scoped. The employee-
  facing commands need to live in **the user's shared dotfiles repo**
  (`~/.claude-dotfiles/commands/`) so they're globally available without
  cloning the WisprAlt repo first.
- `scripts/build-client-local.sh` produces an Apple-Development-signed
  `.app`. There is no GitHub-Release-attached pre-built DMG yet.

### Constraint: distribution model is "Tier 1.5"

- Pre-built signed DMG attached to GitHub Releases (`gh release download`).
- Apple Development cert (free Personal Team) is sufficient — friend-grade
  Gatekeeper warning is acceptable. No notarization, no Developer ID, no
  Sparkle EdDSA key.
- Employees get the API key out-of-band (texted from Omid).
- Employees never run `setup-server` or touch HF / Cloudflare tokens.
- Updates are pull-based (employee runs a slash command), not push-based.

## Decisions

### 1. Multi-token auth (server)
- New SQLite table `users` co-located in the existing `jobs.db` (re-use
  `jobs/store.py` patterns; new `users/store.py` is fine but same DB).
- Schema:
  ```
  users(
    id              INTEGER PRIMARY KEY,
    label           TEXT NOT NULL,        -- e.g. "alice@team", free-form
    token_hash      TEXT NOT NULL UNIQUE, -- sha256 of the bearer token
    role            TEXT NOT NULL,        -- 'admin' | 'employee'
    created_at      INTEGER NOT NULL,
    last_seen_at    INTEGER,
    revoked_at      INTEGER,              -- NULL = active
    notes           TEXT
  );
  CREATE INDEX users_token_hash ON users(token_hash);
  ```
- `auth.py:require_api_key` becomes "look up token by sha256 hash, ensure
  not revoked, attach `request.state.user` for downstream handlers".
- Backwards-compat: on first startup, if `users` is empty AND the legacy
  `WISPRALT_API_KEY` env var is set, seed an admin user from it. Then the
  env var is just a backup admin escape-hatch.

### 2. Per-user usage tracking
- New table `usage_events`:
  ```
  usage_events(
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    ts              INTEGER NOT NULL,        -- unix seconds
    kind            TEXT NOT NULL,           -- 'dictate' | 'meeting' | 'login' | ...
    status          INTEGER NOT NULL,        -- HTTP status
    chars           INTEGER,                 -- transcript length, NULL if N/A
    duration_ms     REAL,                    -- inference ms, NULL if N/A
    bytes_in        INTEGER,                 -- request body bytes
    bytes_out       INTEGER,                 -- response body bytes
    error_class     TEXT,                    -- one of CorruptAudioError, TimeoutError, ...
    request_id      TEXT                     -- correlation
  );
  CREATE INDEX usage_user_ts ON usage_events(user_id, ts);
  CREATE INDEX usage_kind_ts ON usage_events(kind, ts);
  ```
- Recorded in the same observability middleware that already increments
  `request_counter` — write goes async to not block the request path
  (background task or a small SQLite writer thread). Single SQLite WAL DB
  is fine for the volumes this team will produce (single-user dictation
  rate cap is 60/min; a 10-person team's whole-day load is well under SQLite's
  limits).
- Schema is intentionally **forward-compatible** with CRM use:
  - `kind` column is the discriminator that lets future event types
    (e.g., `'crm_lead_created'`, `'meeting_summary_pushed'`) live alongside
    dictation events.
  - Per-user-row cardinality is independent of `users` shape, so adding
    `customer_id`, `team_id`, `tier` etc. later is additive.

### 3. Admin web UI (server-side rendered)
- Mount at `https://transcribe.integrateapi.ai/admin` — same FastAPI app,
  Jinja2 templates, minimal CSS. **No SPA, no React.** Single static
  bundle: one Jinja2 base template + 3 child templates (users list,
  user detail, usage dashboard).
- Auth: same bearer-token middleware, but route requires `role='admin'`.
- Pages (MVP):
  - **`GET /admin/`** — overview: total users, active 7d, total dictations
    24h / 7d / 30d, p50 latency 24h, error count 24h, top 5 most-active
    users, per-day stacked bar (count of dictations by user) for last 14d.
  - **`GET /admin/users`** — table of users: label, role, created, last
    seen, dictations 24h, dictations 7d, status (active/revoked).
    Buttons: Revoke, Mint new token (replaces). Uses HTML `<form>` POST.
  - **`GET /admin/users/{id}`** — per-user detail: same metrics as overview
    but scoped to that user, last 50 events table.
  - **`GET /admin/usage`** — usage drill-down: filter by kind/status/user/
    time range; CSV export.
- Implementation: Jinja2 template files in
  `server/src/wispralt_server/admin/templates/`. Charts via inline SVG
  or a tiny library (Chart.js loaded from CDN — fine for a private admin
  page).
- Forward-compat: pages are organized by `/admin/<resource>` so future
  resources (`/admin/customers`, `/admin/integrations`, `/admin/billing`)
  drop in without restructure. **Don't pre-build any of those now.**

### 4. Employee-facing client distribution
- Pre-built signed DMG attached to GitHub Release (semver tag).
- Build pipeline: a new local-only script `scripts/release-client.sh`
  that builds, signs, packages a DMG, computes its SHA256, creates the
  GitHub release with `gh release create`, attaches the DMG. Manual
  trigger; Omid runs it on his MacBook when he wants to ship a release.
- The current `scripts/build-client-local.sh` stays intact for dev use.

### 5. Slash commands in `~/.claude-dotfiles/commands/`
- **`/wispralt-setup`** — employee-facing first-time install. Steps:
  1. Detect macOS version (≥ 14 required), error clearly if not.
  2. Install Homebrew if missing.
  3. Install `gh` if missing.
  4. `gh release download --repo omdiidi/miniWhisper --pattern '*.dmg'`
     latest release.
  5. Verify SHA256 against the release notes.
  6. Mount the DMG, copy `WisprAlt.app` to `/Applications/`,
     `xattr -dr com.apple.quarantine /Applications/WisprAlt.app`,
     unmount.
  7. Open the app — its existing `PermissionGate.swift` walks the user
     through the 4 macOS permissions.
  8. After permissions, the user pastes the API key Omid texted them
     into the existing settings pane. The app validates it against
     `https://transcribe.integrateapi.ai/healthz` and stores it in
     Keychain (existing behavior).
- **`/wispralt-update`** — employee-facing update. Steps:
  1. `gh release view --repo omdiidi/miniWhisper --json tagName` →
     latest tag.
  2. Read installed version from `/Applications/WisprAlt.app/Contents/
     Info.plist:CFBundleShortVersionString`.
  3. If installed >= latest, exit "already up to date".
  4. If newer available, download DMG, verify SHA256, replace
     `/Applications/WisprAlt.app`, run TCC reset cycle if cdhash changed
     (the four `tccutil reset` calls), open System Settings → Privacy &
     Security and tell the user which permissions to re-grant.
- These commands live in `~/.claude-dotfiles/commands/`, NOT in the
  project's `.claude/commands/`. The project-scoped `/setup-server` and
  `/setup-client` (admin-grade, builds from source) stay where they are.

### 6. Token revocation MVP
- Admin UI "Revoke" button → POST `/admin/users/{id}/revoke` →
  sets `users.revoked_at = now()`. Next request from that token → 401.
- A "Mint new token" button on the same page generates a new
  `secrets.token_hex(32)`, replaces `token_hash` for that user, prints
  the plaintext token ONCE in the UI for Omid to copy + text the
  employee. Old token is dead immediately.
- No JWT, no refresh tokens, no expiration policy — explicit-revoke only.

### 7. Forward-compat hooks left in place (zero work now, future-friendly)
- `users.role` is `TEXT` (not enum) so future roles (`manager`,
  `customer`, `tier_pro`) drop in.
- `usage_events.kind` is `TEXT` discriminator (not enum).
- Admin UI route tree organized by resource (see decision 3).
- Server middleware sets `request.state.user` so future handlers can
  do per-user authz/quotas without grepping headers again.
- DB schema migrations: introduce a `schema_version` table now, even
  with version 1, so the next person can add v2 etc. cleanly.
- The dictation transcript is **not** persisted to the DB. (Privacy +
  storage cost.) `usage_events` only logs metadata (chars, duration,
  status, kind). If the future CRM wants transcripts, that's a separate
  table with explicit user opt-in.

## Rejected alternatives

- **Sparkle EdDSA-signed auto-update** — overkill for ≤10 employees.
  Pull-based update via slash command is the right size.
- **Apple Developer Program ($99/yr) + Developer ID + notarization** —
  not needed for an internal tool. The first-launch Gatekeeper warning is
  acceptable; the slash command runs `xattr -dr com.apple.quarantine`
  to suppress the "downloaded from internet" prompt anyway.
- **App Store distribution** — sandboxing breaks AXUIElement injection
  (the dictation-paste mechanism). Architectural mismatch.
- **Multi-tenant shared SaaS-style server** — Omid hosts ONE server his
  team uses. No customer accounts, no team isolation, no quotas beyond
  rate-limits. (CRM-tied multi-tenant lives in a future iteration.)
- **JWT / OAuth** — for ≤10 internal employees, plain bearer tokens
  with hash-in-DB are simpler and sufficient.
- **React / Vue / SPA admin** — Jinja2 + minimal CSS is faster to write,
  zero build pipeline, and an LLM successor can extend it trivially.
- **Storing transcripts in the DB** — privacy + size + scope creep.
  Metadata only for now.

## LOCKED — Database choice (resolved 2026-04-27)

After verifying the existing Supabase MCP connection
(`project-ref=qglwmwmdoxopnubghnul`, same project plan2bid uses):

- **Use Supabase Postgres for `users` + `usage_events`.** Future CRM
  tie-in becomes a clean cross-schema join. Built-in dashboard for
  ad-hoc analytics. Migrations via Supabase MCP `apply_migration`.
- **Dedicated schema `wispralt`.** All tables live as `wispralt.users`,
  `wispralt.usage_events`, `wispralt.schema_version`. No name
  collisions with plan2bid or future products.
- **Direct Postgres via `asyncpg`.** Faster than REST, proper pool,
  type-rich. Connection string in `server/.env` (mode 0600) as
  `SUPABASE_DATABASE_URL`. **NOT the REST API.**
- **`jobs` stays SQLite.** Machine-local orchestration state on the
  Mac mini. Decision from the brief unchanged.
- **Token-hash auth cache: 60s in-memory LRU.** First lookup hits
  Postgres (~30ms one-time per token); subsequent requests for the
  next 60s resolve in-process. **The dictation hot path is unchanged
  from today.** Postgres is off the latency-critical path.
- **Usage writes: fire-and-forget background task with bounded queue.**
  Every successful request enqueues an event row. A background
  coroutine drains the queue to Postgres. Queue bounded at 1000;
  if Postgres lags and the queue fills, oldest events drop with a
  warning log line. **Dictation never waits on Postgres.**
- **Break-glass admin: legacy `WISPRALT_API_KEY` env var.** If
  Postgres is unreachable at server startup, the env-var token is
  still honored as an admin-role token so the operator can recover.
  Logs a `WARNING` so the operator knows they're in degraded mode.
- **RLS: skip for v1.** FastAPI uses the service-role key; auth
  happens at the FastAPI layer where the request context already lives.
  RLS becomes valuable when the client app (Swift, future web)
  queries Postgres directly — not in this iteration.

These decisions ensure: WisprAlt runs **at least as fast as today**,
with **explicit, logged failure modes** when any new dependency
(Postgres) is unhealthy, and **zero possibility of double-source-of-
truth bugs** between SQLite and Postgres (each table type lives in
exactly one store).

## Direction

Three thin layers, in this order:

1. **Server: multi-token auth + usage tracking + admin UI.** New SQLite
   tables in the existing `jobs.db`, new `routes/admin_ui.py` mounted at
   `/admin/*`, new `admin/templates/` Jinja2 dir. `auth.py` extended to
   look up tokens by hash. Observability middleware extended to write
   `usage_events` async. Backwards-compat: legacy env-var token seeds
   the first admin user on first boot.

2. **Client distribution: GitHub Releases + slash commands.** New
   `scripts/release-client.sh` that produces + uploads a signed DMG.
   Two new slash commands in `~/.claude-dotfiles/commands/` —
   `wispralt-setup.md` and `wispralt-update.md` — that drive
   `gh release download`, install, and walk the user through TCC.

3. **Forward-compat scaffolding (zero work, just shape).** `usage_events`
   schema with `kind` discriminator. `users.role` as text. Admin UI
   organized by resource. `schema_version` table. Decisions in this
   brief documented inline so the next builder can pick up the CRM tie-in
   without rediscovering them.

Total work estimate: ~1 day for layers 1+2, plus polish.
