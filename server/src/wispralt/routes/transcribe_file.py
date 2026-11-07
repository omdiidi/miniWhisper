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

import asyncio
import json
import logging
import re
import secrets
import shutil
import time
from pathlib import Path

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
from pydantic import BaseModel, Field

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

# ── Chunked upload constants ──────────────────────────────────────────────────
#
# 50 MiB chunks leave ~50 MB of margin under Cloudflare's 100 MB free/pro/biz
# request-body limit (the limit applies to the chunk body, not the assembled
# total). 22 chars is the exact length of `secrets.token_urlsafe(16)` output.
# Max chunks 1000 caps a single upload at 50 GB worst case — well above the
# 4 GB ceiling we apply in `/init` below.
_CHUNK_SIZE = 50 * 1024 * 1024
_CHUNK_SLACK = 1024  # accept up to CHUNK_SIZE+slack before 413 (R-H)
_MAX_CHUNKS = 1000
_MAX_TOTAL_BYTES = 4_000_000_000  # R-J: Cloudflare 100s proxy timeout ceiling
_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")  # exact `token_urlsafe(16)` shape


class ChunkedInitRequest(BaseModel):
    """Body for ``POST /transcribe/file/chunked/init``."""

    mode: ProcessingMode = Field(default=ProcessingMode.FILE)
    total_bytes: int = Field(..., gt=0)
    chunk_count: int = Field(..., gt=0, le=_MAX_CHUNKS)
    original_filename: str = Field(..., min_length=1, max_length=512)


class ChunkedInitResponse(BaseModel):
    upload_id: str
    chunk_size: int


def _sanitize_upload_id(upload_id: str) -> str:
    """Validate *upload_id* matches the `secrets.token_urlsafe(16)` shape.

    Raises HTTPException(400) on mismatch. Prevents path traversal because we
    build the chunked-dir path by concatenation below.
    """
    if not _UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(400, "Invalid upload_id")
    return upload_id


def _chunked_root() -> Path:
    return settings.staging_dir / "chunked"


def _load_meta_or_404(chunked_dir: Path) -> dict:
    meta_path = chunked_dir / "meta.json"
    if not meta_path.exists():
        raise HTTPException(404, "upload not found")
    try:
        return json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Corrupt meta.json at %s: %s", meta_path, exc)
        raise HTTPException(500, "Upload metadata corrupt") from exc


def _require_owner(request: Request, meta: dict) -> None:
    """R-B: caller's authenticated user.id must match the one recorded at init.

    Raises HTTPException(403) on mismatch. Anonymous / break-glass requests
    (user.id < 0) cannot own uploads — they would not have hit init either.
    """
    user = getattr(request.state, "user", None)
    if user is None or not hasattr(user, "id"):
        raise HTTPException(401, "Unauthenticated")
    if int(meta.get("api_key_id", -999)) != int(user.id):
        raise HTTPException(403, "Upload owned by a different API key")


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
    # Phase 1 transcript-storage: capture client version + owning api_key_id
    # at submit time so they survive worker crashes and restart re-enqueue.
    client_version = request.headers.get("X-WisprAlt-Client-Version")
    user = getattr(request.state, "user", None)
    api_key_id = (
        int(user.id)
        if user is not None and hasattr(user, "id") and int(user.id) >= 0
        else None
    )
    try:
        # Worker (_run_source) runs ffprobe + ffmpeg + pipeline. Route returns
        # 202 immediately so Cloudflare Tunnel doesn't see a long-running
        # request.
        jid = await runner.submit_source_or_429(
            src_path,
            request_mode=mode,
            client_version=client_version,
            api_key_id=api_key_id,
        )
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


# ── Chunked upload routes ─────────────────────────────────────────────────────
#
# Three-step flow used by the Swift client for files >50 MB so Cloudflare's
# 100 MB request-body limit on free/pro/biz plans never sees the full payload:
#
#   1. POST /transcribe/file/chunked/init       → returns {upload_id, chunk_size}
#   2. POST /transcribe/file/chunked/{id}/{i}   → raw chunk bytes
#   3. POST /transcribe/file/chunked/{id}/finalize → returns {job_id}
#
# After finalize, the assembled file is fed through the SAME `submit_source_or_429`
# code path the single-shot route uses — diarization, transcription, and the
# job-poll API are identical from there on. Stale chunked directories older
# than 1 h are reaped by `staging.sweep_chunked` at startup (see main.py).


