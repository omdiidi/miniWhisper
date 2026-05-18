"""
db.py — asyncpg pool factory for Supabase Postgres.

Single global pool, lazy-initialised from ``settings.supabase_database_url``.
Pool of 10 connections — the usage-event drainer holds one for the duration
of each batch flush; auth lookups + admin UI compete for the rest.

The lifespan in ``main.py`` catches :class:`PostgresUnavailable` so the
server still boots (with the break-glass admin path) when Postgres is
degraded or the URL is missing.

Health & recovery
-----------------
:func:`health_check` is a cheap ``SELECT 1`` with a short timeout; the
lifespan's pool-watcher task uses it to detect a dead pool and call
:func:`recreate_pool` to rebuild it. Without this, a transient Supabase
connectivity blip can leave the pool in a state where every acquire
fails — the only previous recovery was a server restart (fixed
2026-05-02 after a real production outage where ``/me`` returned 503
"Auth temporarily unavailable" persistently and required a manual
launchctl kickstart).
"""

from __future__ import annotations

import asyncio
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
_pool_lock = asyncio.Lock()


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
    async with _pool_lock:
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
    async with _pool_lock:
        if _pool is not None:
            await _pool.close()
            _pool = None


async def health_check(pool: asyncpg.Pool, timeout_s: float = 2.0) -> bool:
    """Cheap probe — return True if the pool can serve a trivial query.

    Used by the watcher task in lifespan to detect dead pools without
    flooding logs. Catches anything that prevents acquiring + querying:
    pool exhaustion, network drop, server-side restart, idle eviction
    by Supabase. Wrapped in :func:`asyncio.wait_for` because asyncpg's
    ``command_timeout`` doesn't bound the acquire phase.
    """
    try:
        async with asyncio.timeout(timeout_s):
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
        return True
    except (asyncpg.Error, OSError, asyncio.TimeoutError):
        # asyncpg.Error covers both server-side PostgresError AND client-side
        # InterfaceError ("pool is closed"). The narrower PostgresError caused
        # the watcher to get stuck in the outer except branch on 2026-05-17 —
        # pool closed silently, watcher never reached the rebuild path.
        return False


async def recreate_pool() -> asyncpg.Pool:
    """Close any existing pool and create a fresh one.

    Holds :data:`_pool_lock` for the duration so concurrent callers
    serialize on a single rebuild rather than racing N create_pools.

    Raises the same set as :func:`get_pool` when re-creation fails.
    """
    global _pool
    async with _pool_lock:
        if _pool is not None:
            try:
                await _pool.close()
            except (asyncpg.Error, OSError):
                # The old pool was broken anyway; log and move on. Use the
                # base asyncpg.Error so InterfaceError (pool already closed)
                # doesn't escape and abort the rebuild.
                logger.exception("Closing dead pool raised; continuing with rebuild")
            _pool = None
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
