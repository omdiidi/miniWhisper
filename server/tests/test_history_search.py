"""Part 6 Task 1: migration 0002 (FTS5 + triggers) and the scoped ``search_history``."""

from __future__ import annotations

import shutil
import sqlite3
import time
from typing import TYPE_CHECKING

import pytest

from wispralt import database as database_mod
from wispralt.database import Database
from wispralt.jobs.store import JobStore, fts_query

if TYPE_CHECKING:
    from pathlib import Path

    from .conftest import MakeUser


def _integrity(db: Database) -> None:
    """Raises ``sqlite3.DatabaseError`` when an index disagrees with its content."""
    with db.write() as con:
        con.execute("INSERT INTO dictations_fts(dictations_fts, rank) VALUES ('integrity-check', 1)")
        con.execute("INSERT INTO jobs_fts(jobs_fts) VALUES ('integrity-check')")
        jobs_with_text = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE transcript_text IS NOT NULL"
        ).fetchone()[0]
        assert con.execute("SELECT COUNT(*) FROM jobs_fts").fetchone()[0] == jobs_with_text


def _dictate(store: JobStore, user_id: int, text: str, created_at: float | None = None) -> str:
    store.insert_dictation(
        user_id=user_id, text=text, duration_ms=1000, client_app_version=None,
        dedup_id=None, created_at=created_at,
    )
    with store.db.read() as con:
        return str(con.execute("SELECT MAX(id) FROM dictations").fetchone()[0])


def _file_job(store: JobStore, user_id: int, text: str | None) -> str:
    jid = store.create("/tmp/in.wav", user_id=user_id)
    assert store.set_running(jid)
    assert store.set_done_if_not_cancelled(jid, "single", transcript_text=text)
    return jid


def _refs(hits) -> set[tuple[str, str]]:
    return {(h.kind, h.row_id) for h in hits}


# ── fts_query ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("roadmap", '"roadmap"*'),
        ("  quarterly   roadm ", '"quarterly" "roadm"*'),
        ('say "hi"', '"say" """hi"""*'),
        ("", None),
        ("   ", None),
        ("* ^ -", None),  # no letters or digits
        (None, None),
    ],
)
def test_fts_query_quotes_every_token(raw: str | None, expected: str | None) -> None:
    assert fts_query(raw) == expected


def test_fts_query_caps_tokens_and_length() -> None:
    assert fts_query(" ".join(f"w{i}" for i in range(30))).count('"') == 24  # 12 tokens
    assert len(fts_query("a" * 500)) <= 200 + 3


# ── migration ────────────────────────────────────────────────────────────────


def test_migration_backfills_rows_written_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    only_0001 = tmp_path / "m"
    only_0001.mkdir()
    shutil.copy(database_mod.MIGRATIONS_DIR / "0001_initial.sql", only_0001)
    real_dir = database_mod.MIGRATIONS_DIR
    monkeypatch.setattr(database_mod, "MIGRATIONS_DIR", only_0001)
    db = Database(tmp_path / "wispralt.db")
    assert db.migrate() == [1]
    with db.write() as con:
        con.execute("INSERT INTO users(id, label, created_at) VALUES (1, 'a', 0)")
        con.execute(
            "INSERT INTO dictations(user_id, created_at, text) VALUES (1, 1, 'alpha roadmap'),"
            " (1, 2, 'beta budget')"
        )
        con.execute(
            "INSERT INTO jobs(id, user_id, status, created_at, wav_path, transcript_text) VALUES"
            " ('j1', 1, 'done', 3, 'x.wav', 'meeting roadmap'),"
            " ('j2', 1, 'pending', 4, 'y.wav', NULL)"
        )
        con.execute(
            "INSERT INTO usage_events(user_id, ts, kind, status) VALUES (1, 5, 'dictate', 200)"
        )

    shutil.copy(real_dir / "0002_portal.sql", only_0001)
    assert db.migrate() == [2]
    _integrity(db)
    store = JobStore(db, tmp_path)
    assert _refs(store.search_history(1, "roadmap")) == {("dictation", "1"), ("file", "j1")}
    with db.read() as con:
        seeds = dict(con.execute("SELECT key, value FROM settings").fetchall())
        assert con.execute("SELECT key_id FROM usage_events").fetchone()[0] is None
    assert seeds["insights_enabled"] == "0" and seeds["insights_cap_30d_usd"] == "8"
    assert seeds["server_starts"] == "0" and seeds["retention_days"] == "0"
    assert db.migrate() == []  # a second migrate is a no-op


def test_fresh_db_has_fts_and_runs_tables(db: Database) -> None:
    with db.read() as con:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert {"dictations_fts", "jobs_fts", "insight_runs", "usage_key_ts"} <= names
    assert {f"{t}_fts_{op}" for t in ("dictations", "jobs") for op in ("ai", "ad", "au")} <= names


