"""The pinned dictation dedup contract (Part 4 Task 6, Locked Decision 7).

Runs the real ``create_app()`` routes and middleware (no lifespan) on the
per-test SQLite DB, with a stub Parakeet that counts calls and can block.
"""

from __future__ import annotations

import io
import logging
import sqlite3
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest
from starlette.testclient import TestClient

from wispralt import observability
from wispralt.auth import CSRF_COOKIE_NAME
from wispralt.dictate.streaming_session import StreamingSessionStore
from wispralt.main import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from conftest import MakeUser, Wire
    from fastapi import FastAPI

    from wispralt.database import Database

HEADER = "X-Client-Dedup-Id"


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(b"\x00\x00" * 800)
    return buf.getvalue()


class _Parakeet:
    """Counts calls; ``gate`` (a ``threading.Event``) blocks inference until set."""

    def __init__(self) -> None:
        self.calls = 0
        self.gate: threading.Event | None = None

    async def transcribe(self, audio_bytes: bytes) -> tuple[str, float]:
        import asyncio

        self.calls += 1
        n = self.calls
        while self.gate is not None and not self.gate.is_set():
            await asyncio.sleep(0.01)
        return f"text {n}", 5.0


@asynccontextmanager
async def _no_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield


@pytest.fixture
def app(wire: Wire) -> FastAPI:
    application = wire(create_app())
    application.router.lifespan_context = _no_lifespan  # no models, no ffmpeg
    application.state.parakeet_service = _Parakeet()
    application.state.parakeet_last_inference_at = None
    application.state.streaming_sessions = StreamingSessionStore(
        max_active=4, max_queue_depth=12, ttl_s=900, finalize_timeout_s=15
    )
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # Entered once: every request runs on ONE event loop (the dedup claim is an asyncio.Lock).
    with TestClient(app) as c:
        yield c


@pytest.fixture
def users(make_user: MakeUser) -> tuple[dict[str, str], dict[str, str]]:
    (_, a), (_, b) = make_user(), make_user()
    return {"Authorization": f"Bearer {a}"}, {"Authorization": f"Bearer {b}"}


def _whole(client: TestClient, auth: dict[str, str], dedup: str | None) -> Any:
    headers = {**auth, **({HEADER: dedup} if dedup else {})}
    return client.post(
        "/transcribe/dictate", files={"file": ("a.wav", _wav(), "audio/wav")}, headers=headers
    )


def _chunk(client: TestClient, auth: dict[str, str], sid: str, dedup: str, index: int = 0) -> Any:
    return client.post(
        f"/transcribe/dictate/stream/{sid}/chunk/{index}",
        files={"file": ("c.wav", _wav(), "audio/wav")},
        headers={**auth, HEADER: dedup},
    )


def _finalize(
    client: TestClient,
    auth: dict[str, str],
    sid: str,
    *,
    header: str | None = None,
    form: str | None = None,
) -> Any:
    data = {"speech_started_at": "1700000000"}
    if form is not None:
        data["client_dedup_id"] = form
    return client.post(
        f"/transcribe/dictate/stream/{sid}/finalize",
        files={"file": ("t.wav", _wav(), "audio/wav")},
        data=data,
        headers={**auth, **({HEADER: header} if header else {})},
    )


def _rows(db: Database, sql: str = "SELECT user_id, text, client_dedup_id FROM dictations") -> list:
    with db.read() as con:
        return [tuple(r) for r in con.execute(sql)]


def _usage(db: Database) -> int:
    return _rows(db, "SELECT COUNT(*) FROM usage_events")[0][0]


