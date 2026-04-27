"""
db.py — asyncpg pool factory for Supabase Postgres.

Single global pool, lazy-initialised from ``settings.supabase_database_url``.
Pool of 10 connections — the usage-event drainer holds one for the duration
of each batch flush; auth lookups + admin UI compete for the rest.

The lifespan in ``main.py`` catches :class:`PostgresUnavailable` so the
server still boots (with the break-glass admin path) when Postgres is
degraded or the URL is missing.
"""

from __future__ import annotations

import logging

import asyncpg

from wispralt_server.config import settings

logger = logging.getLogger(__name__)


class PostgresUnavailable(RuntimeError):
    """Typed error raised when the database URL is missing or pool init fails.

    Lifespan catches this so the server still boots (with break-glass
    admin) when Postgres is degraded.
    """


_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Return the lazily-initialised global asyncpg pool.

    Raises
    ------
    PostgresUnavailable
        When ``SUPABASE_DATABASE_URL`` is unset.  Pool-create errors from
        asyncpg propagate as their native exception types so the caller
        can distinguish "no URL configured" from "DNS down".
    """
    global _pool
    if _pool is not None:
        return _pool
    url = settings.supabase_database_url
    if url is None:
        raise PostgresUnavailable("SUPABASE_DATABASE_URL not set")
    _pool = await asyncpg.create_pool(
        url.get_secret_value(),
        min_size=1,
        max_size=10,
        command_timeout=10.0,
        server_settings={"application_name": "wispralt-server"},
    )
    return _pool


async def close_pool() -> None:
    """Close the global pool, if one exists.  Safe to call multiple times."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
