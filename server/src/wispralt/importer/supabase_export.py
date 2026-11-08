"""
importer/supabase_export.py — read-only Supabase export into a never-deleted snapshot.

Supabase is the owner's only copy of old history, so this module can only read:

* ``PsqlSource`` builds SQL from four fixed SELECT shapes (columns, count, page);
  table names come from ``TABLES`` and column names from ``information_schema``
  (identifier-checked), ints are ints. ``_wrap`` refuses anything that is not a
  single SELECT and sends ``BEGIN TRANSACTION READ ONLY; <select>; COMMIT;`` to
  ``psql -X -q -At -v ON_ERROR_STOP=1`` with ``PGOPTIONS=-c
  default_transaction_read_only=on``. The connection string (env
  ``WISPRALT_IMPORT_PG_URL`` or Keychain ``co.wispralt.import-pg``) becomes ``PG*``
  env vars for the child only, never argv or logs. ``token_hash`` is never selected.
* A paused/unreachable project → ``ExportBlocked`` + ``BLOCKED.json``. Nothing here
  calls a management API or tries to unpause.
* ``export_snapshot`` writes ``<root>/supabase-snapshot-YYYY-MM-DD[-N]/`` (0700) with
  ``<table>.jsonl`` (canonical JSON lines, 0600) and ``counts.json``. Files are created
  with ``O_EXCL`` in a fresh dir: nothing is ever overwritten or deleted.
* ``JsonPagesSource(dir)`` is the manual fallback: ``<table>.json`` arrays saved from
  the same allowlisted SELECTs run through the read-only Supabase MCP.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
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


class RowSource(Protocol):
    def columns(self, table: str) -> list[str]: ...
    def count(self, table: str) -> tuple[int, int | None]: ...  # (count, max_id)
    def page(self, table: str, cols: list[str], after_id: int, limit: int) -> list[dict]: ...


def _wrap(select: str) -> str:
    """The only SQL text ever sent: one SELECT inside a read-only transaction."""
    if not select.startswith("SELECT ") or ";" in select or _FORBIDDEN.search(select):
        raise ValueError(f"refusing non-SELECT SQL: {select[:60]!r}")
    return f"BEGIN TRANSACTION READ ONLY;\n{select};\nCOMMIT;\n"


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
    def __init__(self, url: str | None = None, *, psql: str = "psql", timeout: float = 120) -> None:
        self._env = _child_env(url or _connection_url())
        self._psql = psql
        self._timeout = timeout

    def _query(self, select: str) -> Any:
        try:
            proc = subprocess.run(
                [self._psql, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                input=_wrap(select), capture_output=True, text=True, env=self._env,
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
        return json.loads(proc.stdout.strip() or "null")

    def columns(self, table: str) -> list[str]:
        return self._query(
            "SELECT coalesce(json_agg(column_name::text ORDER BY ordinal_position), '[]')"
            " FROM information_schema.columns"
            f" WHERE table_schema = 'wispralt' AND table_name = '{_table(table)}'"
        )

    def count(self, table: str) -> tuple[int, int | None]:
        got = self._query(
            f"SELECT json_build_object('count', count(*), 'max_id', max(id)) FROM wispralt.{_table(table)}"
        )
        return int(got["count"]), got["max_id"]

    def page(self, table: str, cols: list[str], after_id: int, limit: int) -> list[dict]:
        if not cols or any(not _IDENT.match(c) for c in cols):
            raise ValueError(f"bad column list for {table}")
        names = ", ".join(f'"{c}"' for c in cols)
        return self._query(
            "SELECT coalesce(json_agg(p ORDER BY id), '[]') FROM"
            f" (SELECT {names} FROM wispralt.{_table(table)}"
            f" WHERE id > {int(after_id)} ORDER BY id LIMIT {int(limit)}) p"
        )


class JsonPagesSource:
    """Rows saved by hand from the read-only MCP: ``<dir>/<table>.json`` = a JSON array."""

    def __init__(self, directory: Path) -> None:
        self._rows: dict[str, list[dict]] = {}
        for t in TABLES:
            f = Path(directory) / f"{t}.json"
            rows = json.loads(f.read_text()) if f.exists() else []
            self._rows[t] = sorted(rows, key=lambda r: r["id"])

    def columns(self, table: str) -> list[str]:
        rows = self._rows[_table(table)]
        return list(rows[0]) if rows else []

    def count(self, table: str) -> tuple[int, int | None]:
        rows = self._rows[_table(table)]
        return len(rows), (rows[-1]["id"] if rows else None)

    def page(self, table: str, cols: list[str], after_id: int, limit: int) -> list[dict]:
        return [r for r in self._rows[_table(table)] if r["id"] > after_id][:limit]


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


def _export_table(src: RowSource, d: Path, table: str, page_size: int) -> dict[str, Any]:
    cols = [c for c in src.columns(table) if c not in EXCLUDED.get(table, ())]
    if not cols:  # table never created on this project (e.g. v3 not applied)
        _write_new(d / f"{table}.jsonl", b"")
        return {
            "source_count": 0, "exported": 0, "columns": [], "max_id": None, "absent": True,
            "sha256": hashlib.sha256(b"").hexdigest(), "exported_at": _now_iso(),
        }  # fmt: skip
    if "id" not in cols:
        raise ExportUnavailable(f"{table} has no id column")
    for attempt in (1, 2):
        count, max_id = src.count(table)
        part = d / f"{table}.jsonl.part{attempt}"  # a failed attempt's file is kept, not deleted
        digest, n, after = hashlib.sha256(), 0, 0
        fd = _new_file(part)
        try:
            while True:
                rows = src.page(table, cols, after, page_size)
                for r in rows:
                    line = (_canon({c: r.get(c) for c in cols}) + "\n").encode()
                    os.write(fd, line)
                    digest.update(line)
                    n += 1
                if len(rows) < page_size:
                    break
                after = rows[-1]["id"]
            os.fsync(fd)
        finally:
            os.close(fd)
        if (count, max_id) == src.count(table) and n == count:
            final = d / f"{table}.jsonl"
            if final.exists():
                raise ImportIntegrityError(f"{final} already exists")
            os.rename(part, final)  # into a fresh private dir; never onto an existing file
            return {
                "source_count": count, "exported": n, "columns": cols, "max_id": max_id,
                "sha256": digest.hexdigest(), "exported_at": _now_iso(),
            }  # fmt: skip
    raise ImportIntegrityError(f"{table}: row count changed or did not match during export")


def export_snapshot(
    src: RowSource, root: Path | None = None, today: date | None = None, page_size: int = 1000
) -> Path:
    """Export every table into a new snapshot dir and return it (``ExportBlocked`` → BLOCKED.json)."""
    d = _fresh_dir(root or DEFAULT_ROOT, f"supabase-snapshot-{today or date.today():%Y-%m-%d}")
    try:
        counts = {t: _export_table(src, d, t, page_size) for t in TABLES}
    except ExportBlocked as exc:
        write_json_new(d / "BLOCKED.json", {"reason": exc.reason, "at": _now_iso()})
        raise
    write_json_new(d / "counts.json", counts)
    return d
