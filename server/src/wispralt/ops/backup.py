"""
ops/backup.py — the server lock and offline backup / restore (Locked Decision 9).

* ``server_lock(data_dir)`` — ``flock(LOCK_EX | LOCK_NB)`` on the sibling
  ``<data_dir>.lock`` of the *resolved* data dir (one lock however the path
  is spelled, symlinks included; outside the tree restore swaps, so it never moves).
  The lifespan holds it for the server's life (``pin_until_exit`` keeps it
  held past the lifespan while a file-job thread is still running); backup
  and restore take it
  too, so each refuses (``ServerRunning``) while a server runs. The Part 5
  CLI stops the service, runs them, and restarts it.
* ``preflight(need, parent)`` — ``need × 1.1`` must fit in the free space of
  *parent*, else ``InsufficientSpace`` before anything is written.
* ``backup(data_dir, dest)`` — ``VACUUM INTO`` snapshot (switched to
  ``journal_mode=DELETE``), copies of ``outputs/`` and ``staging/``, and a
  ``manifest.json`` (relative POSIX paths, sha256, bytes, per-table counts,
  ``schema_version``). Dirs 0700, files 0600.
* ``restore(backup_dir, data_dir)`` — refuses anything not in the manifest,
  copies the listed files into a sibling ``.restore-<ts>`` (verifying hashes),
  runs ``integrity_check`` read-only, moves ``data_dir`` aside to
  ``.pre-restore-<ts>`` and swaps; any failure swaps back. Returns the aside
  path (never deleted).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from wispralt._errors import BackupRefused, InsufficientSpace, RestoreRefused, ServerRunning

if TYPE_CHECKING:
    from collections.abc import Iterator

DB_NAME = "wispralt.db"
MANIFEST = "manifest.json"
_TREES = ("outputs", "staging")
_CHUNK = 1 << 20


def lock_path(data_dir: Path) -> Path:
    """``<data_dir>.lock`` beside the *resolved* data dir.

    Every holder (server lifespan, backup, restore, importers, CLI) derives the
    path here, so a symlinked ``DATA_DIR`` and its target share one lock.
    """
    data = Path(data_dir).resolve()
    return data.with_name(data.name + ".lock")


# Lock fds held by ``server_lock`` in this process, and dups kept open until exit.
_HELD: dict[Path, int] = {}
_PINNED: list[int] = []


@contextmanager
def server_lock(data_dir: Path) -> Iterator[Path]:
    """Hold ``<data_dir>.lock`` exclusively, or raise ``ServerRunning`` at once."""
    path = lock_path(data_dir)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ServerRunning(f"a server holds {path}; stop it first") from exc
        _HELD[path] = fd
        try:
            yield path
        finally:
            _HELD.pop(path, None)
    finally:
        os.close(fd)  # closing the description releases the flock (unless pinned)


def pin_until_exit(data_dir: Path) -> bool:
    """Keep the held ``<data_dir>.lock`` locked until this process exits.

    For a shutdown that leaves a thread still writing to the data dir: the
    flock belongs to the open file description, so a dup kept open outlives
    ``server_lock``'s close and the OS releases it only when the process dies.
    Returns False when this process does not hold the lock.
    """
    fd = _HELD.get(lock_path(data_dir))
    if fd is None:
        return False
    _PINNED.append(os.dup(fd))
    return True


def preflight(need_bytes: int, target_parent: Path) -> None:
    free = shutil.disk_usage(target_parent).free
    if need_bytes * 1.1 > free:
        raise InsufficientSpace(f"need {need_bytes * 1.1 / 1e9:.2f} GB, have {free / 1e9:.2f} GB")


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _tree_files(root: Path) -> list[Path]:
    """Regular files under ``root/outputs`` and ``root/staging`` (symlinks skipped)."""
    return [
        p
        for tree in _TREES
        if (root / tree).is_dir()
        for p in sorted((root / tree).rglob("*"))
        if p.is_file() and not p.is_symlink()
    ]


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _copy(src: Path, dst: Path) -> tuple[str, int]:
    """Copy *src* to a new 0600 *dst*; returns ``(sha256, bytes)`` of what was written."""
    _private_dir(dst.parent)
    digest, size = hashlib.sha256(), 0
    fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with open(src, "rb") as fin, os.fdopen(fd, "wb") as fout:
        while chunk := fin.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
            fout.write(chunk)
    dst.chmod(0o600)
    return digest.hexdigest(), size


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def backup(data_dir: Path, dest: Path) -> Path:
    """Snapshot *data_dir* into the new directory *dest*; returns *dest* (server must be stopped)."""
    data = Path(data_dir).resolve(strict=True)
    dest = Path(dest).resolve()
    if dest.exists():
        raise BackupRefused(f"backup target already exists: {dest}")
    if _overlaps(dest, data) or dest == lock_path(data):
        raise BackupRefused("backup target overlaps the data directory")
    with server_lock(data):
        db = data / DB_NAME
        sources = _tree_files(data)
        preflight(db.stat().st_size + sum(p.stat().st_size for p in sources), dest.parent)
        _private_dir(dest)

        snap = dest / DB_NAME
        os.close(os.open(snap, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))  # VACUUM INTO: empty ok
        with closing(sqlite3.connect(db, isolation_level=None)) as con:
            con.execute("VACUUM INTO ?", (str(snap),))
        with closing(sqlite3.connect(snap, isolation_level=None)) as con:
            con.execute("PRAGMA journal_mode=DELETE")
            tables = [
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                    " AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
            schema_version = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        snap.chmod(0o600)

        files = [{"path": DB_NAME, "sha256": _hash(snap), "bytes": snap.stat().st_size}]
        for src in sources:
            rel = src.relative_to(data).as_posix()
            sha, size = _copy(src, dest / rel)
            files.append({"path": rel, "sha256": sha, "bytes": size})
        for tree in _TREES:
            _private_dir(dest / tree)

        manifest: dict[str, Any] = {
            "schema_version": schema_version,
            "created_at": time.time(),
            "tables": counts,
            "files": files,
        }
        fd = os.open(dest / MANIFEST, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(manifest, f, indent=2)
    return dest


def _checked_manifest(root: Path) -> list[dict[str, Any]]:
    """The manifest's file list after every containment check, else ``RestoreRefused``."""
    try:
        manifest = json.loads((root / MANIFEST).read_text())
        files = manifest["files"]
        listed = {str(f["path"]): f for f in files}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RestoreRefused(f"unreadable manifest: {type(exc).__name__}") from exc
    for rel in listed:
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise RestoreRefused(f"bad manifest path: {rel}")
        if not (root / rel).resolve().is_relative_to(root):
            raise RestoreRefused(f"manifest path leaves the backup: {rel}")
    if DB_NAME not in listed:
        raise RestoreRefused("manifest lists no database")
    present: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                raise RestoreRefused(f"symlink in backup: {p.relative_to(root).as_posix()}")
            if name in filenames:
                present.add(p.relative_to(root).as_posix())
    extra = present - set(listed) - {MANIFEST}
    if extra:
        raise RestoreRefused(f"file not in manifest: {sorted(extra)[0]}")
    missing = set(listed) - present
    if missing:
        raise RestoreRefused(f"listed file missing: {sorted(missing)[0]}")
    return files


