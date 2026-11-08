"""
test_file_runner.py — FileJobRunner: re-enqueue, atomic cancel rule, 429 codes.

Uses the conftest JobStore on the per-test SQLite DB (jobs need a real
owner: ``owner`` is a ``make_user`` id). The staging probes/transcode are stubbed (no
ffmpeg), and the pipeline is either the real one with a stubbed mlx-whisper
loader (re-enqueue) or a stub ``transcribe_file`` that lets each test
interleave a cancel at the exact point it cares about.
"""
from __future__ import annotations

import asyncio
import io
import logging
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from wispralt._errors import FileJobInProgressError
from wispralt.accounts.store import User
from wispralt.auth import require_bearer
from wispralt.config import settings
from wispralt.files import output as files_output
from wispralt.files import pipeline as files_pipeline
from wispralt.jobs import runner as runner_mod
from wispralt.jobs.runner import FileJobRunner
from wispralt.jobs.store import JobStore
from wispralt.ops import staging
from wispralt.routes import transcribe_file as transcribe_file_routes

if TYPE_CHECKING:
    from .conftest import MakeUser

_FORMATS = ("json", "srt", "vtt", "txt")


@pytest.fixture
def out_dir() -> Path:
    d = settings.outputs_dir  # <per-test data_dir>/outputs
    d.mkdir(parents=True)
    return d


@pytest.fixture
def store(job_store: JobStore) -> JobStore:
    return job_store


@pytest.fixture
def owner(make_user: MakeUser) -> int:
    return make_user()[0].id


def _runner(store: JobStore, *, shutting_down: bool = False) -> FileJobRunner:
    return FileJobRunner(store, SimpleNamespace(shutting_down=shutting_down))


