"""
usage — Per-request usage event tracking (fire-and-forget).

Subpackages:
- ``events`` — :class:`UsageEvent` dataclass.
- ``queue`` — bounded :class:`UsageEventQueue` (asyncio.Queue, drop-oldest).
- ``writer`` — background drain loop that batches inserts into
  ``wispralt.usage_events``.
"""

from __future__ import annotations
