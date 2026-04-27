"""
users/store.py — asyncpg-backed CRUD for ``wispralt.users``.

Two dataclasses:
- :class:`User` is the minimal auth-time identity (3 fields, cached for 60s).
- :class:`UserRow` is the rich row used by the admin UI.

``last_seen_at`` is **derived** from ``MAX(usage_events.ts)`` at admin-read
time — we do **not** maintain a column write per request (avoids contending
with the usage-event writer's bulk pool acquisitions).
"""

from __future__ import annotations

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
    display_name: str | None  # NEW — additive, defaults to None for any rows fetched
                              # before the migration ran (none in practice; harmless)


@dataclass(frozen=True, slots=True)
class UserProfile:
    """Read-only snapshot of a user for /me responses. Includes derived last_seen_at."""

    id: int
    label: str
    display_name: str | None
    role: str
    created_at: datetime
    last_seen_at: datetime | None  # derived from MAX(usage_events.ts), same as list_all


def hash_token(plaintext: str) -> str:
    """Return the sha256 hex digest of *plaintext*."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


async def lookup(pool: asyncpg.Pool, token_hash: str) -> User | None:
    """Look up a non-revoked user by ``token_hash``.

    Returns ``None`` when no matching active row exists.
    """
    row = await pool.fetchrow(
        "SELECT id, label, role FROM wispralt.users "
        "WHERE token_hash = $1 AND revoked_at IS NULL",
        token_hash,
    )
    if row is None:
        return None
    return User(id=row["id"], label=row["label"], role=row["role"])


async def lookup_by_id(pool: asyncpg.Pool, user_id: int) -> User | None:
    """Look up any user (revoked or not) by primary key."""
    row = await pool.fetchrow(
        "SELECT id, label, role FROM wispralt.users WHERE id = $1",
        user_id,
    )
    if row is None:
        return None
    return User(id=row["id"], label=row["label"], role=row["role"])


async def mint(pool: asyncpg.Pool, *, label: str, role: str) -> tuple[User, str]:
    """Create a new user with a freshly-generated 64-hex-char token.

    Returns the new :class:`User` plus the **plaintext** token — the only
    time the plaintext is ever known to the server.  The caller must
    surface it to the operator exactly once.
    """
    plaintext = secrets.token_hex(32)  # 64 hex chars
    th = hash_token(plaintext)
    row = await pool.fetchrow(
        "INSERT INTO wispralt.users (label, token_hash, role) "
        "VALUES ($1, $2, $3) RETURNING id, label, role",
        label,
        th,
        role,
    )
    return User(id=row["id"], label=row["label"], role=row["role"]), plaintext


async def rotate(pool: asyncpg.Pool, user_id: int) -> tuple[str, str | None]:
    """Replace the user's token in place.  Same id/label/role, new hash.

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
        new_th,
        user_id,
    )
    return plaintext, old_th


async def revoke(pool: asyncpg.Pool, user_id: int) -> str | None:
    """Mark a user as revoked.

    Returns the now-revoked ``token_hash`` so the caller can invalidate
    the cache entry **after** the DB commit (closes the cache-staleness
    race).
    """
    return await pool.fetchval(
        "UPDATE wispralt.users SET revoked_at = now() WHERE id = $1 "
        "RETURNING token_hash",
        user_id,
    )


async def list_all(pool: asyncpg.Pool) -> list[UserRow]:
    """Return every user with derived ``last_seen_at`` for the admin UI.

    Single round-trip: the correlated sub-SELECT against
    ``MAX(usage_events.ts)`` keeps the admin page fast even with thousands
    of usage events.
    """
    rows = await pool.fetch(
        """
        SELECT u.id, u.label, u.role, u.created_at, u.revoked_at, u.display_name,
               (SELECT MAX(ts) FROM wispralt.usage_events e
                WHERE e.user_id = u.id) AS last_seen_at
          FROM wispralt.users u
         ORDER BY u.created_at DESC
        """
    )
    return [
        UserRow(
            id=r["id"],
            label=r["label"],
            role=r["role"],
            created_at=r["created_at"],
            revoked_at=r["revoked_at"],
            last_seen_at=r["last_seen_at"],
            display_name=r["display_name"],
        )
        for r in rows
    ]


async def fetch_profile_by_id(pool: asyncpg.Pool, user_id: int) -> UserProfile | None:
    """Fetch profile for any user (including revoked). Used by both /me and admin user-detail.

    Does NOT filter revoked_at — admin must be able to view revoked users (their old
    usage history, the date they were revoked, etc.). For /me, auth has already
    verified the caller is non-revoked, so the row will be active by construction at
    that path.
    """
    row = await pool.fetchrow(
        """
        SELECT u.id, u.label, u.display_name, u.role, u.created_at,
               (SELECT MAX(e.ts) FROM wispralt.usage_events e
                WHERE e.user_id = u.id) AS last_seen_at
          FROM wispralt.users u
         WHERE u.id = $1
        """,
        user_id,
    )
    if row is None:
        return None
    return UserProfile(
        id=row["id"],
        label=row["label"],
        display_name=row["display_name"],
        role=row["role"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
    )


async def update_display_name(
    pool: asyncpg.Pool, user_id: int, display_name: str | None
) -> None:
    """Update display_name for user_id. Caller validates length 1-40, no control chars, or None.

    Pass NULL to clear. Skips revoked users (UPDATE WHERE revoked_at IS NULL).
    """
    await pool.execute(
        "UPDATE wispralt.users SET display_name = $1 "
        "WHERE id = $2 AND revoked_at IS NULL",
        display_name,
        user_id,
    )
