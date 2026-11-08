"""
dedup.py — the dictation dedup contract (Locked Decision 7).

``X-Client-Dedup-Id`` (a lowercase RFC-4122 UUID) names one dictation. The
routes (``routes/dictate.py``, ``routes/dictate_stream.py``) use:

* ``dedup_id(request, form_value, required=…)`` — parse + validate (422).
* ``DedupLocks.claim(id)`` — per-process in-flight claim keyed by the id alone
  (across users), held across replay check → inference → persist, so two
  concurrent requests with one id run inference once.
* ``replay_or_none`` — the stored text (``request.state.dedup_replay``), or
  the pinned terminal bodies: 409 ``{"error":"dedup_conflict"}`` (another
  user's id), 410 ``{"error":"dictation_deleted"}`` (the owner deleted it).
* ``persist`` — synchronous atomic save; a ``sqlite3.Error`` still returns
  the text with ``saved=False`` and bumps ``dictation_save_failures_total``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from . import observability
from ._errors import DedupConflict, DictationDeleted

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .jobs.store import JobStore

logger = logging.getLogger(__name__)

HEADER = "X-Client-Dedup-Id"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_uuid(value: str) -> bool:
    """RFC-4122 shape, case-insensitive (session ids reuse it)."""
    return _UUID.fullmatch(value.strip().lower()) is not None


class DedupLocks:
    """Refcounted ``asyncio.Lock`` per dedup id; an entry is dropped when its last holder leaves."""

    def __init__(self) -> None:
        self._m: dict[str, list[Any]] = {}

    @asynccontextmanager
    async def claim(self, dedup: str) -> AsyncIterator[None]:
        entry = self._m.setdefault(dedup, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._m.pop(dedup, None)


def locks(request: Request) -> DedupLocks:
    """The app's ``DedupLocks`` (created on first use; one event loop, no await in between)."""
    state = request.app.state
    if getattr(state, "dedup_locks", None) is None:
        state.dedup_locks = DedupLocks()
    return state.dedup_locks


def dedup_id(request: Request, form_value: str | None, *, required: bool) -> str | None:
    """The canonical id: header wins; header ≠ form, missing-when-required, or malformed → 422."""
    header = request.headers.get(HEADER)
    norm = [v.strip().lower() for v in (header, form_value) if v is not None]
    if len(norm) == 2 and norm[0] != norm[1]:
        raise HTTPException(422, f"{HEADER} does not match client_dedup_id")
    if not norm:
        if required:
            raise HTTPException(422, f"{HEADER} is required")
        return None
    if _UUID.fullmatch(norm[0]) is None:
        raise HTTPException(422, f"Malformed {HEADER}")
    return norm[0]


def refusal(exc: DedupConflict | DictationDeleted) -> JSONResponse:
    """The pinned machine-readable bodies (Contract for Part 3)."""
    if isinstance(exc, DedupConflict):
        return JSONResponse({"error": "dedup_conflict"}, status_code=409)
    return JSONResponse({"error": "dictation_deleted"}, status_code=410)


async def replay_or_none(
    request: Request, store: JobStore, dedup: str, user_id: int
) -> str | JSONResponse | None:
    """Stored text (marks the request a replay), a 409/410 refusal, or None (not stored)."""
    try:
        stored = await asyncio.to_thread(store.find_dictation, dedup, user_id)
    except (DedupConflict, DictationDeleted) as exc:
        return refusal(exc)
    if stored is not None:
        request.state.dedup_replay = True
    return stored


async def persist(
    request: Request, store: JobStore, **row: Any
) -> tuple[str, bool] | JSONResponse:
    """Save one dictation. Returns ``(text, saved)`` or a 409/410 refusal.

    A replayed insert returns the stored text and marks the request a replay.
    A ``sqlite3.Error`` returns ``(text, False)``: counter +1, error type logged only.
    """
    try:
        text, replayed = await asyncio.to_thread(lambda: store.insert_dictation(**row))
    except (DedupConflict, DictationDeleted) as exc:
        return refusal(exc)
    except sqlite3.Error as exc:
        observability.dictation_save_failures_total.increment()
        logger.error("dictation save failed: %s", type(exc).__name__)
        return row["text"], False
    if replayed:
        request.state.dedup_replay = True
    return text, True
