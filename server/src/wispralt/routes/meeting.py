"""
routes/meeting.py — File-job poll/download/delete endpoints (legacy URL prefix).

GET    /transcribe/meeting/{job_id}     Poll job status.
GET    /transcribe/meeting/{job_id}/download/{fmt}  Stream a completed output file.
DELETE /transcribe/meeting/{job_id}     Clean up job + output files.

These poll/download/delete routes serve /transcribe/file jobs (the client
still polls these URLs); they move onto the /transcribe/file router in the
next commit. The meeting submit route is gone.

All endpoints require Bearer authentication (via ``Depends(require_api_key)``).

No speaker-rename endpoint — renaming happens entirely client-side (atomic local
file rewrite; see plan locked decisions).
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import require_api_key
from ..jobs.runner import PHASE_LABELS

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
    if job.request_mode is not None:
        body["request_mode"] = job.request_mode
    if job.error is not None:
        body["error"] = job.error

    # Phase 3: progress block + serverFinishing flag for client UI.
    if job.status == "running":
        phase = job.phase
        progress: dict[str, object] = {
            "phase": phase,
            "phase_label": PHASE_LABELS.get(phase) if phase else None,
            "chunk_index": job.chunk_index,
            "total_chunks": job.total_chunks,
        }
        if job.phase_started_at is not None:
            progress["phase_elapsed_s"] = round(
                time.time() - float(job.phase_started_at), 2
            )
        if job.audio_duration_s is not None:
            progress["audio_duration_s"] = job.audio_duration_s
        body["progress"] = progress
        # serverFinishing: the user requested cancellation but the executor
        # is still running (advisory cancel for in-pipeline phases). The
        # client uses this to render the "Previous job finishing on server"
        # banner and to block new submissions client-side.
        if job.cancel_requested:
            body["serverFinishing"] = True

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