@pytest.fixture
def staging_stubs(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls = {"ffprobe": 0, "transcode": 0}

    def ffprobe_channel_count(src: Path) -> int:
        calls["ffprobe"] += 1
        return 1

    def ffprobe_duration(src: Path) -> float:
        return 1.0

    def transcode_to_canonical_wav(src: Path, *, target_channels: int, cancel_cb=None) -> Path:
        calls["transcode"] += 1
        target = src.with_name(f"{src.stem}_canonical.wav")
        shutil.copyfile(src, target)
        return target

    monkeypatch.setattr(staging, "ffprobe_channel_count", ffprobe_channel_count)
    monkeypatch.setattr(staging, "ffprobe_duration", ffprobe_duration)
    monkeypatch.setattr(staging, "transcode_to_canonical_wav", transcode_to_canonical_wav)
    return calls


def _src(tmp_path: Path, name: str = "src.wav") -> Path:
    path = tmp_path / name
    # Source "container" is a WAV whatever its name; the transcode stub copies it.
    sf.write(str(path), np.full(16_000, 0.1, dtype=np.float32), 16_000,
             subtype="FLOAT", format="WAV")
    return path


def _transcript(jid: str) -> dict:
    return {
        "job_id": jid,
        "mode": "single",
        "segments": [
            {"start": 0.0, "end": 1.0, "channel": None, "speaker": "Speaker 1",
             "speaker_raw": "mic", "text": "hello", "words": [], "overlap": False},
        ],
        "speakers": {"mic": {"display_name": "Speaker 1", "channel": None}},
    }


def _no_outputs(out_dir: Path, jid: str) -> bool:
    return not any((out_dir / f"{jid}.{fmt}").exists() for fmt in _FORMATS)


async def _wait_terminal(store: JobStore, jid: str, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        job = store.get(jid)
        if job is not None and job.status in ("done", "failed"):
            return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {jid} never reached a terminal state")


# (a) ------------------------------------------------------------------------


async def test_pending_wav_reenqueues_through_run_source(
    tmp_path: Path, out_dir: Path, store: JobStore, staging_stubs: dict,
    monkeypatch: pytest.MonkeyPatch, owner: int,
) -> None:
    # Real pipeline, stubbed mlx-whisper loader.
    monkeypatch.setattr(files_pipeline._mlx_mod, "load", lambda: None)
    monkeypatch.setattr(files_pipeline._mlx_mod, "reset", lambda: None)
    monkeypatch.setattr(
        files_pipeline._mlx_mod, "transcribe_channel",
        lambda audio, **kw: {"segments": [{"start": 0.0, "end": 1.0, "text": " hi"}]},
    )
    monkeypatch.setattr(files_pipeline, "_models_ready", False)

    src = _src(tmp_path)
    jid = store.create(src, user_id=owner)
    assert store.list_pending_jobs() == [(jid, src)]  # absolute path back
    runner = _runner(store)

    await runner.reenqueue_pending()
    job = await _wait_terminal(store, jid)

    assert job.status == "done", job.error
    assert job.attempts == 1  # one set_running per attempt
    assert job.transcript_text == "hi"
    assert staging_stubs["ffprobe"] == 1 and staging_stubs["transcode"] == 1
    assert all((out_dir / f"{jid}.{fmt}").is_file() for fmt in _FORMATS)
    assert not src.exists()


# (b) ------------------------------------------------------------------------


async def test_cancel_during_transcribe_never_ends_done(
    tmp_path: Path, out_dir: Path, store: JobStore, staging_stubs: dict,
    monkeypatch: pytest.MonkeyPatch, owner: int,
) -> None:
    done_results: list[bool] = []
    real_set_done = store.set_done_if_not_cancelled

    def spy_set_done(*a, **kw) -> bool:
        ok = real_set_done(*a, **kw)
        done_results.append(ok)
        return ok

    monkeypatch.setattr(store, "set_done_if_not_cancelled", spy_set_done)

    def fake_transcribe(wav_path, output_dir, job_id, **kw) -> dict:
        store.set_cancel_requested(job_id)  # DELETE lands mid-transcribe
        t = _transcript(job_id)
        files_output.write_outputs_atomic(t, output_dir, job_id)
        return t

    monkeypatch.setattr(files_pipeline, "transcribe_file", fake_transcribe)

    src = _src(tmp_path, "a.m4a")
    jid = store.create(src, user_id=owner)
    await _runner(store)._run_source(jid, src)

    job = store.get(jid)
    assert done_results == [False]
    assert job.status == "failed"
    assert job.error == "cancelled by user"
    assert job.transcript_text is None
    assert _no_outputs(out_dir, jid)


# (c) ------------------------------------------------------------------------


async def test_cancel_before_start_never_runs_pipeline(
    tmp_path: Path, out_dir: Path, store: JobStore, staging_stubs: dict,
    monkeypatch: pytest.MonkeyPatch, owner: int,
) -> None:
    called = []
    monkeypatch.setattr(
        files_pipeline, "transcribe_file", lambda *a, **kw: called.append(1)
    )
    src = _src(tmp_path, "b.m4a")
    jid = store.create(src, user_id=owner)
    store.set_cancel_requested(jid)

    await _runner(store)._run_source(jid, src)

    job = store.get(jid)
    assert called == []
    assert staging_stubs["ffprobe"] == 0
    assert job.status == "failed"
    assert job.error == "cancelled by user"
    assert job.attempts == 0
    assert not src.exists()


def test_set_running_refuses_cancelled_or_non_pending(store: JobStore, owner: int) -> None:
    jid = store.create("/tmp/x.m4a", user_id=owner)
    store.set_cancel_requested(jid)
    assert store.set_running(jid) is False

    jid2 = store.create("/tmp/y.m4a", user_id=owner)
    assert store.set_running(jid2) is True
    assert store.set_running(jid2) is False  # already running


# (c2) -----------------------------------------------------------------------


@pytest.mark.parametrize("cancelled", [False, True])
async def test_exception_path_removes_partial_outputs(
    tmp_path: Path, out_dir: Path, store: JobStore, staging_stubs: dict,
    monkeypatch: pytest.MonkeyPatch, cancelled: bool, owner: int,
) -> None:
    def fake_transcribe(wav_path, output_dir, job_id, **kw) -> dict:
        files_output.write_json(_transcript(job_id), output_dir / f"{job_id}.json")
        if cancelled:
            store.set_cancel_requested(job_id)
        raise OSError("disk went away after the first format")

    monkeypatch.setattr(files_pipeline, "transcribe_file", fake_transcribe)
    src = _src(tmp_path, "c.m4a")
    jid = store.create(src, user_id=owner)

    await _runner(store)._run_source(jid, src)

    job = store.get(jid)
    assert job.status == "failed"
    if cancelled:
        assert job.error == "cancelled by user"
    else:
        assert "disk went away" in (job.error or "")
    assert _no_outputs(out_dir, jid)


# (e) ------------------------------------------------------------------------


async def test_cancel_after_executor_returns_discards_result(
    tmp_path: Path, out_dir: Path, store: JobStore, staging_stubs: dict,
    monkeypatch: pytest.MonkeyPatch, owner: int,
) -> None:
    def fake_transcribe(wav_path, output_dir, job_id, **kw) -> dict:
        t = _transcript(job_id)
        files_output.write_outputs_atomic(t, output_dir, job_id)
        return t

    monkeypatch.setattr(files_pipeline, "transcribe_file", fake_transcribe)

    # Cancel lands after the executor finished but before set_done: the
    # first store.get() after the executor is the guard read.
    real_get = store.get
    state = {"armed": True}

    def get_then_cancel(jid: str):
        job = real_get(jid)
        if state["armed"] and job is not None and job.status == "running" and job.phase == "transcribe_load":
            state["armed"] = False
            store.set_cancel_requested(jid)
        return job

    monkeypatch.setattr(store, "get", get_then_cancel)

    src = _src(tmp_path, "e.m4a")
    jid = store.create(src, user_id=owner)
    await _runner(store)._run_source(jid, src)

    assert state["armed"] is False
    job = real_get(jid)
    assert job.status == "failed"
    assert job.error == "cancelled by user"
    assert job.transcript_text is None
    assert _no_outputs(out_dir, jid)

    # And directly: the conditional UPDATE refuses a cancelled running row.
    jid2 = store.create("/tmp/z.m4a", user_id=owner)
    assert store.set_running(jid2) is True
    store.set_cancel_requested(jid2)
    assert store.set_done_if_not_cancelled(jid2, "single", transcript_text="x") is False
    assert real_get(jid2).status == "running"


# (d) ------------------------------------------------------------------------


async def test_runner_raises_coded_errors(
    store: JobStore, monkeypatch: pytest.MonkeyPatch, owner: int
) -> None:
    runner = _runner(store)
    release = asyncio.Event()

    async def held_worker(jid: str, src_path: Path) -> None:
        await release.wait()

    monkeypatch.setattr(runner, "_run_source", held_worker)
    # Back-to-back submits with no await between them: the first worker has
    # not acquired the semaphore yet, and the second submit must still 429.
    await runner.submit_source_or_429(Path("/tmp/a.m4a"), user_id=owner)
    assert runner.active  # queued worker already counts as active
    with pytest.raises(FileJobInProgressError) as busy:
        await runner.submit_source_or_429(Path("/tmp/b.m4a"), user_id=owner)
    assert busy.value.code == "file_job_busy"
    assert str(busy.value).startswith("file job in progress:")
    release.set()
    for _ in range(50):
        if not runner._workers:
            break
        await asyncio.sleep(0)
    assert not runner._workers  # finished worker no longer counts as busy

    monkeypatch.setattr(
        runner_mod, "psutil",
        SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=1)),
    )
    with pytest.raises(FileJobInProgressError) as low:
        await runner.submit_source_or_429(Path("/tmp/a.m4a"), user_id=owner)
    assert low.value.code == "insufficient_memory"
    assert str(low.value).startswith("file job in progress:")


