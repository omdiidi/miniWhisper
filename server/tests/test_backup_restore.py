"""Offline backup / restore behind ``<data_dir>.lock`` (Part 4 Task 7, Locked Decision 9)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
from collections import namedtuple
from contextlib import closing
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from wispralt._errors import (
    BackupRefused,
    InsufficientSpace,
    RestoreRefused,
    ServerRunning,
)
from wispralt.database import _migration_files
from wispralt.ops import backup as bk

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from conftest import MakeUser

    from wispralt.jobs.store import JobStore


@pytest.fixture(autouse=True)
def _umask() -> Iterator[None]:
    old = os.umask(0o022)  # modes must not depend on the caller's umask
    yield
    os.umask(old)


@pytest.fixture
def data(job_store: JobStore, make_user: MakeUser) -> Path:
    """A populated data_dir: users, a dictation, a done job + outputs, a pending job + staged WAV."""
    root = job_store.data_dir
    user, _ = make_user()
    job_store.insert_dictation(
        user_id=user.id, text="before backup", duration_ms=1, client_app_version=None, dedup_id=None
    )
    (root / "outputs").mkdir()
    (root / "staging").mkdir()
    done = job_store.create(root / "staging" / "done.wav", user_id=user.id)
    assert job_store.set_running(done)
    assert job_store.set_done_if_not_cancelled(done, "single", transcript_text="hello")
    (root / "outputs" / f"{done}.txt").write_text("hello")
    wav = root / "staging" / "pending.wav"
    wav.write_bytes(b"RIFF" + b"\0" * 64)
    job_store.pending_id = job_store.create(wav, user_id=user.id)  # type: ignore[attr-defined]
    job_store.uid = user.id  # type: ignore[attr-defined]
    return root


def _add(job_store: JobStore, text: str = "after backup") -> None:
    job_store.insert_dictation(
        user_id=job_store.uid,  # type: ignore[attr-defined]
        text=text,
        duration_ms=1,
        client_app_version=None,
        dedup_id=None,
    )


def _texts(job_store: JobStore) -> list[str]:
    with job_store.db.read() as con:
        return [r[0] for r in con.execute("SELECT text FROM dictations ORDER BY id")]


def _tree(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _modes(root: Path) -> set[tuple[str, int]]:
    """{("d"|"f", mode)} for everything under *root* (and root itself)."""
    out = {("d", stat.S_IMODE(root.stat().st_mode))}
    for p in root.rglob("*"):
        out.add(("d" if p.is_dir() else "f", stat.S_IMODE(p.stat().st_mode)))
    return out


@pytest.fixture
def backup_dir(data: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return bk.backup(data, tmp_path_factory.mktemp("bk") / "b1")


# ── server lock ───────────────────────────────────────────────────────────────


def _running_app(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from wispralt import main
    from wispralt.dictate.parakeet import ParakeetService

    monkeypatch.setattr(main, "configure_file_logging", lambda *a: True)
    monkeypatch.setattr(ParakeetService, "load", lambda self: None)
    monkeypatch.setattr(main.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(main.subprocess, "check_output", lambda *a, **kw: "ffmpeg stub\n")
    return TestClient(main.create_app())


def test_a_running_server_blocks_backup_restore_and_a_second_server(
    data: Path,
    backup_dir: Path,
    job_store: JobStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    dest = tmp_path_factory.mktemp("bk2") / "b2"
    before = sorted(p.name for p in data.parent.iterdir())
    with _running_app(monkeypatch):  # idle: lifespan up, no request in flight
        with pytest.raises(ServerRunning):
            bk.backup(data, dest)
        with pytest.raises(ServerRunning):
            bk.restore(backup_dir, data)
        with pytest.raises(ServerRunning), _running_app(monkeypatch):
            pass
        assert not dest.exists()
        assert sorted(p.name for p in data.parent.iterdir()) == before  # nothing touched

    lock = bk.lock_path(data)
    assert lock == data.parent / f"{data.name}.lock" and lock.exists()
    assert bk.backup(data, dest) == dest
    aside = bk.restore(dest, data)
    assert aside is not None and aside.exists()
    assert lock.exists() and bk.lock_path(data) == lock  # same path, not moved by the swap
    # A server started right after restore takes the lock.
    with _running_app(monkeypatch), pytest.raises(ServerRunning):
        bk.backup(data, tmp_path_factory.mktemp("bk3") / "b3")


def test_server_lock_is_exclusive_across_descriptors(tmp_path: Path) -> None:
    with bk.server_lock(tmp_path):
        with pytest.raises(ServerRunning):
            bk.server_lock(tmp_path).__enter__()
        result: list[BaseException] = []

        def other_thread() -> None:
            try:
                with bk.server_lock(tmp_path):
                    pass
            except ServerRunning as exc:
                result.append(exc)

        t = threading.Thread(target=other_thread)
        t.start()
        t.join()
        assert len(result) == 1
    with bk.server_lock(tmp_path):  # released
        pass


# ── backup ────────────────────────────────────────────────────────────────────


def test_backup_contents_snapshot_and_modes(backup_dir: Path, job_store: JobStore) -> None:
    manifest = json.loads((backup_dir / bk.MANIFEST).read_text())
    paths = {f["path"] for f in manifest["files"]}
    assert "wispralt.db" in paths
    assert any(p.startswith("outputs/") for p in paths)
    assert "staging/pending.wav" in paths and "staging/done.wav" not in paths
    snap = backup_dir / "wispralt.db"
    with closing(sqlite3.connect(f"file:{snap}?mode=ro&immutable=1", uri=True)) as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for table, count in manifest["tables"].items():
            assert con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == count
    assert manifest["tables"]["dictations"] == 1 and manifest["tables"]["jobs"] == 2
    assert manifest["schema_version"] == max(_migration_files())
    assert not (backup_dir / "wispralt.db-wal").exists()
    assert not (backup_dir / "wispralt.db-shm").exists()
    for f in manifest["files"]:
        assert hashlib.sha256((backup_dir / f["path"]).read_bytes()).hexdigest() == f["sha256"]
    assert _modes(backup_dir) == {("d", 0o700), ("f", 0o600)}


def test_backup_refuses_a_target_inside_data_or_existing(data: Path, tmp_path_factory) -> None:
    with pytest.raises(BackupRefused):
        bk.backup(data, data / "inside")
    existing = tmp_path_factory.mktemp("exists")
    with pytest.raises(BackupRefused):
        bk.backup(data, existing)
    assert not (data / "inside").exists()


# ── restore ───────────────────────────────────────────────────────────────────


def test_restore_round_trips_and_keeps_the_old_tree_aside(
    data: Path, backup_dir: Path, job_store: JobStore
) -> None:
    original = _tree(backup_dir)
    pending_id = job_store.pending_id  # type: ignore[attr-defined]
    _add(job_store)
    (data / "outputs" / "stray.txt").write_text("new")

    aside = bk.restore(backup_dir, data)

    assert _texts(job_store) == ["before backup"]
    assert job_store.get(pending_id).status == "pending"
    assert job_store.get(pending_id).wav_path.exists()
    assert not (data / "outputs" / "stray.txt").exists()
    assert len(list((data / "outputs").glob("*.txt"))) == 1
    assert aside is not None and (aside / "wispralt.db").exists()
    with closing(sqlite3.connect(aside / "wispralt.db")) as con:
        assert con.execute("SELECT COUNT(*) FROM dictations").fetchone()[0] == 2
    assert _tree(backup_dir) == original  # the backup is never written
    assert _modes(data) == {("d", 0o700), ("f", 0o600)}
    assert not list(data.parent.glob(f"{data.name}.restore-*"))


def _tamper_manifest(root: Path, extra: dict) -> None:
    path = root / bk.MANIFEST
    manifest = json.loads(path.read_text())
    manifest["files"].append(extra)
    path.write_text(json.dumps(manifest))


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ("symlink", "symlink in backup"),
        ("dotdot", "bad manifest path"),
        ("absolute", "bad manifest path"),
        ("extra_wal", "file not in manifest: wispralt.db-wal"),
        ("hash", "hash mismatch"),
        ("no_db", "manifest lists no database"),
    ],
)
def test_restore_refusals_change_nothing(
    data: Path, backup_dir: Path, job_store: JobStore, tamper: str, reason: str
) -> None:
    if tamper == "symlink":
        (backup_dir / "outputs" / "link.txt").symlink_to("/etc/hosts")
    elif tamper == "dotdot":
        _tamper_manifest(backup_dir, {"path": "../escape", "sha256": "0", "bytes": 0})
    elif tamper == "absolute":
        _tamper_manifest(backup_dir, {"path": "/etc/hosts", "sha256": "0", "bytes": 0})
    elif tamper == "extra_wal":
        (backup_dir / "wispralt.db-wal").write_bytes(b"stray")
    elif tamper == "hash":
        target = next((backup_dir / "outputs").glob("*.txt"))
        target.write_text("HELLO")  # same length, different bytes
    elif tamper == "no_db":
        path = backup_dir / bk.MANIFEST
        manifest = json.loads(path.read_text())
        manifest["files"] = [f for f in manifest["files"] if f["path"] != "wispralt.db"]
        path.write_text(json.dumps(manifest))
    _add(job_store)
    before = _tree(data)
    with pytest.raises(RestoreRefused, match=reason):
        bk.restore(backup_dir, data)
    assert _tree(data) == before
    assert _texts(job_store) == ["before backup", "after backup"]
    assert not list(data.parent.glob(f"{data.name}.restore-*"))
    assert not list(data.parent.glob(f"{data.name}.pre-restore-*"))


def test_restore_refuses_a_backup_inside_data(data: Path, backup_dir: Path) -> None:
    import shutil

    inside = data / "bk"
    shutil.copytree(backup_dir, inside)
    with pytest.raises(RestoreRefused):
        bk.restore(inside, data)


def test_failed_swap_puts_the_original_back(
    data: Path, backup_dir: Path, job_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(job_store)
    real = os.replace

    def flaky(src: os.PathLike, dst: os.PathLike) -> None:
        if ".restore-" in str(src):
            raise OSError("injected")
        real(src, dst)

    monkeypatch.setattr(bk.os, "replace", flaky)
    with pytest.raises(OSError, match="injected"):
        bk.restore(backup_dir, data)
    assert _texts(job_store) == ["before backup", "after backup"]
    assert not list(data.parent.glob(f"{data.name}.restore-*"))
    assert not list(data.parent.glob(f"{data.name}.pre-restore-*"))


# ── disk preflight ────────────────────────────────────────────────────────────


def test_preflight_refuses_before_writing_anything(
    data: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory
) -> None:
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(bk.shutil, "disk_usage", lambda _p: usage(10, 10, 1))
    dest = tmp_path_factory.mktemp("full") / "b"
    with pytest.raises(InsufficientSpace, match=r"need .* GB, have .* GB"):
        bk.backup(data, dest)
    assert not dest.exists()
    with pytest.raises(InsufficientSpace):
        bk.restore(backup_dir, data)
    assert not list(data.parent.glob(f"{data.name}.restore-*"))


def test_preflight_message_names_both_sizes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bk.shutil, "disk_usage", lambda _p: SimpleNamespace(free=2_000_000_000))
    with pytest.raises(InsufficientSpace) as exc:
        bk.preflight(5_000_000_000, tmp_path)
    assert str(exc.value) == "need 5.50 GB, have 2.00 GB"
    bk.preflight(1_000_000_000, tmp_path)  # fits
