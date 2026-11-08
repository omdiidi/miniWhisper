---
title: Admin UI
---

# Admin UI

Server-rendered Jinja2 dashboard at `https://transcribe.<your-domain>/admin/*`.
Lets the operator mint tokens, revoke users, browse usage events, and export
CSV without ever touching the database directly.

Source: `server/src/wispralt/routes/admin_ui.py` +
`server/src/wispralt/admin/templates/*.html.j2`.

---

## When to use it

- Onboarding a new employee: mint a token, text it to them.
- Off-boarding: revoke the user; their next request gets 401.
- Spot-checking usage: who's been active, what error rates look like, which
  requests failed and when.
- Auditing for billing or CRM tie-in (see [Future hooks](#future-hooks-crm)).

Everything the admin UI does reads or writes the host's local SQLite DB
(`DATA_DIR/wispralt.db`) through `AccountStore` (`users` + `api_keys`) and
`UsageStore` (`usage_events`).

## Who should have admin role

Exactly one human (the operator). Employees get `role='employee'`. The
schema enforces `role IN ('admin', 'employee')` via a `CHECK` constraint
(`server/src/wispralt/migrations/0001_initial.sql`); future roles require a
migration.

The first admin is created once, by `wispralt setup --role host` (see
[INSTALL.md](INSTALL.md)). It prints the client config — server URL and the
one-time admin token — to a terminal, or writes the token to a new 0600
`--token-file`; a rerun never creates or rotates an admin. Lost the admin key?
`wispralt keys rotate-admin` (add `--user-id N` when there are several admins)
revokes the old key and issues a new one. `python -m wispralt.bootstrap`
remains as a shim over the same code (run it where the host `.env` is). There
is no env admin key and no break-glass path.

---

## Logging in

Two authentication methods, both resolved by `auth.py`:

### Header (curl / Postman)

```
GET /admin/users HTTP/1.1
Host: transcribe.example.com
Authorization: Bearer <admin-token>
```

The bearer is sha256-hashed and looked up in `api_keys ⋈ users` on every
request (no cache).

### Session cookie (browser)

1. Visit `https://transcribe.<your-domain>/admin/login`.
2. Paste your token, submit. **Both admin and employee tokens are
   accepted** — the server routes by role.
3. The server validates the token (`auth.resolve_token`, the same lookup), then
   sets a `wispralt_admin_token` cookie with `HttpOnly`, `Secure`,
   `SameSite=Strict`, `max_age=8h`.
4. **Role-based redirect on success:**
   - `role='admin'` → 303 to `/admin/` (full overview dashboard).
   - `role='employee'` → 303 to `/admin/me` (the employee's own
     `user_detail` page; admin-only nav links are hidden by
     `base.html.j2`, replaced with a single "My Usage" link).

The macOS client's menubar **Open Portal** button targets
`<server>/admin/login` for everyone, so the same install ships to admins
and employees without a per-role configuration.

The cookie is read by `auth._extract_bearer` as a fallback when no
`Authorization: Bearer` header is present.

**CSRF (Part 4).** Every cookie-authenticated mutation (Add employee, Add key, user Revoke / Mint, key Revoke / Rotate, `PATCH /me`, `/me/history` delete / restore) needs the double-submit token: the `X-CSRF-Token` header or the `csrf_token` form field must equal the `wispralt_csrf` cookie (set by the page that rendered the form), else **403**. A request that authenticated with a valid `Authorization: Bearer` header needs no token (a cross-site page cannot set that header). A non-Bearer `Authorization` header (e.g. `Basic`) falls back to the cookie and so needs the token; an invalid Bearer is a 401, never a cookie fallback. `SameSite=Strict` on the cookies is a second layer.

---

## Pages

### `GET /admin/`  — Overview

Single-CTE dashboard (`_OVERVIEW_SQL` in `routes/admin_ui.py`). Tiles:

- Active / total users.
- Dictations in the last 24h / 7d / 30d — counted from `dictations` rows (one per finished dictation, however many stream-chunk requests it took; soft-deleted rows excluded).
- Errors in the last 24h.
- p50 latency over the last 24h (server-side, from `usage_events.duration_ms`).
- Top 5 active users in the last 7 days.
- Last-14-day usage rendered as a CSS-bar table (no chart library; works
  offline).

### `GET /admin/users` — Users

Lists every employee-kind user, including revoked ones (most-recently-created first). The
displayed identifier is `display_name (label)` when both are populated
(e.g. `Alice (alice@example.com)`), or just `label` when
`display_name IS NULL`. Admins can **optionally pre-set** the
`display_name` at create-time via the add-employee form (see "Adding a
new employee" below); when provided, the employee's
`FirstLaunchCoordinator` skips its name-prompt sheet. Employees can
still self-manage their own `display_name` after the fact via the macOS
client's Settings → Identity section, which calls `PATCH /me` (see
[API.md](API.md)). There is no admin UI to edit another employee's
`display_name` after creation — post-create edits are intentionally
self-service.

Per-row controls:

- **Mint** — `POST /admin/users/{id}/mint` revokes the user's active keys
  and issues one new key (same id / label / role); it also re-enables a
  revoked user. The page renders
  `token_minted.html.j2` which shows the new plaintext **once**. Copy it,
  text it to the employee, close the tab.
- **Revoke** — `POST /admin/users/{id}/revoke` sets `users.disabled_at`;
  none of the user's keys authenticate from their next request (401).

`last_seen_at` is **derived** from `MAX(usage_events.ts)` per user — it is
not written on every request.

### `GET /admin/users/{id}` — User detail

Per-user metrics scoped to one row plus the last 50 `usage_events` for
that user. Same 24h / 7d / 30d / errors_24h / p50_24h tiles, narrowed to
`WHERE user_id = $1`. **Admin-only.**

### `GET /admin/me` — My usage (employee self-service)

Same `user_detail` template as `/admin/users/{id}`, scoped to the
**calling user's own id**. Mounted on a separate router that requires
`require_api_key` (any authenticated role) instead of `require_admin`,
so employees can view their own dictation history without being able to
see anyone else's. Admins hitting `/admin/me` are 303'd to `/admin/`.

`base.html.j2` reads `request.state.user.role` and:

- Hides the admin-only nav (Overview / Users / Usage) for non-admin
  sessions, replacing it with a single "My Usage" link.
- Flips the header title from "Wispralt Admin" to "Wispralt Portal" for
  non-admins.

The macOS menubar **Open Portal** button (renamed from "Open Admin
Portal" in be720a1) targets `/admin/login`, so a single client build
serves both roles correctly.

### `GET /admin/data` — Manual insights trigger

The Data tab has a **Run insights now** button at the top-right of the
range-tabs row, with the muted hint "Costs ~$0.65 per run". Clicking it
fires `insights.cron.run_weekly_insights` against the **current
in-progress ISO week** as an admin-initiated, fire-and-forget background
task. The Sunday 23:00 cron continues to fire unchanged — when it runs,
it UPSERT-replaces the manual-run rows for the now-complete week, so
there are no orphan rows.

Source: `routes/admin_data.py:admin_data_run_insights_now` (`POST
/admin/data/insights/run-now`). API contract:
[API.md → POST /admin/data/insights/run-now](API.md#post-admindatainsightsrun-now).

**Why use it**

- Spot-check a configuration change without waiting until Sunday.
- Demo the insight cards for an employee onboarding.
- Recover from a Sunday miss (uvicorn was down, the cron task crashed,
  etc.) — clicking re-runs cleanly because the per-user idempotency skip
  (`cron.py:167-176`) short-circuits OpenRouter calls for users whose
  rows already exist.

**Cost + budget protection**

Each manual run costs roughly $0.65 (varies with team size and weekly
transcript volume). The server pre-checks the rolling 30-day insights
spend against `settings.insights_max_30d_cost_usd` (default $8) BEFORE
spawning the task — over-cap clicks return a toast like
`Budget exceeded ($8.12 / $8.00 cap). Run skipped.` instead of silently
incurring a charge.

**Toast meanings**

| Toast | What it means | What to do |
|---|---|---|
| `Insights run started for Wxx. Refresh page in ~1 minute to see results.` | Task spawned successfully. Button switched to `Running…` (disabled). | Wait ~30–60 seconds, then full-page-refresh `/admin/data` to see the new rows in the per-user drill-down. |
| `Insights unavailable — OPENROUTER_API_KEY not configured.` | `OPENROUTER_API_KEY` env var is unset. No task spawned, no charge. | Set the key in `server/.env`, restart the server, click again. |
| `Budget exceeded ($X.XX / $Y.YY cap). Run skipped.` | Rolling 30-day spend already over the cap. No task spawned, no charge. | Wait for the rolling window to clear, OR raise `insights_max_30d_cost_usd` in `server/.env` and restart. |
| `A run is already in progress. Refresh in ~1 minute.` | The lock is held — another manual or scheduled run is mid-flight. The button repaints as `Running…`. | Wait. The lock auto-releases when the in-flight run completes. |
| `Session expired. Refresh the page and try again.` | The CSRF cookie has expired (30-minute TTL — `auth._CSRF_COOKIE_MAX_AGE_S`). | Refresh the page to mint a new cookie, then click again. |

**Known limitations**

- **Refresh-to-resync model.** The button + toast live OUTSIDE
  `#insights-body`, so clicking a range tab (Today / 7d / 30d / etc.)
  does NOT repaint either of them — including not updating
  `Running…` → enabled when the in-flight run completes. The disabled
  state is read from `app.state.weekly_insights_lock.locked()` at GET
  time, so a full-page refresh is the way to re-sync the button after a
  run finishes. This is intentional per the brief (no polling, no SSE).
- **Phase 2 team-aggregate display bug.** After a manual run completes,
  the team-aggregate top-level tiles on `/admin/data` may still show 0
  across the board — that's a pre-existing display bug being tracked
  separately, NOT a failure of the manual trigger. The actual rows DID
  land in `weekly_insights`; confirm by drilling into a specific user
  via `/admin/data?user_id=N` (the per-user cards render correctly).
- **SIGTERM mid-run.** Manual-trigger tasks are held in
  `app.state.weekly_insights_manual_tasks` (a `set[asyncio.Task]`) but
  are NOT included in the lifespan shutdown's cancel loop — restarting
  uvicorn (or a `launchctl unload`) mid-run drops the task on the
  floor. Click the button again post-restart: the per-user idempotency
  skip means already-processed users are not re-charged, and only the
  un-processed tail of the team re-uses OpenRouter quota.
- **Single-process assumption.** `weekly_insights_lock` is
  process-local. If the deployment ever scales to multiple uvicorn
  workers, the lock-based UX disable will stop working (two workers
  would each show the button as enabled while the other is running).
  The double-billing safety still holds via the per-user idempotency
  skip.

### `GET /admin/usage` — Usage drill-down

Filters: `kind` (e.g. `dictate`, `file`; rows recorded before file jobs were renamed carry `meeting`), `status` (HTTP status),
`user_id`, `since`, `until`. Paginated 100 per page. Each row links back
to its user detail.

Meetings were removed in Part 2. Portal pages (`/me/history`, the stats
grid, the insight split card, the admin data table) now **display** rows
stored with `kind='meeting'` as **File transcription(s)**; the stored value
is unchanged until a later schema pass migrates it.

### `GET /admin/usage.csv` — CSV export

Same filters as the drill-down page; up to 10000 rows per call. Streams
as `text/csv` with `Content-Disposition: attachment;
filename="wispralt-usage.csv"`. Columns:

```
id,user_id,user_label,ts,kind,status,duration_ms,chars,bytes_in,bytes_out,error_class,request_id
```

---

## Adding a new employee

1. Open `/admin/users` and click **+ Add employee** (top-right).
2. Enter a `label` (1–80 chars, no control characters — e.g.
   `nicholas`, `alex-laptop`, `contractor-q2`), optionally enter a
   `display_name` (1–40 chars, no control characters — e.g. `Sarah Chen`;
   leaving it blank lets the employee set it themselves on first launch),
   and pick a role (default `employee`). Submit.
3. The result page shows the install one-liner pre-baked with the
   freshly-minted token and the server URL from `settings.server_url`:

   ```bash
   curl -q -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh \
     | WISPRALT_API_KEY=<token> WISPRALT_SERVER=https://transcribe.integrateapi.ai bash
   ```

   Click **Copy install command**, paste into Signal / iMessage. The
   plaintext token is shown **once** — closing the page without copying
   means rotating via **Mint new token** on the user's row.
4. The employee pastes the command into Terminal on their Mac. The
   installer downloads the latest signed DMG from GitHub Releases,
   copies the app to `/Applications`, seeds the token into the Keychain,
   and opens the System Settings panes for the three macOS permissions.
5. Their first dictation populates `usage_events`; they appear on
   `/admin/users` with a non-null `last_seen_at`.

The route handlers live at `routes/admin_ui.py:users_add_form` (GET) and
`users_add_submit` (POST). Both are gated by `require_admin`; the latter
calls `accounts.service.create_user` and renders
`employee_added.html.j2` with the install one-liner composed from
`settings.server_url`.

## Integration Keys

Integration keys are tokens minted for **third-party programs** (Buzz,
MacWhisper, Bazarr, Open WebUI, an internal Python script, etc.) — as
opposed to **employee** tokens, which are minted for humans using the
macOS client. Both are `users` rows with an `api_keys` credential; the distinction is
the `kind` column (`'employee'` or `'integration'`).

### What integration keys are

- Same shape as employee tokens (64-char hex bearer, sha256-hashed).
- `kind='integration'` — UI groups them separately and the auth layer
  gates them differently.
- Scoped to `/v1/*` **only**:
  - **CAN** call `POST /v1/audio/transcriptions`, `GET /v1/models`,
    `GET /v1/models/{id}`.
  - **CANNOT** call `/me/*` — it returns **403**.
    That surface is for humans (history, insights, personal
    portal), not for programs.
- Subject to a per-token rate limit of 60 requests/60 s, independent
  of the per-IP bucket on `/transcribe/dictate`.

### Minting an integration key

1. Open `/admin/keys/new`.
2. Enter a `label` (1–80 chars, no control chars — name it after the
   program, e.g. `buzz-laptop`, `obs-streaming`, `bazarr-server`).
3. Submit. The result page shows:
   - The plaintext token **once** (copy now or rotate later).
   - A ready-to-paste OpenAI env-var snippet:

     ```bash
     export OPENAI_BASE_URL=https://transcribe.integrateapi.ai/v1
     export OPENAI_API_KEY=<token>
     ```

   - A pointer to [OPENAI-COMPAT.md](OPENAI-COMPAT.md).
4. Paste the snippet into the integrating program's environment.
   Any OpenAI Whisper-API client (`openai-python`, `openai-node`, curl,
   etc.) Just Works.

### Listing integration keys

`/admin/keys` lists every integration key (analogous to `/admin/users`
for employees). Rows: label, created_at, last_seen_at, revoked status.

### Rotating an integration key

`/admin/keys/{id}/rotate` revokes the key and issues a new one: same id /
label / kind. The page shows the new plaintext once. Update the program's
env var, restart the program. The old token stops working on its next
request.

### Revoking an integration key

`/admin/keys/{id}/revoke` disables the key's user (`users.disabled_at`).
Its next `/v1` request returns **401**.

### What integration keys can / can't access

| Surface | Integration key | Employee key |
|---|---|---|
| `POST /v1/audio/transcriptions` | yes | yes |
| `GET /v1/models`, `GET /v1/models/{id}` | yes | yes |
| `POST /transcribe/dictate`, `POST /transcribe/file`, `/transcribe/file/{id}`, etc. | no (403) | yes |
| `GET /me/*` (history, insights, profile) | no (403) | yes |
| `/admin/*` | no | only if `role='admin'` |

The full /v1 contract — error codes, response formats, headers, file
formats — lives in [OPENAI-COMPAT.md](OPENAI-COMPAT.md).

---

## Revoking an employee

1. `/admin/users` → find the row → click **Revoke**.
2. The route sets `users.disabled_at`. Auth looks the key up on every
   request (there is no token cache), so the employee's next request gets
   401 — no wait, no restart.
3. **Mint** on the same row later issues a fresh key and re-enables them.

### From the command line

The same account operations work offline from a terminal on the host (they
open the database directly; the running server sees every change on the next
request):

| Command | Effect |
|---|---|
| `wispralt users list [--json]` | people and integrations with role, kind, last seen, disabled |
| `wispralt users add LABEL [--role admin] [--kind integration] [--token-file F]` | a new user and its one key |
| `wispralt users disable ID` | none of its keys work (the last enabled admin is refused) |
| `wispralt users enable ID [--token-file F]` | re-enable with a **new** key (old keys stay revoked) |
| `wispralt keys list [--user ID] [--json]` | every key, active and revoked (never a hash) |
| `wispralt keys rotate USER_ID [--token-file F]` | revoke the user's keys and issue one new key |
| `wispralt keys revoke KEY_ID` | revoke one key |
| `wispralt keys rotate-admin [--user-id N] [--token-file F]` | recover a lost admin key |

A new key is printed only to a terminal; without one, pass `--token-file` (a
new file, created 0600) or the command refuses (exit 2) before minting.

---

## Server env vars (admin-relevant)

These are read from `server/.env` (mode 0600). For the full
configuration reference see [SETUP-SERVER.md](SETUP-SERVER.md).

| Variable | Required | Notes |
|---|---|---|
| `DATA_DIR` | Yes | Absolute path of the data directory (`wispralt.db`, outputs, staging). `setup-server.sh` writes `$HOME/Library/Application Support/WisprAlt`. |
| `SERVER_LOG_PATH` / `SERVER_LOG_MAX_BYTES` / `SERVER_LOG_BACKUP_COUNT` | No | Rotating application log (default `~/Library/Logs/WisprAlt/wispralt.log`, 10 MB x 5 backups). `/admin/server-log` reads it. launchd's `server.log` / `server.err.log` hold crash output only. |
| `OPENROUTER_API_KEY` | No | Enables weekly insights. When unset, the insights cron is a no-op. (Server smart formatting was removed in Part 4.) Get a key at https://openrouter.ai/keys. |

---

## Usage events from `/v1/audio/transcriptions`

Third-party API traffic via the OpenAI-compat shim is recorded with
`kind = "v1_dictate"` (native client traffic uses `kind = "dictate"`).
Both `kind` values land in the same `usage_events` table and are folded
into the per-user request tiles on `/admin/users/{id}` (the overview's
Dictations tiles count `dictations` rows, which `/v1` does not write). Per-user event lists on `/admin/users/{id}` show
`v1_dictate` rows alongside `dictate` and `file` rows so you can see
which path generated each request.

To split out third-party traffic, query the table directly:

```sql
SELECT count(*) FROM wispralt.usage_events
 WHERE user_id = $1
   AND kind = 'v1_dictate'
   AND ts > now() - interval '7 days';
```

This is by design — the same employee can dictate via the macOS client
AND ship a Python script that hits `/v1/audio/transcriptions`, and both
should count toward "their usage" for billing/quota conversations.

---

## Importing old history (Supabase + transcript files)

The old host's database was wiped, so the only surviving history is the usage
metadata in Supabase (who used it, when, how much: **no dictation text**) plus any
transcript files you kept. Part 7's importer brings both into the local
`wispralt.db`. Run every command from `server/` with the same `.env` the server uses
(`DATA_DIR` set). Supabase is only ever read, and nothing it writes is ever deleted.

1. **Export (read-only).** Put a Postgres connection string for the Supabase project
   in the Keychain (`security add-generic-password -s co.wispralt.import-pg -a wispralt -w`
   and paste it at the prompt) or in env `WISPRALT_IMPORT_PG_URL`, then:

   ```bash
   python -m wispralt.importer export
   ```

   This writes `~/WisprAlt-Backups/supabase-snapshot-YYYY-MM-DD/` (0700; `-2`, `-3`…
   if you export again the same day) with one `.jsonl` file per table and
   `counts.json`. Keep this folder: it is the durable backup, and nothing ever
   deletes it. If the project is paused or unreachable the command exits `3` and
   writes `BLOCKED.json` instead; it never unpauses or changes the project.
2. **Dry run.** `python -m wispralt.importer snapshot ~/WisprAlt-Backups/supabase-snapshot-… --dry-run`
   prints, per table, how many rows would be new / duplicate / linked / malformed /
   unmatched, the earliest record, and the proposed **owner mapping** (the one active
   Supabase admin → the one enabled local admin; that account is linked, not
   copied). Nothing is written.
3. **Import.** Stop the server, then run the same command without `--dry-run`. At a
   terminal it shows the dry run and asks before writing; in a script use `--yes`
   (accepted only when the owner proposal is unambiguous) or name the owner with
   `--owner-map PG_ID=LOCAL_ID`. Run `wispralt setup --role host` first if no local
   admin exists yet. Start the server again afterwards.
4. **Transcript files (optional).** `python -m wispralt.importer files ~/Documents/WisprAlt… --owner LOCAL_USER_ID --dry-run`,
   then again with `--yes`. Each transcript becomes a file job in that person's
   history; the source files are copied, never moved. See
   [TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md#importing-old-transcript-files-part-7).

Imported people get no API key: they rejoin by a new invite/key as usual. Re-running
any step is safe: rows already imported count as `duplicate`, never twice. Exit codes:
`0` ok, `1` error, `2` refused (no `--yes` without a terminal, owner mapping needed,
server running), `3` BLOCKED, `4` integrity failure (the snapshot does not match its
`counts.json`). Problems are listed in [TROUBLESHOOTING.md](TROUBLESHOOTING.md#history-import).

---

## Future hooks (CRM)

Two TEXT discriminators in the schema are pre-positioned for CRM
expansion without further migrations:

- `users.role` — currently `'admin' | 'employee'`. Adding
  `'contractor'`, `'paid'`, `'free'`, etc. is a new numbered migration
  (SQLite rebuilds the table to change a CHECK). The auth middleware already returns
  the `role` string verbatim; the only place that gates on it today is
  `auth.require_admin`.
- `usage_events.kind` — currently `'dictate' | 'file' | 'v1_dictate' | …` (older rows may carry `'meeting'`). New
  event types (`'export'`, `'login'`, `'crm_sync'`, …) just enqueue with
  a new string; no schema change needed.

The `usage_events` table is the analytics goldmine for CRM tie-in: every
authenticated, tracked request produces one row with `(user_id, ts, kind,
status, duration_ms, bytes_in, bytes_out, request_id)`. Joining
`users` against an external CRM by `users.label` (typically the
employee's email) is straightforward; the `usage_events.id` `INTEGER PRIMARY
KEY` gives you a stable cursor for incremental sync.