class _RaisingRunner:
    def __init__(self, code: str) -> None:
        self.code = code

    async def submit_source_or_429(self, *a, **kw) -> str:
        raise FileJobInProgressError(f"file job in progress: {self.code}", code=self.code)


@pytest.mark.parametrize("code", ["file_job_busy", "insufficient_memory"])
def test_submit_429_body_carries_code(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    monkeypatch.setattr(settings, "max_upload_bytes", 1_000_000)
    monkeypatch.setattr(
        transcribe_file_routes, "psutil",
        SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=64 * 1024**3)),
    )
    app = FastAPI()
    app.include_router(transcribe_file_routes.router)
    user = User(id=1, key_id=None, label="u", role="employee", kind="employee")

    async def _auth(request: Request) -> User:
        request.state.user = user
        return user

    app.dependency_overrides[require_bearer] = _auth
    app.state.file_runner = _RaisingRunner(code)

    with TestClient(app) as client:
        resp = client.post(
            "/transcribe/file",
            files={"file": ("a.m4a", b"\x00" * 1024, "audio/mp4")},
        )

    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "60"
    body = resp.json()
    assert body == {"error": f"file job in progress: {code}", "retry_after_s": 60, "code": code}
    # The staged upload is cleaned up on 429.
    assert list(settings.staging_dir.iterdir()) == []


