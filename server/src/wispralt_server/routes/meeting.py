"""
routes/meeting.py — Meeting transcription endpoints.

POST   /transcribe/meeting              Submit a WAV for background transcription.
GET    /transcribe/meeting/{job_id}     Poll job status.
GET    /transcribe/meeting/{job_id}/download/{fmt}  Stream a completed output file.
DELETE /transcribe/meeting/{job_id}     Clean up job + output files.

All endpoints require Bearer authentication (via ``Depends(require_api_key)``).

No speaker-rename endpoint — renaming happens entirely client-side (atomic local
file rewrite; see plan locked decisions).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import require_api_key
from ..config import settings
from .._errors import MeetingInProgressError
from ..jobs.runner import MeetingRunner
from ..ops import staging

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/transcribe/meeting",
    dependencies=[Depends(require_api_key)],
)

_VALID_FORMATS = frozenset({"json", "srt", "vtt", "txt"})
_MEDIA_TYPES: dict[str, str] = {
    "json": "application/json",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
    "txt": "text/plain",
}


def _runner(request: Request) -> MeetingRunner:
    return request.app.state.meeting_runner  # type: ignore[no-any-return]


# ── POST /transcribe/meeting ───────────────────────────────────────────────────


@router.post("", summary="Submit a meeting WAV for background transcription")
async def submit_meeting(
    request: Request,
    file: UploadFile,
    content_md5: str | None = Header(None, alias="Content-MD5"),
    content_length: int | None = Header(None, alias="Content-Length"),
) -> JSONResponse:
    """Accept a 2-channel WAV upload and enqueue it for transcription.

    Returns
    -------
    202 ``{"job_id": str, "status": "pending"}``
    413  Upload exceeds MAX_UPLOAD_BYTES.
    422  WAV header invalid or Content-MD5 mismatch.
    429  A meeting job is already in progress or RAM is insufficient.
    507  Insufficient storage.
    """
    # Fast pre-flight size check from Content-Length header
    if content_length is not None and content_length > settings.max_upload_bytes:
        raise HTTPException(413, "Upload too large")

    runner = _runner(request)

    wav_path, _ = await staging.stream_to_staging(
        file,
        settings.max_upload_bytes,
        settings.staging_dir,
        content_md5,
    )

    try:
        jid = await runner.submit_or_429(wav_path)
    except MeetingInProgressError as exc:
        staging.cleanup(wav_path)
        return JSONResponse(
            {"error": str(exc), "retry_after_s": 60},
            status_code=429,
            headers={"Retry-After": "60"},
        )

    logger.info("Meeting job %s submitted.", jid)
    return JSONResponse({"job_id": jid, "status": "pending"}, status_code=202)


# ── GET /transcribe/meeting/{job_id} ──────────────────────────────────────────


@router.get("/{job_id}", summary="Poll meeting job status")
def poll_meeting(request: Request, job_id: str) -> JSONResponse:
    """Return the current status of a meeting job.

    Returns
    -------
    200 ``{"job_id", "status", ["mode"], ["error"], ["formats"]}``
    404 Job not found.
    """
    job = request.app.state.job_store.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    body: dict[str, object] = {"job_id": job.id, "status": job.status}
    if job.mode is not None:
        body["mode"] = job.mode
    if job.error is not None:
        body["error"] = job.error
    if job.status == "done":
        # `formats` kept for backward compat; `outputs` is what the client expects.
        # Both fields carry the same set of available formats; `outputs` maps each
        # format name to its download URL fragment so the client can resolve it
        # without string-building knowledge of the URL structure.
        body["formats"] = sorted(_VALID_FORMATS)
        body["outputs"] = {
            fmt: f"/transcribe/meeting/{job.id}/download/{fmt}"
            for fmt in sorted(_VALID_FORMATS)
        }

    return JSONResponse(body)


# ── GET /transcribe/meeting/{job_id}/download/{fmt} ───────────────────────────


@router.get(
    "/{job_id}/download/{fmt}",
    summary="Stream a completed transcript output file",
)
def download_meeting(request: Request, job_id: str, fmt: str) -> StreamingResponse:
    """Stream one of the four output formats (json, srt, vtt, txt).

    Returns
    -------
    200  Streaming file response.
    400  Unknown format or invalid job_id.
    404  Job not found or not done.
    410  Output file missing from disk (e.g. manually deleted).
    """
    # G6: defence-in-depth — job_id must be a well-formed UUID (server-issued, so this
    # is effectively a no-op in practice, but prevents path-traversal surprises).
    if not re.fullmatch(r"[0-9a-f-]{36}", job_id):
        raise HTTPException(400, "Invalid job_id format")

    if fmt not in _VALID_FORMATS:
        raise HTTPException(400, f"Unknown format '{fmt}'; valid: {sorted(_VALID_FORMATS)}")

    job = request.app.state.job_store.get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(404, "Job not found or not yet complete")

    output_path = Path(job.output_dir) / f"{job_id}.{fmt}"  # type: ignore[arg-type]
    if not output_path.exists():
        raise HTTPException(410, "Output file missing; the job may have been cleaned up")

    media_type = _MEDIA_TYPES[fmt]

    def _gen():
        with open(output_path, "rb") as fh:
            while chunk := fh.read(1 << 20):
                yield chunk

    return StreamingResponse(_gen(), media_type=media_type)


# ── DELETE /transcribe/meeting/{job_id} ───────────────────────────────────────


@router.delete("/{job_id}", status_code=204, summary="Delete a meeting job and its output files")
def delete_meeting(request: Request, job_id: str) -> Response:
    """Remove the job record and all associated output files.

    Idempotent: returns 204 even if the job does not exist.
    """
    store = request.app.state.job_store
    job = store.get(job_id)

    if job is None:
        return Response(status_code=204)

    # Remove output files if present
    if job.output_dir is not None:
        for fmt in _VALID_FORMATS:
            try:
                (Path(job.output_dir) / f"{job_id}.{fmt}").unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not delete output file %s.%s: %s", job_id, fmt, exc)

    store.delete(job_id)
    logger.info("Meeting job %s deleted.", job_id)
    return Response(status_code=204)