@router.post(
    "/chunked/init",
    summary="Initialize a chunked upload (returns upload_id)",
)
async def init_chunked(
    req: ChunkedInitRequest,
    request: Request,
) -> JSONResponse:
    """Reserve a staging directory + persist metadata for a chunked upload.

    Returns 200 with ``{upload_id, chunk_size}``.

    Errors
    ------
    413  total_bytes > max_upload_bytes OR > 4 GB Cloudflare ceiling.
    415  Unsupported source extension.
    422  chunk_count invalid (validated by Pydantic).
    503  RAM gate (<4 GiB available).
    507  Insufficient disk space (need ≥ 2× total_bytes free).
    """
    # R-F: validate filename suffix against the same set the single-shot route
    # accepts so users get the same 415 they would get from the regular path.
    ext = Path(req.original_filename).suffix.lower()
    if ext not in staging._ALLOWED_EXTENSIONS:
        raise HTTPException(415, f"Unsupported file extension: {ext}")

    if req.total_bytes > settings.max_upload_bytes:
        raise HTTPException(413, "Upload exceeds max_upload_bytes")
    # R-J: stay under Cloudflare's 100s proxy ceiling for the finalize concat.
    # On Mac mini M4, 4 GB copies in ~8s — well within budget; 8 GB would risk
    # timing out the finalize HTTP response.
    if req.total_bytes > _MAX_TOTAL_BYTES:
        raise HTTPException(
            413,
            f"Upload exceeds chunked-upload ceiling ({_MAX_TOTAL_BYTES} bytes)",
        )

    # Ensure base staging dirs exist before disk_usage.
    settings.staging_dir.mkdir(parents=True, exist_ok=True)
    chunked_root = _chunked_root()
    chunked_root.mkdir(parents=True, exist_ok=True)

    # Disk gate: need 2× the total so chunks AND the assembled file fit during
    # the brief window before chunks are deleted post-concat. Mirrors the
    # single-shot route's gate.
    try:
        free = shutil.disk_usage(str(settings.staging_dir)).free
    except OSError:
        free = 0
    if free < req.total_bytes * 2:
        return JSONResponse(
            {
                "error": "Insufficient disk space for chunked upload",
                "retry_after_s": 300,
            },
            status_code=507,
            headers={"Retry-After": "300"},
        )

    # RAM gate at init too — finalize re-checks (R-I) but reject early so the
    # client doesn't push 50 chunks before learning the server is OOM.
    if psutil.virtual_memory().available < _4GiB:
        return JSONResponse(
            {"error": "Server low on memory", "retry_after_s": 60},
            status_code=503,
            headers={"Retry-After": "60"},
        )

    user = getattr(request.state, "user", None)
    if user is None or not hasattr(user, "id") or int(user.id) < 0:
        # Break-glass admin (id=-1) cannot own uploads because we cannot
        # later verify ownership across requests. Force them through the
        # single-shot route.
        raise HTTPException(403, "Chunked upload requires a per-user API key")

    upload_id = secrets.token_urlsafe(16)
    chunked_dir = chunked_root / upload_id
    try:
        chunked_dir.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        # Astronomically unlikely token collision — surface as 500 rather
        # than silently overwriting another upload's state.
        raise HTTPException(500, "upload_id collision; retry")

    meta = {
        "mode": req.mode.value,
        "total_bytes": req.total_bytes,
        "chunk_count": req.chunk_count,
        "original_filename": req.original_filename,
        "ext": ext,
        "created_at": time.time(),
        "api_key_id": int(user.id),  # R-B: persist owner for chunk/finalize
        # Phase 1 transcript-storage: capture client version at init so the
        # value reflects the ORIGINATING client even if the user upgrades
        # mid-multi-hour-upload. Reads as None on in-flight uploads from
        # before this code shipped — row gets NULL client_app_version, no
        # migration needed.
        "client_app_version": request.headers.get("X-WisprAlt-Client-Version"),
    }
    (chunked_dir / "meta.json").write_text(json.dumps(meta))

    logger.info(
        "Chunked upload %s initialized (bytes=%d, chunks=%d, ext=%s).",
        upload_id, req.total_bytes, req.chunk_count, ext,
    )
    return JSONResponse(
        ChunkedInitResponse(upload_id=upload_id, chunk_size=_CHUNK_SIZE).model_dump()
    )


