"""
importer/supabase_export.py — read-only Supabase export into a never-deleted snapshot.

Supabase is the owner's only copy of old history, so this module can only read:

* ``PsqlSource`` builds SQL from three fixed SELECT shapes (columns, table header,
  rows); table names come from ``TABLES`` and column names from
  ``information_schema`` (identifier-checked). ``_check`` refuses anything that is
  not a single SELECT. Column discovery is one catalog read
  (``BEGIN TRANSACTION READ ONLY; <select>; COMMIT;``); then **all data** comes
  from ONE ``psql`` session in ONE ``BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE
  READ READ ONLY`` transaction (per table: a count/max-id header line, then one
  ``row_to_json`` line per row), so every table is the same consistent snapshot.
  Each script goes to ``psql -X -q -At -v ON_ERROR_STOP=1`` on stdin with
  ``PGOPTIONS=-c default_transaction_read_only=on``. The connection string (env
  ``WISPRALT_IMPORT_PG_URL`` or Keychain ``co.wispralt.import-pg``) becomes ``PG*``
  env vars for the child only, never argv or logs. ``token_hash`` is never selected.
* A paused/unreachable project → ``ExportBlocked`` + ``BLOCKED.json``. Nothing here
  calls a management API or tries to unpause.
* ``export_snapshot`` writes ``<root>/supabase-snapshot-YYYY-MM-DD[-N]/`` (0700) with
  ``<table>.jsonl`` (canonical JSON lines, 0600) and ``counts.json``. Files are created
  with ``O_EXCL`` in a fresh dir: nothing is ever overwritten or deleted.
* ``JsonPagesSource(dir)`` is the manual fallback: ``<table>.json`` arrays saved from
  the same allowlisted SELECTs run through the read-only Supabase MCP. Every table
  needs its file (an empty one is saved as ``[]``); a missing file is an error.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from wispralt._errors import ExportBlocked, ExportUnavailable, ImportIntegrityError

TABLES = ("users", "usage_events", "fallback_events")
EXCLUDED = {"users": frozenset({"token_hash"})}
DEFAULT_ROOT = Path.home() / "WisprAlt-Backups"
KEYCHAIN_SERVICE = "co.wispralt.import-pg"
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
# Read-only guard: any of these words in a statement means it is not a plain SELECT.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|UPSERT|TRUNCATE|ALTER|DROP|CREATE|GRANT|REVOKE|COPY|CALL"
    r"|DO|LOCK|VACUUM|SET|RESET|EXECUTE|PREPARE|COMMENT|REFRESH|CLUSTER|REINDEX|INTO)\b",
    re.IGNORECASE,
)
_BLOCKED_MARKERS = (
    "could not translate host",
    "connection refused",
    "timeout",
    "timed out",
    "tenant or user not found",
    "network is unreachable",
)


@dataclass(frozen=True)
class TableDump:
    count: int  # count(*) in the same snapshot as ``rows``
    max_id: int | None
    rows: list[dict]


class RowSource(Protocol):
    def columns(self, table: str) -> list[str]: ...
    def dump(self, plan: dict[str, list[str]]) -> dict[str, TableDump]:
        """Every table in *plan* (table -> columns) from ONE consistent snapshot."""
        ...


SNAPSHOT_BEGIN = "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;"


def _check(select: str) -> str:
    if not select.startswith("SELECT ") or ";" in select or _FORBIDDEN.search(select):
        raise ValueError(f"refusing non-SELECT SQL: {select[:60]!r}")
    return select


def _wrap(select: str) -> str:
    """A catalog read: one SELECT inside a read-only transaction."""
    return f"BEGIN TRANSACTION READ ONLY;\n{_check(select)};\nCOMMIT;\n"


def _snapshot_script(selects: list[str]) -> str:
    """The data read: every SELECT in ONE repeatable-read, read-only transaction."""
    body = "".join(f"{_check(q)};\n" for q in selects)
    return f"{SNAPSHOT_BEGIN}\n{body}COMMIT;\n"


def _table(table: str) -> str:
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}")
    return table


def _connection_url() -> str:
    url = os.environ.get("WISPRALT_IMPORT_PG_URL")
    if url:
        return url
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, check=False, timeout=10,
        )  # fmt: skip
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ExportUnavailable("no WISPRALT_IMPORT_PG_URL and no Keychain access") from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ExportUnavailable(f"set WISPRALT_IMPORT_PG_URL or Keychain item {KEYCHAIN_SERVICE!r}")
    return proc.stdout.strip()


def _child_env(url: str) -> dict[str, str]:
    """Child env: inherited env minus any PG*, plus the parsed URL and the read-only flag."""
    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql") or not parts.hostname:
        raise ExportUnavailable("connection string is not a postgres:// URL")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.pop("WISPRALT_IMPORT_PG_URL", None)
    fields = {
        "PGHOST": parts.hostname,
        "PGPORT": str(parts.port or 5432),
        "PGUSER": unquote(parts.username or ""),
        "PGPASSWORD": unquote(parts.password or ""),
        "PGDATABASE": unquote(parts.path.lstrip("/")) or "postgres",
        "PGSSLMODE": parse_qs(parts.query).get("sslmode", ["require"])[0],
        "PGCONNECT_TIMEOUT": "15",
        "PGOPTIONS": "-c default_transaction_read_only=on",
    }
    env.update({k: v for k, v in fields.items() if v})
    return env


class PsqlSource:
    def __init__(self, url: str | None = None, *, psql: str = "psql", timeout: float = 900) -> None:
        self._env = _child_env(url or _connection_url())
        self._psql = psql
        self._timeout = timeout

    def _run(self, script: str) -> str:
        try:
            proc = subprocess.run(
                [self._psql, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                input=script, capture_output=True, text=True, env=self._env,
                timeout=self._timeout, check=False,
            )  # fmt: skip
        except FileNotFoundError as exc:
            raise ExportUnavailable(f"{self._psql} not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise ExportBlocked("timeout talking to Supabase") from exc
        if proc.returncode != 0:
            err = proc.stderr.strip()
            if any(m in err.lower() for m in _BLOCKED_MARKERS):
                raise ExportBlocked(err[:300])
            raise ExportUnavailable(err[:300] or f"psql exited {proc.returncode}")
        return proc.stdout

    def _query(self, select: str) -> Any:
        return json.loads(self._run(_wrap(select)).strip() or "null")

    def columns(self, table: str) -> list[str]:
        return self._query(
            "SELECT coalesce(json_agg(column_name::text ORDER BY ordinal_position), '[]')"
            " FROM information_schema.columns"
            f" WHERE table_schema = 'wispralt' AND table_name = '{_table(table)}'"
        )

    def dump(self, plan: dict[str, list[str]]) -> dict[str, TableDump]:
        selects: list[str] = []
        for table, cols in plan.items():
            t = _table(table)
            if not cols or "id" not in cols or any(not _IDENT.match(c) for c in cols):
                raise ValueError(f"bad column list for {table}")
            names = ", ".join(f'"{c}"' for c in cols)
            selects += [
                f"SELECT json_build_object('table', '{t}', 'count', count(*), 'max_id', max(id))"
                f"::text FROM wispralt.{t}",
                f"SELECT row_to_json(p)::text FROM (SELECT {names} FROM wispralt.{t}) p"
                " ORDER BY p.id",
            ]
        return _parse_dump(self._run(_snapshot_script(selects)), list(plan))


def _parse_dump(out: str, tables: list[str]) -> dict[str, TableDump]:
    """Per table: one header line, then exactly ``count`` row lines; nothing else."""
    lines = iter(out.splitlines())
    got: dict[str, TableDump] = {}
    try:
        for table in tables:
            head = json.loads(next(lines))
            if head.get("table") != table:
                raise ImportIntegrityError(f"export stream out of order at {table}")
            count = int(head["count"])
            rows = [json.loads(next(lines)) for _ in range(count)]
            got[table] = TableDump(count, head["max_id"], rows)
    except (StopIteration, ValueError, KeyError, TypeError) as exc:
        raise ImportIntegrityError(f"export stream truncated or malformed: {exc!r}") from exc
    if any(line.strip() for line in lines):
        raise ImportIntegrityError("export stream has unexpected trailing lines")
    return got


class JsonPagesSource:
    """Rows saved by hand from the read-only MCP: ``<dir>/<table>.json`` = a JSON array.

    Every table needs its file: a missing (or unreadable / non-array) file is an
    incomplete export and raises ``ImportIntegrityError``. An empty table is
    saved explicitly as ``[]``.
    """

    def __init__(self, directory: Path) -> None:
        self._rows: dict[str, list[dict]] = {}
        for t in TABLES:
            f = Path(directory) / f"{t}.json"
            if not f.is_file():
                raise ImportIntegrityError(f"{f} is missing; save an empty table as []")
            try:
                rows = json.loads(f.read_text())
            except ValueError as exc:  # also an empty (0-byte) file: never a table
                raise ImportIntegrityError(f"{f} is not valid JSON: {exc}") from exc
            if not isinstance(rows, list) or not all(
                isinstance(r, dict) and "id" in r for r in rows
            ):
                raise ImportIntegrityError(f"{f} must be a JSON array of rows with an id")
            self._rows[t] = sorted(rows, key=lambda r: r["id"])

    def columns(self, table: str) -> list[str]:
        rows = self._rows[_table(table)]
        return list(rows[0]) if rows else []

    def dump(self, plan: dict[str, list[str]]) -> dict[str, TableDump]:
        out = {}
        for table, cols in plan.items():
            rows = self._rows[_table(table)]
            out[table] = TableDump(
                len(rows),
                rows[-1]["id"] if rows else None,
                [{c: r.get(c) for c in cols} for r in rows],
            )
        return out


def _canon(row: dict) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_file(path: Path) -> int:
    """Create ``path`` 0600; never opens an existing file."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def _write_new(path: Path, data: bytes) -> None:
    fd = _new_file(path)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_new(path: Path, obj: Any) -> None:
    _write_new(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode())


