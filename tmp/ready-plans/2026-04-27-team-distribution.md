# Plan: Team Distribution + Multi-Tenant + Admin UI

> Brief: `tmp/briefs/2026-04-26-team-distribution.md` (locked design)
> Author: Claude (Opus 4.7, 1M)
> Branch base: `origin/main` @ `5c56465`
> Confidence: 8/10 (Postgres + asyncpg + Jinja2 are well-trodden; the
> admin UI's analytics queries are the main "could need iteration" surface)

## Goal

Roll WisprAlt out to Omid's small team via a "clone-and-run-a-slash-command"
flow. Concretely deliver:

1. Multi-token bearer auth backed by Supabase Postgres (schema `wispralt`).
2. Per-user usage event tracking (fire-and-forget, off the dictation
   hot path).
3. Server-rendered admin UI at `/admin/*` (Jinja2, no SPA).
4. Pre-built signed DMG attached to GitHub Releases.
5. Two slash commands in `~/.claude-dotfiles/commands/` —
   `wispralt-setup.md` and `wispralt-update.md`.

Decisions are **locked** in the brief. No re-litigation.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ EMPLOYEE MAC                                                                │
│   Claude Code session → /wispralt-setup or /wispralt-update                 │
│       gh release download --pattern '*.dmg' → install to /Applications      │
│       open WisprAlt.app                                                     │
│       PermissionGate.swift walks them through 4 macOS permissions           │
│       Settings pane: paste API key (texted by Omid)                         │
│       Keychain (service co.wispralt) stores key                             │
│   FN-hold dictation flow → Bearer <key> → transcribe.integrateapi.ai        │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │ HTTPS + Bearer
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ MAC MINI (already provisioned)                                              │
│   Cloudflare Tunnel → 127.0.0.1:8080 → uvicorn (FastAPI)                    │
│                                                                             │
│   Auth path  ┌── auth.py:require_api_key                                    │
│              │     1. sha256(bearer)                                        │
│              │     2. lookup in TokenCache (60s LRU, in-process)            │
│              │     3. cache miss → asyncpg pool → wispralt.users            │
│              │     4. attach (user_id, role, label) to request.state.user   │
│              │     5. break-glass: env-var WISPRALT_API_KEY = admin if      │
│              │        Postgres unreachable at startup                       │
│              │                                                              │
│   Hot path   ┌── routes/dictate.py / routes/meeting.py (UNCHANGED)          │
│   (dictation)│     ↓ ParakeetService.transcribe (~150ms)                    │
│              │     ↓ middleware/observability.dispatch                      │
│              │       │ records to LatencyHistogram (in-process)             │
│              │       │ AND enqueues UsageEvent → bounded asyncio.Queue      │
│              │       │ (fire-and-forget; Postgres OFF the hot path)         │
│                                                                             │
│   BG drainer ┌── usage.writer:drain_loop()                                  │
│              │     batch 50 events / flush every 1s, whichever first        │
│              │     async write to wispralt.usage_events via asyncpg pool    │
│              │     queue full → drop oldest, log WARNING                    │
│                                                                             │
│   Admin UI   ┌── routes/admin_ui.py (NEW)                                   │
│              │   /admin/                  → overview dashboard              │
│              │   /admin/users             → list + revoke + mint            │
│              │   /admin/users/{id}        → per-user detail                 │
│              │   /admin/usage             → drill-down + CSV export         │
│              │   Jinja2 templates in admin/templates/                       │
│              │   Auth: same bearer middleware, requires role='admin'        │
│                                                                             │
│   SQLite     ┌── jobs.db (UNCHANGED)                                        │
│              │   meeting-job orchestration only                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │ Postgres wire (asyncpg pool)
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ SUPABASE (project qglwmwmdoxopnubghnul; schema `wispralt`)                  │
│   wispralt.schema_version(version, applied_at)                              │
│   wispralt.users(id, label, token_hash, role, created_at,                   │
│                  last_seen_at, revoked_at, notes)                           │
│   wispralt.usage_events(id, user_id, ts, kind, status,                      │
│                          chars, duration_ms, bytes_in,                      │
│                          bytes_out, error_class, request_id)                │
│   wispralt.users_idx_token_hash, usage_idx_user_ts, usage_idx_kind_ts       │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Files Being Changed

```
server/
├── pyproject.toml                                 ← MODIFIED (add asyncpg, jinja2)
├── .env.example                                   ← MODIFIED (add SUPABASE_DATABASE_URL)
├── src/wispralt_server/
│   ├── auth.py                                    ← REPLACED (token-hash + cache + break-glass)
│   ├── config.py                                  ← MODIFIED (SUPABASE_DATABASE_URL setting)
│   ├── main.py                                    ← MODIFIED (asyncpg pool + admin mount + usage drainer task)
│   ├── observability.py                           ← MODIFIED (UsageEventQueue singleton)
│   ├── db.py                                      ← NEW (asyncpg pool factory)
│   ├── users/                                     ← NEW package
│   │   ├── __init__.py
│   │   ├── store.py                               ← NEW (CRUD: lookup, revoke, mint)
│   │   └── cache.py                               ← NEW (60s LRU TokenCache)
│   ├── usage/                                     ← NEW package
│   │   ├── __init__.py
│   │   ├── events.py                              ← NEW (UsageEvent dataclass)
│   │   ├── queue.py                               ← NEW (bounded asyncio.Queue)
│   │   └── writer.py                              ← NEW (drain loop, Postgres batch insert)
│   ├── routes/
│   │   ├── admin.py                               ← MODIFIED (rotate-key now updates wispralt.users)
│   │   └── admin_ui.py                            ← NEW (Jinja2 routes for /admin/*)
│   └── admin/                                     ← NEW package (templates only)
│       └── templates/
│           ├── base.html.j2                       ← NEW (layout, CSS)
│           ├── login.html.j2                      ← NEW (/admin/login form)
│           ├── overview.html.j2                   ← NEW (/admin/)
│           ├── users.html.j2                      ← NEW (/admin/users)
│           ├── user_detail.html.j2                ← NEW (/admin/users/{id})
│           ├── usage.html.j2                      ← NEW (/admin/usage)
│           └── token_minted.html.j2               ← NEW (one-time plaintext display after rotate/mint)
│
├── tests/
│   ├── test_token_cache.py                        ← NEW (LRU + TTL behavior, miss path)
│   ├── test_usage_writer.py                       ← NEW (queue full → drop, batch flush, FK retry)
│   ├── test_admin_routes_auth.py                  ← NEW (employee role gets 403 on /admin/*)
│   └── test_auth_break_glass.py                   ← NEW (Postgres-down → break-glass admin works)
│
scripts/
├── release-client.sh                              ← NEW (build + sign + gh release create)
│
docs/
├── ADMIN.md                                       ← NEW (admin UI walkthrough; future-CRM hooks)
├── DEPLOY-TEAM.md                                 ← NEW (Omid-side: how to ship a release)
├── ARCHITECTURE.md                                ← MODIFIED (auth diagram, usage events, admin UI)
├── API.md                                         ← MODIFIED (auth section, admin routes)
├── OVERVIEW.md                                    ← MODIFIED (file → doc map for new files)
├── SETUP-CLIENT.md                                ← MODIFIED (employee install via slash command)
└── TROUBLESHOOTING.md                             ← MODIFIED ("API key rejected" + "admin UI 401")
│
~/.claude-dotfiles/commands/
├── wispralt-setup.md                              ← NEW (employee first-time install)
└── wispralt-update.md                             ← NEW (employee pull-based update)
```

---

## Migration: Postgres schema (Supabase)

Applied via Supabase MCP `apply_migration`. Single migration, name
`v1_wispralt_schema`.

```sql
-- =============================================================================
-- wispralt v1 schema
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS wispralt;

-- Migration tracking. Future migrations check this table for the highest
-- version applied and run anything newer.
CREATE TABLE wispralt.schema_version (
    version       INTEGER PRIMARY KEY,
    applied_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes         TEXT
);
INSERT INTO wispralt.schema_version (version, notes)
VALUES (1, 'Initial: users + usage_events');

-- Users / bearer tokens.
-- token_hash is sha256 hex of the plaintext bearer.
-- role is text (not enum) to allow future roles without ALTER TYPE.
CREATE TABLE wispralt.users (
    id              SERIAL       PRIMARY KEY,
    label           TEXT         NOT NULL,
    token_hash      TEXT         NOT NULL UNIQUE,
    role            TEXT         NOT NULL CHECK (role IN ('admin', 'employee')),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    notes           TEXT
);
CREATE INDEX users_idx_token_hash ON wispralt.users (token_hash)
    WHERE revoked_at IS NULL;

-- Per-request event log.
-- kind is text (not enum) — future event types add without migrations.
-- chars/duration_ms/bytes are NULL when not applicable to that kind.
-- ON DELETE RESTRICT preserves audit history.  A user can be revoked
-- (revoked_at IS NOT NULL) without losing their usage rows.  If a row
-- ever needs hard-deletion, the operator must first reassign or
-- explicitly delete the audit rows — refused at the DB layer prevents
-- accidental "billing-relevant" data loss via Supabase Studio.
CREATE TABLE wispralt.usage_events (
    id              BIGSERIAL    PRIMARY KEY,
    user_id         INTEGER      NOT NULL REFERENCES wispralt.users(id) ON DELETE RESTRICT,
    ts              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    kind            TEXT         NOT NULL,
    status          INTEGER      NOT NULL,
    chars           INTEGER,
    duration_ms     REAL,
    bytes_in        INTEGER,
    bytes_out       INTEGER,
    error_class     TEXT,
    request_id      TEXT
);
CREATE INDEX usage_idx_user_ts  ON wispralt.usage_events (user_id, ts DESC);
CREATE INDEX usage_idx_kind_ts  ON wispralt.usage_events (kind, ts DESC);
CREATE INDEX usage_idx_ts       ON wispralt.usage_events (ts DESC);
```

---

## Key Pseudocode

### Pinned deps for `pyproject.toml`

Add to main dependencies (match existing pin convention):
- `asyncpg>=0.29,<0.31`
- `jinja2>=3.1,<4`

### `db.py` — asyncpg pool factory

```python
# Single global pool, lazy-init from settings.supabase_database_url.
# Pool of 10 connections — drainer holds one for the whole batch flush;
# auth lookups + admin UI compete for the rest.
import logging
import asyncpg

from wispralt_server.config import settings

logger = logging.getLogger(__name__)


class PostgresUnavailable(RuntimeError):
    """Typed error for when the database URL is missing or pool init fails.

    Lifespan catches this so the server still boots (with break-glass
    admin) when Postgres is degraded.
    """


_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    url = settings.supabase_database_url
    if url is None:
        raise PostgresUnavailable("SUPABASE_DATABASE_URL not set")
    _pool = await asyncpg.create_pool(
        url.get_secret_value(),
        min_size=1, max_size=10,
        command_timeout=10.0,
        server_settings={"application_name": "wispralt-server"},
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
```

### `users/store.py` — CRUD, all coroutines

Two dataclasses: `User` is the minimal auth-time identity (3 fields,
cached); `UserRow` is the rich row used by the admin UI. `last_seen_at` is
**derived** from `MAX(usage_events.ts)` at admin-read time — we do **not**
maintain a column write per request (avoids contending with the usage
writer's bulk pool acquisitions).

```python
import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
import asyncpg


@dataclass(frozen=True, slots=True)
class User:
    """Auth-time identity — small, hashable, cached for 60s."""
    id: int
    label: str
    role: str  # 'admin' | 'employee'


@dataclass(frozen=True, slots=True)
class UserRow:
    """Admin-UI row (richer than User; never cached)."""
    id: int
    label: str
    role: str
    created_at: datetime
    revoked_at: datetime | None
    last_seen_at: datetime | None  # derived from MAX(usage_events.ts)


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


async def lookup(pool: asyncpg.Pool, token_hash: str) -> User | None:
    row = await pool.fetchrow(
        "SELECT id, label, role FROM wispralt.users "
        "WHERE token_hash = $1 AND revoked_at IS NULL",
        token_hash,
    )
    if row is None:
        return None
    return User(id=row["id"], label=row["label"], role=row["role"])


async def lookup_by_id(pool: asyncpg.Pool, user_id: int) -> User | None:
    row = await pool.fetchrow(
        "SELECT id, label, role FROM wispralt.users WHERE id = $1",
        user_id,
    )
    if row is None:
        return None
    return User(id=row["id"], label=row["label"], role=row["role"])


async def mint(pool: asyncpg.Pool, *, label: str, role: str) -> tuple[User, str]:
    plaintext = secrets.token_hex(32)  # 64 hex chars
    th = hash_token(plaintext)
    row = await pool.fetchrow(
        "INSERT INTO wispralt.users (label, token_hash, role) "
        "VALUES ($1, $2, $3) RETURNING id, label, role",
        label, th, role,
    )
    return User(id=row["id"], label=row["label"], role=row["role"]), plaintext


async def rotate(pool: asyncpg.Pool, user_id: int) -> tuple[str, str | None]:
    """Replace the user's token in place. Same id/label/role, new hash.

    Returns ``(new_plaintext, old_token_hash)`` so the caller can invalidate
    the cache for the old hash AFTER the row commit lands.

    Postgres semantics gotcha: a sub-SELECT inside RETURNING evaluates
    against the POST-update snapshot — it would return the new hash, not
    the old.  Use a CTE that captures the old value first.
    """
    plaintext = secrets.token_hex(32)
    new_th = hash_token(plaintext)
    old_th = await pool.fetchval(
        """
        WITH prev AS (
            SELECT token_hash AS old_hash
              FROM wispralt.users WHERE id = $2
        )
        UPDATE wispralt.users
           SET token_hash = $1, revoked_at = NULL
         WHERE id = $2
        RETURNING (SELECT old_hash FROM prev)
        """,
        new_th, user_id,
    )
    return plaintext, old_th


async def revoke(pool: asyncpg.Pool, user_id: int) -> str | None:
    """Mark a user as revoked. Returns the now-revoked token_hash so the
    caller can invalidate the cache entry **after** the DB commit (closes
    the cache-staleness race)."""
    return await pool.fetchval(
        "UPDATE wispralt.users SET revoked_at = now() WHERE id = $1 "
        "RETURNING token_hash",
        user_id,
    )


async def list_all(pool: asyncpg.Pool) -> list[UserRow]:
    """For admin UI. Joins to derive last_seen_at from usage_events.

    Single round-trip; the LEFT JOIN against the per-user MAX(ts) keeps
    the admin page fast even with thousands of usage events.
    """
    rows = await pool.fetch(
        """
        SELECT u.id, u.label, u.role, u.created_at, u.revoked_at,
               (SELECT MAX(ts) FROM wispralt.usage_events e
                WHERE e.user_id = u.id) AS last_seen_at
          FROM wispralt.users u
         ORDER BY u.created_at DESC
        """
    )
    return [
        UserRow(
            id=r["id"], label=r["label"], role=r["role"],
            created_at=r["created_at"], revoked_at=r["revoked_at"],
            last_seen_at=r["last_seen_at"],
        )
        for r in rows
    ]
```

### `users/cache.py` — 60s in-memory LRU cache

```python
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import User


class TokenCache:
    """LRU cache keyed by token_hash. 60s TTL, max 256 entries."""
    _MAX = 256
    _TTL_S = 60.0

    def __init__(self) -> None:
        self._items: OrderedDict[str, tuple[User, float]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, token_hash: str) -> User | None:
        with self._lock:
            entry = self._items.get(token_hash)
            if entry is None:
                return None
            user, ts = entry
            if time.monotonic() - ts > self._TTL_S:
                del self._items[token_hash]
                return None
            self._items.move_to_end(token_hash)  # LRU touch
            return user

    def put(self, token_hash: str, user: User) -> None:
        with self._lock:
            self._items[token_hash] = (user, time.monotonic())
            self._items.move_to_end(token_hash)
            while len(self._items) > self._MAX:
                self._items.popitem(last=False)

    def invalidate(self, token_hash: str) -> None:
        with self._lock:
            self._items.pop(token_hash, None)
```

### `auth.py` — replaced

The lifespan seeds the env-var token's hash as a real `wispralt.users`
row on first boot (see `_seed_admin_if_empty`). Once seeded, **break-
glass and the seeded admin row resolve to the SAME token_hash → same
real `users.id`**, so usage events for break-glass requests have a
valid FK target and don't get dropped. The break-glass branch only
short-circuits when Postgres is unreachable (no pool).

```python
# Replaces the existing single-key compare_digest auth.
# Bearer → sha256 → cache lookup → Postgres miss path → break-glass env var.

def _extract_bearer(request: Request) -> str | None:
    """Extract bearer token from Authorization header.

    The current auth.py inlines this; we factor it into a helper for
    reuse and so the multi-Authorization-header guard is in one place.
    """
    headers = request.headers.getlist("authorization")
    if len(headers) > 1:
        raise HTTPException(400, "Multiple Authorization headers not allowed")
    if not headers:
        # Try cookie fallback for admin UI browser sessions.
        cookie = request.cookies.get("wispralt_admin_token")
        return cookie or None
    raw = headers[0]
    if not raw.lower().startswith("bearer "):
        return None
    token = raw[7:].strip()
    return token or None


async def require_api_key(request: Request) -> User:
    bearer = _extract_bearer(request)
    if not bearer:
        raise HTTPException(401, "Missing bearer token")

    th = users.store.hash_token(bearer)

    # 1. cache fast-path
    cached = token_cache.get(th)
    if cached is not None:
        request.state.user = cached
        return cached

    # 2. Postgres lookup (the seeded break-glass row also lives here once
    #    the lifespan has run; this is the path 99% of break-glass calls
    #    take).
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            user = await users.store.lookup(pool, th)
        except asyncpg.PostgresError:
            logger.exception("Postgres lookup failed; trying break-glass")
            user = None
        if user is not None:
            token_cache.put(th, user)
            request.state.user = user
            return user

    # 3. Break-glass: pool is None OR Postgres errored OR row missing.
    #    Env-var token still grants admin so the operator never locks
    #    themselves out. user.id = -1 is a sentinel; the middleware
    #    skips usage-event enqueue for negative ids (no FK violation).
    bg_hash = getattr(request.app.state, "break_glass_token_hash", None)
    if bg_hash is not None and th == bg_hash:
        user = User(id=-1, label="break-glass-admin", role="admin")
        request.state.user = user
        logger.warning("auth: break-glass admin path used (Postgres degraded)")
        return user

    if pool is None:
        raise HTTPException(503, "Auth temporarily unavailable")
    raise HTTPException(401, "Invalid bearer token")


def require_admin(user: User = Depends(require_api_key)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin role required")
    return user
```

### `usage/queue.py` — bounded queue + UsageEvent dataclass

`UsageEventQueue.offer()` is **async-loop-only**: the underlying
`asyncio.Queue` is not thread-safe. The middleware that calls `offer()`
(`_ObservabilityMiddleware.dispatch`) runs on the FastAPI event loop, so
this is safe. A defensive `assert` on the running loop catches future
misuse from a thread pool.

```python
import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UsageEvent:
    user_id: int
    ts: float                  # unix seconds
    kind: str                  # "dictate" | "meeting" | ...
    status: int
    chars: int | None = None
    duration_ms: float | None = None
    bytes_in: int | None = None
    bytes_out: int | None = None
    error_class: str | None = None
    request_id: str | None = None


class UsageEventQueue:
    """Bounded asyncio.Queue. Drops oldest on overflow with WARNING log.

    NOT thread-safe. Callers MUST be inside the running event loop.
    """
    _MAX = 1000

    def __init__(self) -> None:
        self._q: asyncio.Queue[UsageEvent] = asyncio.Queue(maxsize=self._MAX)
        self._dropped: int = 0

    def offer(self, event: UsageEvent) -> None:
        # Defense: refuse to operate from a non-loop thread.
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # no running loop in this thread
            logger.warning("UsageEventQueue.offer() called from non-async context; dropping")
            self._dropped += 1
            return
        try:
            self._q.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
            try:
                _ = self._q.get_nowait()  # drop oldest
                self._q.put_nowait(event)
            except asyncio.QueueEmpty:
                pass
            logger.warning("usage_event queue full; dropped one (total=%d)", self._dropped)

    async def drain_one(self) -> UsageEvent:
        return await self._q.get()

    @property
    def dropped(self) -> int:
        return self._dropped
```

### `usage/writer.py` — background drain loop

```python
async def drain_loop(queue: UsageEventQueue, pool: asyncpg.Pool) -> None:
    """Long-running task started in lifespan; cancelled on shutdown.

    Batches up to 50 events or flushes every 1s, whichever first.
    """
    BATCH_MAX = 50
    FLUSH_INTERVAL_S = 1.0
    batch: list[UsageEvent] = []
    last_flush = time.monotonic()

    while True:
        try:
            timeout = max(0.05, FLUSH_INTERVAL_S - (time.monotonic() - last_flush))
            event = await asyncio.wait_for(queue.drain_one(), timeout=timeout)
            batch.append(event)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            if batch:
                await _flush(pool, batch)
            raise

        if len(batch) >= BATCH_MAX or time.monotonic() - last_flush >= FLUSH_INTERVAL_S:
            if batch:
                try:
                    await _flush(pool, batch)
                except Exception:
                    logger.exception("usage_event flush failed; dropping batch of %d", len(batch))
                batch = []
                last_flush = time.monotonic()


async def _flush(pool: asyncpg.Pool, batch: list[UsageEvent]) -> None:
    """Insert a batch via ``executemany`` inside an explicit transaction.

    On a ``ForeignKeyViolationError`` the transaction is auto-rolled-back;
    a fresh transaction then retries the survivor rows (those with
    ``user_id >= 0``).  Without the explicit transaction, the post-error
    connection would be in an aborted state and the retry would hit
    ``InFailedSQLTransactionError``.  Typed-error catching only — no
    bare ``except``.
    """
    rows = [
        (e.user_id, datetime.fromtimestamp(e.ts, tz=timezone.utc), e.kind,
         e.status, e.chars, e.duration_ms, e.bytes_in, e.bytes_out,
         e.error_class, e.request_id)
        for e in batch
    ]
    INSERT_SQL = (
        "INSERT INTO wispralt.usage_events "
        "(user_id, ts, kind, status, chars, duration_ms, bytes_in, "
        " bytes_out, error_class, request_id) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)"
    )
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await conn.executemany(INSERT_SQL, rows)
        except asyncpg.ForeignKeyViolationError:
            # Original transaction auto-rolled-back. Retry survivors in
            # a fresh transaction.
            survivors = [r for r in rows if r[0] >= 0]
            if survivors:
                async with conn.transaction():
                    await conn.executemany(INSERT_SQL, survivors)
            logger.warning(
                "usage_event flush dropped %d FK-violating rows; retried %d",
                len(rows) - len(survivors), len(survivors),
            )
```

### Observability middleware extension

```python
# main.py existing _ObservabilityMiddleware.dispatch() gains 3 lines:
async def dispatch(self, request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    latency_ms = (time.perf_counter() - t0) * 1000.0

    parts = [p for p in request.url.path.strip("/").split("/") if p]
    route_key = "/".join(parts[:2]) if parts else "root"
    observability.request_counter.increment(route_key, response.status_code)
    if response.status_code >= 400:
        observability.error_counter.increment(route_key, response.status_code)
    observability.latency_histogram.record(route_key, latency_ms)

    # NEW — enqueue usage event if we have a user and it's a tracked route.
    # user.id < 0 indicates a break-glass admin (no DB row); skip enqueue
    # to avoid FK violations in the writer.
    user = getattr(request.state, "user", None)
    if (user is not None
            and user.id >= 0
            and route_key in TRACKED_ROUTES
            and request.method in TRACKED_METHODS):
        observability.usage_queue.offer(UsageEvent(
            user_id=user.id,
            ts=time.time(),
            kind=route_key.split("/")[-1],   # "dictate" | "meeting"
            status=response.status_code,
            duration_ms=latency_ms,
            bytes_in=int(request.headers.get("content-length", 0) or 0),
            request_id=request.headers.get("x-request-id")
                       or getattr(request.state, "request_id", None),
        ))

    return response

# Track only the request-creating endpoints, NOT status-poll GETs that
# clients may hammer every few seconds.  /transcribe/meeting POST creates
# a job; /transcribe/meeting/{id} GET is the poll path — which we exclude
# by also filtering on request.method.
TRACKED_ROUTES = frozenset(["transcribe/dictate", "transcribe/meeting"])
TRACKED_METHODS = frozenset({"POST"})

# In dispatch, before offering:
# if request.method not in TRACKED_METHODS: skip enqueue
```

### Admin UI auth model

Browsers can't easily attach `Authorization: Bearer ...` headers when
navigating between admin pages. We support TWO auth methods:

1. **Header (`Authorization: Bearer ...`)** — for curl/Postman/extension
   users.  Already covered by `_extract_bearer`.
2. **Session cookie (`wispralt_admin_token`)** — for browser users.
   `GET /admin/login` shows a one-field form; `POST /admin/login` validates
   the bearer against the same auth path and sets an HttpOnly,
   SameSite=Strict, Secure cookie.  `_extract_bearer` already falls back
   to the cookie when no Authorization header is present.

CSRF: SameSite=Strict cookies are blocked from cross-site POSTs by the
browser, so vanilla CSRF is not exploitable for v1.  A future iteration
can add per-session CSRF tokens; not in scope now (single-operator
internal tool).

### Admin UI route shape (`routes/admin_ui.py`)

```python
from pathlib import Path
from fastapi.templating import Jinja2Templates
from jinja2 import select_autoescape

# Resolve relative to this file so launchd's CWD doesn't matter.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "admin" / "templates"

# Templates use the .html.j2 extension, which is NOT in Jinja2's default
# autoescape list — without this, user-supplied fields (label, notes,
# error_class) would render unescaped → XSS in the admin UI.
templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR),
    autoescape=select_autoescape(enabled_extensions=("html.j2", "html", "j2"),
                                 default_for_string=False),
)


async def _require_db_pool(request: Request) -> "asyncpg.Pool":
    """503 if Postgres was unreachable at startup. Admin UI is unusable
    without a pool, so fail loud instead of crashing on AttributeError."""
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Admin UI unavailable: Postgres degraded.")
    return pool


# Two routers under the same /admin prefix:
#  - public_router:  /admin/login (GET/POST) — must be reachable WITHOUT auth
#                    to break the chicken-and-egg of "log in to log in".
#  - authed_router:  everything else, gated by require_admin + DB pool guard.
public_router = APIRouter(prefix="/admin")
authed_router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(require_admin), Depends(_require_db_pool)],
)


@public_router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse("login.html.j2", {"request": request})


@public_router.post("/login")
async def login_submit(request: Request, token: str = Form(...)):
    """Validate the bearer directly against the same lookup paths the
    auth middleware uses, then set the session cookie. We do NOT call
    `require_api_key(request)` because Starlette's `request.cookies`
    is read-only — writing to it has no effect on the next read."""
    th = users.store.hash_token(token)
    pool = getattr(request.app.state, "db_pool", None)
    user: User | None = None

    if pool is not None:
        try:
            user = await users.store.lookup(pool, th)
        except asyncpg.PostgresError:
            logger.exception("Postgres lookup failed during admin login")
            user = None

    # Break-glass: env-var hash always grants admin even when Postgres is degraded.
    if user is None:
        bg = getattr(request.app.state, "break_glass_token_hash", None)
        if bg is not None and th == bg:
            user = User(id=-1, label="break-glass-admin", role="admin")

    if user is None or user.role != "admin":
        return templates.TemplateResponse(
            "login.html.j2",
            {"request": request, "error": "Invalid token or non-admin role"},
            status_code=401,
        )

    resp = RedirectResponse("/admin/", status_code=303)
    resp.set_cookie(
        "wispralt_admin_token", token,
        httponly=True, secure=True, samesite="strict",
        max_age=8 * 3600,  # 8h session
    )
    return resp


# All authenticated admin routes attach to authed_router (defined above).
# Replace `@router.X` in subsequent pseudocode with `@authed_router.X`.
router = authed_router  # alias kept for the rest of the snippets in this file

@router.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    pool = request.app.state.db_pool
    stats = await _aggregate_stats(pool)  # totals + last 14d stacked counts
    return templates.TemplateResponse("overview.html.j2",
                                      {"request": request, **stats})

@router.get("/users", response_class=HTMLResponse)
async def users_list(request: Request):
    rows = await users.store.list_all(request.app.state.db_pool)
    return templates.TemplateResponse("users.html.j2",
                                      {"request": request, "users": rows})

@router.post("/users/{user_id}/revoke", response_class=HTMLResponse)
async def users_revoke(request: Request, user_id: int):
    pool = request.app.state.db_pool
    # Single round-trip: UPDATE...RETURNING gets the now-revoked
    # token_hash. Invalidate cache AFTER the row commit so no
    # request can race in and re-cache the still-valid hash before
    # the DB knows about the revoke.
    revoked_hash = await users.store.revoke(pool, user_id)
    if revoked_hash:
        token_cache.invalidate(revoked_hash)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/mint", response_class=HTMLResponse)
async def users_mint(request: Request, user_id: int):
    # Rotate token in place (same id/role/label, new hash).
    # Invalidate the cached old hash AFTER the DB commit.
    pool = request.app.state.db_pool
    new_plaintext, old_hash = await users.store.rotate(pool, user_id)
    if old_hash:
        token_cache.invalidate(old_hash)
    return templates.TemplateResponse("token_minted.html.j2",
        {"request": request, "user_id": user_id, "plaintext": new_plaintext})

@router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail(request: Request, user_id: int):
    ...

@router.get("/usage", response_class=HTMLResponse)
async def usage_drilldown(request: Request, ...):
    # filters: kind, status, user_id, since/until
    ...

@router.get("/usage.csv")
async def usage_csv(request: Request, ...):
    ...
```

### Lifespan additions in `main.py`

Order matters:
1. **First** populate `break_glass_token_hash` (no I/O — can't fail).
2. **Then** initialize `db_pool` + `usage_drainer` (failure mode:
   `db_pool = None`, `usage_drainer = None`; break-glass admin still works).

Pre-init `app.state.usage_drainer = None` so the shutdown branch
can safely test `getattr(...)` without AttributeError.

```python
# In lifespan(), after parakeet load:

# 1. Break-glass hash — no I/O, set unconditionally.
app.state.break_glass_token_hash = users.store.hash_token(
    settings.wispralt_api_key.get_secret_value()
)

# 2. Pre-set fallbacks so shutdown branch is always safe.
app.state.db_pool = None
app.state.usage_drainer = None

# 3. Postgres pool init + admin seeding + drainer start.
try:
    pool = await db.get_pool()
    app.state.db_pool = pool
    await _seed_admin_if_empty(pool)  # idempotent; uses break-glass hash
    app.state.usage_drainer = asyncio.create_task(
        usage.writer.drain_loop(observability.usage_queue, pool)
    )
except (asyncpg.PostgresError, OSError, db.PostgresUnavailable):
    logger.exception(
        "Postgres unavailable at startup; only break-glass admin will work"
    )

yield  # ← FastAPI serves requests here

# On shutdown — both attrs always set above, so getattr is defensive only:
drainer = getattr(app.state, "usage_drainer", None)
if drainer is not None:
    drainer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await drainer
await db.close_pool()
```

### `_seed_admin_if_empty()`

Idempotent: bails if any user already exists, AND uses
`ON CONFLICT (token_hash) DO NOTHING` so a restart with the same
env-var token (after a manual revoke) doesn't crash.

```python
async def _seed_admin_if_empty(pool: asyncpg.Pool) -> None:
    n = await pool.fetchval("SELECT COUNT(*) FROM wispralt.users")
    if n > 0:
        return
    bearer = settings.wispralt_api_key.get_secret_value()
    th = users.store.hash_token(bearer)
    await pool.execute(
        "INSERT INTO wispralt.users (label, token_hash, role, notes) "
        "VALUES ($1, $2, 'admin', $3) "
        "ON CONFLICT (token_hash) DO NOTHING",
        "break-glass-admin (seeded from env)", th,
        "Auto-seeded on first startup. Rotate via /admin/users to a real label."
    )
    logger.info("Seeded first admin user from WISPRALT_API_KEY env var.")
```

### Request-ID middleware

Cheap correlation ID attached to every request so usage events have a
non-NULL `request_id` for debugging. Generated if the client didn't
send one.

**Add-order gotcha:** Starlette/FastAPI middleware run **outside-in,
in REVERSE order of `add_middleware` calls** — the LAST `add_middleware`
call wraps OUTERMOST. So to make `_RequestIdMiddleware` run BEFORE
`_ObservabilityMiddleware`, call `add_middleware(_RequestIdMiddleware)`
**after** `add_middleware(_ObservabilityMiddleware)`.

```python
class _RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response

# in create_app() — order matters! Outermost is added LAST:
app.add_middleware(_ObservabilityMiddleware)  # inner
app.add_middleware(_RequestIdMiddleware)      # outer (runs first)
```

---

## Slash command shapes

### `~/.claude-dotfiles/commands/wispralt-setup.md`

The whole command body is one bash block the LLM runs verbatim. Pseudocode:

```
1. Check macOS >= 14. If not, exit clearly.
2. Install Homebrew if missing (curl <official install>).
3. Install gh if missing (`brew install gh`).
4. `gh auth status` — if not logged in, prompt: "Run `gh auth login` first".
5. Pick the latest release tag:
     TAG=$(gh release list --repo omdiidi/miniWhisper --limit 1 \
            --json tagName --jq '.[0].tagName')
6. Download BOTH the DMG and its .sha256 sidecar to a clean dir:
     mkdir -p /tmp/wispralt-dmg && cd /tmp/wispralt-dmg
     gh release download "$TAG" --repo omdiidi/miniWhisper \
       --pattern '*.dmg' --pattern '*.dmg.sha256' --clobber
7. Verify SHA256 (the sidecar contains "<hex>  WisprAlt-<ver>.dmg"):
     shasum -a 256 -c WisprAlt-*.dmg.sha256 || {
       echo "SHA256 verification failed — refusing to install."; exit 1; }
8. Mount, copy, unmount, strip quarantine:
     hdiutil attach WisprAlt-*.dmg -nobrowse -mountpoint /tmp/wispralt-mount
     cp -R /tmp/wispralt-mount/WisprAlt.app /Applications/
     hdiutil detach /tmp/wispralt-mount
     xattr -dr com.apple.quarantine /Applications/WisprAlt.app
9. Open WisprAlt.app — its PermissionGate.swift walks 4 permissions.
10. Print: "Now paste the API key Omid texted you in Settings → API Key."
```

### `~/.claude-dotfiles/commands/wispralt-update.md`

```
0. If /Applications/WisprAlt.app does NOT exist, redirect:
     "WisprAlt isn't installed yet. Run /wispralt-setup instead."
   (This is the first-install / clean-employee-machine fallback.)
1. Read installed version from /Applications/WisprAlt.app/Contents/Info.plist
   (CFBundleShortVersionString).
2. Read latest release tag from gh release list.
3. If installed >= latest, exit "Already up to date".
4. Capture pre-update cdhash:
     PRE_HASH=$(codesign -dvvv /Applications/WisprAlt.app 2>&1 | awk '/CDHash=/ {print $2}')
5. Download + verify + replace (same as setup steps 6-8).
6. Capture post-update cdhash. If different from pre-update:
     for tcc in Accessibility ListenEvent ScreenCapture Microphone; do
       tccutil reset $tcc co.wispralt.WisprAlt
     done
   Then open System Settings → Privacy & Security so the user can
   re-grant. Tell the user explicitly: "permissions were reset because
   the app's signature changed; please re-grant all four."
7. Open the updated app.
```

---

## Build / Release pipeline

### `scripts/release-client.sh`

```bash
#!/usr/bin/env bash
# Local-only release script. Omid runs on his MacBook to ship a release.
set -euo pipefail

VERSION="${1:?usage: $0 <version, e.g. 0.2.0>}"
TAG="v${VERSION}"

# 0. Pre-flight guards.
# 0a. Must be on main (or explicit --allow-branch override).
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "${CURRENT_BRANCH}" != "main" && "${ALLOW_BRANCH:-0}" != "1" ]]; then
    echo "Refusing to release from branch '${CURRENT_BRANCH}'." >&2
    echo "Switch to main, or set ALLOW_BRANCH=1 to override." >&2
    exit 1
fi
# 0b. Working tree must be clean — release-client.sh commits the
#     Info.plist version bump, so we cannot have unrelated mods.
if ! git diff-index --quiet HEAD --; then
    echo "Working tree is dirty. Commit or stash before releasing." >&2
    git status --short >&2
    exit 1
fi
# 0c. Refuse to overwrite an existing tag — fail BEFORE building.
if git rev-parse "${TAG}" >/dev/null 2>&1; then
    echo "Tag ${TAG} already exists. Bump VERSION." >&2
    exit 1
fi
if gh release view "${TAG}" --repo omdiidi/miniWhisper >/dev/null 2>&1; then
    echo "Release ${TAG} already exists on GitHub. Bump VERSION." >&2
    exit 1
fi

# 1. Bump CFBundleShortVersionString in Info.plist.
plutil -replace CFBundleShortVersionString -string "${VERSION}" \
  client/WisprAlt/Info.plist

# 2. Build signed .app via existing build-client-local.sh.
bash scripts/build-client-local.sh

# 3. Package as DMG.
DMG_NAME="WisprAlt-${VERSION}.dmg"
DMG_DIR="/tmp/wispralt-release-${VERSION}"
DMG_PATH="${DMG_DIR}/${DMG_NAME}"
rm -rf "${DMG_DIR}" && mkdir -p "${DMG_DIR}"
hdiutil create -volname WisprAlt -srcfolder client/build/WisprAlt.app \
  -fs HFS+ -format UDZO -ov "${DMG_PATH}"

# 4. Compute SHA256 sidecar — write only the BARE filename so the
#    employee's `shasum -c` finds the file by name in their CWD.
(cd "${DMG_DIR}" && shasum -a 256 "${DMG_NAME}" > "${DMG_NAME}.sha256")

# 5. Tag + push + GitHub Release with both assets.
git add client/WisprAlt/Info.plist
git -c user.email="zomid777@gmail.com" -c user.name="omid zahrai" \
    commit -m "release: ${TAG}" || true
git tag "${TAG}"
git push origin "$(git rev-parse --abbrev-ref HEAD)"
git push origin "${TAG}"
gh release create "${TAG}" \
  --title "WisprAlt ${VERSION}" \
  --notes "$(printf "WisprAlt %s\n\nSHA256:\n\n\`\`\`\n%s\n\`\`\`\n" \
    "${VERSION}" "$(cat ${DMG_PATH}.sha256)")" \
  "${DMG_PATH}" "${DMG_PATH}.sha256"

echo "Release ${TAG} shipped."
```

---

## Tasks (in implementation order)

> Each task is ≤ 1 hour of focused work. Order minimizes blocked dependencies.

### Phase A — Database + auth foundation

1. **Apply Postgres migration** via Supabase MCP
   `apply_migration({name: "v1_wispralt_schema", query: <see migration above>})`.
   Verify tables exist via `list_tables` (or just trust the success response).
2. **Add `asyncpg` and `jinja2` to `server/pyproject.toml`** (under main deps,
   not optional). Run `uv sync` will be done by `/setup-server` later or
   ad-hoc; for the implementation phase, just edit the file.
3. **Add `supabase_database_url: SecretStr | None = None` to `config.py`**
   `Settings`. Update `server/.env.example` with a commented line.
4. **Create `server/src/wispralt_server/db.py`** — pool factory per pseudocode.
5. **Create `server/src/wispralt_server/users/__init__.py`,
   `users/store.py`, `users/cache.py`** per pseudocode. `store.py` exports:
   `User`, `UserRow`, `hash_token`, `lookup`, `lookup_by_id`, `mint`,
   `rotate`, `revoke`, `list_all`. (No `_update_last_seen` — derived via
   `MAX(usage_events.ts)` in `list_all`'s SQL.)
6. **Replace `server/src/wispralt_server/auth.py`** — new implementation
   per pseudocode. Keep `_extract_bearer` and the `Multiple Authorization
   headers` 400-check helpers; rewrite the lookup body.
7. **Update `server/src/wispralt_server/routes/dictate.py`** — its
   `Depends(require_api_key)` now provides a `User` via `request.state.user`.
   No code change needed if the dependency is invoked for side effects
   (auth + state); double-check the signature is unchanged.
8. **Same for `routes/meeting.py` and `routes/admin.py`** — verify the
   `Depends(require_api_key)` callsites still work.

### Phase B — Usage event tracking

9. **Create `server/src/wispralt_server/usage/__init__.py`,
   `usage/events.py`, `usage/queue.py`, `usage/writer.py`** per pseudocode.
10. **Add `usage_queue` singleton to `observability.py`**: `usage_queue =
    UsageEventQueue()`. Export alongside `request_counter`.
11. **Extend `_ObservabilityMiddleware.dispatch()` in `main.py`** — append
    the `TRACKED_ROUTES` enqueue logic.
12. **Wire lifespan in `main.py`** — pool init, `_seed_admin_if_empty`,
    `break_glass_token_hash`, drainer task; cancel + close on shutdown.

### Phase C — Admin UI

13. **Create `server/src/wispralt_server/admin/__init__.py`** (package marker).
14. **Create Jinja2 templates** in
    `server/src/wispralt_server/admin/templates/`:
    - `base.html.j2` — minimal CSS (system fonts, neutral palette,
      single `<style>` block). Sidebar with links to Overview, Users,
      Usage. Show signed-in admin label in header.
    - `overview.html.j2` — totals (users, dictations 24h/7d/30d), p50
      latency 24h, error count 24h, top-5 active users table, last-14d
      per-day usage rendered as a CSS-bar table (no Chart.js, no CDN
      dep — works offline). Use `<td style="--w: 0.42">` with a
      `::before` `width: calc(var(--w) * 100%);` background bar.
    - `users.html.j2` — table; per-row Revoke + Mint forms.
    - `user_detail.html.j2` — same metrics scoped to one user, last 50
      events table.
    - `usage.html.j2` — filter form (kind / status / user / since-until),
      paginated event table, CSV download link.
    - `token_minted.html.j2` — single page that displays the new
      plaintext token ONCE (per the brief: "prints the plaintext token
      ONCE in the UI for Omid to copy + text the employee").
15. **Create `routes/admin_ui.py`** per pseudocode. Mount router with
    `prefix="/admin"`, attach `dependencies=[Depends(require_admin)]`.
16. **Mount BOTH routers in `main.py`**:
    `app.include_router(admin_ui.public_router)` (login routes, unauth)
    AND `app.include_router(admin_ui.authed_router)` (everything else,
    behind `require_admin`).  Order matters: include before any catch-all.
17. **Add aggregate-stats helper** in
    `routes/admin_ui.py:_aggregate_stats(pool)` — single SQL call returning
    a dict matching the overview template's expected keys. CTE sketch:

    ```sql
    WITH
      now_ts AS (SELECT now() AS t),
      totals AS (
        SELECT
          (SELECT count(*) FROM wispralt.users WHERE revoked_at IS NULL) AS active_users,
          (SELECT count(*) FROM wispralt.users) AS total_users,
          (SELECT count(*) FROM wispralt.usage_events
            WHERE ts >= (SELECT t FROM now_ts) - INTERVAL '1 day') AS dictations_24h,
          (SELECT count(*) FROM wispralt.usage_events
            WHERE ts >= (SELECT t FROM now_ts) - INTERVAL '7 days') AS dictations_7d,
          (SELECT count(*) FROM wispralt.usage_events
            WHERE ts >= (SELECT t FROM now_ts) - INTERVAL '30 days') AS dictations_30d,
          (SELECT count(*) FROM wispralt.usage_events
            WHERE status >= 400 AND ts >= (SELECT t FROM now_ts) - INTERVAL '1 day') AS errors_24h,
          (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
             FROM wispralt.usage_events
            WHERE duration_ms IS NOT NULL
              AND ts >= (SELECT t FROM now_ts) - INTERVAL '1 day') AS p50_24h
      ),
      top_users AS (
        SELECT u.id, u.label, u.role, count(e.id) AS n
          FROM wispralt.users u
          LEFT JOIN wispralt.usage_events e
            ON e.user_id = u.id
           AND e.ts >= (SELECT t FROM now_ts) - INTERVAL '7 days'
         GROUP BY u.id, u.label, u.role
         ORDER BY n DESC
         LIMIT 5
      ),
      daily AS (
        SELECT date_trunc('day', e.ts) AS day, count(*) AS n
          FROM wispralt.usage_events e
         WHERE e.ts >= (SELECT t FROM now_ts) - INTERVAL '14 days'
         GROUP BY 1
         ORDER BY 1
      )
    SELECT
      (SELECT row_to_json(totals) FROM totals) AS totals,
      (SELECT json_agg(row_to_json(top_users)) FROM top_users) AS top_users,
      (SELECT json_agg(row_to_json(daily)) FROM daily) AS daily;
    ```

    Returns a single row with three JSON columns. The Python helper
    json-decodes them into dicts/lists for the template.

### Phase D — Tests (lightweight, no live DB needed)

18. **`tests/test_token_cache.py`** — basic LRU + TTL behavior with a
    monkeypatched `time.monotonic`. No DB, no asyncio.
19. **`tests/test_usage_writer.py`** — feed N events into `UsageEventQueue`,
    confirm overflow drops oldest + warning logged. Stub `pool.executemany`
    with an async-mock and verify it's called with the right batch shape.
    Include a test that an `asyncpg.ForeignKeyViolationError` filters
    offending rows and retries the survivors.
20. **`tests/test_admin_routes_auth.py`** — stub `pool` + `User` dep, hit
    `/admin/` with role=employee → 403; with role=admin → 200.
21. **`tests/test_auth_break_glass.py`** — high-risk path: monkey-patch
    `request.app.state.db_pool = None`, set
    `break_glass_token_hash = hash_token("known-key")`, send Bearer
    "known-key" → returns `User(id=-1, role='admin')`. Confirms the
    operator never gets locked out when Postgres is degraded.

### Phase E — Distribution / employee experience

22. **Write `scripts/release-client.sh`** per pseudocode. `chmod +x`.
23. **Author `~/.claude-dotfiles/commands/wispralt-setup.md`** — full
    bash script per pseudocode. Include: macOS-version check, brew + gh
    install, `gh release download`, SHA256 verification, hdiutil mount/
    copy/detach, quarantine strip, app launch, "now paste your API key"
    user-facing message.
24. **Author `~/.claude-dotfiles/commands/wispralt-update.md`** — diff
    installed vs latest tag, replace if newer, TCC reset cycle if cdhash
    changed, open System Settings.
25. **Skipped (was: `scripts/seed-admin.sh`)** — the lifespan
    `_seed_admin_if_empty` is sufficient. Document the auto-seed
    behavior in `docs/DEPLOY-TEAM.md` instead. One less script to
    keep in sync.

### Phase F — Documentation

26. **`docs/ADMIN.md`** — admin-UI walkthrough: how to revoke, how to
    mint a new token, how the CSV export works, what each page shows.
    Include a "Future hooks (CRM)" section calling out `users.role` +
    `usage_events.kind` as TEXT discriminators.
27. **`docs/DEPLOY-TEAM.md`** — Omid-side ops: how to ship a release
    (`scripts/release-client.sh`), how to add a new employee
    (admin UI flow), how to revoke when someone leaves.
28. **`docs/SETUP-CLIENT.md`** — add an "Employee install" section above
    the existing build-from-source section. References `/wispralt-setup`.
29. **`docs/ARCHITECTURE.md`** — add the auth diagram, usage event flow,
    admin UI section. Memory-budget note: asyncpg pool ~10 conns × ~1 MB.
30. **`docs/API.md`** — new "Admin API" section listing the Jinja2 routes;
    mark them as HTML-not-JSON.
31. **`docs/OVERVIEW.md`** — update file → doc map for every new file
    listed in the "Files Being Changed" tree above.
32. **`docs/TROUBLESHOOTING.md`** — two new entries:
    - "API key rejected (employee install)" — checks: is the key
      revoked, is the cache stale, can the server reach Postgres.
    - "Admin UI returns 401" — admin role required; verify your token
      is the admin token, not an employee token.
33. **`CLAUDE.md`** — add a one-liner under "Slash command index"
    noting that `/wispralt-setup` and `/wispralt-update` are
    employee-facing and live in `~/.claude-dotfiles/commands/`, not
    in the project's `.claude/commands/` (intentional separation —
    those are admin-grade builds-from-source commands).

### Phase G — Validation

34. **Local syntax-check** every changed Python file
    (`python3 -c "import ast; ast.parse(open(f).read())"`).
35. **Run pytest in stubbed venv** — confirm all old + new tests pass.
36. **Manual smoke-test on the running mini server** (after merge):
    a. `apply_migration` ran cleanly.
    b. POST a dictation with the legacy `WISPRALT_API_KEY` — succeeds
       (break-glass admin path).
    c. Visit `https://transcribe.integrateapi.ai/admin/` in browser
       with `Authorization: Bearer <key>` → overview page renders.
    d. Mint a new employee token via `/admin/users/.../mint` → copy.
    e. Use the new token to dictate → succeeds + shows up in the user's
       per-user detail page.
    f. Revoke the employee token → next dictation gets 401 within 60s.
37. **Run `/codex-review`** on the resulting branch. Address findings.

---

## Validation gates (executable)

- `cd server && python3 -c "import ast, sys
  files = ['src/wispralt_server/auth.py',
           'src/wispralt_server/db.py',
           'src/wispralt_server/main.py',
           'src/wispralt_server/observability.py',
           'src/wispralt_server/users/store.py',
           'src/wispralt_server/users/cache.py',
           'src/wispralt_server/usage/queue.py',
           'src/wispralt_server/usage/writer.py',
           'src/wispralt_server/routes/admin_ui.py']
  for f in files: ast.parse(open(f).read()); print('ok', f)"`
- `cd server && pytest tests/ -v` — must show all tests passing
  (12 prior + 4 new = 16+).
- **Migration applied to right project**: via Supabase MCP run
  `execute_sql({query: "SELECT version FROM wispralt.schema_version"})`
  → returns `[{version: 1}]`.
- `curl -s --max-time 4 https://transcribe.integrateapi.ai/healthz` →
  `{"status":"ok"}`.
- After deploy, `curl --max-time 4 https://transcribe.integrateapi.ai/metrics`
  shows `process_uptime_seconds < 60` (proves restart happened).
- After deploy, `curl --max-time 4 -H "Authorization: Bearer $KEY"
  https://transcribe.integrateapi.ai/admin/users` returns HTML with
  the seeded admin row.

---

## Code to remove / deprecate after this lands

- `auth.py`'s legacy single-key behavior is **replaced**, not extended.
  The new `auth.py` reads the env-var only as a break-glass admin
  hash; downstream callers no longer use `settings.wispralt_api_key`
  for runtime auth comparison (they're entirely served by Postgres).
- The `routes/admin.py:rotate_key` endpoint becomes redundant once the
  admin UI's mint/revoke flow exists. **Don't delete it yet** — it's
  the last-resort tool for rotating the break-glass admin token when
  Postgres is unreachable. Document this in `docs/ADMIN.md`.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Postgres unreachable from Mac mini → server can't auth users | Break-glass env-var admin (Omid stays in). Logged WARNING. Cache hits still work for known tokens. |
| asyncpg pool starvation under load | Pool size **10** with command_timeout=10s. Drainer holds at most 1 conn during the executemany window; auth + admin UI compete for the other 9. Comfortable headroom for ≤10 employees. |
| Schema migration applied to wrong project | Brief lock: project ref `qglwmwmdoxopnubghnul`. Plan applies via `apply_migration` MCP which uses the project-ref already configured. |
| Admin UI accidentally exposes plaintext tokens | `mint` returns plaintext exactly once via `token_minted.html.j2`; never logged, never stored in plaintext. |
| Cache staleness during revoke | Revoke handler invalidates cache entry by hash before the DB update, so the 60s TTL race is closed. |
| Usage events spam admin DB | Bounded queue + 1Hz batch. Volumes are tiny for ≤10 employees; no issue. |
| Future schema changes | `wispralt.schema_version` table tracked from v1. New migrations are additive. |

---

## Confidence: 8/10

Postgres + asyncpg + Jinja2 are all well-trodden patterns for FastAPI.
The two surfaces with one-pass-implementation risk are:
- **The admin UI's aggregate-stats SQL.** Single CTE serving all
  overview tiles needs careful tuning. Worst case: split into multiple
  small queries — still fast for this volume.
- **Lifespan ordering** between Parakeet load and Postgres pool init.
  Plan puts Postgres after Parakeet so dictation works even if
  Postgres is unreachable.

Both are addressable in iteration without rework.
