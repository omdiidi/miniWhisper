"""
test_ops_maintenance.py — retention sweep, staging sweeps, db_watcher log dedupe.

No MLX, no Postgres, no network: JobStore runs on a tmp SQLite file and the
db_watcher loop runs against faked ``db`` functions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from wispralt_server import db
from wispralt_server.jobs.store import JobStore
from wispralt_server.ops import background, staging
from wispralt_server.ops.logging_setup import RepeatedFailureLogger

DAY = 86400


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def _job(store: JobStore, tmp: Path, *, status: str, age_days: float) -> str:
    wav = tmp / "x.wav"
    jid = store.create(str(wav))
    ts = time.time() - age_days * DAY
    store._exec(
        "UPDATE jobs SET status=?, created_at=?, finished_at=? WHERE id=?",
        status,
        ts,
        None if status in ("pending", "running") else ts,
        jid,
    )
    return jid


# ── meeting retention ─────────────────────────────────────────────────────────


def test_retention_deletes_only_old_finished_jobs_and_outputs(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    out = tmp_path / "out"
    out.mkdir()

    old_done = _job(store, tmp_path, status="done", age_days=40)
    old_failed = _job(store, tmp_path, status="failed", age_days=40)
    old_running = _job(store, tmp_path, status="running", age_days=40)
    old_pending = _job(store, tmp_path, status="pending", age_days=40)
    new_done = _job(store, tmp_path, status="done", age_days=1)
    for jid in (old_done, old_failed, old_running, new_done):
        for ext in ("txt", "srt", "vtt", "json"):
            (out / f"{jid}.{ext}").write_text("x")

    deleted = background.sweep_old_meetings(store, 30, out)

    assert deleted == 2
    assert store.get(old_done) is None
    assert store.get(old_failed) is None
    for jid in (old_running, old_pending, new_done):
        assert store.get(jid) is not None
    assert not list(out.glob(f"{old_done}.*"))
    assert not list(out.glob(f"{old_failed}.*"))
    assert len(list(out.glob(f"{old_running}.*"))) == 4
    assert len(list(out.glob(f"{new_done}.*"))) == 4


def test_retention_zero_days_disabled(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    jid = _job(store, tmp_path, status="done", age_days=999)
    assert background.sweep_old_meetings(store, 0, tmp_path) == 0
    assert store.get(jid) is not None


def test_delete_finished_jobs_rechecks_status(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    running = _job(store, tmp_path, status="running", age_days=99)
    assert store.delete_finished_jobs([running]) == 0
    assert store.get(running) is not None


# ── staging sweeps ────────────────────────────────────────────────────────────


def test_staging_sweeps_remove_stale_but_keep_active(tmp_path: Path) -> None:
    stale = tmp_path / "stale.wav"
    fresh = tmp_path / "fresh.wav"
    active = tmp_path / "active.wav"
    for p in (stale, fresh, active):
        p.write_bytes(b"x")
    _age(stale, 2 * DAY)
    _age(active, 2 * DAY)

    assert staging.sweep_old(tmp_path, exclude_paths={active}) == 1
    assert not stale.exists() and fresh.exists() and active.exists()

    chunked = tmp_path / "chunked"
    old_dir, new_dir = chunked / "old", chunked / "new"
    old_dir.mkdir(parents=True)
    new_dir.mkdir()
    (old_dir / "meta.json").write_text("{}")
    _age(old_dir / "meta.json", 7200)
    assert staging.sweep_chunked(tmp_path, max_age_seconds=3600) == 1
    assert not old_dir.exists() and new_dir.exists()


def test_run_daily_sweeps_invokes_staging_and_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging_dir, out = tmp_path / "staging", tmp_path / "out"
    staging_dir.mkdir()
    out.mkdir()
    monkeypatch.setattr(background.settings, "staging_dir", staging_dir)
    monkeypatch.setattr(background.settings, "meeting_output_dir", out)
    monkeypatch.setattr(background.settings, "meeting_job_retention_days", 30)

    store = JobStore(tmp_path / "jobs.db")
    old = _job(store, tmp_path, status="done", age_days=40)
    (out / f"{old}.txt").write_text("x")
    stale_tmp = out / "a.tmp"
    stale_tmp.write_text("x")
    _age(stale_tmp, 2 * 3600)
    stale_wav = staging_dir / "s.wav"
    stale_wav.write_bytes(b"x")
    _age(stale_wav, 2 * DAY)

    background.run_daily_sweeps(store)

    assert store.get(old) is None
    assert not (out / f"{old}.txt").exists()
    assert not stale_tmp.exists()
    assert not stale_wav.exists()


# ── log dedupe ────────────────────────────────────────────────────────────────


def test_repeated_failure_logger_dedupes_and_summarises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = [0.0]
    lim = RepeatedFailureLogger(
        logging.getLogger("t.dedupe"), 300.0, clock=lambda: now[0]
    )
    with caplog.at_level(logging.INFO, logger="t.dedupe"):
        assert lim.failure("k", "boom") is True
        for _ in range(100):
            now[0] += 10
            lim.failure("k", "boom")
    # 1 full log + a summary roughly every 300 s (1000 s elapsed -> 3).
    assert 2 <= len(caplog.records) <= 5
    assert "repeated" in caplog.records[-1].getMessage()
    assert lim.failure("other", "different") is True


def test_db_watcher_logs_repeated_outage_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app = SimpleNamespace(state=SimpleNamespace(db_pool=None, shutting_down=False))
    calls = {"n": 0}

    async def _recreate() -> object:
        calls["n"] += 1
        if calls["n"] >= 50:
            app.state.shutting_down = True
        raise db.PostgresUnavailable("pool is closed")

    monkeypatch.setattr(db, "recreate_pool", _recreate)

    async def _run() -> None:
        await background.db_watcher_loop(app, interval_s=0.0)

    with caplog.at_level(logging.WARNING, logger=background.logger.name):
        asyncio.run(_run())

    assert calls["n"] == 50  # self-healing: it kept retrying every iteration
    tracebacks = [r for r in caplog.records if r.exc_info]
    assert len(tracebacks) == 1
    assert len(caplog.records) <= 4


# ── retention safeguards ──────────────────────────────────────────────────────


def test_retention_skips_runner_active_job(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    zombie = _job(store, tmp_path, status="failed", age_days=99)  # watchdog-failed
    gone = _job(store, tmp_path, status="failed", age_days=99)
    n = background.sweep_old_meetings(store, 30, tmp_path, lambda j: j == zombie)
    assert n == 1
    assert store.get(zombie) is not None and store.get(gone) is None


def test_retention_keeps_row_when_file_removal_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(tmp_path / "jobs.db")
    jid = _job(store, tmp_path, status="done", age_days=99)
    monkeypatch.setattr(background, "remove_job_outputs", lambda *_: False)
    assert background.sweep_old_meetings(store, 30, tmp_path) == 0
    assert store.get(jid) is not None


def test_retention_uses_persisted_dir_and_blocks_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    sub = root / "sub"
    outside = tmp_path / "outside"
    sub.mkdir(parents=True)
    outside.mkdir()
    store = JobStore(tmp_path / "jobs.db")
    inside_job = _job(store, tmp_path, status="done", age_days=99)
    evil_job = _job(store, tmp_path, status="done", age_days=99)
    store._exec("UPDATE jobs SET output_dir=? WHERE id=?", str(sub), inside_job)
    store._exec(
        "UPDATE jobs SET output_dir=? WHERE id=?",
        str(root / ".." / "outside"), evil_job,
    )
    (sub / f"{inside_job}.txt").write_text("x")
    (outside / f"{evil_job}.txt").write_text("x")

    background.sweep_old_meetings(store, 30, root)

    assert not (sub / f"{inside_job}.txt").exists()  # persisted dir honoured
    assert (outside / f"{evil_job}.txt").exists()  # traversal blocked


# ── logging idempotence ───────────────────────────────────────────────────────


def test_configure_file_logging_is_idempotent(tmp_path: Path) -> None:
    import logging.handlers

    from wispralt_server.ops.logging_setup import configure_file_logging

    root = logging.getLogger()
    saved = list(root.handlers)
    log = tmp_path / "app.log"
    try:
        assert configure_file_logging(log, 1024, 2)
        assert configure_file_logging(log, 1024, 2)
        mine = [
            h for h in root.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
            and h.baseFilename == str(log.resolve())
        ]
        assert len(mine) == 1
    finally:
        for h in list(root.handlers):
            if h not in saved:
                root.removeHandler(h)
                h.close()
        for h in saved:
            if h not in root.handlers:
                root.addHandler(h)
