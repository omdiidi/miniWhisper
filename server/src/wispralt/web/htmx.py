"""HTMX request helpers."""
from __future__ import annotations

from fastapi import Request


def is_htmx(request: Request) -> bool:
    """True iff the request carries the ``HX-Request: true`` header.

    Use as a FastAPI dependency (`Depends(is_htmx)`) or call directly inside
    a handler. Comparison is case-insensitive because HTTP headers are
    case-insensitive in spec but some intermediaries normalize differently.
    """
    return request.headers.get("hx-request", "").lower() == "true"
