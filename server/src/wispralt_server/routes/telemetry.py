"""Telemetry ingest — out-of-band write paths for things the server didn't observe.

Currently one endpoint: ``POST /telemetry/cloud-dictation``. Swift client sends
batches here when a dictation succeeded via the OpenRouter cloud-fallback path
(server was offline) so the row eventually lands in /me/history + future
period insights. Idempotent via per-dictation ``client_dedup_id`` (UUIDv4)
+ partial unique index in dictations.

Auth: same Bearer-token model as other authed routes
(Depends(forbid_integration_kind)). Integration keys (kind='integration') are
rejected here — telemetry ingest is a human-employee surface and integration
programs have no reason to push cloud-fallback dictation rows.
Bearer-ONLY — cookie-only requests are rejected to prevent browser-CSRF abuse
from a malicious site that holds a session cookie but no token.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from wispralt_server.auth import forbid_integration_kind

if TYPE_CHECKING:
    from wispralt_server.users.store import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


class CloudDictation(BaseModel):
    client_dedup_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-fA-F-]{36}$",
    )
    text: str = Field(min_length=1, max_length=200_000)
    dictated_at: float = Field(ge=0)  # UTC epoch; bounds-checked in handler
    word_count: int | None = Field(default=None, ge=0)
    client_app_version: str | None = Field(default=None, max_length=64)


class CloudDictationBatch(BaseModel):
    dictations: list[CloudDictation] = Field(min_length=1, max_length=200)


class CloudDictationBatchResponse(BaseModel):
    inserted: int  # rows that actually landed (excludes dedup conflicts)
    received: int  # rows in the batch (post-validation)


@router.post(
    "/cloud-dictation",
    response_model=CloudDictationBatchResponse,
)
async def post_cloud_dictation(
    body: CloudDictationBatch,
    request: Request,
    user: "User" = Depends(forbid_integration_kind),
) -> CloudDictationBatchResponse:
    """Accept a batch of cloud-fallback dictations from a Swift client.

    Validation in two layers: pydantic enforces shape (UUID format, text size,
    batch size). The handler then bounds ``dictated_at`` (±365d back, +5min
    forward) so a clock-skew or replay attack can't backfill the cron's input.

    Bearer-only: a cookie-only request (no Authorization header) is rejected
    with 401 even if the cookie auth would otherwise succeed. This prevents
    a malicious site from triggering ingest from a logged-in browser.

    Break-glass admin (user.id < 0) is rejected with 403 — break-glass has
    no user row to attribute the dictation to.
    """
    # Bearer-only — reject if no Authorization header
    if "authorization" not in {k.lower() for k in request.headers}:
        raise HTTPException(401, "Bearer authentication required")

    if user.id < 0:
        raise HTTPException(403, "Break-glass admin cannot ingest dictations")

    store = request.app.state.job_store
    now = time.time()
    floor = now - 365 * 86400
    ceiling = now + 300

    inserted_count = 0
    for d in body.dictations:
        # Bounds-check dictated_at
        if d.dictated_at < floor or d.dictated_at > ceiling:
            logger.warning(
                "cloud dictation skipped: dictated_at=%s out of range "
                "(now=%s) user=%s dedup=%s",
                d.dictated_at,
                now,
                user.id,
                d.client_dedup_id,
            )
            continue
        # Reject whitespace-only text
        if not d.text.strip():
            continue
        wc = d.word_count if d.word_count is not None else len(d.text.split())
        try:
            rowid = await asyncio.to_thread(
                store.insert_cloud_fallback_dictation,
                api_key_id=user.id,
                text=d.text,
                dictated_at=d.dictated_at,
                word_count=wc,
                client_app_version=d.client_app_version,
                client_dedup_id=d.client_dedup_id,
            )
            if rowid is not None:
                inserted_count += 1
        except Exception:
            logger.exception(
                "cloud dictation insert failed user=%s dedup=%s",
                user.id,
                d.client_dedup_id,
            )
            # Continue the batch — one bad row doesn't fail the whole drain.

    return CloudDictationBatchResponse(
        inserted=inserted_count,
        received=len(body.dictations),
    )
