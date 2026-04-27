"""
usage/writer.py — background drain loop that flushes events into Postgres.

The drain loop is started as an ``asyncio.Task`` by the lifespan in
``main.py``.  It batches up to 50 events or flushes every 1 s, whichever
comes first.  On shutdown the lifespan cancels the task; a final flush
runs from the ``CancelledError`` handler so in-flight events survive.

Failure modes
-------------
- Postgres transient error during a flush → log + drop the batch.  We do
  NOT requeue: that would risk replaying duplicates after a restart and
  the events are observability-grade, not billing-grade.
- ``ForeignKeyViolationError`` (e.g. break-glass user_id slipped through):
  the original transaction auto-rolls back; we retry the survivors
  (``user_id >= 0``) in a fresh transaction.  Without the explicit
  transaction, the post-error connection would be in an aborted state and
  the retry would hit ``InFailedSQLTransactionError``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import asyncpg

from .events import UsageEvent
from .queue import UsageEventQueue

logger = logging.getLogger(__name__)


_INSERT_SQL = (
    "INSERT INTO wispralt.usage_events "
    "(user_id, ts, kind, status, chars, duration_ms, bytes_in, "
    " bytes_out, error_class, request_id) "
    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)"
)


async def drain_loop(queue: UsageEventQueue, pool: asyncpg.Pool) -> None:
    """Long-running task: pull events off *queue* and flush in batches.

    Cancel on shutdown — the ``CancelledError`` handler runs one final
    flush so the last in-flight events are persisted.
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
            # Drain whatever is still in the queue so we don't lose the last
            # second's worth of events on shutdown. ``get_nowait`` is safe
            # because we've been cancelled — no future producer can stall us
            # waiting for an empty queue.
            while True:
                try:
                    batch.append(queue._q.get_nowait())  # noqa: SLF001 — drain on shutdown
                except asyncio.QueueEmpty:
                    break
                if len(batch) >= BATCH_MAX:
                    try:
                        await _flush(pool, batch)
                    except (asyncpg.PostgresError, OSError):
                        logger.exception(
                            "usage_event final flush failed; dropping batch of %d",
                            len(batch),
                        )
                    batch = []
            if batch:
                try:
                    await _flush(pool, batch)
                except (asyncpg.PostgresError, OSError):
                    logger.exception(
                        "usage_event final flush failed; dropping batch of %d",
                        len(batch),
                    )
            raise

        if len(batch) >= BATCH_MAX or time.monotonic() - last_flush >= FLUSH_INTERVAL_S:
            if batch:
                try:
                    await _flush(pool, batch)
                except (asyncpg.PostgresError, OSError):
                    logger.exception(
                        "usage_event flush failed; dropping batch of %d",
                        len(batch),
                    )
                batch = []
                last_flush = time.monotonic()


async def _flush(pool: asyncpg.Pool, batch: list[UsageEvent]) -> None:
    """Insert *batch* via ``executemany`` inside an explicit transaction.

    On a :class:`asyncpg.ForeignKeyViolationError` the transaction
    auto-rolls back; a fresh transaction retries the survivor rows
    (``user_id >= 0``).
    """
    rows = [
        (
            e.user_id,
            datetime.fromtimestamp(e.ts, tz=timezone.utc),
            e.kind,
            e.status,
            e.chars,
            e.duration_ms,
            e.bytes_in,
            e.bytes_out,
            e.error_class,
            e.request_id,
        )
        for e in batch
    ]
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await conn.executemany(_INSERT_SQL, rows)
        except asyncpg.ForeignKeyViolationError:
            # Original transaction auto-rolled-back. Retry survivors in
            # a fresh transaction.
            survivors = [r for r in rows if r[0] >= 0]
            if survivors:
                async with conn.transaction():
                    await conn.executemany(_INSERT_SQL, survivors)
            logger.warning(
                "usage_event flush dropped %d FK-violating rows; retried %d",
                len(rows) - len(survivors),
                len(survivors),
            )
