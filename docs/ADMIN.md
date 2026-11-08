---
title: Admin UI
---

# Admin UI

Server-rendered Jinja2 portal at `https://transcribe.<your-domain>/admin/*`
(Jinja + vendored HTMX/Alpine, one stylesheet `admin/static/portal.css`, no build
step). It lets the operator manage people, devices, invites and integration keys,
watch the host, search team transcripts, read measured stats and (optionally) AI
insights, without touching the database directly.

Source: `server/src/wispralt/routes/{admin_ui,admin_invites,admin_system,admin_data}.py`
+ `server/src/wispralt/admin/templates/*.html.j2`.

Navigation (admins): **Overview · People · Integrations · Invites · Usage · Records ·
Data · System · Settings**, plus **My history · My insights**. Employees see only
**My history · My insights · My usage**. The sidebar becomes a scrolling strip on a
phone; light and dark follow the system setting.

---

## When to use it

- Onboarding a new person: create them (**Add employee**) and send the install
  command, server URL and key.
- Adding a device for someone who already has an account: **Add device** on their page.
- Off-boarding: disable the person; their next request gets 401.
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

### First-admin setup in the portal (`/admin/setup`)

The terminal bootstrap stays the primary path. As an alternative, while **no enabled
admin exists** the server writes a one-time code to `DATA_DIR/setup-code` (mode 0600,
128 bits) at startup and logs only its path. Open `/admin/setup`, paste the file's
content (`cat "$DATA_DIR/setup-code"` on the server) and a label: the server creates
the admin, shows its key **once** (`Cache-Control: no-store`), signs the browser in
and deletes the file. From then on `/admin/setup` returns 404. Reading the file
proves local access to the host; the page cannot be used from the tunnel without it.
Two people submitting at once still produce exactly one admin.

---

## Logging in

Two authentication methods, both resolved by `auth.py`:

### Header (curl / Postman)

```
GET /admin/people HTTP/1.1
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
   `SameSite=Strict`, `max_age=8h`. On plain `http://` to a loopback host
   (`http://127.0.0.1:8000/admin` during setup) the session and CSRF cookies are sent
   without `Secure` (Safari drops Secure cookies there); every other request keeps it.
4. **Role-based redirect on success:**
   - `role='admin'` → 303 to `/admin/` (full overview dashboard).
   - `role='employee'` → 303 to `/admin/me` (the employee's own person page,
     without the admin actions; the sidebar shows only My history, My insights
     and My usage).

The macOS client's menubar **Open Portal** button targets
`<server>/admin/login` for everyone, so the same install ships to admins
and employees without a per-role configuration.

The cookie is read by `auth._extract_bearer` as a fallback when no
`Authorization: Bearer` header is present.

**CSRF (Part 4).** Every cookie-authenticated mutation (Add employee, person Rotate / Disable, Revoke device, Invites create / revoke, integration Add / Rotate / Revoke, AI runs, Settings, `PATCH /me`, `/me/history` delete / restore) needs the double-submit token: the `X-CSRF-Token` header or the `csrf_token` form field must equal the `wispralt_csrf` cookie (set by the page that rendered the form), else **403**. A request that authenticated with a valid `Authorization: Bearer` header needs no token (a cross-site page cannot set that header). A non-Bearer `Authorization` header (e.g. `Basic`) falls back to the cookie and so needs the token; an invalid Bearer is a 401, never a cookie fallback. `SameSite=Strict` on the cookies is a second layer.

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

### `GET /admin/people` — People

Lists every employee-kind person, including disabled ones (newest first): name
(`display_name`, else `label`), role, created, last seen (derived from
`MAX(usage_events.ts)`, never written per request) and status. Admins can
**optionally pre-set** `display_name` when adding someone (the app then skips its
name prompt); afterwards it is self-service through `PATCH /me` (see [API.md](API.md)).

### `GET /admin/people/{id}` — Person page

Request tiles (24 h / 7 d / 30 d, errors, p50), **Devices** and recent requests.
Each device is one API key with its label (from the invite), when it was added and
when it was **last seen** (`usage_events.key_id`, recorded per request since Part 6).
The three actions differ on purpose:

