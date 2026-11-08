"""
usage/events.py — :class:`UsageEvent` dataclass.

A single per-request observation captured by the observability middleware
and written to ``usage_events`` by ``UsageStore.record``.

Fields are nullable when not applicable to the kind of event (e.g. a
``file`` submit has no ``chars`` count yet).
"""

from __future__ import annotations

from dataclasses import dataclass

# ``usage_events.kind`` values. A whole request is one dictation or file job (the
# streamed finalize / chunked finalize included); a part is one piece of it.
DICTATE = "dictate"
DICTATE_CHUNK = "dictate_chunk"
FILE = "file"
FILE_CHUNK = "file_chunk"
V1_DICTATE = "v1_dictate"
V1_TRANSLATIONS = "v1_translations"
WHOLE_REQUEST_KINDS = (DICTATE, FILE, V1_DICTATE)


def kind_for_path(method: str, path: str) -> str | None:
    """The usage kind for a request, or None when it is not tracked.

    Only POSTs that create work are tracked (polls and downloads are not).
    Streamed-dictation chunks and chunked-upload pieces get their own kinds,
    so ``dictate`` latency and error rates mean one row per dictation.
    """
    if method != "POST":
        return None
    parts = [p for p in path.strip("/").split("/") if p]
    match parts:
        case ["transcribe", "dictate"] | ["transcribe", "dictate", "stream", _, "finalize"]:
            return DICTATE
        case ["transcribe", "dictate", "stream", _, "chunk", _]:
            return DICTATE_CHUNK
        case ["transcribe", "file"] | ["transcribe", "file", "chunked", _, "finalize"]:
            return FILE
        case ["transcribe", "file", "chunked", *_]:
            return FILE_CHUNK
        case ["v1", "audio", "translations"]:
            return V1_TRANSLATIONS
        case ["v1", "audio", *_]:
            return V1_DICTATE
    return None


@dataclass(slots=True)
class UsageEvent:
    """One row of ``usage_events``."""

    user_id: int
    ts: float  # unix seconds
    kind: str  # one of the kind constants above (see ``kind_for_path``)
    status: int
    chars: int | None = None
    duration_ms: float | None = None
    bytes_in: int | None = None
    bytes_out: int | None = None
    error_class: str | None = None
    request_id: str | None = None
    key_id: int | None = None  # the api_keys row that authenticated (per-device last seen)
