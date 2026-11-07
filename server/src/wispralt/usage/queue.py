"""
usage/queue.py — bounded :class:`UsageEventQueue` for the dictation hot path.

The middleware enqueues a :class:`UsageEvent` after every tracked request.
The bounded ``asyncio.Queue`` keeps memory predictable; on overflow we drop
the oldest entry rather than block the request thread.

Thread-safety
-------------
``asyncio.Queue`` is **not** thread-safe.  ``offer()`` MUST be called from
the FastAPI event loop (which is where ``_ObservabilityMiddleware.dispatch``
runs).  A defensive ``get_running_loop`` probe catches future misuse from
a thread pool — we drop and warn instead of corrupting the queue.
"""

from __future__ import annotations

import asyncio
import logging

from .events import UsageEvent

logger = logging.getLogger(__name__)


class UsageEventQueue:
    """Bounded asyncio.Queue.  Drops oldest on overflow with WARNING log.

    NOT thread-safe.  Callers MUST be inside the running event loop.
    """

    _MAX = 1000

    def __init__(self) -> None:
        self._q: asyncio.Queue[UsageEvent] = asyncio.Queue(maxsize=self._MAX)
        self._dropped: int = 0

    def offer(self, event: UsageEvent) -> None:
        """Enqueue *event*; drop the oldest if the queue is full.

        Safe to call from any coroutine on the event loop.  Returns
        immediately — never blocks.
        """
        # Defense: refuse to operate from a non-loop thread.
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # no running loop in this thread
            logger.warning(
                "UsageEventQueue.offer() called from non-async context; dropping"
            )
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
            logger.warning(
                "usage_event queue full; dropped one (total=%d)", self._dropped
            )

    async def drain_one(self) -> UsageEvent:
        """Block until at least one event is available, then return it."""
        return await self._q.get()

    @property
    def dropped(self) -> int:
        """Cumulative count of events dropped due to overflow / loop misuse."""
        return self._dropped