def _wait(cond: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def _refcount(app: FastAPI, dedup: str) -> int:
    locks = getattr(app.state, "dedup_locks", None)
    entry = locks._m.get(dedup) if locks else None
    return entry[1] if entry else 0


def _new() -> str:
    return str(uuid.uuid4())


# 1 + 8
def test_whole_file_replay_returns_stored_text_once(
    client: TestClient, app: FastAPI, users: tuple, db: Database
) -> None:
    a, _ = users
    dedup = _new()
    first = _whole(client, a, dedup)
    assert first.status_code == 200
    assert first.json()["saved"] is True
    assert first.json()["replayed"] is False
    usage_after_first = _usage(db)
    second = _whole(client, a, dedup)
    assert second.status_code == 200
    assert second.json()["text"] == first.json()["text"] == "text 1"
    assert second.json()["replayed"] is True and second.json()["saved"] is True
    assert app.state.parakeet_service.calls == 1
    assert len(_rows(db)) == 1
    assert usage_after_first == 1 and _usage(db) == 1  # replay writes no usage row


# 2
def test_other_users_id_is_409_dedup_conflict(
    client: TestClient, app: FastAPI, users: tuple
) -> None:
    a, b = users
    dedup = _new()
    assert _whole(client, a, dedup).status_code == 200
    resp = _whole(client, b, dedup)
    assert resp.status_code == 409
    assert resp.json() == {"error": "dedup_conflict"}
    assert app.state.parakeet_service.calls == 1


# 2b
def test_concurrent_cross_user_runs_inference_once(
    client: TestClient, app: FastAPI, users: tuple, db: Database
) -> None:
    a, b = users
    dedup = _new()
    gate = app.state.parakeet_service.gate = threading.Event()
    try:
        with ThreadPoolExecutor(2) as pool:
            fa = pool.submit(_whole, client, a, dedup)
            _wait(lambda: app.state.parakeet_service.calls == 1)
            fb = pool.submit(_whole, client, b, dedup)
            _wait(lambda: _refcount(app, dedup) == 2)
            gate.set()
            ra, rb = fa.result(), fb.result()
    finally:
        gate.set()
    assert app.state.parakeet_service.calls == 1
    assert ra.status_code == 200
    assert rb.status_code == 409 and rb.json() == {"error": "dedup_conflict"}
    rows = _rows(db)
    assert len(rows) == 1 and rows[0][2] == dedup
    assert _refcount(app, dedup) == 0  # entry released


# 9
def test_concurrent_same_user_runs_inference_once(
    client: TestClient, app: FastAPI, users: tuple, db: Database
) -> None:
    a, _ = users
    dedup = _new()
    gate = app.state.parakeet_service.gate = threading.Event()
    try:
        with ThreadPoolExecutor(2) as pool:
            f1 = pool.submit(_whole, client, a, dedup)
            _wait(lambda: app.state.parakeet_service.calls == 1)
            f2 = pool.submit(_whole, client, a, dedup)
            _wait(lambda: _refcount(app, dedup) == 2)
            gate.set()
            r1, r2 = f1.result(), f2.result()
    finally:
        gate.set()
    assert app.state.parakeet_service.calls == 1
    assert len(_rows(db)) == 1
    assert r1.json()["text"] == r2.json()["text"]
    assert r1.json()["replayed"] is False and r2.json()["replayed"] is True


# 3
def test_finalize_replays_after_session_expired(
    client: TestClient, app: FastAPI, users: tuple
) -> None:
    a, _ = users
    sid, dedup = _new(), _new()
    assert _chunk(client, a, sid, dedup).status_code == 202
    first = _finalize(client, a, sid, header=dedup, form=dedup)
    assert first.status_code == 200, first.text
    assert first.json()["saved"] is True
    app.state.streaming_sessions._sessions.clear()  # expired / swept
    again = _finalize(client, a, sid, header=dedup)
    assert again.status_code == 200, again.text
    assert again.json()["text"] == first.json()["text"]
    assert again.json()["replayed"] is True


# 4
@pytest.mark.parametrize(
    ("use_header", "use_form", "differ", "status"),
    [
        (True, False, False, 200),
        (False, True, False, 200),
        (True, True, False, 200),
        (True, True, True, 422),
        (False, False, False, 422),
    ],
)
def test_finalize_header_and_form_rules(
    client: TestClient,
    users: tuple,
    use_header: bool,
    use_form: bool,
    differ: bool,
    status: int,
) -> None:
    a, _ = users
    sid, dedup = _new(), _new()
    assert _chunk(client, a, sid, dedup).status_code == 202
    resp = _finalize(
        client,
        a,
        sid,
        header=dedup if use_header else None,
        form=(_new() if differ else dedup) if use_form else None,
    )
    assert resp.status_code == status, resp.text


# 5
def test_uppercase_is_stored_lowercase_and_malformed_is_422(
    client: TestClient, users: tuple, db: Database
) -> None:
    a, _ = users
    dedup = _new()
    assert _whole(client, a, dedup.upper()).status_code == 200
    assert _rows(db)[0][2] == dedup
    assert _whole(client, a, "not-a-uuid").status_code == 422
    assert _whole(client, a, dedup[:-1] + "g").status_code == 422


# 6
def test_chunk_for_stored_id_is_finalized_without_work(
    client: TestClient, app: FastAPI, users: tuple
) -> None:
    a, _ = users
    dedup = _new()
    assert _whole(client, a, dedup).status_code == 200
    resp = _chunk(client, a, _new(), dedup, index=3)
    assert resp.status_code == 202
    assert resp.json() == {"received_index": 3, "queue_depth": 0, "finalized": True}
    assert app.state.parakeet_service.calls == 1
    assert app.state.streaming_sessions._sessions == {}


def test_chunk_requires_the_header(client: TestClient, users: tuple) -> None:
    a, _ = users
    resp = client.post(
        f"/transcribe/dictate/stream/{_new()}/chunk/0",
        files={"file": ("c.wav", _wav(), "audio/wav")},
        headers=a,
    )
    assert resp.status_code == 422


# 7
def test_chunk_with_another_id_on_open_session_is_409(client: TestClient, users: tuple) -> None:
    a, _ = users
    sid = _new()
    assert _chunk(client, a, sid, _new(), index=0).status_code == 202
    resp = _chunk(client, a, sid, _new(), index=1)
    assert resp.status_code == 409
    assert "error" not in resp.json()


# 10
def test_deleted_dictation_is_410_and_closed_session_keeps_detail(
    client: TestClient, app: FastAPI, users: tuple, db: Database
) -> None:
    a, _ = users
    dedup = _new()
    assert _whole(client, a, dedup).status_code == 200
    row_id = _rows(db, "SELECT id FROM dictations")[0][0]
    client.cookies.set(CSRF_COOKIE_NAME, "tok")
    deleted = client.request(
        "DELETE", f"/me/history/dictation/{row_id}", data={"csrf_token": "tok"}, headers=a
    )
    assert deleted.status_code == 200, deleted.text
    resp = _whole(client, a, dedup)
    assert resp.status_code == 410
    assert resp.json() == {"error": "dictation_deleted"}
    assert app.state.parakeet_service.calls == 1
    assert len(_rows(db)) == 1

    sid, other = _new(), _new()
    assert _chunk(client, a, sid, other).status_code == 202
    app.state.streaming_sessions._sessions[sid].status = "aborted"
    closed = _finalize(client, a, sid, header=other)
    assert closed.status_code == 410
    assert "detail" in closed.json() and "error" not in closed.json()


# 11
def test_save_failure_still_returns_text_with_saved_false(
    client: TestClient,
    app: FastAPI,
    users: tuple,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    a, _ = users

    def _boom(**_row: Any) -> None:
        raise sqlite3.OperationalError("database is locked: secret text 1")

    monkeypatch.setattr(app.state.job_store, "insert_dictation", _boom)
    before = observability.dictation_save_failures_total.value
    with caplog.at_level(logging.ERROR):
        resp = _whole(client, a, _new())
    assert resp.status_code == 200
    assert resp.json()["text"] == "text 1" and resp.json()["saved"] is False
    assert observability.dictation_save_failures_total.value == before + 1
    failures = [r for r in caplog.records if "dictation save failed" in r.getMessage()]
    assert [r.getMessage() for r in failures] == ["dictation save failed: OperationalError"]
    assert not any("text 1" in r.getMessage() for r in caplog.records)
    assert failures[0].exc_info is None


def test_whole_file_without_header_still_saves(
    client: TestClient, users: tuple, db: Database
) -> None:
    a, _ = users
    for _ in range(2):
        assert _whole(client, a, None).json()["saved"] is True
    assert [r[2] for r in _rows(db)] == [None, None]



def test_blank_text_is_never_stored_whole_or_streamed(
    client: TestClient, app: FastAPI, users: tuple, db: Database
) -> None:
    """Both dictation routes skip saving blank text and report ``saved: true``."""
    a, _ = users

    async def silence(audio_bytes: bytes) -> tuple[str, float]:
        return "  ", 5.0

    app.state.parakeet_service.transcribe = silence
    whole = _whole(client, a, _new())
    assert whole.status_code == 200, whole.text
    assert whole.json()["saved"] is True

    sid, dedup = _new(), _new()
    assert _chunk(client, a, sid, dedup).status_code == 202
    streamed = _finalize(client, a, sid, header=dedup)
    assert streamed.status_code == 200, streamed.text
    assert streamed.json()["saved"] is True and not streamed.json()["text"].strip()
    assert _rows(db) == []


def test_retried_finalize_of_a_blank_stream_replays_it(
    client: TestClient, app: FastAPI, users: tuple, db: Database
) -> None:
    """Blank text is not stored, but a retried finalize still replays it (as a
    retried non-blank finalize replays its stored row) instead of a 410."""
    a, _ = users

    async def silence(audio_bytes: bytes) -> tuple[str, float]:
        return "  ", 5.0

    app.state.parakeet_service.transcribe = silence
    sid, dedup = _new(), _new()
    assert _chunk(client, a, sid, dedup).status_code == 202
    first = _finalize(client, a, sid, header=dedup)
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False
    again = _finalize(client, a, sid, header=dedup)
    assert again.status_code == 200, again.text
    assert again.json()["text"] == first.json()["text"]
    assert again.json()["replayed"] is True and again.json()["saved"] is True
    assert _rows(db) == []
