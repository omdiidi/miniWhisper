"""
routes/dictate_stream.py — Additive streaming-dictation endpoints.

Pseudocode source: ``tmp/ready-plans/2026-05-18-streaming-dictation-chunks.md`` §5.

Two POSTs under ``/transcribe/dictate/stream/{session_id}/...``:

* ``.../chunk/{index}`` — accepts a mid-recording WAV chunk, enqueues
  inference, returns 202 immediately.
* ``.../finalize`` — accepts the tail WAV, awaits all pending inference (with
  a hard timeout), joins partial transcripts, persists one idempotent dictation row, and
  returns the final transcript.

Auth: same ``require_bearer`` (Bearer only) dep as ``/transcribe/dictate``; session
ownership is enforced by the authenticated user's id.

Dedup (``dedup.py``, Locked Decision 7): both routes require
``X-Client-Dedup-Id``. A chunk for an already-stored id returns 202
``finalized: true`` without inference or a session. Finalize claims the id,
replays a stored dictation BEFORE the session lookup (so a Retry after the
session expired still gets its text), then saves synchronously; every 200
carries ``"saved"``. A session remembers the id it opened with; another id → 409.

Concurrency contract: every read or write of ``StreamingSession`` state must
hold ``session.lock``; this route obeys that rule and snapshots ``pending_tasks``
under the lock before awaiting ``store.finalize``.
"""

from __future__ import annotations

import logging
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

