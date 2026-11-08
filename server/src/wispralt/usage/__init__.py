"""
usage — per-request usage events on the local SQLite DB.

- ``events`` — :class:`UsageEvent` dataclass.
- ``store`` — :class:`UsageStore`: ``record`` (written in-request by the
  observability middleware, best-effort) plus the portal reads.
"""

from __future__ import annotations
