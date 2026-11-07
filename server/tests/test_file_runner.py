"""
test_file_runner.py — FileJobRunner: re-enqueue, atomic cancel rule, 429 codes.

Uses a real tmp JobStore. The staging probes/transcode are stubbed (no
ffmpeg), and the pipeline is either the real one with a stubbed mlx-whisper
loader (re-enqueue) or a stub ``transcribe_file`` that lets each test
interleave a cancel at the exact point it cares about.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("WISPRALT_API_KEY", "stub_api_key_for_tests_only")
os.environ.setdefault("SERVER_URL", "http://test.local")
os.environ.setdefault("MEETING_OUTPUT_DIR", "/tmp/wispralt-test-meeting")
os.environ.setdefault("JOB_DB_PATH", "/tmp/wispralt-test-jobs.db")
os.environ.setdefault("STAGING_DIR", "/tmp/wispralt-test-staging")

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from starlette.testclient import TestClient

from wispralt._errors import FileJobInProgressError
from wispralt.auth import require_api_key
from wispralt.config import settings
from wispralt.files import output as files_output
from wispralt.files import pipeline as files_pipeline
from wispralt.jobs import runner as runner_mod
from wispralt.jobs.runner import FileJobRunner
from wispralt.jobs.store import JobStore
from wispralt.ops import staging
from wispralt.routes import transcribe_file as transcribe_file_routes

_FORMATS = ("json", "srt", "vtt", "txt")


@pytest.fixture
def out_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "outputs"
    d.mkdir()
    monkeypatch.setattr(settings, "meeting_output_dir", d)
    return d


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.db")


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
    monkeypatch: pytest.MonkeyPatch,
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
    jid = store.create(str(src), request_mode="meeting")  # legacy .wav row
    runner = FileJobRunner(store)

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
    monkeypatch: pytest.MonkeyPatch,
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
    jid = store.create(str(src))
    await FileJobRunner(store)._run_source(jid, src)

    job = store.get(jid)
    assert done_results == [False]
    assert job.status == "failed"
    assert job.error == "cancelled by user"
    assert job.transcript_text is None
    assert _no_outputs(out_dir, jid)


# (c) ------------------------------------------------------------------------


async def test_cancel_before_start_never_runs_pipeline(
    tmp_path: Path, out_dir: Path, store: JobStore, staging_stubs: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = []
    monkeypatch.setattr(
        files_pipeline, "transcribe_file", lambda *a, **kw: called.append(1)
    )
    src = _src(tmp_path, "b.m4a")
    jid = store.create(str(src))
    store.set_cancel_requested(jid)

    await FileJobRunner(store)._run_source(jid, src)

    job = store.get(jid)
    assert called == []
    assert staging_stubs["ffprobe"] == 0
    assert job.status == "failed"
    assert job.error == "cancelled by user"
    assert job.attempts == 0
    assert not src.exists()


def test_set_running_refuses_cancelled_or_non_pending(store: JobStore) -> None:
    jid = store.create("/tmp/x.m4a")
    store.set_cancel_requested(jid)
    assert store.set_running(jid) is False

    jid2 = store.create("/tmp/y.m4a")
    assert store.set_running(jid2) is True
    assert store.set_running(jid2) is False  # already running


# (c2) -----------------------------------------------------------------------


@pytest.mark.parametrize("cancelled", [False, True])
async def test_exception_path_removes_partial_outputs(
    tmp_path: Path, out_dir: Path, store: JobStore, staging_stubs: dict,
    monkeypatch: pytest.MonkeyPatch, cancelled: bool,
) -> None:
    def fake_transcribe(wav_path, output_dir, job_id, **kw) -> dict:
        files_output.write_json(_transcript(job_id), output_dir / f"{job_id}.json")
        if cancelled:
            store.set_cancel_requested(job_id)
        raise OSError("disk went away after the first format")

    monkeypatch.setattr(files_pipeline, "transcribe_file", fake_transcribe)
    src = _src(tmp_path, "c.m4a")
    jid = store.create(str(src))

    await FileJobRunner(store)._run_source(jid, src)

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
    monkeypatch: pytest.MonkeyPatch,
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
    jid = store.create(str(src))
    await FileJobRunner(store)._run_source(jid, src)

    assert state["armed"] is False
    job = real_get(jid)
    assert job.status == "failed"
    assert job.error == "cancelled by user"
    assert job.transcript_text is None
    assert _no_outputs(out_dir, jid)

    # And directly: the conditional UPDATE refuses a cancelled running row.
    jid2 = store.create("/tmp/z.m4a")
    assert store.set_running(jid2) is True
    store.set_cancel_requested(jid2)
    assert store.set_done_if_not_cancelled(jid2, "single", str(out_dir), transcript_text="x") is False
    assert real_get(jid2).status == "running"


# (d) ------------------------------------------------------------------------


async def test_runner_raises_coded_errors(store: JobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FileJobRunner(store)
    await runner._semaphore.acquire()
    try:
        with pytest.raises(FileJobInProgressError) as busy:
            await runner.submit_source_or_429(Path("/tmp/a.m4a"))
        assert busy.value.code == "file_job_busy"
        assert str(busy.value).startswith("file job in progress:")
    finally:
        runner._semaphore.release()

    monkeypatch.setattr(
        runner_mod, "psutil",
        SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=1)),
    )
    with pytest.raises(FileJobInProgressError) as low:
        await runner.submit_source_or_429(Path("/tmp/a.m4a"))
    assert low.value.code == "insufficient_memory"
    assert str(low.value).startswith("file job in progress:")


class _RaisingRunner:
    def __init__(self, code: str) -> None:
        self.code = code

    async def submit_source_or_429(self, *a, **kw) -> str:
        raise FileJobInProgressError(f"file job in progress: {self.code}", code=self.code)


@pytest.mark.parametrize("code", ["file_job_busy", "insufficient_memory"])
def test_submit_429_body_carries_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    monkeypatch.setattr(settings, "staging_dir", tmp_path / "staging")
    monkeypatch.setattr(settings, "max_upload_bytes", 1_000_000)
    monkeypatch.setattr(
        transcribe_file_routes, "psutil",
        SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=64 * 1024**3)),
    )
    app = FastAPI()
    app.include_router(transcribe_file_routes.router)
    app.dependency_overrides[require_api_key] = lambda: None
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
    assert list((tmp_path / "staging").iterdir()) == []
