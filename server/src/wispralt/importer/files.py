"""
importer/files.py — import transcript files (TXT/JSON/SRT/VTT) as ``jobs`` rows.

* ``scan`` walks the given paths (``followlinks=False``; symlinks, dotfiles and dot
  dirs are skipped) and groups files by ``(parent dir, stem)``.
* ``parse_group`` reads the primary file in order json > txt > srt > vtt (UTF-8, BOM
  stripped; an unreadable file falls back to the next with a note). Text: JSON
  ``segments[].text`` joined like the runner does; SRT/VTT cue text without timing.
  ``created_at`` = JSON ``created_at``, else the group's newest mtime with
  ``estimated=1``. ``source_id`` = JSON ``job_id``, else ``sha256(primary) + ':u<owner>'``.
* One ``jobs`` row per group (``status='done'``, ``mode='file'``, ``wav_path=''``).
  Every file in the group is **copied** (never moved) to ``outputs/<id><ext>``, the
  layout the download and retention code already use: first to a private
  ``.import-*.part`` temp, renamed only after the row commits. The owner's source
  folders are never modified; only our own temps are ever removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wispralt._errors import ImportIntegrityError, OwnerMappingRequired
from wispralt.ops.backup import server_lock
from wispralt.importer.report import Report
from wispralt.importer.runs import ImportRuns
from wispralt.imports import insert_rows

if TYPE_CHECKING:
    from collections.abc import Iterable

    from wispralt.database import Database

SOURCE = "files"
PRIMARY_ORDER = (".json", ".txt", ".srt", ".vtt")
BATCH = 500


@dataclass(frozen=True)
class Group:
    parent: Path
    stem: str
    files: dict[str, Path]  # ext (lower) -> path


@dataclass(frozen=True)
class Parsed:
    text: str
    created_at: float
    estimated: bool
    duration_s: float | None
    source_id: str
    notes: tuple[str, ...]


def scan(paths: Iterable[Path]) -> list[Group]:
    found: dict[tuple[Path, str], dict[str, Path]] = {}

    def add(f: Path) -> None:
        ext = f.suffix.lower()
        if ext in PRIMARY_ORDER and not f.name.startswith(".") and not f.is_symlink():
            found.setdefault((f.parent, f.stem), {})[ext] = f

    for p in map(Path, paths):
        if p.is_symlink():
            continue
        if p.is_file():
            add(p)
        for root, dirs, names in os.walk(p, followlinks=False) if p.is_dir() else ():
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for n in names:
                add(Path(root) / n)
    return [Group(parent, stem, files) for (parent, stem), files in sorted(found.items())]


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8").removeprefix("﻿")  # UnicodeDecodeError ⊂ ValueError


def _cue_text(text: str) -> str:
    keep = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s == "WEBVTT" or s.startswith("WEBVTT ") or s.isdigit() or "-->" in s:
            continue
        keep.append(s)
    return "\n".join(keep)


def _from_json(path: Path) -> tuple[str, dict[str, Any]]:
    obj = json.loads(_read(path))
    if not isinstance(obj, dict):
        raise ValueError("not a transcript object")
    segs = obj.get("segments") or []
    text = " ".join((s.get("text") or "").strip() for s in segs if isinstance(s, dict)).strip()
    return text, obj


def parse_group(g: Group, owner_id: int) -> Parsed:
    """Raise ``ValueError`` when no file in the group yields text."""
    notes: list[str] = []
    for ext in PRIMARY_ORDER:
        path = g.files.get(ext)
        if path is None:
            continue
        meta: dict[str, Any] = {}
        try:
            if ext == ".json":
                text, meta = _from_json(path)
            else:
                text = _read(path)
                text = text.strip() if ext == ".txt" else _cue_text(text)
            created = meta.get("created_at")
            ts = datetime.fromisoformat(created).timestamp() if isinstance(created, str) else None
        except (OSError, ValueError) as exc:
            notes.append(f"{path.name}: unreadable ({type(exc).__name__}), fell back")
            continue
        if not text:
            notes.append(f"{path.name}: no text")
            continue
        job_id = meta.get("job_id")
        sid = job_id if isinstance(job_id, str) and job_id else None
        sid = sid or f"{hashlib.sha256(path.read_bytes()).hexdigest()}:u{owner_id}"
        duration = meta.get("duration_s")
        return Parsed(
            text=text,
            created_at=ts if ts is not None else max(f.stat().st_mtime for f in g.files.values()),
            estimated=ts is None,
            duration_s=float(duration) if isinstance(duration, int | float) else None,
            source_id=sid,
            notes=tuple(notes),
        )
    raise ValueError("; ".join(notes) or "no transcript file")


def _row(p: Parsed, owner_id: int) -> dict[str, Any]:
    return {
        "source_id": p.source_id, "id": str(uuid.uuid4()), "user_id": owner_id,
        "status": "done", "mode": "file", "created_at": p.created_at,
        "finished_at": p.created_at, "wav_path": "", "audio_duration_s": p.duration_s,
        "transcript_text": p.text, "word_count": len(p.text.split()),
        "estimated": int(p.estimated),
    }  # fmt: skip


def _stage(g: Group, job_id: str, outputs: Path) -> list[tuple[Path, Path]]:
    """Copy every file of ``g`` to private temps; returns ``(temp, final)`` pairs."""
    pairs: list[tuple[Path, Path]] = []
    try:
        for ext, src in sorted(g.files.items()):
            tmp = outputs / f".import-{job_id}{ext}.part"
            shutil.copy2(src, tmp)
            tmp.chmod(0o600)
            pairs.append((tmp, outputs / f"{job_id}{ext}"))
    except OSError:
        _drop(pairs)
        raise
    return pairs


def _drop(pairs: list[tuple[Path, Path]]) -> None:
    for tmp, _ in pairs:  # our own temp copies only — never a source file
        tmp.unlink(missing_ok=True)


def import_files(
    db: Database,
    paths: Iterable[Path],
    *,
    owner_id: int,
    outputs_dir: Path,
    dry_run: bool = False,
) -> Report:
    paths = [Path(p).absolute() for p in paths]
    with db.read() as con:
        if not con.execute("SELECT 1 FROM users WHERE id = ?", (owner_id,)).fetchone():
            raise OwnerMappingRequired(f"--owner {owner_id} is not a local user")
        have = {r[0] for r in con.execute("SELECT source_id FROM jobs WHERE source = ?", (SOURCE,))}
    report = Report("Transcript files", dry_run, "dry_run" if dry_run else "done")
    groups = scan(paths)
    planned: list[tuple[dict[str, Any], Group]] = []
    malformed = 0
    for g in groups:
        try:
            parsed = parse_group(g, owner_id)
        except ValueError as exc:
            malformed += 1
            report.problems.append(f"{g.parent / g.stem}: malformed ({exc})")
            continue
        report.notes += [f"{g.parent / g.stem}: {n}" for n in parsed.notes]
        planned.append((_row(parsed, owner_id), g))
    counts = {"source_count": len(groups), "malformed": malformed, "linked": 0, "unmatched": 0}
    if dry_run:
        seen = set(have)
        dup = 0
        for row, _ in planned:
            dup += row["source_id"] in seen
            seen.add(row["source_id"])
        report.tables["files"] = {
            **counts,
            "new": len(planned) - dup,
            "duplicate": dup,
            "errors": 0,
        }
        return report
    totals = _apply(db, planned, outputs_dir, ":".join(map(str, paths)), report)
    report.tables["files"] = {**counts, **totals}
    return report


def _apply(
    db: Database,
    planned: list[tuple[dict[str, Any], Group]],
    outputs: Path,
    origin: str,
    report: Report,
) -> dict[str, int]:
    outputs.mkdir(parents=True, exist_ok=True)
    runs = ImportRuns(db)
    with server_lock(db.path.parent):
        run = runs.begin(SOURCE, origin)
        total = len(planned)
        sids = [row["source_id"] for row, _ in planned]
        if run.checkpoint in sids:
            planned = planned[sids.index(run.checkpoint) + 1 :]
        c = {"new": 0, "duplicate": 0, "errors": 0, **run.counts}
        for i in range(0, len(planned), BATCH):
            staged: dict[str, list[tuple[Path, Path]]] = {}
            rows = []
            for row, g in planned[i : i + BATCH]:
                try:
                    staged[row["id"]] = _stage(g, row["id"], outputs)
                except OSError as exc:
                    c["errors"] += 1
                    report.problems.append(f"{g.parent / g.stem}: copy failed ({exc})")
                    continue
                rows.append(row)
            committed = False
            try:
                with db.write() as con:
                    res = insert_rows(con, "jobs", rows, SOURCE)
                    c = {
                        "new": c["new"] + res.inserted,
                        "duplicate": c["duplicate"] + res.duplicates,
                        "errors": c["errors"] + len(res.errors),
                    }
                    if rows:
                        ImportRuns.checkpoint(con, run.id, rows[-1]["source_id"], c)
                committed = True
            finally:
                if not committed:
                    for pairs in staged.values():
                        _drop(pairs)
            for row in rows:
                pairs = staged[row["id"]]
                if res.ids.get(row["source_id"]) == row["id"]:  # this row was inserted
                    for tmp, final in pairs:
                        os.rename(tmp, final)  # fresh uuid name: never an existing file
                else:
                    _drop(pairs)
            report.problems += [f"files {e.source_id}: {e.error}" for e in res.errors]
        if c["new"] + c["duplicate"] + c["errors"] != total:
            runs.finish(run.id, "failed", c)
            raise ImportIntegrityError(f"files: accounted for {sum(c.values())} of {total} groups")
        runs.finish(run.id, "done", c)
    return c