def test_missing_fts5_is_a_migration_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wispralt._errors import MigrationError

    mig = tmp_path / "m"
    mig.mkdir()
    # An unknown module fails exactly like a build without FTS5 ("no such module: fts5…").
    (mig / "0001_x.sql").write_text("CREATE VIRTUAL TABLE t USING fts5x(text);\n")
    monkeypatch.setattr(database_mod, "MIGRATIONS_DIR", mig)
    with pytest.raises(MigrationError, match="FTS5"):
        Database(tmp_path / "x.db").migrate()


# ── triggers keep the index exact ────────────────────────────────────────────


def test_every_mutation_keeps_the_index_exact(
    job_store: JobStore, make_user: MakeUser, db: Database
) -> None:
    alice, _ = make_user()
    uid = alice.id
    did = _dictate(job_store, uid, "quarterly roadmap review")
    _integrity(db)

    # Update: same text (trigger guarded by WHEN) then changed text.
    with db.write() as con:
        con.execute("UPDATE dictations SET text = text WHERE id = ?", (did,))
    _integrity(db)
    with db.write() as con:
        con.execute("UPDATE dictations SET text = 'budget planning' WHERE id = ?", (did,))
    _integrity(db)
    assert job_store.search_history(uid, "roadmap") == []
    assert _refs(job_store.search_history(uid, "budget")) == {("dictation", did)}

    # Soft delete hides, restore shows again, no reindex needed.
    assert job_store.soft_delete_history_row(uid, "dictation", did)
    assert job_store.search_history(uid, "budget") == []
    _integrity(db)
    assert job_store.restore_history_row(uid, "dictation", did)
    assert _refs(job_store.search_history(uid, "budget")) == {("dictation", did)}

    # Jobs: created without text, text set late (NULL -> text), then text -> NULL.
    jid = job_store.create("/tmp/a.wav", user_id=uid)
    _integrity(db)
    assert job_store.set_running(jid)
    assert job_store.set_done_if_not_cancelled(jid, "single", transcript_text="meeting about budget")
    _integrity(db)
    assert _refs(job_store.search_history(uid, "budget")) == {("dictation", did), ("file", jid)}
    with db.write() as con:
        con.execute("UPDATE jobs SET transcript_text = 'standup notes' WHERE id = ?", (jid,))
    _integrity(db)
    assert _refs(job_store.search_history(uid, "standup")) == {("file", jid)}
    with db.write() as con:
        con.execute("UPDATE jobs SET transcript_text = NULL WHERE id = ?", (jid,))
    _integrity(db)
    assert job_store.search_history(uid, "standup") == []

    # Hard delete.
    with db.write() as con:
        con.execute("DELETE FROM dictations WHERE id = ?", (did,))
    _integrity(db)
    assert job_store.search_history(uid, "budget") == []


def test_retention_purge_keeps_the_index_exact(
    job_store: JobStore, make_user: MakeUser, db: Database
) -> None:
    alice, _ = make_user()
    old = time.time() - 40 * 86_400
    _dictate(job_store, alice.id, "ancient roadmap", created_at=old)
    fresh = _dictate(job_store, alice.id, "fresh roadmap")
    jid = _file_job(job_store, alice.id, "old meeting roadmap")
    with db.write() as con:
        con.execute("UPDATE jobs SET finished_at = ?, created_at = ? WHERE id = ?", (old, old, jid))
    assert job_store.purge_dictations(30) == 1
    assert job_store.delete_finished_jobs(job_store.expired_jobs(30)) == 1
    _integrity(db)
    assert _refs(job_store.search_history(alice.id, "roadmap")) == {("dictation", fresh)}


def test_integrity_check_detects_a_desynced_index(
    job_store: JobStore, make_user: MakeUser, db: Database
) -> None:
    alice, _ = make_user()
    did = _dictate(job_store, alice.id, "real text")
    with db.write() as con:  # the classic bug: delete with the wrong old text
        con.execute(
            "INSERT INTO dictations_fts(dictations_fts, rowid, text) VALUES ('delete', ?, 'wrong')",
            (int(did),),
        )
    with pytest.raises(sqlite3.DatabaseError):
        _integrity(db)


# ── scope, filters, snippets ─────────────────────────────────────────────────


_ADVERSARIAL = [
    '" OR "',
    "*",
    "NEAR(secret roadmap)",
    "text:secret",
    "^secret",
    "secret OR roadmap",
    '"secret',
    "secr",
    "sécrét",  # diacritics fold to the same token
    "ROADMAP -secret",
]


