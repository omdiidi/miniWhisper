"""
ops/monitoring.py — the ``/admin/system`` snapshot: one JSON-serialisable dict.

Every section is always present. A section that cannot be read (a DB error, psutil
refusing) degrades to ``None`` values instead of raising, so the page never 500s; the
``db`` section then reports ``ok: false``. Disk walks of ``outputs/`` and ``staging/``
run in a thread and are cached for 30 s; the ``SERVER_URL`` reachability probe (GET
``/healthz``, 2 s timeout) is cached for 60 s. ``on_startup`` bumps the persisted
``server_starts`` counter, records the start time and primes ``psutil.cpu_percent``
(its first call always returns 0.0).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import platform
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import psutil

from wispralt import observability
from wispralt.config import settings
from wispralt.database import _migration_files
from wispralt.files import pipeline as files_pipeline

if TYPE_CHECKING:
    from fastapi import FastAPI

    from wispralt.settings_store import SettingsStore

logger = logging.getLogger(__name__)

DISK_TTL_S = 30.0
NETWORK_TTL_S = 60.0
_cache: dict[str, tuple[float, Any]] = {}


def on_startup(app: FastAPI, settings_store: SettingsStore) -> None:
    app.state.started_at = time.time()
    try:
        app.state.server_starts = settings_store.incr("server_starts")
    except sqlite3.Error:
        logger.exception("server_starts counter not updated")
        app.state.server_starts = None
    psutil.cpu_percent(None)


@functools.cache
def git_sha() -> str | None:
    """Short SHA when the server runs from a git checkout, else None (computed once)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def _dir_bytes(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _file_bytes(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


async def _cached(key: str, ttl: float, compute) -> Any:  # noqa: ANN001
    hit = _cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    value = await compute()
    _cache[key] = (now, value)
    return value


async def probe_reachable(url: str) -> bool:
    """GET ``<SERVER_URL>/healthz`` with a 2 s budget. Tests replace this function."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(url.rstrip("/") + "/healthz")
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def _disk(data_dir: Path) -> dict[str, Any]:
    async def compute() -> dict[str, Any]:
        def walk() -> dict[str, Any]:
            try:
                usage = psutil.disk_usage(str(data_dir))
                free, total = usage.free, usage.total
            except OSError:
                free = total = None
            db = data_dir / "wispralt.db"
            return {
                "free": free,
                "total": total,
                "db_bytes": _file_bytes(db),
                "wal_bytes": _file_bytes(db.with_name(db.name + "-wal")),
                "outputs_bytes": _dir_bytes(data_dir / "outputs"),
                "staging_bytes": _dir_bytes(data_dir / "staging"),
            }

        return await asyncio.to_thread(walk)

    return await _cached(f"disk:{data_dir}", DISK_TTL_S, compute)


async def _network(url: str, probe: bool) -> dict[str, Any]:
    if not probe or not url:
        return {"server_url": url, "reachable": None}

    async def compute() -> bool:
        return await probe_reachable(url)

    return {"server_url": url, "reachable": await _cached(f"net:{url}", NETWORK_TTL_S, compute)}


def _db_section(app: FastAPI) -> dict[str, Any]:
    db = getattr(app.state, "db", None)
    if db is None:
        return {"ok": False, "reason": "db_error", "schema_version": None, "journal_mode": None}
    reason = db.check()
    try:
        with db.read() as con:
            version = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            journal = con.execute("PRAGMA journal_mode").fetchone()[0]
    except sqlite3.Error:
        version = journal = None
    return {
        "ok": reason is None,
        "reason": reason,
        "schema_version": version,
        "code_schema_version": max(_migration_files(), default=None),
        "journal_mode": journal,
    }


async def snapshot(app: FastAPI, *, now: float | None = None, probe: bool = True) -> dict[str, Any]:
    """Everything the system page shows. Never raises for a DB or OS failure."""
    now = time.time() if now is None else now
    state = app.state
    started_at = getattr(state, "started_at", None)

    try:
        load = list(os.getloadavg())
    except OSError:
        load = None
    vm = psutil.virtual_memory()

    async def guarded(fn, default):  # noqa: ANN001, ANN202
        try:
            return await asyncio.to_thread(fn)
        except sqlite3.Error as exc:
            logger.warning("system snapshot read failed: %s", type(exc).__name__)
            return default

    job_store = getattr(state, "job_store", None)
    usage = getattr(state, "usage", None)
    empty_queue = {"pending": None, "running": None, "failed_24h": None, "oldest_pending_age_s": None}
    queue = await guarded(lambda: job_store.queue_counts(now), empty_queue) if job_store else empty_queue
    errors_1h = (
        await guarded(lambda: usage.latency_error_summary(now - 3600, now), None) if usage else None
    )
    clients = (
        await guarded(lambda: usage.clients_last_seen(now), {"active": [], "recent": []})
        if usage else {"active": [], "recent": []}
    )
    parakeet = getattr(state, "parakeet_service", None)
    file_loaded, file_loading = files_pipeline.state()
    hist = observability.latency_histogram

    return {
        "generated_at": now,
        "host": {
            "name": platform.node(),
            "version": getattr(app, "version", None),
            "git_sha": git_sha(),
            "uptime_s": None if started_at is None else max(0.0, now - started_at),
            "starts": getattr(state, "server_starts", None),
        },
        "cpu": {"pct": psutil.cpu_percent(None), "load": load},
        "mem": {
            "used": vm.total - vm.available,
            "total": vm.total,
            "rss": psutil.Process().memory_info().rss,
        },
        "disk": await _disk(Path(settings.data_dir)),
        "models": {
            "dictation_ready": bool(parakeet is not None and getattr(parakeet, "ready", False)),
            "file_loaded": file_loaded,
            "file_loading": file_loading,
        },
        "queue": queue,
        "latency": {
            route: {"p50": p["p50"], "p95": p["p95"]}
            for route in sorted(hist.all_routes())
            for p in (hist.percentiles(route),)
        },
        "errors": {
            "rate_5xx_1h": None if errors_1h is None else errors_1h["rate_5xx"],
            "requests_1h": None if errors_1h is None else errors_1h["total"],
        },
        "counters": {
            "usage_write_failures": observability.usage_write_failures_total.value,
            "dictation_save_failures": observability.dictation_save_failures_total.value,
            "eviction_failures": observability.eviction_failures_total.value,
            "stream_aborts": sum(observability.streaming_sessions_aborted_total.as_dict().values()),
        },
        "db": await asyncio.to_thread(_db_section, app),
        "network": await _network(settings.server_url, probe),
        "clients": clients,
    }