@router.post(
    # Starlette ``:int`` path converter constrains ``chunk_index`` to a digit
    # run. Without this, ``/chunked/{id}/finalize`` would route here first
    # (Starlette matches in registration order) and Pydantic would fail to
    # coerce ``"finalize"`` → int, returning RequestValidationError → 422
    # before the finalize handler ever runs. The 422 reaches the client as
    # ``{"detail": [...]}`` (a list, not a string), which the Swift error
    # mapper interprets as a truncated upload. Surface bug: every chunked
    # upload finalize 422'd. Don't drop ``:int`` without re-ordering routes.
    "/chunked/{upload_id}/{chunk_index:int}",
    summary="Upload a single chunk (raw bytes body)",
)
async def upload_chunk(
    upload_id: str,
    chunk_index: int,
    request: Request,
    content_length: int | None = Header(None, alias="Content-Length"),
) -> JSONResponse:
    """Stream a single chunk's bytes to disk.

    Body MUST be ``application/octet-stream`` (raw bytes, NOT multipart). A
    Content-Length header is required so we can validate against bytes-written.

    Errors
    ------
    400  bytes_written != Content-Length (corrupt chunk).
    403  Caller's API key does not match the one that ran /init.
    404  upload_id not found.
    411  Missing Content-Length header.
    413  Chunk exceeds CHUNK_SIZE + 1 KiB slack.
    422  chunk_index out of range for this upload.
    """
    upload_id = _sanitize_upload_id(upload_id)
    chunked_dir = _chunked_root() / upload_id
    if not chunked_dir.exists():
        raise HTTPException(404, "upload not found")
    meta = _load_meta_or_404(chunked_dir)
    _require_owner(request, meta)

    if chunk_index < 0 or chunk_index >= int(meta["chunk_count"]):
        raise HTTPException(422, "chunk_index out of range")

    if content_length is None:
        raise HTTPException(411, "Content-Length header required for chunk")
    if content_length < 0 or content_length > _CHUNK_SIZE + _CHUNK_SLACK:
        # R-H: hard ceiling on declared size BEFORE we start streaming so the
        # client gets a quick 413 instead of pushing megabytes that will be
        # rejected.
        raise HTTPException(413, "Chunk too large")

    target = chunked_dir / f"chunk-{chunk_index:04d}.part"
    target_tmp = target.with_suffix(".part.tmp")
    bytes_written = 0
    try:
        # R-A: sync open() inside an async route — same pattern as
        # `staging.stream_to_staging_raw`. Repo deliberately has no async-file
        # dependency; the OS buffer cache absorbs the cost at our scale.
        with open(target_tmp, "wb") as fh:
            async for blob in request.stream():
                if not blob:
                    continue
                bytes_written += len(blob)
                if bytes_written > _CHUNK_SIZE + _CHUNK_SLACK:
                    # R-H: abort mid-stream if the body claims more than
                    # Content-Length allowed (chunked transfer or buggy client).
                    raise HTTPException(413, "Chunk body exceeds size limit")
                fh.write(blob)
    except HTTPException:
        target_tmp.unlink(missing_ok=True)
        raise
    except Exception:
        target_tmp.unlink(missing_ok=True)
        raise

    if bytes_written != content_length:
        # R-G: declared vs observed mismatch — reject so we never assemble a
        # corrupt blob from a truncated chunk.
        target_tmp.unlink(missing_ok=True)
        raise HTTPException(400, "Chunk size mismatch with Content-Length")

    # Atomic publish + bump meta.json mtime so the sweep TTL resets while the
    # upload is actively progressing (R-M signal).
    target_tmp.rename(target)
    try:
        (chunked_dir / "meta.json").touch()
    except OSError:
        pass

    logger.info(
        "chunked upload %s: chunk %d/%d received (%d bytes)",
        upload_id,
        chunk_index + 1,
        int(meta["chunk_count"]),
        bytes_written,
    )
    return JSONResponse({"ok": True, "received_bytes": bytes_written})


def _concat_chunks(parts: list[Path], out_path: Path) -> None:
    """Concatenate *parts* into *out_path*, deleting each part after copy.

    Runs in a thread executor (see finalize) to keep the event loop free
    during the multi-GB I/O. Each chunk is unlinked immediately after its
    contents are flushed to halve peak disk usage during the assemble window.
    """
    with open(out_path, "wb") as out:
        for p in parts:
            with open(p, "rb") as inp:
                shutil.copyfileobj(inp, out, length=8 * 1024 * 1024)
            try:
                p.unlink()
            except OSError:
                pass


