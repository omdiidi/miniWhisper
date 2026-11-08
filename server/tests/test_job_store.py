"""
test_job_store.py — ``JobStore`` on the shared SQLite DB: transitions, recovery,
paths, FKs, insights scope, per-user stats and dictation counts.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import numpy as np
import pytest
import soundfile as sf

from wispralt.jobs.runner import FileJobRunner
from wispralt.jobs.store import MAX_ATTEMPTS, JobStore

if TYPE_CHECKING:
    from .conftest import MakeUser

DAY = 86_400.0


@pytest.fixture
def uid(make_user: MakeUser) -> int:
    return make_user()[0].id


def _wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(1600, dtype=np.float32), 16_000, subtype="FLOAT", format="WAV")
    return path


def _staged(job_store: JobStore, name: str = "in.wav") -> Path:
    return _wav(job_store.data_dir / "staging" / name)


def _stored_path(job_store: JobStore, jid: str) -> str:
    with job_store.db.read() as con:
        return con.execute("SELECT wav_path FROM jobs WHERE id=?", (jid,)).fetchone()[0]


# ── transitions ───────────────────────────────────────────────────────────────


def test_transitions_and_cancel_rules(job_store: JobStore, uid: int) -> None:
    jid = job_store.create("/tmp/a.m4a", user_id=uid)
    assert job_store.get_status(jid) == "pending"
    job_store.set_cancel_requested(jid)
    assert job_store.set_running(jid) is False  # cancel before start: never runs

    jid = job_store.create("/tmp/b.m4a", user_id=uid)
    assert job_store.set_running(jid) and not job_store.set_running(jid)
    job_store.set_cancel_requested(jid)
    assert job_store.set_done_if_not_cancelled(jid, "single", transcript_text="x y") is False
    job_store.set_failed(jid, "cancelled by user")
    assert job_store.get(jid).status == "failed"

    jid = job_store.create("/tmp/c.m4a", user_id=uid)
    assert job_store.set_running(jid)
    assert job_store.set_done_if_not_cancelled(jid, "single", transcript_text="x y z")
    job = job_store.get(jid)
    assert (job.status, job.word_count, job.attempts) == ("done", 3, 1)
    assert job_store.count_24h("done") == 1


def test_requeue_interrupted_refunds_the_attempt(job_store: JobStore, uid: int) -> None:
    jid = job_store.create("/tmp/a.m4a", user_id=uid)
    assert job_store.set_running(jid)
    job_store.update_phase(jid, "transcribe")
    job_store.requeue_interrupted(jid)
    job = job_store.get(jid)
    assert (job.status, job.attempts, job.phase, job.started_at) == ("pending", 0, None, None)
    job_store.requeue_interrupted(jid)  # guarded: a pending row is untouched
    assert job_store.get(jid).attempts == 0


def test_unknown_user_is_integrity_error(job_store: JobStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        job_store.create("/tmp/a.m4a", user_id=999)
    with pytest.raises(sqlite3.IntegrityError):
        job_store.insert_dictation(999, "hi", None, None)


def test_wav_path_relative_in_db_absolute_out(job_store: JobStore, uid: int) -> None:
    wav = _staged(job_store)
    jid = job_store.create(wav, user_id=uid)
    assert _stored_path(job_store, jid) == "staging/in.wav"
    assert job_store.get(jid).wav_path == wav
    canonical = _staged(job_store, "in_canonical.wav")
    job_store.update_after_transcode(jid, wav_path=canonical)
    assert _stored_path(job_store, jid) == "staging/in_canonical.wav"
    assert job_store.list_pending_jobs() == [(jid, canonical)]


# ── recovery ──────────────────────────────────────────────────────────────────


def _running(job_store: JobStore, uid: int, wav: Path, *, attempts: int = 1) -> str:
    jid = job_store.create(wav, user_id=uid)
    assert job_store.set_running(jid)
    with job_store.db.write() as con:
        con.execute("UPDATE jobs SET attempts=?, phase='transcribe' WHERE id=?", (attempts, jid))
    return jid


async def test_recover_orphans_policy(job_store: JobStore, uid: int) -> None:
    alive = _running(job_store, uid, _staged(job_store, "alive.wav"))
    missing = _running(job_store, uid, job_store.data_dir / "staging" / "gone.wav")
    spent = _running(job_store, uid, _staged(job_store, "spent.wav"), attempts=MAX_ATTEMPTS)
    cancelled = _running(job_store, uid, _staged(job_store, "cancel.wav"))
    job_store.set_cancel_requested(cancelled)
    truncated_wav = job_store.data_dir / "staging" / "trunc.wav"
    truncated_wav.write_bytes(b"RIFF\x00\x00")
    truncated = job_store.create(truncated_wav, user_id=uid)

    result = job_store.recover_orphans()

    assert result["requeue"] == [alive]
    assert sorted(result["failed"]) == sorted([missing, spent, cancelled, truncated])
    errors = {jid: job_store.get(jid).error for jid in result["failed"]}
    assert errors[missing] == "staging file missing after restart"
    assert errors[spent] == "max retries exceeded"
    assert errors[cancelled] == "cancelled by user"
    assert errors[truncated].startswith("WAV truncated")
    assert not truncated_wav.exists()
    job = job_store.get(alive)
    assert (job.status, job.phase, job.attempts) == ("pending", None, 1)  # a crash spends it

    spawned: list[tuple[str, Path]] = []
    runner = FileJobRunner(job_store, SimpleNamespace(shutting_down=False))
    runner._spawn = lambda jid, path: spawned.append((jid, path))  # type: ignore[method-assign]
    await runner.reenqueue_pending()
    assert spawned == [(alive, job_store.data_dir / "staging" / "alive.wav")]
    assert spawned[0][1].is_absolute() and spawned[0][1].exists()


# ── dictations ────────────────────────────────────────────────────────────────


def test_insert_dictation_while_a_job_is_running(job_store: JobStore, uid: int) -> None:
    jid = job_store.create("/tmp/a.m4a", user_id=uid)
    assert job_store.set_running(jid)
    stop = threading.Event()

    def churn() -> None:
        while not stop.is_set():
            job_store.update_phase(jid, "transcribe")

    worker = threading.Thread(target=churn)
    worker.start()
    try:
        ids = [job_store.insert_dictation(uid, f"text {i}", 10, "1.0") for i in range(20)]
    finally:
        stop.set()
        worker.join()
    assert len(set(ids)) == 20
    assert job_store.get_status(jid) == "running"


def test_dictation_counts_and_most_recent(job_store: JobStore, uid: int) -> None:
    now = time.time()
    for age in (60, 2 * DAY, 10 * DAY, 40 * DAY):
        job_store.insert_streaming_dictation_idempotent(
            user_id=uid,
            text="hello",
            duration_ms=None,
            client_app_version=None,
            client_dedup_id=f"00000000-0000-4000-8000-{int(age):012d}",
            dictated_at=now - age,
        )
    deleted = job_store.insert_dictation(uid, "gone", None, None)
    job_store.soft_delete_history_row(uid, "dictation", str(deleted))
    assert job_store.dictation_counts(now) == {"24h": 1, "7d": 2, "30d": 3}
    assert job_store.get_most_recent_dictation(uid)["text"] == "hello"


# ── insights + stats ──────────────────────────────────────────────────────────


def test_insight_upserts_keep_one_row_per_scope(job_store: JobStore, uid: int) -> None:
    for n in (1, 2):
        for user_id in (None, uid):
            job_store.upsert_weekly_insight(
                iso_year=2026,
                iso_week=44,
                user_id=user_id,
                insight={"n": n, "filler_word_count": n},
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.5,
                model="m",
            )
    with job_store.db.read() as con:
        keys = [r[0] for r in con.execute("SELECT scope_key FROM weekly_insights ORDER BY 1")]
    assert keys == ["team", f"user:{uid}"]
    assert job_store.get_weekly_insight_team(2026, 44)["n"] == 2
    assert job_store.get_weekly_insight_person(uid, 2026, 44)["n"] == 2
    assert job_store.list_filler_counts_for_week(2026, 44) == [(uid, 2)]
    assert job_store.rolling_insights_cost_usd(30) == 1.0


def test_stats_by_user_matches_per_user_stats(job_store: JobStore, make_user: MakeUser) -> None:
    a, b, idle = (make_user()[0].id for _ in range(3))
    for user_id, n in ((a, 3), (b, 1)):
        for i in range(n):
            job_store.insert_dictation(user_id, "one two three", 100 + i, None)
    jid = job_store.create("/tmp/f.m4a", user_id=b)
    assert job_store.set_running(jid)
    assert job_store.set_done_if_not_cancelled(jid, "single", transcript_text="four five")

    since = time.time() - DAY
    grouped = job_store.stats_by_user(since)
    assert set(grouped) == {a, b}
    for user_id in (a, b):
        assert grouped[user_id] == job_store.compute_user_stats(user_id, since_epoch=since)
    assert grouped[b]["file_count"] == 1 and grouped[b]["total_words"] == 5
    assert job_store.compute_user_stats(idle, since_epoch=since)["dictation_count"] == 0

    rows = job_store.transcripts_in_range(b, since_epoch=since, until_epoch=time.time() + 1)
    assert sorted(r["kind"] for r in rows) == ["dictation", "file"]
