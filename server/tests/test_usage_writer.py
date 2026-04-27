"""
test_usage_writer.py — Coverage for usage queue overflow + writer FK retry.

These are the two correctness-critical paths in the usage pipeline:

    1. ``UsageEventQueue.offer`` must drop the oldest entry on overflow,
       not block the request thread or silently corrupt the queue.
    2. ``writer._flush`` must run inside an explicit transaction and, on
       :class:`asyncpg.ForeignKeyViolationError`, retry the survivor rows
       (``user_id >= 0``) in a fresh transaction.  Without the explicit
       transaction the post-error connection would be in an aborted state
       and the retry would hit ``InFailedSQLTransactionError``.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from wispralt_server.usage.events import UsageEvent
from wispralt_server.usage.queue import UsageEventQueue
from wispralt_server.usage.writer import _flush


def _make_event(user_id: int, *, kind: str = "dictate", status: int = 200) -> UsageEvent:
    return UsageEvent(
        user_id=user_id,
        ts=1_700_000_000.0,  # fixed unix ts; exact value doesn't matter to the mocks
        kind=kind,
        status=status,
        chars=None,
        duration_ms=150.0,
        bytes_in=4096,
        bytes_out=None,
        error_class=None,
        request_id=f"req-{user_id}",
    )


# ── pool / connection mock helpers ─────────────────────────────────────────────


class _FakeTransaction:
    """Minimal async context manager imitating ``conn.transaction()``.

    asyncpg's transaction is an async context manager; on raised
    exceptions inside the body the transaction auto-rolls-back.  Here
    the rollback is a no-op — we just need ``__aenter__``/``__aexit__``
    to behave so the writer's ``async with`` runs to completion.
    """

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> "_FakeTransaction":
        self.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        self.exited += 1
        # Returning False propagates exceptions — matches asyncpg.
        return False


class _FakeConn:
    """Stand-in for an asyncpg connection.  Records ``executemany`` calls."""

    def __init__(self) -> None:
        self.executemany_calls: list[tuple[str, list]] = []
        self._fk_on_first_call = False
        self.transactions: list[_FakeTransaction] = []

    def transaction(self) -> _FakeTransaction:
        tx = _FakeTransaction()
        self.transactions.append(tx)
        return tx

    async def executemany(self, sql: str, rows: list) -> None:
        self.executemany_calls.append((sql, list(rows)))
        if self._fk_on_first_call and len(self.executemany_calls) == 1:
            # Simulate Postgres rejecting the whole batch on a FK
            # violation in any row.
            raise asyncpg.ForeignKeyViolationError("simulated FK violation")


class _FakePoolAcquire:
    """Async context manager for ``pool.acquire()`` — yields a :class:`_FakeConn`."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        return False


