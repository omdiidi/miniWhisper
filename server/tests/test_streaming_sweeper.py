"""The streaming-session sweeper keeps the process-wide store bounded.

Finalized and aborted sessions stay for one TTL after they end (so a retried
finalize can still replay a blank result, and late chunks get 410), then are
evicted. Active idle sessions are aborted first and evicted a TTL later.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from wispralt.dictate.streaming_session import StreamingSessionStore

TTL = 900.0


def _store() -> StreamingSessionStore:
    return StreamingSessionStore(
        max_active=4, max_queue_depth=4, ttl_s=TTL, finalize_timeout_s=5.0
    )


async def test_finalized_session_is_evicted_one_ttl_after_it_ends() -> None:
    store = _store()
    session, status = await store.open_or_get("s1", 7, "d1")
    assert status == "new"
    session.status = "finalized"
    session.blank_text = ""

    t0 = 1_000_000.0
    await store.sweep_once(t0)  # first sight: stamped, kept
    assert session.ended_at == t0
    await store.sweep_once(t0 + TTL)  # still inside the replay window
    assert (await store.get_for_owner("s1", 7)) is session

    await store.sweep_once(t0 + TTL + 1)
    assert store.snapshot_all() == []
    with pytest.raises(HTTPException) as exc:
        await store.get_for_owner("s1", 7)
    assert exc.value.status_code == 404


async def test_stale_active_session_is_aborted_then_evicted() -> None:
    store = _store()
    session, _ = await store.open_or_get("s2", 7, "d2")
    later = session.last_seen + TTL + 1

    await store.sweep_once(later)
    assert session.status == "aborted"
    assert session.last_error == "ttl_expired"
    assert store.snapshot_all() == [session]  # the 410 stays visible for a TTL

    await store.sweep_once(later + TTL + 1)
    assert store.snapshot_all() == []


async def test_finalizing_and_locked_sessions_are_never_evicted() -> None:
    store = _store()
    finalizing, _ = await store.open_or_get("s3", 7, "d3")
    finalizing.status = "finalizing"
    locked, _ = await store.open_or_get("s4", 8, "d4")
    locked.status = "aborted"
    locked.ended_at = 0.0

    async with locked.lock:
        await store.sweep_once(10 * TTL)
    assert {s.session_id for s in store.snapshot_all()} == {"s3", "s4"}
    assert finalizing.status == "finalizing"


async def test_eviction_never_drops_a_reopened_session_with_the_same_id() -> None:
    store = _store()
    old, _ = await store.open_or_get("s5", 7, "d5")
    old.status = "aborted"
    old.ended_at = 0.0
    await store.remove("s5")
    fresh, status = await store.open_or_get("s5", 7, "d5")
    assert status == "new"
    await store._evict(old)  # the old object is gone; the new one must survive
    assert (await store.get_for_owner("s5", 7)) is fresh
