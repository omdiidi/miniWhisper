"""
routes/transcribe_file.py — Container-agnostic transcription endpoint.

POST /transcribe/file accepts any audio/video container (m4a, mp3, mp4, mov,
wav, aac, flac, opus, ogg, webm, m4v, caf, aiff). The route streams the upload
to staging in its original container, then hands it to the meeting runner's
file-source path (`submit_source_or_429`) which runs ffprobe + ffmpeg + the
existing pipeline in the background.

Mode is now an EXPLICIT submit-time form field: ``mode=file`` (default —
single-speaker) or ``mode=meeting`` (diarized). This replaces the previous
channel-count heuristic so a mono meeting recording is transcribed AND
diarized correctly. FastAPI auto-validates the enum value and returns a
structured 422 on invalid input.

Auth: ``Bearer`` via ``Depends(require_api_key)``.
Usage telemetry: route-key ``transcribe/file`` is registered in
``main.TRACKED_ROUTES`` + ``_KIND_MAP``; the middleware emits the usage_events
row, the route does not call any recorder.

Resource gates (Phase 2):
- Disk: free < content_length × 2 → 507 + Retry-After 300.
- RAM: psutil.virtual_memory().available < 4 GiB → 503 + Retry-After 60.
"""

from __future__ import annotations

import logging
import shutil

import psutil
from fastapi import (
    APIRouter,
    Depends,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse

from .._errors import MeetingInProgressError
from ..auth import require_api_key
from ..config import settings
from ..jobs.runner import MeetingRunner, ProcessingMode
from ..ops import staging

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/transcribe/file",
    dependencies=[Depends(require_api_key)],
)

_4GiB = 4 * 1024**3


def _runner(request: Request) -> MeetingRunner:
    return request.app.state.meeting_runner  # type: ignore[no-any-return]


@router.post("", summary="Submit any audio/video file for transcription")
async def submit_file(
    request: Request,
    file: UploadFile,
    mode: ProcessingMode = Form(ProcessingMode.FILE),
    content_length: int | None = Header(None, alias="Content-Length"),
) -> JSONResponse:
    """Accept any supported container and enqueue a transcription job.

    Returns
    -------
    202 ``{"job_id": str, "status": "pending"}``
    413  Upload exceeds MAX_UPLOAD_BYTES.
    415  Unsupported source extension.
    422  ffprobe / ffmpeg rejected the source (no audio stream, decode failed)
         OR ``mode`` was not a valid ``ProcessingMode`` value.
    429  A meeting/file job is already in progress or RAM is insufficient.
    503  Server low on memory (<4 GiB available) — try again in 60s.
    507  Insufficient storage on the staging filesystem.
    """
    if content_length is not None and content_length > settings.max_upload_bytes:
        raise HTTPException(413, "Upload too large")

    # Pre-flight: ensure the staging dir exists before disk_usage is called
    # on it (otherwise OSError on first-ever submission after install).
    settings.staging_dir.mkdir(parents=True, exist_ok=True)

    # Disk gate: require ≥ 2× the upload size free so we have room for both
    # the source AND the canonical-WAV transcode beside it.
    if content_length is not None:
        try:
            free = shutil.disk_usage(str(settings.staging_dir)).free
        except OSError:
            free = 0
        if free < content_length * 2:
            return JSONResponse(
                {
                    "error": "Insufficient disk space for upload + transcode",
                    "retry_after_s": 300,
                },
                status_code=507,
                headers={"Retry-After": "300"},
            )

    # RAM gate: bumped from the runner's 2 GiB inner check to 4 GiB at the
    # boundary. Reject early so the upload bytes are not even streamed.
    if psutil.virtual_memory().available < _4GiB:
        return JSONResponse(
            {"error": "Server low on memory", "retry_after_s": 60},
            status_code=503,
            headers={"Retry-After": "60"},
        )

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
        jid = await runner.submit_source_or_429(src_path, request_mode=mode)
    except MeetingInProgressError as exc:
        src_path.unlink(missing_ok=True)
        return JSONResponse(
            {"error": str(exc), "retry_after_s": 60},
            status_code=429,
            headers={"Retry-After": "60"},
        )

    logger.info(
        "File job %s submitted (source=%s, mode=%s).",
        jid, src_path.name, mode.value,
    )
    return JSONResponse({"job_id": jid, "status": "pending"}, status_code=202)
