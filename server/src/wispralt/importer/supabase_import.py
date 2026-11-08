"""
importer/supabase_import.py — import a verified Supabase snapshot into the local DB.

Flow: ``load_snapshot`` (sha256 + line counts vs ``counts.json``, before any write)
→ ``resolve_owner`` (owner → local admin) → ``plan`` (map every row into exactly one
bucket: new/duplicate, linked, malformed, unmatched; the per-table equation is
checked here, fail closed) → dry run stops, else ``apply`` under the offline lock.

* users → ``users`` (source ``supabase.users``; ``revoked_at`` → ``disabled_at``; no
  ``api_keys``: people rejoin by invite). The confirmed owner is *linked* to the
  local admin, not inserted.
* usage_events → ``usage_events`` (``meeting`` → ``file``); fallback_events →
  ``usage_events`` (``fallback_<kind>``); ``user_id`` remapped through the imported
  users; an unknown user → unmatched.
* Batches of ``BATCH`` rows, each committed with its ``import_runs`` checkpoint; a
  re-run resumes after it. Correctness never depends on the checkpoint: the
  ``(source, source_id)`` conflict makes every re-run idempotent.
* First run of a source (no ``done`` run) with any duplicate → ``ImportIntegrityError``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from wispralt._errors import ExportBlocked, ImportIntegrityError, OwnerMappingRequired
from wispralt.importer.lock import offline_lock
from wispralt.importer.report import Report
from wispralt.importer.runs import ImportRuns
from wispralt.importer.supabase_export import TABLES
from wispralt.imports import insert_rows, link_existing

if TYPE_CHECKING:
    from pathlib import Path

    from wispralt.database import Database

BATCH = 500
SOURCE = {t: f"supabase.{t}" for t in TABLES}
LOCAL = {"users": "users", "usage_events": "usage_events", "fallback_events": "usage_events"}
SNAPSHOT_ONLY = {"users": ("last_seen_at",), "fallback_events": ("provider", "provider_request_id", "cost_micro_usd")}  # fmt: skip
NO_TEXT = "No dictation text survives the 2026 host wipe; Supabase never stored text."


@dataclass(frozen=True)
class Snapshot:
    path: Path
    counts: dict[str, dict[str, Any]]
    rows: dict[str, list[dict[str, Any]]]


@dataclass
class TablePlan:
    source_count: int
    rows: list[dict[str, Any]] = field(default_factory=list)  # to insert (new or duplicate)
    linked: int = 0
    malformed: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    duplicate: int = 0  # filled from a read of existing (source, source_id)

    def bucket_total(self) -> int:
        return len(self.rows) + self.linked + len(self.malformed) + len(self.unmatched)


def load_snapshot(d: Path) -> Snapshot:
    """Verify every table against ``counts.json`` before anything is written."""
    counts_file = d / "counts.json"
    if not counts_file.exists():
        blocked = d / "BLOCKED.json"
        if blocked.exists():
            raise ExportBlocked(json.loads(blocked.read_text()).get("reason", "blocked"))
        raise ImportIntegrityError(f"{d} has no counts.json")
    counts = json.loads(counts_file.read_text())
    rows: dict[str, list[dict[str, Any]]] = {}
    for t in TABLES:
        c = counts.get(t)
        if c is None:
            raise ImportIntegrityError(f"counts.json has no {t}")
        data = (d / f"{t}.jsonl").read_bytes()
        lines = data.splitlines()
        if hashlib.sha256(data).hexdigest() != c["sha256"]:
            raise ImportIntegrityError(f"{t}.jsonl sha256 differs from counts.json")
        if not len(lines) == c["exported"] == c["source_count"]:
            raise ImportIntegrityError(
                f"{t}.jsonl has {len(lines)} lines, expected {c['source_count']}"
            )
        rows[t] = [json.loads(line) for line in lines]
    return Snapshot(d, counts, rows)


def _epoch(value: Any) -> float:
    dt = datetime.fromisoformat(value) if isinstance(value, str) else None
    if dt is None or dt.tzinfo is None:
        raise ValueError(f"not an ISO timestamp with offset: {value!r}")
    return dt.timestamp()


def _local_admins(con: Any) -> list[int]:
    q = "SELECT id FROM users WHERE role = 'admin' AND disabled_at IS NULL ORDER BY id"
    return [r[0] for r in con.execute(q)]


def propose_owner(db: Database, snap: Snapshot) -> tuple[int, int] | None:
    """(pg id, local id) when exactly one enabled admin exists on each side."""
    pg = [
        u["id"] for u in snap.rows["users"] if u.get("role") == "admin" and not u.get("revoked_at")
    ]
    with db.read() as con:
        local = _local_admins(con)
    return (pg[0], local[0]) if len(pg) == 1 and len(local) == 1 else None


def resolve_owner(
    db: Database, snap: Snapshot, owner_map: dict[int, int] | None, *, confirm: bool, dry_run: bool
) -> dict[int, int]:
    """The confirmed pg-id → local-id owner map (an earlier link counts as confirmed)."""
    pg_ids = {u["id"] for u in snap.rows["users"]}
    with db.read() as con:
        existing = {
            int(r["source_id"]): r["id"]
            for r in con.execute(  # a linked local account has keys; imported users never do
                "SELECT id, source_id FROM users u WHERE source = ?"
                " AND EXISTS (SELECT 1 FROM api_keys k WHERE k.user_id = u.id)",
                (SOURCE["users"],),
            )
        }
        local_admins = _local_admins(con)
        known = {r[0] for r in con.execute("SELECT id FROM users")}
    if owner_map:
        bad = [f"{p}={lid}" for p, lid in owner_map.items() if p not in pg_ids or lid not in known]
        if bad:
            raise OwnerMappingRequired(f"owner map names unknown users: {', '.join(bad)}")
        return dict(owner_map)
    if existing:
        return existing
    proposal = propose_owner(db, snap)
    if proposal and (confirm or dry_run):
        return {proposal[0]: proposal[1]}
    has_pg_admin = any(u.get("role") == "admin" for u in snap.rows["users"])
    if dry_run or (not local_admins and not has_pg_admin):
        return {}
    if not local_admins:
        raise OwnerMappingRequired("no local admin yet: run `python -m wispralt.bootstrap` first")
    hint = f"proposed {proposal[0]}={proposal[1]}" if proposal else "no unambiguous proposal"
    raise OwnerMappingRequired(f"confirm the owner mapping ({hint}): --yes or --owner-map PG=LOCAL")


def _map_user(r: dict[str, Any]) -> dict[str, Any]:
    kind = r.get("kind") or "employee"
    if r.get("role") not in ("admin", "employee") or kind not in ("employee", "integration"):
        raise ValueError(f"role/kind {r.get('role')!r}/{kind!r}")
    if not isinstance(r.get("label"), str) or not r["label"]:
        raise ValueError("empty label")
    return {
        "source_id": str(r["id"]), "label": r["label"], "display_name": r.get("display_name"),
        "notes": r.get("notes"), "role": r["role"], "kind": kind,
        "created_at": _epoch(r["created_at"]),
        "disabled_at": _epoch(r["revoked_at"]) if r.get("revoked_at") else None,
    }  # fmt: skip


def _map_event(t: str, r: dict[str, Any], user_id: int) -> dict[str, Any]:
    if not isinstance(r.get("kind"), str) or not isinstance(r.get("status"), int):
        raise ValueError(f"kind/status {r.get('kind')!r}/{r.get('status')!r}")
    if t == "fallback_events":
        kind = f"fallback_{r['kind']}"
    else:
        kind = "file" if r["kind"] == "meeting" else r["kind"]
    return {
        "source_id": str(r["id"]), "user_id": user_id, "ts": _epoch(r["ts"]), "kind": kind,
        "status": r["status"], "chars": r.get("chars"), "duration_ms": r.get("duration_ms"),
        "bytes_in": r.get("bytes_in"), "bytes_out": r.get("bytes_out"),
        "error_class": r.get("error_class"), "request_id": r.get("request_id"),
    }  # fmt: skip


def plan(db: Database, snap: Snapshot, owner_map: dict[int, int]) -> dict[str, TablePlan]:
    """Map every row; ``user_id`` here is the *pg* id (remapped to local ids in apply)."""
    plans = {t: TablePlan(snap.counts[t]["source_count"]) for t in TABLES}
    users = plans["users"]
    for r in snap.rows["users"]:
        if r["id"] in owner_map:
            users.linked += 1
            continue
        try:
            users.rows.append(_map_user(r))
        except (KeyError, ValueError) as exc:
            users.malformed.append(f"users {r.get('id')}: {exc}")
    known = {int(u["source_id"]) for u in users.rows} | owner_map.keys()
    for t in ("usage_events", "fallback_events"):
        for r in snap.rows[t]:
            try:
                row = _map_event(t, r, r["user_id"])
            except (KeyError, ValueError) as exc:
                plans[t].malformed.append(f"{t} {r.get('id')}: {exc}")
                continue
            if r["user_id"] in known:
                plans[t].rows.append(row)
            else:
                plans[t].unmatched.append(f"{t} {r['id']}: user {r['user_id']} not imported")
    with db.read() as con:
        for t, p in plans.items():
            have = {
                x[0]
                for x in con.execute(
                    f"SELECT source_id FROM {LOCAL[t]} WHERE source = ?", (SOURCE[t],)
                )
            }
            p.duplicate = sum(1 for row in p.rows if row["source_id"] in have)
            if p.bucket_total() != p.source_count:
                raise ImportIntegrityError(
                    f"{t}: {p.source_count} rows but buckets add up to {p.bucket_total()}"
                )
    return plans


def _users_map(db: Database) -> dict[int, int]:
    with db.read() as con:
        q = "SELECT source_id, id FROM users WHERE source = ?"
        return {int(s): i for s, i in con.execute(q, (SOURCE["users"],))}


def _apply_table(
    db: Database, runs: ImportRuns, snap: Snapshot, t: str, p: TablePlan, report: Report
) -> dict[str, int]:
    source, first = SOURCE[t], not runs.has_done(SOURCE[t])
    rows = p.rows
    if t != "users":
        umap = _users_map(db)
        rows = [{**r, "user_id": umap.get(r["user_id"], -1)} for r in rows]
    run = runs.begin(source, str(snap.path))
    sids = [r["source_id"] for r in rows]
    if run.checkpoint in sids:
        rows = rows[sids.index(run.checkpoint) + 1 :]
    counts = {"inserted": 0, "duplicates": 0, "errors": 0, **run.counts}
    try:
        for i in range(0, len(rows), BATCH):
            batch = rows[i : i + BATCH]
            with db.write() as con:
                res = insert_rows(con, LOCAL[t], batch, source)
                if first and res.duplicates:
                    raise ImportIntegrityError(f"{t}: {res.duplicates} duplicates on the first run")
                counts = {
                    "inserted": counts["inserted"] + res.inserted,
                    "duplicates": counts["duplicates"] + res.duplicates,
                    "errors": counts["errors"] + len(res.errors),
                }
                ImportRuns.checkpoint(con, run.id, batch[-1]["source_id"], counts)
            report.problems += [f"{t} {e.source_id}: {e.error}" for e in res.errors]
        if counts["inserted"] + counts["duplicates"] != len(p.rows):
            raise ImportIntegrityError(
                f"{t}: wrote {counts['inserted']}+{counts['duplicates']} of {len(p.rows)} planned rows"
            )
    except ImportIntegrityError:
        runs.finish(run.id, "failed", counts)
        raise
    runs.finish(run.id, "done", counts)
    return counts


def _earliest(snap: Snapshot) -> str | None:
    stamps = [u["created_at"] for u in snap.rows["users"]]
    stamps += [r["ts"] for t in ("usage_events", "fallback_events") for r in snap.rows[t]]
    epochs = []
    for s in stamps:
        try:
            epochs.append(_epoch(s))
        except ValueError:
            continue
    return datetime.fromtimestamp(min(epochs), UTC).isoformat() if epochs else None


def import_snapshot(
    db: Database,
    d: Path,
    *,
    dry_run: bool = False,
    confirm: bool = False,
    owner_map: dict[int, int] | None = None,
) -> Report:
    """Verify, plan and (unless ``dry_run``) apply one snapshot dir. See module docstring."""
    snap = load_snapshot(d)
    report = Report(f"Supabase snapshot {d.name}", dry_run, "dry_run" if dry_run else "done")
    owners = resolve_owner(db, snap, owner_map, confirm=confirm, dry_run=dry_run)
    proposal = propose_owner(db, snap)
    plans = plan(db, snap, owners)
    for p in plans.values():
        report.problems += p.malformed + p.unmatched
    report.notes += [
        "owner proposal: "
        + (
            f"Supabase user {proposal[0]} -> local admin {proposal[1]}"
            if proposal
            else "none (ambiguous)"
        ),
        "owner mapping used: " + (", ".join(f"{k}->{v}" for k, v in owners.items()) or "none"),
        f"earliest record: {_earliest(snap) or 'n/a'}",
        "snapshot-only fields (kept in the snapshot, not the DB): "
        + "; ".join(f"{t}.{', '.join(cols)}" for t, cols in SNAPSHOT_ONLY.items()),
        NO_TEXT,
    ]
    applied: dict[str, dict[str, int]] = {}
    if not dry_run:
        runs = ImportRuns(db)
        with offline_lock(db.path.parent):
            with db.write() as con:
                for pg_id, local_id in owners.items():
                    link_existing(con, "users", local_id, SOURCE["users"], str(pg_id))
            applied = {t: _apply_table(db, runs, snap, t, p, report) for t, p in plans.items()}
    for t, p in plans.items():
        new = applied[t]["inserted"] if not dry_run else len(p.rows) - p.duplicate
        dup = applied[t]["duplicates"] if not dry_run else p.duplicate
        report.tables[t] = {
            "source_count": p.source_count, "new": new, "duplicate": dup, "linked": p.linked,
            "malformed": len(p.malformed), "unmatched": len(p.unmatched),
            "errors": applied[t]["errors"] if not dry_run else 0,
        }  # fmt: skip
    return report