@router.post(
    "/chunked/{upload_id}/finalize",
    summary="Concatenate chunks and submit the assembled file to the runner",
)
async def finalize_chunked(
    upload_id: str,
    request: Request,
) -> JSONResponse:
    """Verify all chunks are present, concatenate, then submit to the runner.

    Returns 202 with ``{"job_id": str, "status": "pending"}`` matching the
    single-shot route's response shape.

    Errors
    ------
    403  Ownership mismatch.
    404  upload_id not found.
    409  Missing chunks OR total-bytes mismatch.
    429  Meeting/file job already in progress (assembled file is cleaned up).
    503  RAM gate (<4 GiB available).
    507  Disk gate (free < total_bytes).
    """
    upload_id = _sanitize_upload_id(upload_id)
    chunked_dir = _chunked_root() / upload_id
    if not chunked_dir.exists():
        raise HTTPException(404, "upload not found")
    meta = _load_meta_or_404(chunked_dir)
    _require_owner(request, meta)

    total_bytes = int(meta["total_bytes"])
    chunk_count = int(meta["chunk_count"])
    ext: str = meta["ext"]

    # Re-check disk now (the concat is about to land another `total_bytes`
    # beside the chunks for a brief window — even with delete-as-we-go the
    # first chunk doubles up).
    try:
        free = shutil.disk_usage(str(settings.staging_dir)).free
    except OSError:
        free = 0
    if free < total_bytes:
        return JSONResponse(
            {"error": "Insufficient disk space to finalize", "retry_after_s": 300},
            status_code=507,
            headers={"Retry-After": "300"},
        )

    # R-I: RAM gate at finalize — submit_source_or_429 will load models.
    if psutil.virtual_memory().available < _4GiB:
        return JSONResponse(
            {"error": "Server low on memory", "retry_after_s": 60},
            status_code=503,
            headers={"Retry-After": "60"},
        )

    parts = sorted(chunked_dir.glob("chunk-*.part"))
    if len(parts) != chunk_count:
        raise HTTPException(
            409,
            f"Expected {chunk_count} chunks, got {len(parts)}",
        )
    # R-G: pre-flight total size check before any I/O work — catches
    # truncation cheaply.
    observed_total = 0
    for p in parts:
        try:
            observed_total += p.stat().st_size
        except OSError as exc:
            raise HTTPException(409, f"Chunk stat failed: {exc}") from exc
    if observed_total != total_bytes:
        raise HTTPException(
            409,
            f"Chunk total {observed_total} != declared {total_bytes}",
        )

    # Concat off the event loop (R-J: keep handler responsive within
    # Cloudflare's 100s ceiling).
    out_path = settings.staging_dir / f"{upload_id}{ext}"
    loop = asyncio.get_running_loop()
    # Snapshot per-part sizes BEFORE concat — `_concat_chunks` unlinks each
    # part as it copies, so stat()ing afterward would return ENOENT.
    part_sizes_pre = [p.stat().st_size for p in parts]
    concat_started = time.monotonic()
    try:
        await loop.run_in_executor(None, _concat_chunks, parts, out_path)
    except OSError as exc:
        out_path.unlink(missing_ok=True)
        shutil.rmtree(chunked_dir, ignore_errors=True)
        logger.error("Chunked finalize concat failed: %s", exc)
        raise HTTPException(500, "Concat failed") from exc
    concat_elapsed_ms = int((time.monotonic() - concat_started) * 1000)
    logger.info(
        "chunked upload %s: concat finished in %d ms (%d parts, sizes=%s)",
        upload_id,
        concat_elapsed_ms,
        len(part_sizes_pre),
        part_sizes_pre,
    )

    # Sanity: assembled size must match declaration.
    try:
        actual = out_path.stat().st_size
    except OSError:
        actual = 0
    if actual != total_bytes:
        out_path.unlink(missing_ok=True)
        shutil.rmtree(chunked_dir, ignore_errors=True)
        raise HTTPException(500, "Assembled size mismatch")

    # Submit to runner using the same path as the single-shot route. The
    # MeetingInProgressError branch mirrors transcribe_file.py:128-134 so a
    # 429 still cleans up the assembled file (R-C).
    runner = _runner(request)
    # Phase 1 transcript-storage: read client version that was captured at
    # /init (more robust than the live header — survives mid-upload client
    # upgrade). api_key_id is already ownership-verified above.
    try:
        mode_enum = ProcessingMode(meta["mode"])
    except (KeyError, ValueError) as exc:
        out_path.unlink(missing_ok=True)
        shutil.rmtree(chunked_dir, ignore_errors=True)
        raise HTTPException(422, "meta.json missing or has invalid mode") from exc

    client_version = meta.get("client_app_version")
    try:
        meta_api_key_id = int(meta.get("api_key_id", -999))
    except (TypeError, ValueError):
        meta_api_key_id = -999
    api_key_id = meta_api_key_id if meta_api_key_id >= 0 else None
    try:
        jid = await runner.submit_source_or_429(
            out_path,
            request_mode=mode_enum,
            client_version=client_version,
            api_key_id=api_key_id,
        )
    except MeetingInProgressError as exc:
        out_path.unlink(missing_ok=True)
        shutil.rmtree(chunked_dir, ignore_errors=True)
        return JSONResponse(
            {"error": str(exc), "retry_after_s": 60},
            status_code=429,
            headers={"Retry-After": "60"},
        )

    # Success: chunked dir is now empty (parts already unlinked) — just rmtree
    # to drop meta.json + the dir itself.
    shutil.rmtree(chunked_dir, ignore_errors=True)
    logger.info(
        "Chunked upload %s finalized → job %s (bytes=%d, mode=%s).",
        upload_id, jid, total_bytes, mode_enum.value,
    )
    return JSONResponse({"job_id": jid, "status": "pending"}, status_code=202)
