"""
routes/transcribe_file.py — Container-agnostic transcription endpoint.

POST /transcribe/file accepts any audio/video container (m4a, mp3, mp4, mov,
wav, aac, flac, opus, ogg, webm, m4v, caf, aiff). The route streams the upload
to staging in its original container, then hands it to the meeting runner's
file-source path (`submit_source_or_429`) which runs ffprobe + ffmpeg + the
existing pipeline in the background.

Mode is INFERRED via ffprobe on the staged source (not declared by the
client) — channel_count == 1 → single-channel pipeline, otherwise stereo. The
lifecycle endpoints (poll / download / delete) remain on /transcribe/meeting/{id}/*
so both submission paths share the same job-status surface.

Auth: ``Bearer`` via ``Depends(require_api_key)``.
Usage telemetry: route-key ``transcribe/file`` is registered in
``main.TRACKED_ROUTES`` + ``_KIND_MAP``; the middleware emits the usage_events
row, the route does not call any recorder.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from .._errors import MeetingInProgressError
from ..auth import require_api_key
from ..config import settings
from ..jobs.runner import MeetingRunner
from ..ops import staging

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/transcribe/file",
    dependencies=[Depends(require_api_key)],
)


def _runner(request: Request) -> MeetingRunner:
    return request.app.state.meeting_runner  # type: ignore[no-any-return]


@router.post("", summary="Submit any audio/video file for transcription")
async def submit_file(
    request: Request,
    file: UploadFile,
    content_length: int | None = Header(None, alias="Content-Length"),
) -> JSONResponse:
    """Accept any supported container and enqueue a transcription job.

    Returns
    -------
    202 ``{"job_id": str, "status": "pending"}``
    413  Upload exceeds MAX_UPLOAD_BYTES.
    415  Unsupported source extension.
    422  ffprobe / ffmpeg rejected the source (no audio stream, decode failed).
    429  A meeting/file job is already in progress or RAM is insufficient.
    507  Insufficient storage.
    """
    if content_length is not None and content_length > settings.max_upload_bytes:
        raise HTTPException(413, "Upload too large")

    src_path = await staging.stream_to_staging_raw(
        file,
        settings.max_upload_bytes,
        settings.staging_dir,
    )

    runner = _runner(request)
    try:
        # Worker (_run_source) runs ffprobe + ffmpeg + pipeline. Route returns
        # 202 immediately so Cloudflare Tunnel doesn't see a long-running
        # request.
        jid = await runner.submit_source_or_429(src_path)
    except MeetingInProgressError as exc:
        src_path.unlink(missing_ok=True)
        return JSONResponse(
            {"error": str(exc), "retry_after_s": 60},
            status_code=429,
            headers={"Retry-After": "60"},
        )

    logger.info("File job %s submitted (source=%s).", jid, src_path.name)
    return JSONResponse({"job_id": jid, "status": "pending"}, status_code=202)
