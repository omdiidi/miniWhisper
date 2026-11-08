"""
test_file_job_routes.py — /transcribe/file/{job_id} poll, download, delete.

TestClient over the transcribe_file router with the conftest JobStore on the
per-test SQLite DB. Auth is replaced by a dependency that SETS
``request.state.user`` (the ownership check reads it); the three users exist
as real rows (jobs have a ``user_id`` FK): owner (id=1), non-owner (id=2),
admin (id=3).

The end-to-end case runs the real staging (ffprobe + ffmpeg transcode) and
output writers; only the mlx-whisper loader is stubbed. It is skipped when
ffmpeg is not installed.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Callable

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from wispralt.accounts.store import User
from wispralt.auth import require_api_key
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
OWNER = User(id=1, key_id=None, label="owner", role="employee", kind="employee")
OTHER = User(id=2, key_id=None, label="other", role="employee", kind="employee")
ADMIN = User(id=3, key_id=None, label="admin", role="admin", kind="employee")


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _users(make_user: MakeUser) -> None:
    """OWNER / OTHER / ADMIN as real rows (ids 1-3 on the fresh per-test DB)."""
    made = [make_user(label="owner")[0], make_user(label="other")[0], make_user("admin", label="admin")[0]]
    assert [u.id for u in made] == [OWNER.id, OTHER.id, ADMIN.id]


@pytest.fixture
def out_dir() -> Path:
    d = settings.outputs_dir
    d.mkdir(parents=True)
    return d


@pytest.fixture
def staging_dir(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "max_upload_bytes", 50_000_000)
    return settings.staging_dir


@pytest.fixture
def store(job_store: JobStore) -> JobStore:
    return job_store


@pytest.fixture
def plenty_of_ram(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both 4 GiB route gates and the runner's 2 GiB gate pass."""
    vm = SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=16 * 1024**3))
    monkeypatch.setattr(transcribe_file_routes, "psutil", vm)
    monkeypatch.setattr(runner_mod, "psutil", vm)


