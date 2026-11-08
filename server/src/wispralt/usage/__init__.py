"""
usage — Per-request usage event tracking (fire-and-forget).

Subpackages:
- ``events`` — :class:`UsageEvent` dataclass.
- ``queue`` — bounded :class:`UsageEventQueue` (asyncio.Queue, drop-oldest).
- ``writer`` — background drain loop that batches inserts into
  ``wispralt.usage_events``.
- ``store`` — :class:`UsageStore` on the local SQLite DB (``record`` plus the
  portal reads); replaces ``queue`` + ``writer`` at the auth cutover.
"""

from __future__ import annotations