# (e2) cancel precedence ------------------------------------------------------


async def test_cancel_during_decode_never_loads_the_model(
    tmp_path: Path, out_dir: Path, store: JobStore, monkeypatch: pytest.MonkeyPatch,
    staging_stubs: dict, owner: int,
) -> None:
    pipeline_calls: list[str] = []
    monkeypatch.setattr(
        files_pipeline, "transcribe_file", lambda *a, **kw: pipeline_calls.append("called")
    )
    src = _src(tmp_path, "c.m4a")
    jid = store.create(src, user_id=owner)

    def decode_while_user_cancels(src: Path, *, target_channels: int, cancel_cb=None) -> Path:
        store.set_cancel_requested(jid)  # the decode itself does not notice
        target = src.with_name(f"{src.stem}_canonical.wav")
        shutil.copyfile(src, target)
        return target

    monkeypatch.setattr(staging, "transcode_to_canonical_wav", decode_while_user_cancels)
    await _runner(store)._run_source(jid, src)

    job = store.get(jid)
    assert job.status == "failed" and job.error == "cancelled by user"
    assert pipeline_calls == []
    assert not job.wav_path.exists() and not src.exists()
    assert _no_outputs(out_dir, jid)


async def test_phase_timeout_with_cancel_requested_reports_cancelled(
    store: JobStore, owner: int
) -> None:
    jid = store.create("/tmp/x.m4a", user_id=owner)
    assert store.set_running(jid)
    store.set_cancel_requested(jid)
    release = threading.Event()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await _runner(store)._phase(jid, "ffprobe", 0.05, release.wait, 5)
    finally:
        release.set()  # the executor thread is not cancellable
    assert store.get(jid).error == "cancelled by user"


