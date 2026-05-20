"""
routes/dictate_stream.py — Additive streaming-dictation endpoints.

Pseudocode source: ``tmp/ready-plans/2026-05-18-streaming-dictation-chunks.md`` §5.

Two POSTs under ``/transcribe/dictate/stream/{session_id}/...``:

* ``.../chunk/{index}`` — accepts a mid-recording WAV chunk, enqueues
  inference, returns 202 immediately.
* ``.../finalize`` — accepts the tail WAV, awaits all pending inference (with
  a hard timeout), joins partial transcripts, optionally runs Mercury smart-
  format on the joined text, persists one idempotent dictation row, and
  returns the final transcript.

Auth: same ``require_api_key`` dep as ``/transcribe/dictate``. Break-glass
admin (``user.id < 0``) is rejected — streaming requires a per-user api_key
because session ownership is enforced by ``api_key_id``.

Concurrency contract: every read or write of ``StreamingSession`` state must
hold ``session.lock``; this route obeys that rule and snapshots ``pending_tasks``
under the lock before awaiting ``store.finalize``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse

from .. import observability
from .._errors import CorruptAudioError
from ..audio import wav_header_duration_ms
from ..auth import require_api_key
from ..config import settings
from ..dictate.parakeet import MODEL_ID
from ..dictate.streaming_session import (
    FinalizeFailed,
    FinalizeGap,
    FinalizeTimeout,
    StreamingSessionStore,
)
from ..jobs.store import JobStore
from ..users.store import User

logger = logging.getLogger(__name__)

router = APIRouter()

# Same regex as routes/dictate.py — keep them in sync. The streaming protocol
# requires both session_id and client_dedup_id to be UUID-shaped (lowercase
# hex with hyphens; the i-flag tolerates uppercase from older Swift builds).
_UUID_RE = re.compile(r"^[0-9a-f-]{36}$", re.I)


@router.post(
    "/transcribe/dictate/stream/{session_id}/chunk/{index}",
    summary="Stream a mid-recording dictation chunk (additive endpoint)",
)
async def stream_chunk(
    session_id: str,
    index: int,
    request: Request,
    file: UploadFile,
    user: User = Depends(require_api_key),
) -> JSONResponse:
    if not _UUID_RE.match(session_id) or index < 0:
        raise HTTPException(422, "Invalid session_id or index")
    if int(user.id) < 0:
        raise HTTPException(403, "Streaming requires per-user API key")

    store: StreamingSessionStore = request.app.state.streaming_sessions
    session, status_code = await store.open_or_get(session_id, int(user.id))
    if status_code == "denied_capacity":
        raise HTTPException(503, "Streaming capacity exceeded")
    if status_code == "denied_user_busy":
        raise HTTPException(409, "Another streaming session is active for this user")
    if status_code == "denied_owner_mismatch":
        raise HTTPException(403, "Owner mismatch")

    # Body read happens OUTSIDE the lock — chunk payloads can be ~1-2 MiB and
    # holding the session lock across a network read would serialize sibling
    # chunks unnecessarily.
    ct = (file.content_type or "").lower()
    if not ct.startswith("audio/"):
        raise HTTPException(
            415, f"Unsupported media type '{ct}'; expected audio/*"
        )
    audio_bytes = await file.read(settings.max_upload_bytes + 1)
    if len(audio_bytes) > settings.max_upload_bytes:
        raise HTTPException(413, "Chunk exceeds max_upload_bytes")

    try:
        chunk_duration_ms = wav_header_duration_ms(audio_bytes)
    except CorruptAudioError as exc:
        raise HTTPException(422, f"Corrupt chunk audio: {exc}") from exc

    async with session.lock:
        if session.status != "active":
            raise HTTPException(410, f"Session is {session.status}")
        if len(session.pending_tasks) >= store.max_queue_depth:
            raise HTTPException(429, "Per-session queue depth exceeded")
        # Streaming-only mid-recording cap: refuse chunks past 270 s so the
        # final join still fits inside the 300 s dictation_max_duration_s cap
        # even with a multi-second tail.
        if session.cumulative_audio_ms + chunk_duration_ms > 270_000:
            raise HTTPException(413, "Streaming audio exceeds 270 s cap")
        session.cumulative_audio_ms += chunk_duration_ms
        session.last_seen = time.time()

    await store.enqueue_inference(
        session, index, audio_bytes, request.app.state.parakeet_service
    )
    return JSONResponse(
        {
            "received_index": index,
            "queue_depth": len(session.pending_tasks),
        },
        status_code=202,
    )


@router.post(
    "/transcribe/dictate/stream/{session_id}/finalize",
    summary="Finalize a streaming-dictation session (joins partials + persists)",
)
async def stream_finalize(
    session_id: str,
    request: Request,
    file: UploadFile,
    smart_format: bool = Form(False),
    client_dedup_id: str = Form(...),
    speech_started_at: float = Form(...),
    user: User = Depends(require_api_key),
) -> JSONResponse:
    if not _UUID_RE.match(session_id) or not _UUID_RE.match(client_dedup_id):
        raise HTTPException(422, "Invalid session_id or client_dedup_id")
    if int(user.id) < 0:
        raise HTTPException(403, "Streaming requires per-user API key")
    store: StreamingSessionStore = request.app.state.streaming_sessions
    session = await store.get_for_owner(session_id, int(user.id))

    # v5.2: try/finally wraps from the FIRST status mutation onward so any
    # exception (CorruptAudioError from wav_header_duration_ms, HTTPException
    # from cap re-check, generic sqlite/Mercury error) leaves the session in
    # "aborted" rather than stuck in "finalizing" forever.
    #
    # Concurrency note: two concurrent /finalize calls race on the
    # active → finalizing transition. Only the call that actually performed
    # the mutation is allowed to flip status to "aborted" on exception;
    # otherwise the second (losing) call's finally would clobber the
    # first (winning) call's eventual "finalized" → "aborted". Track this
    # with a local we_set_finalizing flag.
    we_set_finalizing = False
    handler_start = time.perf_counter()
    try:
        # Snapshot under the lock; release before the (potentially long)
        # gather() inside store.finalize.
        async with session.lock:
            if session.status != "active":
                raise HTTPException(410, f"Session is {session.status}")
            session.status = "finalizing"
            we_set_finalizing = True
            session.last_seen = time.time()
            pending_snapshot = dict(session.pending_tasks)
            tail_index = (
                (max(pending_snapshot.keys()) + 1) if pending_snapshot else 0
            )

        ct = (file.content_type or "").lower()
        if not ct.startswith("audio/"):
            raise HTTPException(
                415, f"Unsupported media type '{ct}'; expected audio/*"
            )
        tail_bytes = await file.read(settings.max_upload_bytes + 1)
        if len(tail_bytes) > settings.max_upload_bytes:
            raise HTTPException(413, "Tail exceeds max_upload_bytes")
        try:
            tail_duration_ms = wav_header_duration_ms(tail_bytes)
        except CorruptAudioError as exc:
            raise HTTPException(422, f"Corrupt tail audio: {exc}") from exc

        async with session.lock:
            if (
                session.cumulative_audio_ms + tail_duration_ms
                > settings.dictation_max_duration_s * 1000
            ):
                raise HTTPException(
                    413,
                    f"Joined audio exceeds {settings.dictation_max_duration_s} s cap",
                )
            session.cumulative_audio_ms += tail_duration_ms

        try:
            joined, total_inference_ms, chunk_count = await store.finalize(
                session,
                tail_bytes,
                tail_index,
                request.app.state.parakeet_service,
                store.finalize_timeout_s,
                pending_snapshot=pending_snapshot,
            )
        except FinalizeTimeout as exc:
            observability.streaming_sessions_aborted_total.increment("timeout")
            raise HTTPException(504, "Finalize timed out") from exc
        except FinalizeFailed as exc:
            observability.streaming_sessions_aborted_total.increment(
                "inference_failed"
            )
            raise HTTPException(502, f"Inference failed: {exc}") from exc
        except FinalizeGap as exc:
            observability.streaming_sessions_aborted_total.increment("gap")
            raise HTTPException(409, "Partial transcripts have gaps") from exc

        text = joined
        applied_smart_format = False
        sf_ms = 0.0
        mercury = getattr(request.app.state, "mercury_client", None)
        if smart_format and mercury is not None:
            sf_start = time.perf_counter()
            cleaned = await mercury.clean_up(joined)
            sf_ms = (time.perf_counter() - sf_start) * 1_000.0
            if cleaned is not None:
                text = cleaned
                applied_smart_format = True

        job_store: JobStore = request.app.state.job_store
        client_version = request.headers.get("X-WisprAlt-Client-Version")
        await asyncio.to_thread(
            job_store.insert_streaming_dictation_idempotent,
            api_key_id=int(user.id),
            text=text,
            duration_ms=int(total_inference_ms),
            client_app_version=client_version,
            smart_format_applied=applied_smart_format,
            client_dedup_id=client_dedup_id,
            dictated_at=speech_started_at,
        )

        async with session.lock:
            session.status = "finalized"

        observability.streaming_sessions_finalized_total.increment()

        total_handler_ms = (time.perf_counter() - handler_start) * 1_000.0
        logger.info(
            "stream finalize: session=%s chunks=%d total_inference_ms=%.1f "
            "mercury_ms=%.1f total_handler_ms=%.1f chars=%d sf=%s dedup=%s",
            session_id,
            chunk_count,
            total_inference_ms,
            sf_ms,
            total_handler_ms,
            len(text),
            applied_smart_format,
            client_dedup_id,
        )

        return JSONResponse(
            {
                "text": text,
                "model_id": MODEL_ID,
                "duration_ms": round(total_inference_ms, 2),
                "smart_formatted": applied_smart_format,
            }
        )
    finally:
        # Any non-finalized exit (HTTPException, sqlite error, Mercury error)
        # must reset status to "aborted" so the session doesn't stick in
        # "finalizing" forever. The lock is short-held — no awaitable work.
        #
        # Guarded by we_set_finalizing: only the call that actually
        # transitioned active → finalizing is allowed to abort the session.
        # A second concurrent /finalize that lost the race (got 410) must
        # NOT touch status here — otherwise it would clobber the winning
        # call's "finalizing" → "finalized" transition.
        if we_set_finalizing:
            async with session.lock:
                if session.status not in ("finalized", "aborted"):
                    session.status = "aborted"
                    observability.streaming_sessions_aborted_total.increment(
                        "finalize_error"
                    )