def restore(backup_dir: Path, data_dir: Path) -> Path | None:
    """Replace *data_dir* with the backup; returns where the old tree was moved (None if absent)."""
    root = Path(backup_dir).resolve(strict=True)
    data = Path(data_dir).resolve()
    if _overlaps(root, data):
        raise RestoreRefused("backup and data directory overlap")
    with server_lock(data):
        files = _checked_manifest(root)
        preflight(sum(int(f["bytes"]) for f in files), data.parent)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        staged = data.with_name(f"{data.name}.restore-{ts}")
        _private_dir(staged)
        verified = False
        try:
            for f in files:
                sha, size = _copy(root / f["path"], staged / f["path"])
                if sha != f["sha256"] or size != int(f["bytes"]):
                    raise RestoreRefused(f"hash mismatch: {f['path']}")
            for tree in _TREES:
                _private_dir(staged / tree)
            uri = f"file:{staged / DB_NAME}?mode=ro&immutable=1"
            try:
                with closing(sqlite3.connect(uri, uri=True)) as con:
                    result = con.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.Error as exc:
                raise RestoreRefused(f"database unreadable: {type(exc).__name__}") from exc
            if result != "ok":
                raise RestoreRefused(f"integrity_check: {result}")
            verified = True
        finally:
            if not verified:  # a refusal removes its staged copy
                shutil.rmtree(staged, ignore_errors=True)

        aside = data.with_name(f"{data.name}.pre-restore-{ts}") if data.exists() else None
        if aside is not None:
            os.replace(data, aside)
        try:
            os.replace(staged, data)
        except OSError:
            if aside is not None:
                os.replace(aside, data)
            shutil.rmtree(staged, ignore_errors=True)
            raise
    return aside