async def test_watchdog_with_cancel_requested_reports_cancelled(
    store: JobStore, owner: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid = store.create("/tmp/x.m4a", user_id=owner)
    assert store.set_running(jid)
    store.update_phase(jid, "transcribe")
    with store.db.write() as con:  # the phase started long ago
        con.execute("UPDATE jobs SET phase_started_at=? WHERE id=?", (time.time() - 1e6, jid))
    store.set_cancel_requested(jid)

    async def no_wait(_s: float) -> None:
        return None

    monkeypatch.setattr(runner_mod.asyncio, "sleep", no_wait)
    await asyncio.wait_for(_runner(store)._phase_watchdog(jid, 1.0), 5)
    assert store.get(jid).error == "cancelled by user"


# (f) shutdown keeps inputs ---------------------------------------------------


async def test_job_done_during_shutdown_still_cleans_its_wav(
    tmp_path: Path, out_dir: Path, store: JobStore, staging_stubs: dict,
    monkeypatch: pytest.MonkeyPatch, owner: int,
) -> None:
    seen: list[Path] = []

    def fake_transcribe(wav_path, output_dir, job_id, **kw) -> dict:
        seen.append(wav_path)
        t = _transcript(job_id)
        files_output.write_outputs_atomic(t, output_dir, job_id)
        return t

    monkeypatch.setattr(files_pipeline, "transcribe_file", fake_transcribe)
    src = _src(tmp_path, "g.m4a")
    jid = store.create(src, user_id=owner)
    await _runner(store, shutting_down=True)._run_source(jid, src)

    assert store.get(jid).status == "done"
    assert seen and not seen[0].exists() and not src.exists()


def test_shutdown_mid_job_keeps_input_and_requeues(
    store: JobStore,
    out_dir: Path,
    staging_stubs: dict,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Real lifespan: exiting the TestClient runs shutdown while the pipeline blocks."""
    from wispralt import main
    from wispralt.dictate.parakeet import ParakeetService

    monkeypatch.setattr(main, "configure_file_logging", lambda *a: True)
    monkeypatch.setattr(ParakeetService, "load", lambda self: None)
    monkeypatch.setattr(main.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(main.subprocess, "check_output", lambda *a, **kw: "ffmpeg stub\n")
    ram = SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=64 * 1024**3))
    monkeypatch.setattr(transcribe_file_routes, "psutil", ram)
    monkeypatch.setattr(runner_mod, "psutil", ram)

    entered, release = threading.Event(), threading.Event()

    def blocking_transcribe(wav_path, output_dir, job_id, **kw) -> dict:
        entered.set()
        release.wait(30)
        raise RuntimeError("abandoned executor thread")  # its future was cancelled

    monkeypatch.setattr(files_pipeline, "transcribe_file", blocking_transcribe)
    _user, token = make_user()
    wav = np.full(1600, 0.1, dtype=np.float32)
    buf = io.BytesIO()
    sf.write(buf, wav, 16_000, subtype="FLOAT", format="WAV")
    try:
        with caplog.at_level(logging.INFO, logger=main.logger.name), TestClient(
            main.create_app()
        ) as c:
            resp = c.post(
                "/transcribe/file",
                files={"file": ("a.m4a", buf.getvalue(), "audio/mp4")},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 202, resp.text
            jid = resp.json()["job_id"]
            assert entered.wait(10)
            assert store.get(jid).attempts == 1
            # The thread finishes while shutdown drains it (inside EXECUTOR_DRAIN_S).
            threading.Timer(0.3, release.set).start()
    finally:
        release.set()

    job = store.get(jid)
    assert job.status == "pending" and job.attempts == 0  # attempt refunded
    assert job.wav_path.exists()  # the staged (canonical) input survived
    messages = [r.getMessage() for r in caplog.records]
    assert messages.index("Shutdown: file runner stopped.") < messages.index(
        "Shutdown: database released."
    )

    # A fresh start recovers the row and runs it to done.
    def quick_transcribe(wav_path, output_dir, job_id, **kw) -> dict:
        t = _transcript(job_id)
        files_output.write_outputs_atomic(t, output_dir, job_id)
        return t

    monkeypatch.setattr(files_pipeline, "transcribe_file", quick_transcribe)
    with TestClient(main.create_app()):
        deadline = time.monotonic() + 10
        while store.get(jid).status not in ("done", "failed") and time.monotonic() < deadline:
            time.sleep(0.05)
    final = store.get(jid)
    assert final.status == "done", final.error
    assert final.attempts == 1
    assert not job.wav_path.exists()


def test_shutdown_waits_for_executor_and_pins_lock_when_abandoned(
    store: JobStore,
    out_dir: Path,
    staging_stubs: dict,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread still running past the drain bound keeps the server lock held."""
    import os

    from wispralt import main
    from wispralt._errors import ServerRunning
    from wispralt.dictate.parakeet import ParakeetService
    from wispralt.ops import backup

    monkeypatch.setattr(main, "configure_file_logging", lambda *a: True)
    monkeypatch.setattr(ParakeetService, "load", lambda self: None)
    monkeypatch.setattr(main.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(main.subprocess, "check_output", lambda *a, **kw: "ffmpeg stub\n")
    ram = SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=64 * 1024**3))
    monkeypatch.setattr(transcribe_file_routes, "psutil", ram)
    monkeypatch.setattr(runner_mod, "psutil", ram)
    monkeypatch.setattr(runner_mod, "EXECUTOR_DRAIN_S", 0.2)

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocking_transcribe(wav_path, output_dir, job_id, **kw) -> dict:
        entered.set()
        release.wait(30)
        finished.set()
        return _transcript(job_id)

    monkeypatch.setattr(files_pipeline, "transcribe_file", blocking_transcribe)
    _user, token = make_user()
    buf = io.BytesIO()
    sf.write(buf, np.full(1600, 0.1, dtype=np.float32), 16_000, subtype="FLOAT", format="WAV")
    pinned_before = len(backup._PINNED)
    try:
        with TestClient(main.create_app()) as c:
            resp = c.post(
                "/transcribe/file",
                files={"file": ("a.m4a", buf.getvalue(), "audio/mp4")},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 202, resp.text
            assert entered.wait(10)
        # Lifespan is over but the thread runs on: the lock must still be held.
        assert not finished.is_set()
        assert len(backup._PINNED) == pinned_before + 1
        with pytest.raises(ServerRunning):
            backup.server_lock(settings.data_dir).__enter__()
    finally:
        release.set()
        finished.wait(10)
        while len(backup._PINNED) > pinned_before:
            os.close(backup._PINNED.pop())
    with backup.server_lock(settings.data_dir):  # released once the pin is closed
        pass


async def test_shutdown_drains_an_idle_executor(store: JobStore) -> None:
    assert await _runner(store).shutdown(drain_timeout_s=5) is True


# (g) a DB error while failing a job keeps its input ---------------------------


async def test_pipeline_error_with_db_error_keeps_wav_for_recovery(
    tmp_path: Path, out_dir: Path, store: JobStore, staging_stubs: dict,
    monkeypatch: pytest.MonkeyPatch, owner: int,
) -> None:
    import sqlite3

    seen: list[Path] = []

    def boom(wav_path, output_dir, job_id, **kw) -> dict:
        seen.append(wav_path)
        raise RuntimeError("pipeline blew up")

    def locked(jid: str, error: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(files_pipeline, "transcribe_file", boom)
    monkeypatch.setattr(store, "set_failed", locked)
    src = _src(tmp_path, "h.m4a")
    jid = store.create(src, user_id=owner)
    await _runner(store)._run_source(jid, src)

    assert store.get(jid).status == "running"  # not terminal: recover_orphans requeues it
    assert seen and seen[0].exists()  # its input survived


async def test_source_error_with_db_error_keeps_source(
    tmp_path: Path, store: JobStore, staging_stubs: dict,
    monkeypatch: pytest.MonkeyPatch, owner: int,
) -> None:
    import sqlite3

    db_down = {"on": True}
    real_set_failed = store.set_failed

    def bad_duration(src: Path) -> float:
        raise RuntimeError("ffprobe exploded")

    def maybe_locked(jid: str, error: str) -> None:
        if db_down["on"]:
            raise sqlite3.OperationalError("database is locked")
        real_set_failed(jid, error)

    monkeypatch.setattr(staging, "ffprobe_duration", bad_duration)
    monkeypatch.setattr(store, "set_failed", maybe_locked)
    src = _src(tmp_path, "i.m4a")
    jid = store.create(src, user_id=owner)
    await _runner(store)._run_source(jid, src)
    assert store.get(jid).status == "running" and src.exists()

    db_down["on"] = False  # a plain failure still removes the source
    jid2 = store.create(src, user_id=owner)
    await _runner(store)._run_source(jid2, src)
    assert store.get(jid2).status == "failed" and not src.exists()


# (h) ffmpeg stderr is drained -------------------------------------------------


def test_transcode_drains_a_flood_of_ffmpeg_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More stderr than a pipe holds must not stall the decode (it used to block)."""
    import os
    import stat

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "ffmpeg"
    fake.write_text(
        "#!/bin/sh\n"
        "head -c 1000000 /dev/zero | tr '\\0' 'x' >&2\n"  # ~1 MB, far past 64 KB
        'for last; do :; done\n'
        'head -c 200 /dev/zero > "$last"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    src = tmp_path / "in.m4a"
    src.write_bytes(b"not really audio")

    started = time.monotonic()
    out = staging.transcode_to_canonical_wav(
        src, target_channels=1,
        cancel_cb=lambda: time.monotonic() - started > 20,  # a stall fails, never hangs
    )
    assert out.exists() and out.stat().st_size == 200


def test_transcode_failure_reports_the_stderr_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import stat

    from fastapi import HTTPException

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "ffmpeg"
    fake.write_text(
        "#!/bin/sh\nhead -c 300000 /dev/zero | tr '\\0' 'x' >&2\n"
        "echo >&2\necho 'Invalid data found when processing input' >&2\nexit 1\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    src = tmp_path / "in.m4a"
    src.write_bytes(b"junk")
    with pytest.raises(HTTPException) as exc:
        staging.transcode_to_canonical_wav(src, target_channels=1)
    assert exc.value.status_code == 422
    assert "Invalid data found" in exc.value.detail