class _FakePool:
    """Stand-in for ``asyncpg.Pool`` exposing only ``acquire``."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakePoolAcquire:
        return _FakePoolAcquire(self._conn)


# ── UsageEventQueue tests ──────────────────────────────────────────────────────


class TestUsageEventQueue:
    """Bounded-queue overflow behavior."""

    @pytest.mark.asyncio
    async def test_offer_below_capacity_accepts_all(self) -> None:
        q = UsageEventQueue()
        for i in range(10):
            q.offer(_make_event(i))
        assert q.dropped == 0
        # Drain one to confirm enqueue actually landed.
        first = await q.drain_one()
        assert first.user_id == 0

    @pytest.mark.asyncio
    async def test_offer_at_capacity_drops_oldest_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        q = UsageEventQueue()
        # Force the small bound for fast iteration; mutates the
        # private attr but is the cleanest way to exercise overflow
        # without enqueuing 1000 events.
        q._q = asyncio.Queue(maxsize=3)
        with caplog.at_level(logging.WARNING, logger="wispralt_server.usage.queue"):
            q.offer(_make_event(1))
            q.offer(_make_event(2))
            q.offer(_make_event(3))
            # This one trips overflow — oldest (user_id=1) should be dropped.
            q.offer(_make_event(4))

        assert q.dropped == 1
        # The drained order should now be 2, 3, 4 (1 was dropped).
        seen = [(await q.drain_one()).user_id for _ in range(3)]
        assert seen == [2, 3, 4]
        # WARNING log emitted at least once.
        assert any(
            "queue full" in rec.getMessage().lower() for rec in caplog.records
        ), "expected a 'queue full' WARNING"

    def test_offer_outside_event_loop_drops_silently(self) -> None:
        # Defensive path: calling ``offer`` from a non-async thread must
        # not raise.  It increments the dropped counter and logs WARNING.
        q = UsageEventQueue()
        q.offer(_make_event(99))
        assert q.dropped == 1


# ── writer._flush tests ────────────────────────────────────────────────────────


class TestFlushHappyPath:
    """The straight-line insert path: one ``executemany`` call."""

    @pytest.mark.asyncio
    async def test_flush_calls_executemany_once_with_correct_shape(self) -> None:
        conn = _FakeConn()
        pool = _FakePool(conn)
        batch = [_make_event(1), _make_event(2), _make_event(3)]

        await _flush(pool, batch)  # type: ignore[arg-type]

        assert len(conn.executemany_calls) == 1
        sql, rows = conn.executemany_calls[0]
        # SQL targets the right table and column order.
        assert "wispralt.usage_events" in sql
        assert "user_id, ts, kind, status, chars, duration_ms, bytes_in" in sql
        # Row shape: 10 positional columns matching the INSERT.
        assert len(rows) == 3
        for row in rows:
            assert len(row) == 10
        # First column should be user_id, last should be request_id.
        assert rows[0][0] == 1
        assert rows[0][-1] == "req-1"

    @pytest.mark.asyncio
    async def test_flush_runs_inside_a_transaction(self) -> None:
        conn = _FakeConn()
        pool = _FakePool(conn)
        await _flush(pool, [_make_event(1)])  # type: ignore[arg-type]
        # exactly one transaction was opened on the happy path
        assert len(conn.transactions) == 1
        assert conn.transactions[0].entered == 1
        assert conn.transactions[0].exited == 1


class TestFlushForeignKeyRetry:
    """FK violation must drop ``user_id < 0`` rows and retry survivors."""

    @pytest.mark.asyncio
    async def test_fk_violation_retries_survivors_in_fresh_transaction(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        conn = _FakeConn()
        conn._fk_on_first_call = True
        pool = _FakePool(conn)
        # Mix of valid and break-glass-sentinel rows.
        batch = [
            _make_event(-1),  # break-glass admin — would FK-violate
            _make_event(1),
            _make_event(2),
            _make_event(-1),  # second offender
        ]
        with caplog.at_level(logging.WARNING, logger="wispralt_server.usage.writer"):
            await _flush(pool, batch)  # type: ignore[arg-type]

        # Two executemany calls: first FK-bombs, second is the survivor retry.
        assert len(conn.executemany_calls) == 2
        # Retry call should only contain user_id >= 0 rows.
        _, retry_rows = conn.executemany_calls[1]
        assert len(retry_rows) == 2
        assert all(r[0] >= 0 for r in retry_rows)
        # Both transactions opened (first auto-rolled-back on the FK exception,
        # second wraps the retry).
        assert len(conn.transactions) == 2
        assert all(tx.entered == 1 and tx.exited == 1 for tx in conn.transactions)
        # WARNING about the dropped count.
        assert any(
            "FK-violating" in rec.getMessage() for rec in caplog.records
        ), "expected an FK-violation WARNING"

    @pytest.mark.asyncio
    async def test_fk_violation_with_no_survivors_does_not_retry(self) -> None:
        # If every row was a break-glass sentinel, there's nothing to
        # retry — writer should log+return cleanly without opening
        # a second transaction.
        conn = _FakeConn()
        conn._fk_on_first_call = True
        pool = _FakePool(conn)
        batch = [_make_event(-1), _make_event(-1)]
        await _flush(pool, batch)  # type: ignore[arg-type]
        # Only the original (failing) executemany was attempted.
        assert len(conn.executemany_calls) == 1
        # Only one transaction opened.
        assert len(conn.transactions) == 1


class TestFlushPoolInteraction:
    """Verify the writer uses ``pool.acquire`` exactly once per flush."""

    @pytest.mark.asyncio
    async def test_pool_acquire_called_once(self) -> None:
        # Wrap _FakePool to count acquire() calls.
        conn = _FakeConn()
        pool = _FakePool(conn)
        pool_mock = MagicMock(wraps=pool)
        # Hand MagicMock the real bound method so wraps works for `acquire`.
        pool_mock.acquire = MagicMock(side_effect=pool.acquire)
        await _flush(pool_mock, [_make_event(1), _make_event(2)])  # type: ignore[arg-type]
        assert pool_mock.acquire.call_count == 1


# Sanity: AsyncMock-style usage example for future writers — the rest of
# the suite uses _FakeConn for clarity.
@pytest.mark.asyncio
async def test_async_mock_executemany_sanity() -> None:
    conn = MagicMock()
    conn.executemany = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=_FakeTransaction())
    pool_acquire = _FakePoolAcquire(conn)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=pool_acquire)
    await _flush(pool, [_make_event(1)])  # type: ignore[arg-type]
    conn.executemany.assert_awaited_once()