| Action | Route | Effect |
|---|---|---|
| **Add device** | `POST /admin/invites` with `user_id` | A single-use invite for this person; redeeming adds a key. Existing devices keep working. |
| **Revoke device** | `POST /admin/keys/{key_id}/revoke` | That one key stops working on its next request; the person's other devices keep working. |
| **Rotate key** | `POST /admin/people/{id}/rotate` | Revokes **every** key of the person, issues one new key shown **once** (`no-store`) and re-enables a disabled person. |
| **Disable person** | `POST /admin/people/{id}/disable` | None of their keys authenticate from the next request (401). You cannot disable yourself. Rotate re-enables. |

Auth looks every key up on every request (no cache), so each change applies to the
next request with no restart.

### `GET /admin/me` — My usage (employee self-service)

The same person page scoped to the **calling user** without the admin actions.
Mounted on a router that only requires a valid key, so employees see their own
devices and requests and nobody else's. Admins hitting `/admin/me` are 303'd to `/admin/`.

### `GET /admin/invites` — Invites

Invite a new person (label + role) or, from a person page, add a device. Each invite
is **single use**, expires after **10 minutes**, and is stored only as a sha256 hash.
The result page shows, once and with `Cache-Control: no-store`:

- the join link `wispralt://join?host=<SERVER_URL>&invite=<token>`, and
- the raw invite code.

