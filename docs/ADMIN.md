---
title: Admin UI
---

# Admin UI

Server-rendered Jinja2 dashboard at `https://transcribe.<your-domain>/admin/*`.
Lets the operator mint tokens, revoke users, browse usage events, and export
CSV without ever touching the database directly.

Source: `server/src/wispralt_server/routes/admin_ui.py` +
`server/src/wispralt_server/admin/templates/*.html.j2`.

---

## When to use it

- Onboarding a new employee: mint a token, text it to them.
- Off-boarding: revoke the user; their dictation gets 401 within 60 seconds.
- Spot-checking usage: who's been active, what error rates look like, which
  requests failed and when.
- Auditing for billing or CRM tie-in (see [Future hooks](#future-hooks-crm)).

Everything the admin UI does is also reachable via raw SQL against the
`wispralt` schema — the UI is a thin convenience over `wispralt.users` +
`wispralt.usage_events`.

## Who should have admin role

Exactly one human (the operator). Employees get `role='employee'`. The
schema enforces `role IN ('admin', 'employee')` via a `CHECK` constraint
(`server/migrations/2026-04-27-v1-wispralt-schema.sql`); future roles
require a migration.

The first admin is auto-seeded from the `WISPRALT_API_KEY` env var on the
first server boot if `wispralt.users` is empty (see
`main.py:_seed_admin_if_empty`). Rotate it from
`/admin/users/<id>/mint` once a real label is in place.

---

## Logging in

Two authentication methods, both resolved by `auth.py`:

### Header (curl / Postman)

```
GET /admin/users HTTP/1.1
Host: transcribe.example.com
Authorization: Bearer <admin-token>
```

The bearer is sha256-hashed, looked up in the in-process `TokenCache`
(60s TTL), then in `wispralt.users` via the asyncpg pool.

### Session cookie (browser)

1. Visit `https://transcribe.<your-domain>/admin/login`.
2. Paste your token, submit. **Both admin and employee tokens are
   accepted** — the server routes by role.
3. The server validates the token (cache → Postgres → break-glass), then
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
`Authorization` header is present. CSRF is mitigated by `SameSite=Strict`
— browsers refuse to attach the cookie to cross-site POSTs.

---

## Pages

### `GET /admin/`  — Overview

Single-CTE dashboard (`_OVERVIEW_SQL` in `routes/admin_ui.py`). Tiles:

- Active / total users.
- Dictations in the last 24h / 7d / 30d.
- Errors in the last 24h.
- p50 latency over the last 24h (server-side, from `usage_events.duration_ms`).
- Top 5 active users in the last 7 days.
- Last-14-day usage rendered as a CSS-bar table (no chart library; works
  offline).

### `GET /admin/users` — Users

Lists every row in `wispralt.users` (most-recently-created first). The
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

- **Mint** — `POST /admin/users/{id}/mint` rotates the user's token in
  place: same id / label / role, new sha256 hash. The page redirects to
  `token_minted.html.j2` which shows the new plaintext **once**. Copy it,
  text it to the employee, close the tab.
- **Revoke** — `POST /admin/users/{id}/revoke` sets
  `wispralt.users.revoked_at = now()` and invalidates the cache entry by
  hash. Their next dictation hits Postgres, finds `revoked_at IS NOT NULL`,
  and returns 401.

`last_seen_at` is **derived** from `MAX(usage_events.ts)` per user — we
don't write it on every request (would contend with the usage drainer).

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

Filters: `kind` (e.g. `dictate`, `meeting`), `status` (HTTP status),
`user_id`, `since`, `until`. Paginated 100 per page. Each row links back
to its user detail.

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
   curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh \
     | WISPRALT_API_KEY=<token> WISPRALT_SERVER=https://transcribe.integrateapi.ai bash
   ```

   Click **Copy install command**, paste into Signal / iMessage. The
   plaintext token is shown **once** — closing the page without copying
   means rotating via **Mint new token** on the user's row.
4. The employee pastes the command into Terminal on their Mac. The
   installer downloads the latest signed DMG from GitHub Releases,
   copies the app to `/Applications`, seeds the token into the Keychain,
   and opens the System Settings panes for the four macOS permissions.
5. Their first dictation populates `usage_events`; they appear on
   `/admin/users` with a non-null `last_seen_at`.

The route handlers live at `routes/admin_ui.py:users_add_form` (GET) and
`users_add_submit` (POST). Both are gated by `require_admin` +
`_require_db_pool`; the latter calls `users_store.mint` and renders
`employee_added.html.j2` with the install one-liner composed from
`settings.server_url`.

## Revoking an employee

1. `/admin/users` → find the row → click **Revoke**.
2. The route sets `revoked_at = now()` and calls `token_cache.invalidate`
   on their hash.
3. Within 60 seconds (the `TokenCache._TTL_S`), every cache hit for that
   token expires. The next request goes back to Postgres, finds the row
   excluded by the partial index `users_idx_token_hash WHERE revoked_at
   IS NULL`, returns 401.

If you need an instant lockout (no 60s wait), restart the server — the
cache is in-process only.

---

## Break-glass admin (Postgres degraded)

If Supabase is unreachable at server startup, `lifespan` logs a WARNING
and continues with `app.state.db_pool = None`. In that mode:

- Every authenticated route except the admin UI still works for whoever
  holds the env-var `WISPRALT_API_KEY`. Their requests resolve via the
  break-glass branch in `auth.py:require_api_key` and get a synthetic
  `User(id=-1, label="break-glass-admin", role="admin")`.
- The admin UI returns 503 ("Admin UI unavailable: Postgres degraded.")
  because every page issues a query.
- Usage events with `user_id == -1` are skipped at the middleware so the
  drainer doesn't hit FK violations once Postgres comes back.

`POST /admin/rotate-key` (`routes/admin.py`) is intentionally retained
as the last-resort tool for rotating the env-var token while Postgres is
down — once the multi-token system is fully migrated, all rotation
should happen via the admin UI's mint flow.

---

## Server env vars (admin-relevant)

These are read from `server/.env` (mode 0600). For the full
configuration reference see [SETUP-SERVER.md](SETUP-SERVER.md).

| Variable | Required | Notes |
|---|---|---|
| `WISPRALT_API_KEY` | Yes | First-boot break-glass admin token; seeds the initial admin row in `wispralt.users` if the table is empty. |
| `SUPABASE_DATABASE_URL` | Yes (multi-tenant) | Postgres connection string for the `wispralt` schema. Without it, multi-token auth and usage events both fall back to break-glass mode. |
| `HF_TOKEN` | Yes | Pyannote gated-model access. |
| `OPENROUTER_API_KEY` | No | Enables the **Smart formatting** toggle. When set, `/transcribe/dictate` requests with `X-Smart-Format: true` and at least `SMART_FORMAT_MIN_WORDS` words (default 100) are post-processed by Mercury 2 — punctuation, casing, filler removal, and light list formatting; meaning preserved. Below the threshold, the call short-circuits to raw. When unset, the toggle is silently a no-op — the server returns raw text and `smart_formatted: false` regardless of the header. Get a key at https://openrouter.ai/keys. |
| `SMART_FORMAT_MIN_WORDS` | No | Minimum word count for smart-formatting to engage. Default 100. Below this, raw Parakeet output is returned unchanged — short utterances aren't worth the LLM round-trip. |

---

## Usage events from `/v1/audio/transcriptions`

Third-party API traffic via the OpenAI-compat shim is recorded with
`kind = "v1_dictate"` (native client traffic uses `kind = "dictate"`).
Both `kind` values land in the same `wispralt.usage_events` table and
are folded into the **Dictations 24h / 7d / 30d** tiles on
`/admin/` and `/admin/users/{id}` because the underlying SQL aggregates
by user, not by kind. Per-user event lists on `/admin/users/{id}` show
`v1_dictate` rows alongside `dictate` and `meeting` rows so you can see
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

## Future hooks (CRM)

Two TEXT discriminators in the schema are pre-positioned for CRM
expansion without further migrations:

- `wispralt.users.role` — currently `'admin' | 'employee'`. Adding
  `'contractor'`, `'paid'`, `'free'`, etc. is `ALTER TABLE wispralt.users
  DROP CONSTRAINT ...` + a new CHECK. The auth middleware already returns
  the `role` string verbatim; the only place that gates on it today is
  `auth.require_admin`.
- `wispralt.usage_events.kind` — currently `'dictate' | 'meeting'`. New
  event types (`'export'`, `'login'`, `'crm_sync'`, …) just enqueue with
  a new string; no schema change needed.

The `usage_events` table is the analytics goldmine for CRM tie-in: every
authenticated, tracked request produces one row with `(user_id, ts, kind,
status, duration_ms, bytes_in, bytes_out, request_id)`. Joining
`wispralt.users` against an external CRM by `users.label` (typically the
employee's email) is straightforward; the `usage_events.id` `BIGSERIAL`
gives you a stable cursor for incremental sync.