@pytest.mark.parametrize("query", _ADVERSARIAL)
def test_a_user_never_sees_another_users_rows(
    job_store: JobStore, make_user: MakeUser, query: str
) -> None:
    alice, _ = make_user()
    bob, _ = make_user()
    _dictate(job_store, bob.id, "secret roadmap text: secret")
    _file_job(job_store, bob.id, "secret roadmap meeting")
    mine = _dictate(job_store, alice.id, "my secret roadmap")
    hits = job_store.search_history(alice.id, query)
    assert {h.user_id for h in hits} <= {alice.id}
    assert all(h.row_id == mine for h in hits)


def test_unicode_and_diacritics_match(job_store: JobStore, make_user: MakeUser) -> None:
    alice, _ = make_user()
    did = _dictate(job_store, alice.id, "Café résumé naïve 東京")
    assert _refs(job_store.search_history(alice.id, "cafe resume")) == {("dictation", did)}
    assert _refs(job_store.search_history(alice.id, "NAÏVE")) == {("dictation", did)}


def test_kind_and_date_filters_and_paging(
    job_store: JobStore, make_user: MakeUser, db: Database
) -> None:
    alice, _ = make_user()
    now = time.time()
    d_old = _dictate(job_store, alice.id, "roadmap one", created_at=now - 10 * 86_400)
    d_new = _dictate(job_store, alice.id, "roadmap two", created_at=now - 60)
    jid = _file_job(job_store, alice.id, "roadmap three")
    assert _refs(job_store.search_history(alice.id, "roadmap", kind="file")) == {("file", jid)}
    assert _refs(job_store.search_history(alice.id, "roadmap", kind="dictation")) == {
        ("dictation", d_old), ("dictation", d_new)
    }
    recent = job_store.search_history(alice.id, "roadmap", since=now - 86_400, until=now + 60)
    assert _refs(recent) == {("dictation", d_new), ("file", jid)}
    ordered = [h.row_id for h in job_store.search_history(alice.id, "roadmap")]
    assert ordered == [jid, d_new, d_old]  # newest first
    assert [h.row_id for h in job_store.search_history(alice.id, "roadmap", limit=1, offset=1)] == [
        d_new
    ]


def test_snippet_carries_sentinels_and_raw_text(job_store: JobStore, make_user: MakeUser) -> None:
    alice, _ = make_user()
    _dictate(job_store, alice.id, "<script>alert(1)</script> quarterly roadmap")
    [hit] = job_store.search_history(alice.id, "roadm")
    assert "\x02roadmap\x03" in hit.snippet
    assert "<script>" in hit.snippet  # raw: escaping is the renderer's job (Task 6)


def test_team_search_labels_owners_and_can_filter(
    job_store: JobStore, make_user: MakeUser
) -> None:
    alice, _ = make_user(label="alice")
    bob, _ = make_user(label="bob")
    _dictate(job_store, alice.id, "shared roadmap")
    _dictate(job_store, bob.id, "shared roadmap too")
    hits = job_store.search_history_team("roadmap")
    assert {h.user_label for h in hits} == {"alice", "bob"}
    assert {h.user_label for h in job_store.search_history_team("roadmap", user_id=bob.id)} == {
        "bob"
    }


def test_empty_or_symbol_only_query_is_no_search(job_store: JobStore, make_user: MakeUser) -> None:
    alice, _ = make_user()
    _dictate(job_store, alice.id, "anything")
    assert job_store.search_history(alice.id, "") == []
    assert job_store.search_history(alice.id, "* ^") == []


# ── merged history pagination (dictations + file jobs) ───────────────────────


@pytest.mark.parametrize("limit", [50, 7])
def test_history_pages_interleaved_legs_without_gaps_or_repeats(
    job_store: JobStore, make_user: MakeUser, limit: int
) -> None:
    """50 dictations and 50 file jobs, interleaved in time: every row exactly once, in order."""
    uid = make_user()[0].id
    base = time.time() - 10_000
    expected: list[tuple[str, str]] = []
    for i in range(100):
        at = base + i * 10
        if i % 2 == 0:
            expected.append(("dictation", _dictate(job_store, uid, f"dictation {i}", at)))
        else:
            jid = _file_job(job_store, uid, f"file {i}")
            with job_store.db.write() as con:
                con.execute("UPDATE jobs SET created_at=? WHERE id=?", (at, jid))
            expected.append(("file", jid))
    expected.reverse()  # newest first

    seen: list[tuple[str, str]] = []
    dc = jc = None
    for _ in range(200):
        rows, dc, jc = job_store.transcripts_in_range_filtered(
            uid, since_epoch=0, until_epoch=time.time() + 60,
            dict_cursor=dc, jobs_cursor=jc, limit=limit,
        )
        assert len(rows) <= limit
        seen += [(r["kind"], r["row_id"]) for r in rows]
        if dc is None and jc is None:
            break
    assert seen == expected