**The macOS app does not handle join links or invite codes yet**, so for now onboard
people with **Add employee** (below). Any client can redeem an invite through the API:
it calls with `POST /invites/redeem` (see [API.md](API.md#post-invitesredeem)),
which creates the person (new invites) or adds a key (add device) atomically. The list
shows each invite as pending, expired or redeemed; **Revoke** expires a pending one.
An invite also stops working when the admin who made it is disabled.

### `GET /admin/records` — Team records

Search or list every person's dictations and file transcriptions (full-text search,
highlighted matches; filter by person and kind). **Admins can read team transcripts**;
the record page (`/admin/records/{kind}/{id}`) says so. Employees never reach this
page (403): they only ever see their own history at `/me/history`.

### `GET /admin/data` — Data & Insights

Two clearly separate parts, for the team or one person (`?user_id=N`), over the
selected range (Today / 7d / 30d / 90d / 1y / All):

- **Measured** — computed from the database, shown whether AI is on or off:
  dictations, file transcriptions, words, active days, file audio minutes, filler
  words (um, uh, like, you know), **time saved (an estimate against a 40 wpm typing
  baseline)**, dictation latency p50/p95 (one row per dictation: streamed chunks are not counted), error rate over whole requests, an 8-week trend and an
  hour-of-day chart. Every count tile opens the records behind it for the same
  window. The team view adds a per-person table (dictations, files, words, fillers,
  time saved).
- **AI interpretation** — only when an admin has enabled it in **Settings**. Shows the
  model, when it was generated and its cost. Every item (digest, action items,
  decisions, blockers, projects, topics, quotable line; for the team: themes, action
  items, blockers, decisions, knowledge gaps) links to the records it came from;
  items whose records were deleted are hidden. A **stale** notice appears when
  records from that week arrived after it was generated, and a topic also seen in
  earlier weeks says "seen N weeks". Insights made in the old format without sources
  show a "regenerate" notice instead.

**AI runs** (bottom of the page): pick the last full week or a backfill of up to 12
weeks, optionally **Regenerate existing**, and click **Estimate cost**. The estimate
(words that would be sent × 1.35 tokens/word × the model's input rate, plus your
30-day spend against the cap) appears with a **Start** button; nothing is sent until
you press it. Progress (people done / total) updates every 2 seconds while a run is
going. Each run ends as `done`, `failed` (every person failed, the team summary
failed, or a crash), `aborted` (`budget`: the next call's worst-case cost would cross
the 30-day cap, checked before every call; `rate_limit`: OpenRouter returned 429) or
`skipped` (AI off). Running a week again (without Regenerate) skips people who already
have an insight but still builds the team summary from all of them. Failures list only the person id and an error class.

The scheduler checks once an hour and runs the **last full week** when it has no
finished scheduled run (a failed attempt is retried after 6 hours). Older weeks are
only run by a backfill.

### `GET /admin/settings` — Settings

AI interpretation is **off** until an admin turns it on here after reading the
disclosure: when on, the text of each person's dictations and file transcriptions
from the week (up to about 30,000 words per person) is sent to OpenRouter and the
chosen model. Turning it on requires ticking the acknowledgement once (its time is
recorded). Also here: the **model** (default `x-ai/grok-4.3`), the **30-day spending
cap** (default $8) and the **timezone** for weeks and charts (blank = the server's
zone, else UTC). The OpenRouter key is never entered in the portal: the page shows
whether `OPENROUTER_API_KEY` is configured in the host `.env` (`~/.config/wispralt/.env`).

### `GET /admin/system` — System

Live host state, refreshed every 5 seconds: uptime and how many times the server has
started, version and git commit, CPU and load, memory (and the server's RSS), disk
free plus the sizes of the DB, its WAL, `outputs/` and `staging/`, model readiness
(dictation ready, file model loaded or loading), the file-job queue (pending,
running, failed in 24 h, oldest pending), the 5xx rate over the last hour, failure
counters since start (usage writes lost, dictations not saved, eviction self-check,
stream aborts), per-route latency, DB health and schema version, whether
`SERVER_URL` answers `/healthz` (checked at most once a minute), and clients:
**active** (seen in the last 2 minutes) versus **recent** (last 7 days), per device.
`/admin/system/snapshot?format=json` returns the same data as JSON.

### `GET /admin/usage` — Usage drill-down

Filters: `kind` (e.g. `dictate`, `file`; rows recorded before file jobs were renamed carry `meeting`), `status` (HTTP status),
`user_id`, `since`, `until`. Paginated 100 per page. Each row links back
to the person's page.

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

1. Open `/admin/people` and click **Add employee** (top-right).
2. Enter a `label` (1–80 chars, no control characters — e.g.
   `sam`, `alex-laptop`, `contractor-q2`), optionally enter a
   `display_name` (1–40 chars, no control characters — e.g. `Sarah Chen`;
   leaving it blank lets the employee set it themselves on first launch),
   and pick a role (default `employee`). Submit.
3. The result page shows, once, the install command, the server URL (from
   `settings.server_url`) and the freshly-minted key:

   ```bash
   git clone <repo-url> wispralt && cd wispralt
   ./install.sh --role client --client-mode local
   ```

   Send them by Signal / iMessage, never email. Closing the page without
   copying the key means rotating via **Rotate key** on the person's page.
4. The employee runs the command on their Mac (see [INSTALL.md](INSTALL.md)):
   it builds and opens WisprAlt in Local mode. They paste the server URL and
   key into Settings > Advanced (the key goes to the Keychain item
   `co.wispralt`) and grant the three macOS permissions.
5. Their first dictation populates `usage_events`; they appear on
   `/admin/people` with a non-null last seen.

The route handlers live at `routes/admin_ui.py:people_add_form` (GET) and
`people_add_submit` (POST). Both are gated by `require_admin`; the latter
calls `accounts.service.create_user` and renders
`employee_added.html.j2` with the install command, `settings.server_url` and the key.

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

1. Open `/admin/integrations/new` (**Integrations → Add API key**).
2. Enter the program name (1–40 chars, no control chars — e.g. `Buzz on
   Bob's MacBook`, `Open WebUI prod`); the label becomes `key-<slug>`.
3. Submit. The result page shows:
   - The plaintext token **once** (copy now or rotate later).
   - A ready-to-paste OpenAI env-var snippet:

     ```bash
     export OPENAI_BASE_URL=https://wispralt.example.com/v1
     export OPENAI_API_KEY=<token>
     ```

   - A pointer to [OPENAI-COMPAT.md](OPENAI-COMPAT.md).
4. Paste the snippet into the integrating program's environment.
   Any OpenAI Whisper-API client (`openai-python`, `openai-node`, curl,
   etc.) Just Works.

### Listing integration keys

`/admin/integrations` lists every integration key (analogous to `/admin/people`
for employees). Rows: label, created_at, last_seen_at, revoked status.

### Rotating an integration key

`POST /admin/integrations/{id}/rotate` revokes the key and issues a new one: same id /
label / kind. The page shows the new plaintext once. Update the program's
env var, restart the program. The old token stops working on its next
request.

### Revoking an integration key

`POST /admin/integrations/{id}/revoke` disables the key's user (`users.disabled_at`). Both integration routes only act on integration-kind users: a person or admin id is a 404 (yourself a 400); people are disabled or rotated from their own page.
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

## Off-boarding, lost devices and rotation

- **A person leaves:** People → their page → **Disable person**. Every key of theirs
  gets 401 from the next request (no token cache, no restart).
- **One laptop is lost:** their page → **Revoke device** on that row. Their other
  devices keep working.
- **A key may have leaked, or they need a fresh start:** **Rotate key** revokes all
  of their keys and shows one new key once. It also re-enables a disabled person.

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

These are read from `~/.config/wispralt/.env` (mode 0600; `wispralt setup` writes it). For the full
configuration reference see [SETUP-SERVER.md](SETUP-SERVER.md).

| Variable | Required | Notes |
|---|---|---|
| `DATA_DIR` | Yes | Absolute path of the data directory (`wispralt.db`, outputs, staging). `wispralt setup` writes `$HOME/Library/Application Support/WisprAlt/host` (or `--data-dir`). |
| `SERVER_LOG_PATH` / `SERVER_LOG_MAX_BYTES` / `SERVER_LOG_BACKUP_COUNT` | No | Rotating application log (default `~/Library/Logs/WisprAlt/wispralt.log`, 10 MB x 5 backups). `/admin/server-log` reads it. launchd's `server.log` / `server.err.log` hold crash output only. |
| `OPENROUTER_API_KEY` | No | Needed for AI insights, which also stay off until an admin enables them in **Settings** after the disclosure. When unset, no run can start. (Server smart formatting was removed in Part 4.) Get a key at https://openrouter.ai/keys. |
| `INSIGHTS_TIMEZONE` | No | Default timezone for insight weeks and charts when none is set in **Settings**; unset = the host's zone (`/etc/localtime`), else UTC. |

---

## Usage events from `/v1/audio/transcriptions`

Third-party API traffic via the OpenAI-compat shim is recorded with
`kind = "v1_dictate"` (native client traffic uses `kind = "dictate"`).
Both `kind` values land in the same `usage_events` table and are folded
into the per-person request tiles on `/admin/people/{id}` (the overview's
Dictations tiles count `dictations` rows, which `/v1` does not write). Per-person request lists on `/admin/people/{id}` show
`v1_dictate` rows alongside `dictate` and `file` rows so you can see
which path generated each request.

To split out third-party traffic, query the host's `wispralt.db` (SQLite; `ts` is
epoch seconds) directly:

```sql
SELECT count(*) FROM usage_events
 WHERE user_id = ?
   AND kind = 'v1_dictate'
   AND ts > unixepoch() - 7 * 86400;
```

This is by design — the same employee can dictate via the macOS client
AND ship a Python script that hits `/v1/audio/transcriptions`, and both
should count toward "their usage" for billing/quota conversations.

---

## Importing old history (Supabase + transcript files)

The old host's database was wiped, so the only surviving history is the usage
metadata in Supabase (who used it, when, how much: **no dictation text**) plus any
transcript files you kept. Part 7's importer brings both into the local
`wispralt.db`. Run every command from `~/.config/wispralt` (the `.env` the server
uses, with `DATA_DIR` set) with the Python `wispralt` is installed in
(`"$(uv tool dir)/wispralt/bin/python" -m wispralt.importer …`). Supabase is only ever read, and nothing it writes is ever deleted.

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
- `usage_events.kind` — currently `'dictate' | 'dictate_chunk' | 'file' | 'file_chunk' | 'v1_dictate' | 'v1_translations'` (see `usage.events.kind_for_path`) (older rows may carry `'meeting'`). New
  event types (`'export'`, `'login'`, `'crm_sync'`, …) just enqueue with
  a new string; no schema change needed.

The `usage_events` table is the analytics goldmine for CRM tie-in: every
authenticated, tracked request produces one row with `(user_id, ts, kind,
status, duration_ms, bytes_in, bytes_out, request_id)`. Joining
`users` against an external CRM by `users.label` (typically the
employee's email) is straightforward; the `usage_events.id` `INTEGER PRIMARY
KEY` gives you a stable cursor for incremental sync.
