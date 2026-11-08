"""
routes/admin.py — Administrative endpoints for WisprAlt server.

GET /metrics  (bearer auth)
    Returns structured observability data for parakeet, the file-job
    pipeline (``file``), memory, and disk.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

import psutil
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .. import observability
from ..auth import require_admin
from ..config import settings
from ..files import pipeline as files_pipeline
from ..jobs.runner import PHASE_LABELS

logger = logging.getLogger(__name__)

router = APIRouter()


def _mlx_memory_mb() -> dict[str, int]:
    """Read MLX/Metal unified-memory counters and return them in MB.

    `mlx.core.metal.get_active_memory()` is the bytes currently held by live
    MLX tensors; `get_cache_memory()` is the bytes MLX is keeping around for
    fast re-allocation. The sum is what Activity Monitor sees on top of plain
    python RSS. Returns zeros if MLX isn't importable or the API has moved
    (older / unreleased MLX builds) — observability must never crash /metrics.
    """
    try:
        import mlx.core as _mx
        active = int(_mx.metal.get_active_memory()) // (1024 * 1024)
        cache = int(_mx.metal.get_cache_memory()) // (1024 * 1024)
        return {"mlx_active_mb": active, "mlx_cache_mb": cache}
    except Exception:
        return {"mlx_active_mb": 0, "mlx_cache_mb": 0}

@router.get(
    "/metrics",
    dependencies=[Depends(require_admin)],
    summary="Structured server observability metrics",
)
async def metrics(request: Request) -> JSONResponse:
    """Return observability snapshot.

    The ``file.*`` block reports the file-job runner and the files pipeline.

    Fields
    ------
    parakeet.p50_ms / p95_ms
        Percentiles of recent Parakeet inference durations (last 100 calls).
    parakeet.queue_depth
        Number of requests currently waiting for the single-thread executor
        (always 0 or 1 in steady state with one worker).
    parakeet.last_inference_at
        ISO-8601 timestamp of the most recent inference, or null.
    file.active
        True if a file job is queued or running.
    file.completed_24h / failed_24h
        File-job counts from the last 24 hours (JobStore).
    file.current_eta_s
        Estimated seconds until the active job completes, or null.
    memory.rss_mb
        Server process RSS in MiB.
    memory.available_mb
        System available RAM in MiB.
    disk.free_gb
        Free disk space on the staging volume in GiB.
    disk.staging_count
        Number of staging WAV files currently on disk.
    """
    parakeet_service = request.app.state.parakeet_service
    mem = psutil.virtual_memory()
    proc = psutil.Process(os.getpid())
    proc_mem = proc.memory_info()

    # Disk free — always from shutil (works even if staging_dir does not exist yet)
    staging_dir: Path = settings.staging_dir
    try:
        disk_usage = shutil.disk_usage(staging_dir if staging_dir.exists() else Path.home())
        free_gb = disk_usage.free // (1024 ** 3)
    except OSError:
        free_gb = 0

    # Read directly from FileJobRunner and JobStore where available.
    file_runner = getattr(request.app.state, "file_runner", None)
    job_store = getattr(request.app.state, "job_store", None)

    file_job_active: bool = file_runner.active if file_runner is not None else False
    active_job_id: str | None = file_runner.active_job_id if file_runner is not None else None

    completed_24h: int = job_store.count_24h("done") if job_store is not None else 0
    failed_24h: int = job_store.count_24h("failed") if job_store is not None else 0

    # ETA not yet calculated — reserved for a future delta.
    current_eta_s: int | None = None
    last_inference_at: str | None = getattr(request.app.state, "parakeet_last_inference_at", None)

    _files_warm, _files_loading = files_pipeline.state()

    # Staging count from filesystem
    staging_count_live: int
    try:
        staging_count_live = len(list(settings.staging_dir.glob("*.wav"))) if settings.staging_dir.exists() else 0
    except OSError:
        staging_count_live = 0

    # Observability data from module-level singletons (G4).
    all_routes = observability.latency_histogram.all_routes()
    latencies_by_route = {route: observability.latency_histogram.percentiles(route) for route in sorted(all_routes)}

    return JSONResponse(
        content={
            "parakeet": {
                "p50_ms": round(parakeet_service.p50_ms(), 1),
                "p95_ms": round(parakeet_service.p95_ms(), 1),
                "queue_depth": 0,  # Phase 2: wire from executor queue size
                "last_inference_at": last_inference_at,
            },
            "file": {
                "active": file_job_active,
                "active_job_id": active_job_id,
                "completed_24h": completed_24h,
                "failed_24h": failed_24h,
                "current_eta_s": current_eta_s,
                "models_warm": _files_warm,
                "models_loading": _files_loading,
                "idle_seconds": round(files_pipeline.idle_seconds(), 1),
                "idle_eviction_threshold_s": settings.file_model_idle_eviction_seconds,
                "eviction_failures_total": observability.eviction_failures_total.value,
            },
            "memory": {
                "rss_mb": proc_mem.rss // (1024 * 1024),
                "available_mb": mem.available // (1024 * 1024),
                # MLX unified-memory accounting. RSS does NOT include MLX/Metal
                # allocations on Apple Silicon (unified memory is tracked
                # separately by Metal), so a python process can show 1 GB RSS
                # while Activity Monitor's "Memory" column reports 11 GB. These
                # two fields close that gap so /metrics matches what the OS
                # reports and we can detect MLX pool growth in production.
                # Both are 0 if MLX/Metal isn't initialized yet or the API is
                # missing on this build.
                **_mlx_memory_mb(),
            },
            "disk": {
                "free_gb": free_gb,
                "staging_count": staging_count_live,
            },
            "requests_total": observability.request_counter.as_dict(),
            "errors_total": observability.error_counter.as_dict(),
            "latencies": latencies_by_route,
            # Streaming-dictation observability (additive endpoints under
            # /transcribe/dictate/stream/*). ``opened`` and ``finalized`` are
            # scalar monotonic counters; ``aborted`` is keyed by reason so
            # operators can attribute aborts (timeout | inference_failed |
            # gap | chunk_failed | ttl_expired | lifespan_shutdown).
            "streaming": {
                "sessions_opened_total": (
                    observability.streaming_sessions_opened_total.value
                ),
                "sessions_finalized_total": (
                    observability.streaming_sessions_finalized_total.value
                ),
                "sessions_aborted_total": (
                    observability.streaming_sessions_aborted_total.as_dict()
                ),
            },
            "usage_write_failures_total": observability.usage_write_failures_total.value,
            "dictation_save_failures_total": (
                observability.dictation_save_failures_total.value
            ),
            "process_uptime_seconds": round(
                time.monotonic() - observability.process_started_at_monotonic, 1
            ),
        }
    )


# M4: _find_env_path removed — use ops.env_writer.find_env_path() instead.

# ── helpers ───────────────────────────────────────────────────────────────────


@router.get(
    "/admin/active",
    dependencies=[Depends(require_admin)],
    summary="List active (pending or running) jobs with rich phase projection",
)
async def admin_active(request: Request) -> JSONResponse:
    """Return all non-terminal jobs with phase + progress detail.

    Helps the operator (and the client UI) diagnose hangs: see which phase
    a job is stuck in, how long it has been stuck, what fraction of chunks
    have been processed, and whether the user has requested cancellation.
    """
    store = getattr(request.app.state, "job_store", None)
    if store is None:
        return JSONResponse({"jobs": []})

    now = time.time()
    proc = psutil.Process(os.getpid())
    current_rss_mb = proc.memory_info().rss // (1024 * 1024)

    jobs_payload: list[dict[str, object]] = []
    for job in store.list_active_jobs():
        phase = job.phase
        phase_label = PHASE_LABELS.get(phase) if phase else None
        phase_elapsed_s: float | None = None
        if job.phase_started_at is not None:
            phase_elapsed_s = round(now - float(job.phase_started_at), 2)
        jobs_payload.append({
            "id": job.id,
            "status": job.status,
            "mode": job.mode,
            "phase": phase,
            "phase_label": phase_label,
            "phase_elapsed_s": phase_elapsed_s,
            "chunk_index": job.chunk_index,
            "total_chunks": job.total_chunks,
            "started_at": job.started_at,
            "wav_path": str(job.wav_path),
            "audio_duration_s": job.audio_duration_s,
            "attempts": job.attempts,
            "cancel_requested": bool(job.cancel_requested),
            "current_rss_mb": current_rss_mb,
        })

    return JSONResponse({"jobs": jobs_payload})


@router.get(
    "/admin/server-log/{job_id}",
    dependencies=[Depends(require_admin)],
    summary="Return the slice of server.log bracketing a job_id's appearances",
)
async def admin_server_log(request: Request, job_id: str) -> PlainTextResponse:
    """Return up to ~100 log lines bracketing the job_id's first and last
    appearance in ``server.log``.

    Includes inter-id lines (ffmpeg stderr, library warnings, etc.) that
    fall in that range so the operator sees the full neighborhood. Plain
    text response — the client renders it as monospace in the View Server
    Log sheet.
    """
    # Rotating application log (see ops/logging_setup.py), NOT launchd's
    # server.log, which only holds crash output.
    log_path = Path(settings.server_log_path)
    if not log_path.exists():
        raise HTTPException(404, f"Server log not found at {log_path}")
    # Rotated backups, oldest first, so a job that straddles a rotation is
    # still found. Reading is newest-first-capped below.
    backups = [
        b for b in (
            log_path.with_name(f"{log_path.name}.{n}")
            for n in range(settings.server_log_backup_count, 0, -1)
        )
        if b.exists()
    ]

    # Read up to a reasonable cap so a multi-GB log doesn't OOM the server.
    # 50 MiB is enough to reach back hours of output at info-level.
    _MAX_BYTES = 50 * 1024 * 1024
    try:
        chunks: list[bytes] = []
        for f in [*backups, log_path]:
            chunks.append(f.read_bytes())
        data = b"".join(chunks)
        if len(data) > _MAX_BYTES:
            data = data[-_MAX_BYTES:]
            # Drop partial first line so we start at a line boundary.
            data = data.split(b"\n", 1)[-1]
    except OSError as exc:
        raise HTTPException(500, f"Could not read server log: {exc}") from exc

    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()

    # Find indices where the job_id appears.
    indices = [i for i, line in enumerate(lines) if job_id in line]
    if not indices:
        return PlainTextResponse(
            f"(no log lines found for job_id={job_id} in {log_path})",
            media_type="text/plain",
        )

    first, last = indices[0], indices[-1]
    # 100-line window: 50 before first appearance, 50 after last.
    start = max(0, first - 50)
    end = min(len(lines), last + 50 + 1)
    selected = lines[start:end]
    return PlainTextResponse("\n".join(selected) + "\n", media_type="text/plain")
