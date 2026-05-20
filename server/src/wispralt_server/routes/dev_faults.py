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

import asyncio
import logging
import os
import socket
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

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


@router.post("/transcribe/dictate/stream/{session_id}/chunk/{index}")
async def fault_stream_chunk(
    session_id: str,
    index: int,
    fault: Annotated[
        str | None,
        Query(description="Streaming-chunk fault to inject. Currently supports 'stall'."),
    ] = None,
) -> JSONResponse:
    """Shadow handler for the streaming chunk endpoint.

    Mirrors the existing ``/transcribe/dictate`` shadow above: registered with
    the same path as the real ``routes/dictate_stream.py`` handler, but
    ``include_router(dev_faults.router)`` runs AFTER
    ``include_router(dictate_stream.router)`` in ``main.py`` so the production
    route wins when no ``?fault=`` query parameter is set.

    Used by streaming-plan Test K: a 20-second sleep (longer than
    ``STREAMING_FINALIZE_TIMEOUT_S=15``) so the client's per-chunk POST
    timeout fires and the recorder falls back to the existing dictation
    ladder.
    """
    logger.warning(
        "DEV FAULT INJECTION: /transcribe/dictate/stream/%s/chunk/%d?fault=%s "
        "reached. MUST NOT happen in production.",
        session_id, index, fault,
    )
    if fault == "stall":
        await asyncio.sleep(20)
        return JSONResponse(
            {"received_index": index, "queue_depth": 0}, status_code=202
        )
    raise HTTPException(status_code=400, detail=f"Unsupported fault: {fault!r}")


@router.post("/transcribe/dictate/stream/{session_id}/finalize")
async def fault_stream_finalize(
    session_id: str,
    fault: Annotated[
        str | None,
        Query(description="Streaming-finalize fault to inject. Currently supports 'stall'."),
    ] = None,
) -> JSONResponse:
    """Shadow handler for the streaming finalize endpoint. Same precedence
    contract as ``fault_stream_chunk`` — production route wins by default.

    ``fault=stall`` simulates a server-side hang past the finalize timeout
    so the recorder's safety-net path activates.
    """
    logger.warning(
        "DEV FAULT INJECTION: /transcribe/dictate/stream/%s/finalize?fault=%s "
        "reached. MUST NOT happen in production.",
        session_id, fault,
    )
    if fault == "stall":
        await asyncio.sleep(20)
        raise HTTPException(status_code=504, detail="Stalled by dev fault")
    raise HTTPException(status_code=400, detail=f"Unsupported fault: {fault!r}")


@router.post("/dev/db/close")
async def dev_db_close(request: Request) -> JSONResponse:
    """Force the asyncpg pool closed to reproduce InterfaceError.

    Used by scripts/check-watcher.sh to exercise the watcher recovery
    path end-to-end. Calls pool.close() directly, which reproduces the
    EXACT failure mode from 2026-05-17 (InterfaceError "pool is closed"
    on next acquire) — pg_terminate_backend cannot do this because
    asyncpg surfaces backend termination as ConnectionDoesNotExistError
    (a PostgresError subclass) which the pre-f5178be code would have
    caught fine. This endpoint is the only way to reproduce the actual
    bug in a deterministic way.

    Returns 200 with the previous pool's status. The watcher loop will
    detect the closed pool within ~10s and rebuild it.
    """
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        return JSONResponse(
            {"status": "no_pool", "action": "noop"},
            status_code=200,
        )
    try:
        await pool.close()
    except Exception:  # noqa: BLE001 — best-effort fault injection
        logger.exception("dev_db_close: pool.close() raised; pool may already be closed")
    return JSONResponse(
        {"status": "closed", "next_rebuild_within_s": 10},
        status_code=200,
    )
