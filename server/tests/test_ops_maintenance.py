"""
test_ops_maintenance.py — the retention setting/purge and the staging sweeps.

No MLX, no network: the conftest JobStore runs on the per-test SQLite DB.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from wispralt.config import settings
from wispralt.jobs.store import JobStore
from wispralt.ops import background, staging

DAY = 86400


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


@pytest.fixture
def store(job_store: JobStore, make_user) -> JobStore:
    job_store.owner = make_user()[0].id  # type: ignore[attr-defined]
    return job_store


def _job(store: JobStore, tmp: Path, *, status: str, age_days: float) -> str:
    jid = store.create(tmp / "x.wav", user_id=store.owner)  # type: ignore[attr-defined]
    ts = time.time() - age_days * DAY
    with store.db.write() as con:
        con.execute(
            "UPDATE jobs SET status=?, created_at=?, finished_at=? WHERE id=?",
            (status, ts, None if status in ("pending", "running") else ts, jid),
        )
    return jid


# ── retention (DB setting) ────────────────────────────────────────────────────


def _dictation(store: JobStore, *, age_days: float, source: str | None = None) -> None:
    store.insert_dictation(
        user_id=store.owner,  # type: ignore[attr-defined]
        text="hi",
        duration_ms=None,
        client_app_version=None,
        dedup_id=None,
        created_at=time.time() - age_days * DAY,
    )
    if source:
        with store.db.write() as con:
            con.execute(
                "UPDATE dictations SET source=?, source_id=id WHERE id=(SELECT MAX(id) FROM dictations)",
                (source,),
            )


def _imported(store: JobStore, jid: str) -> str:
    with store.db.write() as con:
        con.execute("UPDATE jobs SET source='supabase', source_id=id WHERE id=?", (jid,))
    return jid


def test_default_retention_is_forever(tmp_path: Path, store: JobStore) -> None:
    jid = _job(store, tmp_path, status="done", age_days=9999)
    _dictation(store, age_days=9999)
    assert store.retention_days() == 0
    assert store.retention_preview(0) == {"dictations": 0, "jobs": 0}
    assert background.run_retention(store) == (0, 0)
    assert store.get(jid) is not None
    with store.db.read() as con:
        assert con.execute("SELECT COUNT(*) FROM dictations").fetchone()[0] == 1


def test_preview_matches_purge_and_imports_survive(tmp_path: Path, store: JobStore) -> None:
    out = settings.outputs_dir
    out.mkdir()
    old_done = _job(store, tmp_path, status="done", age_days=40)
    old_failed = _job(store, tmp_path, status="failed", age_days=40)
    old_running = _job(store, tmp_path, status="running", age_days=40)
    old_pending = _job(store, tmp_path, status="pending", age_days=40)
    new_done = _job(store, tmp_path, status="done", age_days=1)
    imported = _imported(store, _job(store, tmp_path, status="done", age_days=400))
    for jid in (old_done, old_failed, old_running, new_done):
        for ext in ("txt", "srt", "vtt", "json"):
            (out / f"{jid}.{ext}").write_text("x")
    for age in (40, 50, 1):
        _dictation(store, age_days=age)
    _dictation(store, age_days=400, source="supabase")

    store.set_retention_days(30)
    assert store.retention_days() == 30
    preview = store.retention_preview(30)
    assert preview == {"dictations": 2, "jobs": 2}
    assert background.run_retention(store) == (preview["dictations"], preview["jobs"])

    assert store.get(old_done) is None and store.get(old_failed) is None
    for jid in (old_running, old_pending, new_done, imported):
        assert store.get(jid) is not None
    assert not list(out.glob(f"{old_done}.*"))
    assert len(list(out.glob(f"{old_running}.*"))) == 4
    with store.db.read() as con:
        left = [r[0] for r in con.execute("SELECT source FROM dictations ORDER BY id")]
    assert left == [None, "supabase"]
    with pytest.raises(ValueError):
        store.set_retention_days(-1)


def test_delete_finished_jobs_rechecks_status(tmp_path: Path, store: JobStore) -> None:
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
    tmp_path: Path, store: JobStore
) -> None:
    staging_dir, out = settings.staging_dir, settings.outputs_dir
    staging_dir.mkdir()
    out.mkdir()
    store.set_retention_days(30)

    old = _job(store, tmp_path, status="done", age_days=40)
    (out / f"{old}.txt").write_text("x")
    _dictation(store, age_days=40)
    stale_tmp = out / "a.tmp"
    stale_tmp.write_text("x")
    _age(stale_tmp, 2 * 3600)
    stale_wav = staging_dir / "s.wav"
    stale_wav.write_bytes(b"x")
    _age(stale_wav, 2 * DAY)

    background.run_daily_sweeps(store)

    assert store.get(old) is None
    assert not (out / f"{old}.txt").exists()
    assert store.retention_preview(30) == {"dictations": 0, "jobs": 0}
    assert not stale_tmp.exists()
    assert not stale_wav.exists()


async def test_daily_loop_applies_the_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: JobStore
) -> None:
    import asyncio
    from types import SimpleNamespace

    settings.staging_dir.mkdir()
    settings.outputs_dir.mkdir()
    store.set_retention_days(7)
    old = _job(store, tmp_path, status="done", age_days=8)
    app = SimpleNamespace(state=SimpleNamespace(shutting_down=False))
    sleeps = 0

    async def one_day(_s: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            app.state.shutting_down = True

    monkeypatch.setattr(background.asyncio, "sleep", one_day)
    await asyncio.wait_for(background.daily_sweep_loop(app, store), 5)  # type: ignore[arg-type]
    assert store.get(old) is None


# ── retention safeguards ──────────────────────────────────────────────────────


def test_retention_skips_runner_active_job(tmp_path: Path, store: JobStore) -> None:
    zombie = _job(store, tmp_path, status="failed", age_days=99)  # watchdog-failed
    gone = _job(store, tmp_path, status="failed", age_days=99)
    n = background.purge_expired_jobs(store, 30, tmp_path, lambda j: j == zombie)
    assert n == 1
    assert store.get(zombie) is not None and store.get(gone) is None


def test_retention_keeps_row_when_file_removal_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: JobStore
) -> None:
    jid = _job(store, tmp_path, status="done", age_days=99)
    monkeypatch.setattr(background, "remove_job_outputs", lambda *_: False)
    assert background.purge_expired_jobs(store, 30, tmp_path) == 0
    assert store.get(jid) is not None


def test_retention_never_touches_files_outside_the_output_root(
    tmp_path: Path, store: JobStore
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    jid = _job(store, tmp_path, status="done", age_days=99)
    with store.db.write() as con:  # a hostile id that would escape the root
        con.execute("UPDATE jobs SET id='../victim' WHERE id=?", (jid,))
    assert background.purge_expired_jobs(store, 30, out) == 0
    assert victim.read_text() == "keep me"
    assert store.get("../victim") is not None


def test_recovery_before_sweep_keeps_the_requeued_input(store: JobStore) -> None:
    """A job running at a crash is requeued, so the startup sweep keeps its (old) WAV."""
    import numpy as np
    import soundfile as sf

    settings.staging_dir.mkdir()
    wav = settings.staging_dir / "in.wav"
    sf.write(str(wav), np.zeros(1600, dtype=np.float32), 16_000, subtype="FLOAT", format="WAV")
    _age(wav, 2 * DAY)  # older than the 24 h staging TTL
    jid = store.create(wav, user_id=store.owner)  # type: ignore[attr-defined]
    assert store.set_running(jid)

    assert store.recover_orphans()["requeue"] == [jid]  # startup order: recover, then sweep
    background.sweep_staging(store)

    assert wav.exists()
    assert store.get(jid).status == "pending"


# ── logging idempotence ───────────────────────────────────────────────────────


def test_configure_file_logging_is_idempotent(tmp_path: Path) -> None:
    import logging.handlers

    from wispralt.ops.logging_setup import configure_file_logging

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
