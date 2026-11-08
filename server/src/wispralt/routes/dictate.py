"""
routes/dictate.py — POST /transcribe/dictate

Accepts a multipart audio upload, validates it, and transcribes it via
ParakeetService.  Returns structured JSON with the transcript text, model
identifier, and wall-clock inference duration.

Validation at this boundary (per backend-patterns rule):
- Content-Type must be audio/*  (HTTP 415)
- Content-Length must be <= settings.max_upload_bytes  (HTTP 413)
- Corrupt audio reported by the audio layer  (HTTP 422)
- Malformed ``X-Client-Dedup-Id`` (HTTP 422)

Dedup (``dedup.py``, Locked Decision 7): the optional ``X-Client-Dedup-Id``
is claimed for the whole request; a stored id replays its text without
inference (``"replayed": true``) or returns the pinned 409/410 body. The row
is saved synchronously; every 200 carries ``"saved": true|false``.
"""

from __future__ import annotations

import contextlib
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from .. import dedup
from ..accounts.store import User
from ..audio import CorruptAudioError
from ..auth import require_bearer
from ..config import settings
from ..dictate.parakeet import MODEL_ID
from ..jobs.store import JobStore

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/transcribe/dictate",
    summary="Transcribe a short dictation clip",
)
async def transcribe_dictate(
    request: Request,
    file: UploadFile,
    user: User = Depends(require_bearer),
) -> JSONResponse:
    """Transcribe an audio file using the warm Parakeet model.

    Parameters (multipart/form-data)
    ----------------------------------
    file : UploadFile
        Audio clip (WAV preferred; any soundfile-supported format accepted).

    Returns
    -------
    200 JSON
        ``{ "text", "model_id", "duration_ms", "replayed": bool, "saved": bool }``

    Errors
    ------
    413  Content-Length exceeds MAX_UPLOAD_BYTES
    415  Content-Type is not audio/*
    409  ``{"error": "dedup_conflict"}`` — the dedup id is another user's
    410  ``{"error": "dictation_deleted"}`` — the owner deleted that dictation
    422  Bytes cannot be decoded as valid audio, or a malformed dedup id
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

    # 4. Dedup: claim the id for the whole request, then replay if stored.
    did = dedup.dedup_id(request, None, required=False)
    job_store: JobStore = request.app.state.job_store
    claim = dedup.locks(request).claim(did) if did else contextlib.nullcontext()
    async with claim:
        if did is not None:
            stored = await dedup.replay_or_none(request, job_store, did, user.id)
            if isinstance(stored, JSONResponse):
                return stored
            if stored is not None:
                return _response(stored, 0.0, replayed=True, saved=True)

        # 5. Dispatch to ParakeetService (runs in single-thread executor)
        try:
            text, inference_ms = await request.app.state.parakeet_service.transcribe(
                audio_bytes
            )
        except CorruptAudioError as exc:
            logger.warning("Corrupt audio upload: %s", exc)
            raise HTTPException(status_code=422, detail=f"Corrupt audio: {exc}") from exc

        # Record timestamp of last successful inference for /metrics observability.
        request.app.state.parakeet_last_inference_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        total_handler_ms = (time.perf_counter() - body_start) * 1_000.0
        logger.info(
            "dictate: body_read_ms=%.1f inference_ms=%.1f "
            "total_handler_ms=%.1f bytes_in=%d chars=%d words=%d",
            body_read_ms,
            inference_ms,
            total_handler_ms,
            len(audio_bytes),
            len(text),
            len(text.split()),
        )

        # 6. Save synchronously. Blank text is not stored (insights noise);
        #    there is nothing to keep, so it reports saved=true.
        #    duration_ms stores inference latency, not audio length.
        if not text.strip():
            return _response(text, inference_ms, replayed=False, saved=True)
        saved = await dedup.persist(
            request,
            job_store,
            user_id=user.id,
            text=text,
            duration_ms=int(inference_ms),
            client_app_version=request.headers.get("X-WisprAlt-Client-Version"),
            dedup_id=did,
        )
        if isinstance(saved, JSONResponse):
            return saved
        stored_text, ok = saved
        replayed = bool(getattr(request.state, "dedup_replay", False))
        return _response(stored_text, inference_ms, replayed=replayed, saved=ok)


def _response(text: str, inference_ms: float, *, replayed: bool, saved: bool) -> JSONResponse:
    return JSONResponse(
        content={
            "text": text,
            "model_id": MODEL_ID,
            "duration_ms": round(inference_ms, 2),
            "replayed": replayed,
            "saved": saved,
        }
    )
