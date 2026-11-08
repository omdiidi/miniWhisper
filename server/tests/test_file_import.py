"""Part 7 Task 4: transcript-file import (TXT/JSON/SRT/VTT → jobs rows, copied outputs)."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import TYPE_CHECKING

import pytest

from wispralt._errors import OwnerMappingRequired
from wispralt.importer import files as fi

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import MakeUser

    from wispralt.database import Database
    from wispralt.jobs.store import JobStore

CREATED = "2026-03-02T09:30:00+00:00"
SRT = "1\n00:00:00,000 --> 00:00:01,000\nhello there\n\n2\n00:00:01,000 --> 00:00:02,000\nsecond cue\n"
VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nvtt words here\n"


def _tree(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(d.rglob("*")):
        st = f.lstat()
        h.update(str(f.relative_to(d)).encode() + str(st.st_mtime_ns).encode())
        if f.is_file():
            h.update(f.read_bytes())
    return h.hexdigest()


@pytest.fixture
def owner(make_user: MakeUser) -> int:
    return make_user(role="admin")[0].id


@pytest.fixture
def src(tmp_path: Path) -> Path:
    d = tmp_path / "meetings"
    (d / "sub").mkdir(parents=True)
    doc = {"job_id": "job-a", "created_at": CREATED, "duration_s": 12.5,
           "segments": [{"text": " Hello "}, {"text": "world. "}]}  # fmt: skip
    (d / "a.json").write_text(json.dumps(doc))
    (d / "a.txt").write_text("Hello\nworld.")
    (d / "a.srt").write_text(SRT)
    (d / "a.vtt").write_text(VTT)
    (d / "b.txt").write_text("﻿just text from b")
    os.utime(d / "b.txt", (1_700_000_000, 1_700_000_000))
    (d / "c.vtt").write_text(VTT)
    (d / "d.txt").write_text("")
    (d / "sub" / "b.txt").write_text("﻿just text from b")  # same bytes, other folder
    (d / ".hidden.txt").write_text("skip me")
    (d / "link.txt").symlink_to(d / "b.txt")
    return d


def _jobs(db: Database) -> list[dict]:
    with db.read() as con:
        return [dict(r) for r in con.execute("SELECT * FROM jobs ORDER BY created_at")]


def test_import_groups_parses_and_copies(
    db: Database, src: Path, owner: int, _data_dir: Path
) -> None:
    before = _tree(src)
    outputs = _data_dir / "outputs"
    report = fi.import_files(db, [src], owner_id=owner, outputs_dir=outputs)
    c = report.tables["files"]
    assert (c["source_count"], c["new"], c["duplicate"], c["malformed"]) == (5, 3, 1, 1)
    assert c["source_count"] == c["new"] + c["duplicate"] + c["malformed"] + c["errors"]
    rows = {r["source_id"]: r for r in _jobs(db)}
    a = rows["job-a"]
    assert a["transcript_text"] == "Hello world." and a["estimated"] == 0
    assert a["created_at"] == a["finished_at"] == datetime.fromisoformat(CREATED).timestamp()
    assert (a["status"], a["mode"], a["wav_path"], a["audio_duration_s"]) == (
        "done",
        "file",
        "",
        12.5,
    )
    assert a["user_id"] == owner and a["word_count"] == 2
    assert sorted(p.name for p in outputs.iterdir() if p.name.startswith(a["id"])) == [
        f"{a['id']}{e}" for e in (".json", ".srt", ".txt", ".vtt")
    ]
    b = next(r for r in rows.values() if r["transcript_text"] == "just text from b")
    assert b["estimated"] == 1 and b["created_at"] == 1_700_000_000
    assert b["source_id"].endswith(f":u{owner}")
    c_row = next(r for r in rows.values() if r["transcript_text"] == "vtt words here")
    assert "-->" not in c_row["transcript_text"]
    assert not list(outputs.glob(".import-*"))  # no temp left behind
    assert any("d" in p and "malformed" in p for p in report.problems)
    assert _tree(src) == before  # sources untouched
    assert len(rows) == 3


def test_rerun_is_idempotent(db: Database, src: Path, owner: int, _data_dir: Path) -> None:
    outputs = _data_dir / "outputs"
    fi.import_files(db, [src], owner_id=owner, outputs_dir=outputs)
    n_files = len(list(outputs.iterdir()))
    again = fi.import_files(db, [src], owner_id=owner, outputs_dir=outputs)
    assert again.tables["files"]["new"] == 0 and again.tables["files"]["duplicate"] == 4
    assert len(list(outputs.iterdir())) == n_files and len(_jobs(db)) == 3


def test_dry_run_copies_and_writes_nothing(
    db: Database, src: Path, owner: int, _data_dir: Path
) -> None:
    outputs = _data_dir / "outputs"
    report = fi.import_files(db, [src], owner_id=owner, outputs_dir=outputs, dry_run=True)
    assert report.tables["files"]["new"] == 3 and report.tables["files"]["duplicate"] == 1
    assert not outputs.exists() or not list(outputs.iterdir())
    assert _jobs(db) == []


def test_invalid_json_falls_back_to_txt(
    db: Database, tmp_path: Path, owner: int, _data_dir: Path
) -> None:
    d = tmp_path / "x"
    d.mkdir()
    (d / "e.json").write_text("{not json")
    (d / "e.txt").write_text("fallback text")
    report = fi.import_files(db, [d], owner_id=owner, outputs_dir=_data_dir / "outputs")
    (row,) = _jobs(db)
    assert row["transcript_text"] == "fallback text" and row["estimated"] == 1
    assert any("e.json: unreadable" in n for n in report.notes)


def test_non_utf8_is_malformed(db: Database, tmp_path: Path, owner: int, _data_dir: Path) -> None:
    d = tmp_path / "y"
    d.mkdir()
    (d / "f.txt").write_bytes(b"\xff\xfe\x00bad")
    report = fi.import_files(db, [d], owner_id=owner, outputs_dir=_data_dir / "outputs")
    assert report.tables["files"]["malformed"] == 1 and _jobs(db) == []


def test_copy_failure_rolls_back_that_row(
    db: Database, src: Path, owner: int, _data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = fi.shutil.copy2

    def flaky(a, b):  # noqa: ANN001, ANN202
        if str(a).endswith("c.vtt"):
            raise OSError("disk full")
        return real(a, b)

    monkeypatch.setattr(fi.shutil, "copy2", flaky)
    report = fi.import_files(db, [src], owner_id=owner, outputs_dir=_data_dir / "outputs")
    assert report.tables["files"]["errors"] == 1 and report.tables["files"]["new"] == 2
    assert not any(r["transcript_text"] == "vtt words here" for r in _jobs(db))
    assert not list((_data_dir / "outputs").glob(".import-*"))


def test_partial_copy_is_removed(
    db: Database, src: Path, owner: int, _data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy that dies midway (its ``.part`` already on disk) leaves no ``.part`` behind."""
    real = fi.shutil.copy2

    def dies_midway(a, b):  # noqa: ANN001, ANN202
        if str(a).endswith("c.vtt"):
            with open(b, "wb") as f:  # the half-written temp a real copy2 leaves
                f.write(b"half")
            raise OSError("disk full")
        return real(a, b)

    monkeypatch.setattr(fi.shutil, "copy2", dies_midway)
    report = fi.import_files(db, [src], owner_id=owner, outputs_dir=_data_dir / "outputs")
    assert report.tables["files"]["errors"] == 1
    assert not list((_data_dir / "outputs").glob(".import-*"))