def _fresh_dir(root: Path, name: str) -> Path:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for n in range(1, 1000):
        d = root / (name if n == 1 else f"{name}-{n}")
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            continue
        d.chmod(0o700)  # mkdir mode is masked by umask
        return d
    raise ExportUnavailable(f"too many snapshots named {name} in {root}")


def _write_table(d: Path, table: str, cols: list[str], got: TableDump) -> dict[str, Any]:
    if not cols:  # table never created on this project (e.g. v3 not applied)
        _write_new(d / f"{table}.jsonl", b"")
        return {
            "source_count": 0, "exported": 0, "columns": [], "max_id": None, "absent": True,
            "sha256": hashlib.sha256(b"").hexdigest(), "exported_at": _now_iso(),
        }  # fmt: skip
    ids = [r.get("id") for r in got.rows]
    if len(got.rows) != got.count or (max(ids) if ids else None) != got.max_id:
        raise ImportIntegrityError(f"{table}: rows do not match the snapshot's count / max id")
    part = d / f"{table}.jsonl.part"  # a failed write's file is kept, not deleted
    digest = hashlib.sha256()
    fd = _new_file(part)
    try:
        for r in got.rows:
            line = (_canon({c: r.get(c) for c in cols}) + "\n").encode()
            os.write(fd, line)
            digest.update(line)
        os.fsync(fd)
    finally:
        os.close(fd)
    final = d / f"{table}.jsonl"
    if final.exists():
        raise ImportIntegrityError(f"{final} already exists")
    os.rename(part, final)  # into a fresh private dir; never onto an existing file
    return {
        "source_count": got.count, "exported": len(got.rows), "columns": cols,
        "max_id": got.max_id, "sha256": digest.hexdigest(), "exported_at": _now_iso(),
    }  # fmt: skip


def export_snapshot(src: RowSource, root: Path | None = None, today: date | None = None) -> Path:
    """Export every table into a new snapshot dir and return it (``ExportBlocked`` → BLOCKED.json)."""
    d = _fresh_dir(root or DEFAULT_ROOT, f"supabase-snapshot-{today or date.today():%Y-%m-%d}")
    try:
        cols = {t: [c for c in src.columns(t) if c not in EXCLUDED.get(t, ())] for t in TABLES}
        for t, c in cols.items():
            if c and "id" not in c:
                raise ExportUnavailable(f"{t} has no id column")
        dumped = src.dump({t: c for t, c in cols.items() if c})  # one consistent snapshot
        empty = TableDump(0, None, [])
        counts = {t: _write_table(d, t, cols[t], dumped.get(t, empty)) for t in TABLES}
    except ExportBlocked as exc:
        write_json_new(d / "BLOCKED.json", {"reason": exc.reason, "at": _now_iso()})
        raise
    write_json_new(d / "counts.json", counts)
    return d
