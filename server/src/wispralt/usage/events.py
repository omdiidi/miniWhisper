"""
usage/events.py — :class:`UsageEvent` dataclass.

A single per-request observation captured by the observability middleware
and drained into ``wispralt.usage_events`` by the background writer.

Fields are nullable when not applicable to the kind of event (e.g. a
``meeting`` create has no ``chars`` count yet).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class UsageEvent:
    """One row to be inserted into ``wispralt.usage_events``."""

    user_id: int
    ts: float  # unix seconds
    kind: str  # "dictate" | "meeting" | ...
    status: int
    chars: int | None = None
    duration_ms: float | None = None
    bytes_in: int | None = None
    bytes_out: int | None = None
    error_class: str | None = None
    request_id: str | None = None
