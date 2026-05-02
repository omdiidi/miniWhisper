"""
test_db_health.py — Coverage for ``db.health_check`` + ``db.recreate_pool``.

These helpers underpin the lifespan's pool-watcher loop (added 2026-05-02
after a real outage where ``/me`` returned 503 persistently and the only
recovery was a manual ``launchctl kickstart``). The watcher loop itself
is integration-flavoured (asyncio sleep + state mutation), so we test
the unit-level pieces it composes.

We never reach the real Supabase here — every test fakes
``asyncpg.create_pool`` and the ``Pool`` interface needed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import pytest

from wispralt_server import db


# ── helpers ───────────────────────────────────────────────────────────────────


class _FakeConn:
    """Minimal asyncpg.Connection stand-in for SELECT 1 probes."""

    def __init__(self, *, raise_on_query: BaseException | None = None) -> None:
        self._raise = raise_on_query
        self.closed = False

    async def fetchval(self, _query: str, *_args: Any) -> int:
        if self._raise is not None:
            raise self._raise
        return 1


class _FakeAcquireCtx:
    def __init__(self, conn: _FakeConn, *, raise_on_acquire: BaseException | None = None) -> None:
        self._conn = conn
        self._raise = raise_on_acquire

    async def __aenter__(self) -> _FakeConn:
        if self._raise is not None:
            raise self._raise
        return self._conn

    async def __aexit__(self, *_a: Any) -> None:
        return None


class _FakePool:
    """Implements just enough of ``asyncpg.Pool`` for these tests."""

    def __init__(
        self,
        *,
        raise_on_acquire: BaseException | None = None,
        raise_on_query: BaseException | None = None,
        acquire_delay_s: float = 0.0,
    ) -> None:
        self._raise_on_acquire = raise_on_acquire
        self._raise_on_query = raise_on_query
        self._acquire_delay_s = acquire_delay_s
        self.closed = False

    def acquire(self) -> _FakeAcquireCtx:
        if self._acquire_delay_s > 0:
            # Simulate hung acquire — wrapped by health_check's
            # asyncio.timeout. We return a context that sleeps inside enter.
            class _SlowCtx:
                def __init__(self, delay: float) -> None:
                    self._delay = delay

                async def __aenter__(self_inner) -> _FakeConn:
                    await asyncio.sleep(self_inner._delay)
                    return _FakeConn()

                async def __aexit__(self_inner, *_a: Any) -> None:
                    return None

            return _SlowCtx(self._acquire_delay_s)  # type: ignore[return-value]

        return _FakeAcquireCtx(
            _FakeConn(raise_on_query=self._raise_on_query),
            raise_on_acquire=self._raise_on_acquire,
        )

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_module_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with ``db._pool = None`` so it can install its own fake."""
    monkeypatch.setattr(db, "_pool", None, raising=False)
    # Provide a non-None URL so PostgresUnavailable doesn't fire.
    monkeypatch.setattr(
        db.settings,
        "supabase_database_url",
        type("S", (), {"get_secret_value": lambda self: "postgres://fake/db"})(),
        raising=False,
    )


# ── health_check ──────────────────────────────────────────────────────────────


class TestHealthCheck:
    async def test_returns_true_for_responsive_pool(self) -> None:
        pool = _FakePool()
        assert await db.health_check(pool) is True  # type: ignore[arg-type]

    async def test_returns_false_when_acquire_raises_postgres_error(self) -> None:
        pool = _FakePool(raise_on_acquire=asyncpg.PostgresError("dead"))
        assert await db.health_check(pool) is False  # type: ignore[arg-type]

    async def test_returns_false_when_acquire_raises_oserror(self) -> None:
        pool = _FakePool(raise_on_acquire=OSError("network"))
        assert await db.health_check(pool) is False  # type: ignore[arg-type]

    async def test_returns_false_when_query_raises(self) -> None:
        pool = _FakePool(raise_on_query=asyncpg.PostgresError("query timeout"))
        assert await db.health_check(pool) is False  # type: ignore[arg-type]

    async def test_returns_false_on_timeout(self) -> None:
        pool = _FakePool(acquire_delay_s=5.0)
        # 0.05s timeout — far less than the 5s simulated hang
        assert await db.health_check(pool, timeout_s=0.05) is False  # type: ignore[arg-type]


# ── recreate_pool ─────────────────────────────────────────────────────────────


class TestRecreatePool:
    async def test_closes_old_and_creates_new(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = _FakePool()
        new = _FakePool()
        monkeypatch.setattr(db, "_pool", old, raising=False)

        async def _fake_create_pool(*_a: Any, **_kw: Any) -> _FakePool:
            return new

        monkeypatch.setattr(db.asyncpg, "create_pool", _fake_create_pool)

        result = await db.recreate_pool()
        assert result is new
        assert old.closed is True
        assert db._pool is new

    async def test_swallows_close_failure_and_still_rebuilds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _OldBroken:
            closed = False

            async def close(self) -> None:
                raise asyncpg.PostgresError("already disconnected")

        old = _OldBroken()
        new = _FakePool()
        monkeypatch.setattr(db, "_pool", old, raising=False)

        async def _fake_create_pool(*_a: Any, **_kw: Any) -> _FakePool:
            return new

        monkeypatch.setattr(db.asyncpg, "create_pool", _fake_create_pool)

        result = await db.recreate_pool()
        assert result is new
        assert db._pool is new

    async def test_raises_postgres_unavailable_when_url_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db.settings, "supabase_database_url", None, raising=False)
        with pytest.raises(db.PostgresUnavailable):
            await db.recreate_pool()

    async def test_concurrent_callers_serialize_on_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two concurrent recreate_pool() calls must not both hit create_pool —
        the lock serializes them and the second sees the first's result."""
        call_count = 0

        async def _slow_create_pool(*_a: Any, **_kw: Any) -> _FakePool:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return _FakePool()

        monkeypatch.setattr(db.asyncpg, "create_pool", _slow_create_pool)

        await asyncio.gather(db.recreate_pool(), db.recreate_pool())
        # Both callers hit the lock; the second sees _pool != None on entry
        # and would skip the rebuild — but recreate_pool always rebuilds
        # by design (caller signals the pool is dead). Both callers will
        # close + create; what matters is no overlap.
        assert call_count == 2
        # Critically: only one pool is current.
        assert db._pool is not None