from .. import dedup, observability
from .._errors import CorruptAudioError
from ..accounts.store import User
from ..audio import wav_header_duration_ms
from ..auth import require_bearer
from ..config import settings
from ..dictate.parakeet import MODEL_ID
from ..dictate.streaming_session import (
    FinalizeFailed,
    FinalizeGap,
    FinalizeTimeout,
    StreamingSessionStore,
)
from ..jobs.store import JobStore

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/transcribe/dictate/stream/{session_id}/chunk/{index}",
    summary="Stream a mid-recording dictation chunk (additive endpoint)",
)
async def stream_chunk(
    session_id: str,
    index: int,
    request: Request,
    file: UploadFile,
    user: User = Depends(require_bearer),
) -> JSONResponse:
    if not dedup.is_uuid(session_id) or index < 0:
        raise HTTPException(422, "Invalid session_id or index")
    did = dedup.dedup_id(request, None, required=True)
    assert did is not None

    # Replay check BEFORE opening a session: a stored dictation needs no work.
    stored = await dedup.replay_or_none(request, request.app.state.job_store, did, user.id)
    if isinstance(stored, JSONResponse):
        return stored
    if stored is not None:
        return JSONResponse(
            {"received_index": index, "queue_depth": 0, "finalized": True}, status_code=202
        )

    store: StreamingSessionStore = request.app.state.streaming_sessions
    session, status_code = await store.open_or_get(session_id, int(user.id), did)
    if status_code == "denied_dedup_mismatch":
        raise HTTPException(409, "Dedup id does not match session")
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
        # v0.4.2: count IN-FLIGHT tasks only, not the dict's lifetime size.
        # pending_tasks accumulates a Task object per chunk index for the
        # entire session; older entries stay in the dict so finalize() can
        # await them. Counting len(...) makes the gate effectively "total
        # chunks ever sent" which leaks queue capacity on long sessions.
        in_flight = sum(1 for t in session.pending_tasks.values() if not t.done())
        if in_flight >= store.max_queue_depth:
            raise HTTPException(429, "Per-session queue depth exceeded")
        # Streaming-only mid-recording cap: leave a 30 s tail-headroom under the
        # configured dictation_max_duration_s so the final join still fits even
        # with a multi-second tail. v0.4.3: derives the cap from settings instead
        # of hardcoding 270_000, so raising dictation_max_duration_s flows through.
        _stream_chunk_cap_ms = (settings.dictation_max_duration_s - 30) * 1000
        if session.cumulative_audio_ms + chunk_duration_ms > _stream_chunk_cap_ms:
            raise HTTPException(413, f"Streaming audio exceeds {_stream_chunk_cap_ms // 1000} s cap")
        session.cumulative_audio_ms += chunk_duration_ms
        session.last_seen = time.time()

    await store.enqueue_inference(
        session, index, audio_bytes, request.app.state.parakeet_service
    )
    # queue_depth in response reflects post-enqueue in-flight count, same
    # accounting as the gate above (not lifetime dict size).
    post_in_flight = sum(1 for t in session.pending_tasks.values() if not t.done())
    return JSONResponse(
        {
            "received_index": index,
            "queue_depth": post_in_flight,
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
    speech_started_at: float = Form(...),
    client_dedup_id: str | None = Form(None),
    user: User = Depends(require_bearer),
) -> JSONResponse:
    if not dedup.is_uuid(session_id):
        raise HTTPException(422, "Invalid session_id")
    did = dedup.dedup_id(request, client_dedup_id, required=True)
    assert did is not None
    async with dedup.locks(request).claim(did):
        return await _finalize(request, session_id, did, file, speech_started_at, user)


async def _finalize(
    request: Request,
    session_id: str,
    did: str,
    file: UploadFile,
    speech_started_at: float,
    user: User,
) -> JSONResponse:
    """Replay → session → join → persist, under the caller's dedup claim."""
    job_store: JobStore = request.app.state.job_store
    stored = await dedup.replay_or_none(request, job_store, did, user.id)
    if isinstance(stored, JSONResponse):
        return stored
    if stored is not None:
        return _final_response(stored, 0.0, replayed=True, saved=True)

    store: StreamingSessionStore = request.app.state.streaming_sessions
    session = await store.get_for_owner(session_id, int(user.id))
    if session.dedup_id != did:
        raise HTTPException(409, "Dedup id does not match session")
    async with session.lock:
        blank_replay = session.blank_text if session.status == "finalized" else None
    if blank_replay is not None:
        # A retry of a finalize whose text was blank (never stored, so the
        # replay above found nothing): replay it like a stored dictation.
        request.state.dedup_replay = True
        return _final_response(blank_replay, 0.0, replayed=True, saved=True)

    # v5.2: try/finally wraps from the FIRST status mutation onward so any
    # exception (CorruptAudioError from wav_header_duration_ms, HTTPException
    # from cap re-check, generic sqlite error) leaves the session in
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

        # Blank text is not stored (same rule as /transcribe/dictate): there is
        # nothing to keep, so it reports saved=true. The session remembers it
        # (blank_text, set below) so a retried finalize replays it.
        if not joined.strip():
            text, ok = joined, True
        else:
            saved = await dedup.persist(
                request,
                job_store,
                user_id=int(user.id),
                text=joined,
                duration_ms=int(total_inference_ms),
                client_app_version=request.headers.get("X-WisprAlt-Client-Version"),
                dedup_id=did,
                created_at=speech_started_at,
            )
            if isinstance(saved, JSONResponse):
                return saved  # session stays "finalizing" → finally aborts it
            text, ok = saved

        async with session.lock:
            session.status = "finalized"
            if not joined.strip():
                session.blank_text = joined

        observability.streaming_sessions_finalized_total.increment()

        total_handler_ms = (time.perf_counter() - handler_start) * 1_000.0
        logger.info(
            "stream finalize: session=%s chunks=%d total_inference_ms=%.1f "
            "total_handler_ms=%.1f chars=%d dedup=%s",
            session_id,
            chunk_count,
            total_inference_ms,
            total_handler_ms,
            len(text),
            did,
        )
        replayed = bool(getattr(request.state, "dedup_replay", False))
        return _final_response(text, total_inference_ms, replayed=replayed, saved=ok)
    finally:
        # Any non-finalized exit (HTTPException, sqlite error)
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


def _final_response(text: str, inference_ms: float, *, replayed: bool, saved: bool) -> JSONResponse:
    return JSONResponse(
        {
            "text": text,
            "model_id": MODEL_ID,
            "duration_ms": round(inference_ms, 2),
            "replayed": replayed,
            "saved": saved,
        }
    )
