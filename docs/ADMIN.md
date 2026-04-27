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
2. Paste the admin token, submit.
3. The server validates it (cache → Postgres → break-glass), then sets a
   `wispralt_admin_token` cookie with `HttpOnly`, `Secure`,
   `SameSite=Strict`, `max_age=8h`.
4. Subsequent navigation works without the header.

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

Lists every row in `wispralt.users` (most-recently-created first).
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
`WHERE user_id = $1`.

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

1. `/admin/users` — find the placeholder row for the new employee, OR
   ask Omid to insert one via Supabase Studio for now (the v1 admin UI
   only rotates existing rows; "create user" is a planned addition).
2. Click **Mint** on their row. The next page shows a 64-hex-char
   plaintext token.
3. Copy the token. Text it to the employee via Signal / iMessage — it is
   shown **once** and never persisted in plaintext anywhere.
4. The employee runs `/wispralt-setup` in Claude Code, which downloads
   the latest signed DMG from GitHub Releases, copies it to
   `/Applications`, walks them through the four macOS permission
   prompts, and tells them to paste the token into the Settings pane.
5. Their first dictation populates `usage_events`; they appear on
   `/admin/users` with a non-null `last_seen_at`.

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
