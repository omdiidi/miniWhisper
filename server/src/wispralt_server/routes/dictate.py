"""
routes/dictate.py — POST /transcribe/dictate

Accepts a multipart audio upload, validates it, and transcribes it via
ParakeetService.  Returns structured JSON with the transcript text, model
identifier, and wall-clock inference duration.

Validation at this boundary (per backend-patterns rule):
- Content-Type must be audio/*  (HTTP 415)
- Content-Length must be <= settings.max_upload_bytes  (HTTP 413)
- Corrupt audio reported by the audio layer  (HTTP 422)
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from ..audio import CorruptAudioError
from ..auth import require_api_key
from ..config import settings
from ..dictate.parakeet import MODEL_ID
from ..jobs.store import JobStore
from ..smart_format.mercury_client import _word_count
from ..users.store import User

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/transcribe/dictate",
    summary="Transcribe a short dictation clip",
)
async def transcribe_dictate(
    request: Request,
    file: UploadFile,
    user: User = Depends(require_api_key),
) -> JSONResponse:
    """Transcribe an audio file using the warm Parakeet model.

    Parameters (multipart/form-data)
    ----------------------------------
    file : UploadFile
        Audio clip (WAV preferred; any soundfile-supported format accepted).

    Returns
    -------
    200 JSON
        ``{ "text": str, "model_id": str, "duration_ms": float }``

    Errors
    ------
    413  Content-Length exceeds MAX_UPLOAD_BYTES
    415  Content-Type is not audio/*
    422  Bytes cannot be decoded as valid audio
    """
    # 1. Content-Type validation — must be audio/*
    content_type = (file.content_type or "").lower()
    if not content_type.startswith("audio/"):
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported media type '{content_type}'; expected audio/*",
        )

    # 2. Pre-flight size check from Content-Length header (fast path; avoids
    #    reading the entire body before rejecting)
    content_length_header = request.headers.get("content-length")
    if content_length_header is not None:
        try:
            declared_length = int(content_length_header)
        except ValueError:
            declared_length = 0
        if declared_length > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Content-Length {declared_length} exceeds "
                    f"MAX_UPLOAD_BYTES {settings.max_upload_bytes}"
                ),
            )

    # 3. Read body — enforce size limit while reading
    body_start = time.perf_counter()
    audio_bytes = await file.read(settings.max_upload_bytes + 1)
    body_read_ms = (time.perf_counter() - body_start) * 1_000.0
    if len(audio_bytes) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Upload size {len(audio_bytes)} exceeds "
                f"MAX_UPLOAD_BYTES {settings.max_upload_bytes}"
            ),
        )

    # 4. Dispatch to ParakeetService (runs in single-thread executor)
    parakeet_service = request.app.state.parakeet_service
    inference_start = time.perf_counter()

    try:
        text, inference_ms = await parakeet_service.transcribe(audio_bytes)
    except CorruptAudioError as exc:
        logger.warning("Corrupt audio upload: %s", exc)
        raise HTTPException(status_code=422, detail=f"Corrupt audio: {exc}") from exc

    # Record timestamp of last successful inference for /metrics observability.
    request.app.state.parakeet_last_inference_at = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    )

    # Smart formatting: client-only opt-in via X-Smart-Format header.
    # /v1/audio/transcriptions never sets this header (raw output by contract).
    # Permissive value parsing: accept "true", "1", "yes" (case-insensitive).
    header_val = request.headers.get("X-Smart-Format", "").strip().lower()
    smart_format_requested = header_val in {"true", "1", "yes"}
    mercury_client = getattr(request.app.state, "mercury_client", None)
    applied_smart_format = False
    smart_format_ms = 0.0
    if smart_format_requested and mercury_client is not None:
        raw_text = text
        sf_start = time.perf_counter()
        cleaned = await mercury_client.clean_up(text)
        smart_format_ms = (time.perf_counter() - sf_start) * 1_000.0
        if cleaned is not None:
            text = cleaned
            applied_smart_format = True
            logger.info(
                "dictate: smart-format applied raw_words=%d cleaned_words=%d ms=%.1f",
                _word_count(raw_text),
                _word_count(text),
                smart_format_ms,
            )

    # Single per-request timing line — the breakdown a future investigator
    # needs to attribute latency to a stage without re-deriving it from
    # request_id correlation. Order: how a request flows.
    total_handler_ms = (time.perf_counter() - body_start) * 1_000.0
    logger.info(
        "dictate: body_read_ms=%.1f inference_ms=%.1f smart_format_ms=%.1f "
        "total_handler_ms=%.1f bytes_in=%d chars=%d sf=%s",
        body_read_ms,
        inference_ms,
        smart_format_ms,
        total_handler_ms,
        len(audio_bytes),
        len(text),
        applied_smart_format,
    )

    # Phase 1 transcript-storage: fire-and-forget INSERT into the dictations
    # table. Skip break-glass admin (user.id < 0) and empty/whitespace-only
    # text — both would pollute the future weekly-insights cron with noise.
    # The dictation HTTP response has already been computed above; this task
    # runs after the route returns and never blocks the user-facing path.
    if (
        user is not None
        and user.id >= 0
        and text
        and text.strip()
    ):
        client_version = request.headers.get("X-WisprAlt-Client-Version")
        job_store: JobStore | None = getattr(
            request.app.state, "job_store", None
        )
        # duration_ms here stores Parakeet inference latency (NOT audio
        # length). Phase 2 weekly cron can use it for per-user latency
        # trends; if we ever want true audio length, add a separate column.
        captured_inference_ms = (
            int(inference_ms) if inference_ms is not None else None
        )
        captured_text = text
        captured_smart_format = applied_smart_format
        captured_api_key_id = int(user.id)

        async def _persist_dictation() -> None:
            if job_store is None:
                logger.error("dictation persist skipped: job_store missing")
                return
            try:
                # asyncio.to_thread is required: JobStore._exec acquires a
                # threading.Lock (sync). Calling synchronously from the event
                # loop would block under SQLite-WAL contention.
                await asyncio.to_thread(
                    job_store.insert_dictation,
                    captured_api_key_id,
                    captured_text,
                    captured_inference_ms,
                    client_version,
                    captured_smart_format,
                )
            except (sqlite3.Error, OSError) as exc:
                # Narrow typed errors — only the realistic failure modes for
                # insert_dictation. Use logger.error (NOT .exception) so a
                # custom traceback formatter can't include the transcript
                # text via locals; log the exception type only.
                logger.error(
                    "dictation persist failed: %s", type(exc).__name__
                )

        # Task-leak guard: hold a strong reference until completion. Without
        # this, asyncio.create_task tasks with no other reference can be
        # garbage-collected before they run (documented asyncio behavior).
        # request.app.state.pending_persists is initialized in lifespan().
        pending: set[asyncio.Task[None]] | None = getattr(
            request.app.state, "pending_persists", None
        )
        task = asyncio.create_task(_persist_dictation())
        if pending is not None:
            pending.add(task)
            task.add_done_callback(pending.discard)

    return JSONResponse(
        content={
            "text": text,
            "model_id": MODEL_ID,
            "duration_ms": round(inference_ms, 2),
            "smart_formatted": applied_smart_format,
        }
    )