@pytest.fixture
def stub_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real pipeline; only the mlx-whisper model calls are stubbed."""
    monkeypatch.setattr(files_pipeline._mlx_mod, "load", lambda: None)
    monkeypatch.setattr(
        files_pipeline._mlx_mod, "transcribe_channel",
        lambda audio, **kw: {"segments": [{"start": 0.0, "end": 1.0, "text": " stub words"}]},
    )
    monkeypatch.setattr(files_pipeline, "_models_ready", False)


def _as(user: User | None) -> Callable:
    async def dep(request: Request) -> User | None:
        request.state.user = user
        return user
    return dep


@pytest.fixture
def make_app(store: JobStore) -> Callable[..., FastAPI]:
    def _make(user: User | None = OWNER, runner: object | None = None) -> FastAPI:
        app = FastAPI()
        app.include_router(transcribe_file_routes.router)
        app.dependency_overrides[require_api_key] = _as(user)
        app.state.job_store = store
        app.state.file_runner = runner if runner is not None else FileJobRunner(store, app.state)
        return app
    return _make


def _transcript(jid: str) -> dict:
    return {
        "job_id": jid,
        "mode": "single",
        "segments": [
            {"start": 0.0, "end": 1.0, "channel": None, "speaker": "Speaker 1",
             "speaker_raw": "mic", "text": "hello there", "words": [], "overlap": False},
        ],
        "speakers": {"mic": {"display_name": "Speaker 1", "channel": None}},
    }


def _done_job(store: JobStore, out_dir: Path, owner: int) -> str:
    jid = store.create("/tmp/src.m4a", user_id=owner)
    assert store.set_running(jid)
    files_output.write_outputs_atomic(_transcript(jid), out_dir, jid)
    assert store.set_done_if_not_cancelled(jid, "single", transcript_text="hello there")
    return jid


def _wav_bytes(seconds: float = 1.0) -> bytes:
    n = int(16_000 * seconds)
    t = np.arange(n) / 16_000
    pcm = (0.2 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _wait_status(c: TestClient, jid: str, want: set[str], timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = c.get(f"/transcribe/file/{jid}").json()
        if body.get("status") in want:
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {jid} never reached {want}; last={body}")


# ── ownership ─────────────────────────────────────────────────────────────────


def test_owner_polls_done_job_and_downloads(make_app, store, out_dir) -> None:
    jid = _done_job(store, out_dir, owner=OWNER.id)
    c = TestClient(make_app(OWNER))

    resp = c.get(f"/transcribe/file/{jid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["outputs"] == {
        fmt: f"/transcribe/file/{jid}/download/{fmt}" for fmt in sorted(_FORMATS)
    }

    dl = c.get(body["outputs"]["txt"])
    assert dl.status_code == 200
    assert dl.headers["content-type"].startswith("text/plain")
    assert dl.content == (out_dir / f"{jid}.txt").read_bytes()
    assert b"hello there" in dl.content


def test_non_owner_gets_404_and_delete_is_noop(make_app, store, out_dir) -> None:
    jid = _done_job(store, out_dir, owner=OWNER.id)
    c = TestClient(make_app(OTHER))

    assert c.get(f"/transcribe/file/{jid}").status_code == 404
    assert c.get(f"/transcribe/file/{jid}/download/txt").status_code == 404
    assert c.delete(f"/transcribe/file/{jid}").status_code == 204
    assert store.get(jid) is not None
    assert (out_dir / f"{jid}.txt").is_file()


def test_admin_polls_another_users_job(make_app, store, out_dir) -> None:
    jid = _done_job(store, out_dir, owner=OWNER.id)
    resp = TestClient(make_app(ADMIN)).get(f"/transcribe/file/{jid}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"


# ── validation / missing outputs ──────────────────────────────────────────────


def test_bad_uuid_and_unknown_format_are_400(make_app, store, out_dir) -> None:
    jid = _done_job(store, out_dir, owner=OWNER.id)
    c = TestClient(make_app(OWNER))
    assert c.get("/transcribe/file/not-a-uuid").status_code == 400
    assert c.get("/transcribe/file/not-a-uuid/download/txt").status_code == 400
    assert c.delete("/transcribe/file/not-a-uuid").status_code == 400
    assert c.get(f"/transcribe/file/{jid}/download/docx").status_code == 400


def test_missing_output_file_is_410(make_app, store, out_dir) -> None:
    jid = _done_job(store, out_dir, owner=OWNER.id)
    (out_dir / f"{jid}.srt").unlink()
    resp = TestClient(make_app(OWNER)).get(f"/transcribe/file/{jid}/download/srt")
    assert resp.status_code == 410


# ── delete ────────────────────────────────────────────────────────────────────


def test_delete_missing_job_is_204(make_app) -> None:
    resp = TestClient(make_app(OWNER)).delete(
        "/transcribe/file/00000000-0000-0000-0000-000000000000"
    )
    assert resp.status_code == 204


@pytest.mark.parametrize("running", [False, True])
def test_delete_active_job_is_202_and_sets_cancel(make_app, store, running: bool) -> None:
    jid = store.create("/tmp/src.m4a", user_id=OWNER.id)
    if running:
        assert store.set_running(jid)
    resp = TestClient(make_app(OWNER)).delete(f"/transcribe/file/{jid}")
    assert resp.status_code == 202
    job = store.get(jid)
    assert job is not None and job.cancel_requested
    assert job.status == ("running" if running else "pending")


def test_delete_job_that_finishes_during_cancel_is_removed(
    make_app, store, out_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid = store.create("/tmp/src.m4a", user_id=OWNER.id)
    assert store.set_running(jid)
    files_output.write_outputs_atomic(_transcript(jid), out_dir, jid)
    real_cancel = store.set_cancel_requested

    def finish_then_cancel(job_id: str) -> None:
        # The runner wins the race: done lands before the cancel flag.
        assert store.set_done_if_not_cancelled(job_id, "single")
        real_cancel(job_id)

    monkeypatch.setattr(store, "set_cancel_requested", finish_then_cancel)
    resp = TestClient(make_app(OWNER)).delete(f"/transcribe/file/{jid}")
    assert resp.status_code == 204
    assert store.get(jid).outputs_removed_at is not None
    assert not any((out_dir / f"{jid}.{fmt}").exists() for fmt in _FORMATS)


def test_delete_after_download_keeps_history(make_app, store, out_dir) -> None:
    """DELETE removes the outputs; the row + text stay for /me/history, hidden on /transcribe/file."""
    from wispralt.routes.me import router as me_router

    jid = _done_job(store, out_dir, owner=OWNER.id)
    assert all((out_dir / f"{jid}.{fmt}").is_file() for fmt in _FORMATS)
    app = make_app(OWNER)
    app.include_router(me_router)
    client = TestClient(app)
    assert client.get(f"/transcribe/file/{jid}/download/txt").status_code == 200
    resp = client.delete(f"/transcribe/file/{jid}")
    assert resp.status_code == 204
    assert not any((out_dir / f"{jid}.{fmt}").exists() for fmt in _FORMATS)
    job = store.get(jid)
    assert job.outputs_removed_at is not None and job.transcript_text == "hello there"
    assert client.get(f"/transcribe/file/{jid}").status_code == 404
    assert client.get(f"/transcribe/file/{jid}/download/txt").status_code == 404
    assert client.delete(f"/transcribe/file/{jid}").status_code == 204  # idempotent
    history = client.get("/me/history?kind=file")
    assert history.status_code == 200, history.text
    assert "hello there" in history.text


def test_delete_watchdog_failed_job_still_executing_is_202(make_app, store) -> None:
    jid = store.create("/tmp/src.m4a", user_id=OWNER.id)
    assert store.set_running(jid)
    store.set_failed(jid, "Phase 'transcribe' watchdog timeout (advisory)")
    runner = SimpleNamespace(active_job_id=jid)  # its executor thread has not returned
    resp = TestClient(make_app(OWNER, runner=runner)).delete(f"/transcribe/file/{jid}")
    assert resp.status_code == 202
    job = store.get(jid)
    assert job.cancel_requested and job.outputs_removed_at is None


def test_delete_keeps_row_when_outputs_cannot_be_removed(
    make_app, store, out_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid = _done_job(store, out_dir, owner=OWNER.id)
    monkeypatch.setattr(transcribe_file_routes, "remove_job_outputs", lambda d, j: False)
    resp = TestClient(make_app(OWNER)).delete(f"/transcribe/file/{jid}")
    assert resp.status_code == 500
    assert store.get(jid) is not None  # kept so a retry / the sweep can clean up


def test_delete_failed_job_removes_leftovers_from_default_root(make_app, store, out_dir) -> None:
    jid = store.create("/tmp/src.m4a", user_id=OWNER.id)
    store.set_failed(jid, "boom")  # failed jobs have no output_dir
    (out_dir / f"{jid}.txt").write_text("partial")
    resp = TestClient(make_app(OWNER)).delete(f"/transcribe/file/{jid}")
    assert resp.status_code == 204
    assert store.get(jid).outputs_removed_at is not None
    assert not (out_dir / f"{jid}.txt").exists()


# ── real runner ───────────────────────────────────────────────────────────────


def _ffmpeg_usable() -> bool:
    """ffmpeg AND ffprobe on PATH and actually runnable (a half-upgraded
    Homebrew install is on PATH but dies at dyld load)."""
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            return False
        try:
            if subprocess.run([tool, "-version"], capture_output=True, timeout=10).returncode != 0:
                return False
        except (OSError, subprocess.TimeoutExpired):
            return False
    return True


@pytest.mark.skipif(not _ffmpeg_usable(), reason="ffmpeg/ffprobe not installed or not runnable")
def test_real_staging_end_to_end(
    make_app, store, out_dir, staging_dir, plenty_of_ram, stub_whisper
) -> None:
    app = make_app(OWNER)
    with TestClient(app) as c:
        resp = c.post(
            "/transcribe/file",
            files={"file": ("tone.wav", _wav_bytes(1.0), "audio/wav")},
        )
        assert resp.status_code == 202, resp.text
        jid = resp.json()["job_id"]

        body = _wait_status(c, jid, {"done", "failed"})
        assert body["status"] == "done", body
        assert body["outputs"]["txt"] == f"/transcribe/file/{jid}/download/txt"

        txt = c.get(body["outputs"]["txt"])
        assert txt.status_code == 200
        assert "stub words" in txt.text

    job = store.get(jid)
    assert job is not None and job.user_id == OWNER.id


def test_submit_while_running_is_429_file_job_busy(
    make_app, store, out_dir, staging_dir, plenty_of_ram, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Staging stubbed (no ffmpeg needed); the pipeline blocks on an Event so
    # the first job holds the runner's single slot.
    monkeypatch.setattr(staging, "ffprobe_channel_count", lambda src: 1)
    monkeypatch.setattr(staging, "ffprobe_duration", lambda src: 1.0)

    def transcode(src: Path, *, target_channels: int, cancel_cb=None) -> Path:
        target = src.with_name(f"{src.stem}_canonical.wav")
        shutil.copyfile(src, target)
        return target

    monkeypatch.setattr(staging, "transcode_to_canonical_wav", transcode)
    release = threading.Event()
    entered = threading.Event()

    def blocking_transcribe(wav_path, output_dir, job_id, **kw) -> dict:
        entered.set()
        release.wait(10)
        t = _transcript(job_id)
        files_output.write_outputs_atomic(t, output_dir, job_id)
        return t

    monkeypatch.setattr(files_pipeline, "transcribe_file", blocking_transcribe)
    app = make_app(OWNER)
    try:
        with TestClient(app) as c:
            first = c.post("/transcribe/file", files={"file": ("a.wav", _wav_bytes(0.2), "audio/wav")})
            assert first.status_code == 202
            assert entered.wait(10)

            busy = c.post("/transcribe/file", files={"file": ("b.wav", _wav_bytes(0.2), "audio/wav")})
            assert busy.status_code == 429
            body = busy.json()
            assert body["code"] == "file_job_busy"
            assert "error" in body and body["retry_after_s"] == 60

            release.set()
            assert _wait_status(c, first.json()["job_id"], {"done", "failed"})["status"] == "done"
    finally:
        release.set()


def test_chunked_finalize_with_legacy_meeting_meta_finishes_as_file_job(
    make_app, store, out_dir, staging_dir, plenty_of_ram, stub_whisper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(staging, "ffprobe_channel_count", lambda src: 1)
    monkeypatch.setattr(staging, "ffprobe_duration", lambda src: 1.0)

    def transcode(src: Path, *, target_channels: int, cancel_cb=None) -> Path:
        target = src.with_name(f"{src.stem}_canonical.wav")
        sf.write(str(target), np.full(16_000, 0.1, dtype=np.float32), 16_000,
                 subtype="FLOAT", format="WAV")
        return target

    monkeypatch.setattr(staging, "transcode_to_canonical_wav", transcode)

    upload_id = "A" * 22
    chunked_dir = staging_dir / "chunked" / upload_id
    chunked_dir.mkdir(parents=True)
    payload = _wav_bytes(1.0)
    (chunked_dir / "chunk-0000.part").write_bytes(payload)
    (chunked_dir / "meta.json").write_text(json.dumps({
        "total_bytes": len(payload),
        "chunk_count": 1,
        "original_filename": "old.wav",
        "ext": ".wav",
        "created_at": time.time(),
        "user_id": OWNER.id,
        "client_app_version": "0.9.0",
        "mode": "meeting",  # pre-upgrade key: ignored
    }))

    with TestClient(make_app(OWNER)) as c:
        resp = c.post(f"/transcribe/file/chunked/{upload_id}/finalize")
        assert resp.status_code == 202, resp.text
        jid = resp.json()["job_id"]
        body = _wait_status(c, jid, {"done", "failed"})
        assert body["status"] == "done", body

    job = store.get(jid)
    assert job is not None
    assert job.user_id == OWNER.id
    assert job.client_app_version == "0.9.0"
    assert not chunked_dir.exists()


def test_chunked_finalize_concat_error_keeps_chunks_for_a_retry(
    make_app, staging_dir, plenty_of_ram, monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_id = "C" * 22
    chunked_dir = staging_dir / "chunked" / upload_id
    chunked_dir.mkdir(parents=True)
    for i in range(3):
        (chunked_dir / f"chunk-{i:04d}.part").write_bytes(b"x" * 10)
    (chunked_dir / "meta.json").write_text(json.dumps({
        "total_bytes": 30, "chunk_count": 3, "original_filename": "a.wav",
        "ext": ".wav", "created_at": time.time(), "user_id": OWNER.id,
    }))
    real_concat = transcribe_file_routes._concat_chunks

    def failing_concat(parts, out_path) -> None:
        real_concat(parts[:2], out_path)  # partial output, then the disk errors
        raise OSError("I/O error")

    monkeypatch.setattr(transcribe_file_routes, "_concat_chunks", failing_concat)
    resp = TestClient(make_app(OWNER)).post(f"/transcribe/file/chunked/{upload_id}/finalize")
    assert resp.status_code == 500, resp.text
    assert not (staging_dir / f"{upload_id}.wav").exists()  # partial output removed
    assert sorted(p.name for p in chunked_dir.iterdir()) == [
        "chunk-0000.part", "chunk-0001.part", "chunk-0002.part", "meta.json",
    ]

    # A retried finalize gets past the concat (the runner is not under test here).
    monkeypatch.setattr(transcribe_file_routes, "_concat_chunks", real_concat)

    async def fake_submit(path, *, user_id, client_version=None) -> str:
        assert path.read_bytes() == b"x" * 30
        return "job-1"

    runner = SimpleNamespace(submit_source_or_429=fake_submit)
    resp = TestClient(make_app(OWNER, runner)).post(
        f"/transcribe/file/chunked/{upload_id}/finalize"
    )
    assert resp.status_code == 202, resp.text
    assert not chunked_dir.exists()


@pytest.mark.parametrize("stray", ["chunk-0001.part.tmp", "chunk-0001-old.part"])
def test_chunked_finalize_with_a_gap_is_409_naming_the_index(
    make_app, staging_dir, plenty_of_ram, stray: str
) -> None:
    upload_id = "B" * 22
    chunked_dir = staging_dir / "chunked" / upload_id
    chunked_dir.mkdir(parents=True)
    for name in ("chunk-0000.part", "chunk-0002.part", stray):  # index 1 never arrived
        (chunked_dir / name).write_bytes(b"x" * 10)
    (chunked_dir / "meta.json").write_text(json.dumps({
        "total_bytes": 30,
        "chunk_count": 3,
        "original_filename": "a.wav",
        "ext": ".wav",
        "created_at": time.time(),
        "user_id": OWNER.id,
    }))
    resp = TestClient(make_app(OWNER)).post(f"/transcribe/file/chunked/{upload_id}/finalize")
    assert resp.status_code == 409, resp.text
    assert "Missing chunk 1 of 3" in resp.json()["detail"]
