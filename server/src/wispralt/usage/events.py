"""
usage/events.py — :class:`UsageEvent` dataclass.

A single per-request observation captured by the observability middleware
and written to ``usage_events`` by ``UsageStore.record``.

Fields are nullable when not applicable to the kind of event (e.g. a
``file`` submit has no ``chars`` count yet).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class UsageEvent:
    """One row of ``usage_events``."""

    user_id: int
    ts: float  # unix seconds
    kind: str  # "dictate" | "file" | "v1_dictate" | ...
    status: int
    chars: int | None = None
    duration_ms: float | None = None
    bytes_in: int | None = None
    bytes_out: int | None = None
    error_class: str | None = None
    request_id: str | None = None
    key_id: int | None = None  # the api_keys row that authenticated (per-device last seen)
