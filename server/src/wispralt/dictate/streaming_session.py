"""
streaming_session.py — Server-side state for cut-on-silence streaming dictation.

Phase 1 of the streaming-dictation plan. Holds per-session inference Tasks,
their partial transcripts, and a sweeper coroutine that aborts stale sessions
without ever racing the finalize path.

Concurrency contract (mirrors plan §"Lock contract"):
  * Every read or write of ``StreamingSession.{status, partial_texts,
    pending_tasks, last_seen, cumulative_audio_ms}`` MUST hold
    ``session.lock``.
  * The sweeper SKIPS sessions whose ``lock`` is already held OR whose
    ``status`` is in ``{"finalizing", "finalized", "aborted"}`` — this is what
    closes the sweeper-at-lock-boundary race.
  * ``finalize`` snapshots ``pending_tasks.values()`` and ``partial_texts``
    under the lock BEFORE awaiting ``asyncio.gather``. The sweeper cannot
    invalidate that snapshot.

No HTTP types here — the FastAPI route layer (``routes/dictate_stream.py``,
landed in a later chunk) maps ``HTTPException`` onto ``open_or_get`` /
``get_for_owner`` results.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from fastapi import HTTPException

from .. import observability

logger = logging.getLogger(__name__)


# ── exceptions ───────────────────────────────────────────────────────────────


class FinalizeTimeout(Exception):
    """Pending inference + tail did not finish within ``finalize_timeout_s``."""


class FinalizeFailed(Exception):
    """At least one chunk inference task raised. The exception type name is
    preserved in ``args[0]`` so the route layer can log it without exposing
    stack frames."""


class FinalizeGap(Exception):
    """``partial_texts[0..tail_index]`` contained at least one ``None`` after
    all inference tasks completed — chunk lost in transit, refuse to persist
    a partial transcript."""


# ── session state ────────────────────────────────────────────────────────────


@dataclass
class StreamingSession:
    session_id: str
    owner_api_key_id: int
    started_at: float
    last_seen: float
    # Index-aligned with chunk indices. Holes (None) mean "inference still
    # pending" while the corresponding task is in ``pending_tasks``; an
    # uncovered hole at finalize time → FinalizeGap.
    partial_texts: list[str | None] = field(default_factory=list)
    # Inference Tasks keyed by chunk index. Cleared as tasks complete (or by
    # the sweeper on abort). The /finalize handler awaits ``.values()``.
    pending_tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict)
    cumulative_audio_ms: float = 0.0
    cumulative_inference_ms: float = 0.0
    status: Literal["active", "finalizing", "finalized", "aborted"] = "active"
    last_error: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ── store ────────────────────────────────────────────────────────────────────


class StreamingSessionStore:
    """Process-wide registry of active streaming sessions.

    Enforces:
      * ``max_active`` concurrent sessions (global cap, protects the single-
        thread Parakeet executor from queue depth blow-up).
      * One active session per ``api_key_id`` (atomic via ``_store_lock``).
      * TTL-based sweeping of idle sessions.

    Per-session queue-depth enforcement (``max_queue_depth``) is the route
    layer's job — this store does not police chunk POSTs.
    """

    def __init__(
        self,
        *,
        max_active: int,
        max_queue_depth: int,
        ttl_s: float,
        finalize_timeout_s: float,
    ) -> None:
        self.max_active = max_active
        self.max_queue_depth = max_queue_depth
        self.ttl_s = ttl_s
        self.finalize_timeout_s = finalize_timeout_s
        self._sessions: dict[str, StreamingSession] = {}
        # Coarse lock — held only for the atomic check-and-create in
        # open_or_get and for snapshot_all. Session-level mutations use
        # ``StreamingSession.lock`` instead.
        self._store_lock = asyncio.Lock()

    async def open_or_get(
        self, session_id: str, owner_api_key_id: int
    ) -> tuple[StreamingSession, str]:
        """Atomically open a new session or return an existing one.

        Status strings:
          * ``"new"`` — created on this call.
          * ``"existing"`` — same ``(session_id, owner_api_key_id)`` already
            owned by this caller; ``last_seen`` is refreshed.
          * ``"denied_owner_mismatch"`` — session id exists but belongs to a
            different api_key.
          * ``"denied_user_busy"`` — caller already owns another active
            session (per-user single-session enforcement).
          * ``"denied_capacity"`` — global ``max_active`` reached.
        """
        async with self._store_lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                if existing.owner_api_key_id != owner_api_key_id:
                    return existing, "denied_owner_mismatch"
                # Same owner re-poking same session id — refresh last_seen.
                existing.last_seen = time.time()
                return existing, "existing"

            # Per-user single-session: scan for another active session owned
            # by this api_key. Active = status == "active". "finalizing" is
            # excluded so a client that fires /chunk while a previous
            # /finalize is in-flight gets owner_mismatch on a fresh sid,
            # NOT user_busy. (Plan: per-user concurrency cap = 1 active
            # transcription; finalize completes within ~15s.)
            for s in self._sessions.values():
                if s.owner_api_key_id == owner_api_key_id and s.status == "active":
                    return s, "denied_user_busy"

            # Capacity check across all non-terminal sessions.
            active_count = sum(
                1 for s in self._sessions.values() if s.status in ("active", "finalizing")
            )
            if active_count >= self.max_active:
                # Return a sentinel session — callers must inspect the status
                # string and reject the request before using the session.
                placeholder = StreamingSession(
                    session_id=session_id,
                    owner_api_key_id=owner_api_key_id,
                    started_at=0.0,
                    last_seen=0.0,
                )
                return placeholder, "denied_capacity"

            now = time.time()
            session = StreamingSession(
                session_id=session_id,
                owner_api_key_id=owner_api_key_id,
                started_at=now,
                last_seen=now,
            )
            self._sessions[session_id] = session
            observability.streaming_sessions_opened_total.increment()
            return session, "new"

    async def get_for_owner(
        self, session_id: str, owner_api_key_id: int
    ) -> StreamingSession:
        """Look up an existing session. Raises ``HTTPException(404)`` if the
        session id is unknown (or was swept) and ``HTTPException(403)`` if the
        caller does not own it. Does not refresh ``last_seen``; the caller
        does that under ``session.lock`` once it begins to mutate state.
        """
        async with self._store_lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="streaming session not found")
        if session.owner_api_key_id != owner_api_key_id:
            raise HTTPException(status_code=403, detail="streaming session owner mismatch")
        return session

    async def enqueue_inference(
        self,
        session: StreamingSession,
        index: int,
        audio_bytes: bytes,
        parakeet_service,  # avoids a circular import; route layer passes in the singleton
    ) -> None:
        """Schedule chunk inference. The Task awaits ``parakeet_service.transcribe``
        (which itself dispatches to the single-thread executor). On success the
        partial text is written to ``session.partial_texts[index]`` under
        ``session.lock``. On any exception the session is marked ``"aborted"``.

        Grows ``partial_texts`` so the index is in range. Caller (route layer)
        has already validated index monotonicity and queue depth.
        """

        async def _run() -> None:
            t0 = time.monotonic()
            try:
                # ParakeetService.transcribe returns (text, inference_ms). We
                # already measure wall-clock here so the inference_ms component
                # is informational only — discard via tuple unpack rather than
                # storing the tuple in partial_texts.
                text, _inference_ms = await parakeet_service.transcribe(audio_bytes)
            except Exception as exc:  # noqa: BLE001 — propagate type name to last_error
                async with session.lock:
                    session.status = "aborted"
                    session.last_error = type(exc).__name__
                observability.streaming_sessions_aborted_total.increment(
                    "chunk_failed"
                )
                logger.warning(
                    "streaming chunk inference failed sid=%s idx=%d err=%s",
                    session.session_id,
                    index,
                    type(exc).__name__,
                )
                raise
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            async with session.lock:
                while len(session.partial_texts) <= index:
                    session.partial_texts.append(None)
                session.partial_texts[index] = text
                session.cumulative_inference_ms += elapsed_ms
                session.last_seen = time.time()

        task = asyncio.create_task(_run())
        # Register under session.lock so the sweeper's snapshot is consistent.
        async with session.lock:
            while len(session.partial_texts) <= index:
                session.partial_texts.append(None)
            session.pending_tasks[index] = task
            session.last_seen = time.time()

    async def finalize(
        self,
        session: StreamingSession,
        tail_bytes: bytes,
        tail_index: int,
        parakeet_service,
        finalize_timeout_s: float,
        pending_snapshot: dict[int, asyncio.Task[None]],
    ) -> tuple[str, float, int]:
        """Drain pending inference + tail, then concatenate.

        Caller has ALREADY:
          1. Acquired ``session.lock``.
          2. Set ``session.status = "finalizing"``.
          3. Snapshotted ``pending_tasks`` (passed in as ``pending_snapshot``).
          4. Released ``session.lock`` so the gather can run without blocking
             chunk-completion callbacks above.

        Returns ``(joined_text, cumulative_inference_ms, chunk_count)``.
        Raises:
          * ``FinalizeTimeout`` — gather exceeded ``finalize_timeout_s``.
          * ``FinalizeFailed`` — any task (including the tail) raised.
          * ``FinalizeGap`` — concat-time scan found a ``None`` at index
            ``0..tail_index``.
        """
        # Schedule the tail inference using the same code path as a regular
        # chunk so its result lands in partial_texts[tail_index].
        await self.enqueue_inference(session, tail_index, tail_bytes, parakeet_service)
        # Read the tail task out of pending_tasks under the lock so we await
        # the same Task object enqueue_inference stored.
        async with session.lock:
            tail_task = session.pending_tasks.get(tail_index)
        all_tasks = list(pending_snapshot.values())
        if tail_task is not None and tail_task not in all_tasks:
            all_tasks.append(tail_task)

        try:
            await asyncio.wait_for(
                asyncio.gather(*all_tasks, return_exceptions=False),
                timeout=finalize_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise FinalizeTimeout(
                f"finalize exceeded {finalize_timeout_s:.1f}s"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — propagate type name
            raise FinalizeFailed(type(exc).__name__) from exc

        # Concatenate under the lock so we read a coherent partial_texts.
        async with session.lock:
            for i in range(tail_index + 1):
                if i >= len(session.partial_texts) or session.partial_texts[i] is None:
                    raise FinalizeGap(
                        f"partial_texts gap at index {i} (tail_index={tail_index})"
                    )
            parts = [
                (session.partial_texts[i] or "").strip()
                for i in range(tail_index + 1)
            ]
            joined = " ".join(p for p in parts if p)
            chunk_count = tail_index + 1
            inference_ms = session.cumulative_inference_ms
        return joined, inference_ms, chunk_count

    def snapshot_all(self) -> list[StreamingSession]:
        """Synchronous snapshot for the sweeper. Returns a shallow copy of the
        session list so the sweeper can iterate without holding ``_store_lock``
        across awaits.
        """
        return list(self._sessions.values())

    async def remove(self, session_id: str) -> None:
        """Remove a session from the registry. Safe to call multiple times."""
        async with self._store_lock:
            self._sessions.pop(session_id, None)


# ── sweeper ──────────────────────────────────────────────────────────────────


async def _streaming_sweeper(app) -> None:
    """Background coroutine — every 30 s mark stale sessions ``"aborted"`` and
    cancel their pending inference tasks. Bound to the FastAPI lifespan;
    cancellation on shutdown is silent.

    Sweep contract (plan §"Lock contract"):
      * Skip sessions where ``lock.locked()`` is True — finalize / chunk POST
        is mid-flight and will move the session forward itself.
      * Skip sessions whose status is already ``"finalizing"``, ``"finalized"``,
        or ``"aborted"``.
      * For survivors, check ``time.time() - last_seen > ttl_s`` and, if stale,
        acquire the lock, set status to ``"aborted"``, cancel every task in
        ``pending_tasks``.
    """
    store: StreamingSessionStore | None = getattr(
        app.state, "streaming_sessions", None
    )
    if store is None:
        logger.warning(
            "streaming sweeper started without app.state.streaming_sessions"
        )
        return
    try:
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for session in store.snapshot_all():
                if session.lock.locked():
                    continue
                if session.status in ("finalizing", "finalized", "aborted"):
                    continue
                if now - session.last_seen <= store.ttl_s:
                    continue
                # Stale. Re-check status under the lock to avoid racing a
                # finalize that grabbed the lock between our locked() check
                # and the acquire below.
                async with session.lock:
                    if session.status not in ("active",):
                        continue
                    session.status = "aborted"
                    session.last_error = "ttl_expired"
                    for task in session.pending_tasks.values():
                        task.cancel()
                observability.streaming_sessions_aborted_total.increment(
                    "ttl_expired"
                )
                logger.info(
                    "streaming sweeper aborted stale session sid=%s owner=%d"
                    " age_s=%.1f",
                    session.session_id,
                    session.owner_api_key_id,
                    now - session.started_at,
                )
    except asyncio.CancelledError:
        # Clean lifespan shutdown — propagate so the gathering task notices.
        raise
