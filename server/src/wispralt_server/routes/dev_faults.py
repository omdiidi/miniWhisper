"""Development-only fault injection routes.

Mounted ONLY when ``WISPRALT_DEV_FAULTS=1`` AND the host is non-prod (a
prod-mini hostname guard that the operator can configure via
``WISPRALT_PROD_HOSTNAME``, default ``omidsmacmini.local``).

Used by the fallback-test plan to verify Success Criterion #3:
  > Transient origin 503 with X-Request-Id → does NOT trigger fallback.

Without a synthetic 503 path, that criterion is unverifiable. We deliberately
keep this in a SEPARATE router so the production ``dictate.py`` has zero new
branches — the entire surface only exists when the env flag is set.

Usage (dev only):
    WISPRALT_DEV_FAULTS=1 uvicorn wispralt_server.main:app --reload
    curl -X POST 'http://localhost:8000/transcribe/dictate?fault=503' ...
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger(__name__)

router = APIRouter()


def is_dev_faults_enabled() -> bool:
    """True iff this process should mount the dev-faults router.

    Both gates must be true:
      1. ``WISPRALT_DEV_FAULTS=1``
      2. Hostname is NOT the configured prod hostname.
    """
    if os.environ.get("WISPRALT_DEV_FAULTS") != "1":
        return False
    prod_host = os.environ.get("WISPRALT_PROD_HOSTNAME", "omidsmacmini.local")
    if socket.gethostname() == prod_host:
        logger.error(
            "WISPRALT_DEV_FAULTS=1 set on prod host %s — refusing to mount "
            "dev_faults router. Unset the env var.",
            prod_host,
        )
        return False
    return True


@router.post("/transcribe/dictate")
async def fault_dictate(
    request: Request,
    fault: Annotated[
        str | None,
        Query(description="Fault to inject. Currently supports '503'."),
    ] = None,
) -> dict[str, str]:
    """Synthetic dictation handler — returns the requested HTTP fault.

    The route is registered with the SAME path as the real ``/transcribe/dictate``
    handler, BUT FastAPI uses the first matching route for a method+path
    combination. Because ``include_router(dev_faults.router)`` runs AFTER
    ``include_router(dictate.router)`` in ``main.py``, the real route wins
    when no fault parameter is set — ``fault=503`` only intercepts when the
    request explicitly opts in via the query parameter.

    NOTE: We rely on FastAPI / Starlette returning 422 / no-match when the
    Query parameter is absent. To make routing deterministic, we ALWAYS log
    a WARNING when this handler is reached so operators can see if it ever
    fires accidentally.
    """
    logger.warning(
        "DEV FAULT INJECTION: /transcribe/dictate?fault=%s reached. This MUST NOT "
        "happen in production. Verify WISPRALT_DEV_FAULTS is unset.",
        fault,
    )
    if fault == "503":
        # FastAPI exception handler emits {"detail": ...}; the request-id
        # middleware adds X-Request-Id automatically. Both characteristics
        # are required by Success Criterion #3 (origin response, not
        # tunnel-level).
        raise HTTPException(status_code=503, detail="Auth temporarily unavailable")
    raise HTTPException(status_code=400, detail=f"Unsupported fault: {fault!r}")