def test_unknown_owner_refused(db: Database, src: Path, _data_dir: Path) -> None:
    with pytest.raises(OwnerMappingRequired):
        fi.import_files(db, [src], owner_id=999, outputs_dir=_data_dir / "outputs")


def test_imported_job_shows_in_history(
    db: Database, src: Path, owner: int, _data_dir: Path, job_store: JobStore
) -> None:
    fi.import_files(db, [src], owner_id=owner, outputs_dir=_data_dir / "outputs")
    rows = job_store.transcripts_in_range(owner, since_epoch=0, until_epoch=2e9)
    assert {r["kind"] for r in rows} == {"file"} and len(rows) == 3


def test_outputs_are_in_place_before_the_row_commits(
    db: Database, src: Path, owner: int, _data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix: a committed (checkpointed) row always has its outputs; never rename after."""
    outputs = _data_dir / "outputs"
    real = fi.ImportRuns.checkpoint
    seen: list[set[str]] = []

    def check(con, run_id, checkpoint, counts):  # noqa: ANN001, ANN202
        ids = {r[0] for r in con.execute("SELECT id FROM jobs WHERE source = 'files'")}
        assert all(list(outputs.glob(f"{i}.*")) for i in ids)  # in the same transaction
        seen.append(ids)
        return real(con, run_id, checkpoint, counts)

    monkeypatch.setattr(fi.ImportRuns, "checkpoint", staticmethod(check))
    fi.import_files(db, [src], owner_id=owner, outputs_dir=outputs)
    assert seen and len(seen[-1]) == 3


def test_rename_failure_keeps_that_row_out_and_leaves_nothing(
    db: Database, src: Path, owner: int, _data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = _data_dir / "outputs"
    real = fi.os.rename

    def flaky(a, b):  # noqa: ANN001, ANN202
        if str(b).endswith(".srt"):  # only group a has an .srt: fail it mid-way
            raise OSError("rename failed")
        return real(a, b)

    monkeypatch.setattr(fi.os, "rename", flaky)
    report = fi.import_files(db, [src], owner_id=owner, outputs_dir=outputs)
    assert report.tables["files"]["errors"] == 1 and report.tables["files"]["new"] == 2
    assert "job-a" not in {r["source_id"] for r in _jobs(db)}
    ids = {r["id"] for r in _jobs(db)}
    assert {p.name.split(".")[0] for p in outputs.iterdir()} == ids  # no orphan, no temp
    monkeypatch.setattr(fi.os, "rename", real)
    again = fi.import_files(db, [src], owner_id=owner, outputs_dir=outputs)
    assert again.tables["files"]["new"] == 1  # the retry imports it, with its outputs
    a = next(r for r in _jobs(db) if r["source_id"] == "job-a")
    assert len(list(outputs.glob(f"{a['id']}.*"))) == 4


def test_failed_commit_removes_placed_outputs(
    db: Database, src: Path, owner: int, _data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = _data_dir / "outputs"

    def boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("db gone")

    monkeypatch.setattr(fi, "insert_rows", boom)
    with pytest.raises(RuntimeError):
        fi.import_files(db, [src], owner_id=owner, outputs_dir=outputs)
    assert list(outputs.iterdir()) == [] and _jobs(db) == []
