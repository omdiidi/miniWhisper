"""Part 7 Task 2: the read-only Supabase export (FakeSource + a fake psql; no network)."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from wispralt._errors import ExportBlocked, ExportUnavailable, ImportIntegrityError
from wispralt.importer import supabase_export as sx

from ._import_fakes import FakeSource, supabase_rows

DAY = date(2026, 11, 7)
URL = "postgresql://reader:s3cr%40t@db.example.supabase.co:6543/postgres?sslmode=require"
WRITE_VERBS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|ALTER|DROP|CREATE|GRANT|REVOKE|COPY|CALL|DO|LOCK"
    r"|VACUUM|SET|EXECUTE|INTO)\b|pg_|unpause|restore_project",
    re.IGNORECASE,
)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _tree_hash(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(d.rglob("*")):
        h.update(f.name.encode() + f.read_bytes())
    return h.hexdigest()


def test_snapshot_layout_modes_and_counts(tmp_path: Path) -> None:
    d = sx.export_snapshot(FakeSource(), tmp_path, DAY)
    assert d.name == "supabase-snapshot-2026-11-07" and _mode(d) == 0o700
    files = sorted(p.name for p in d.iterdir())
    assert files == ["counts.json", "fallback_events.jsonl", "usage_events.jsonl", "users.jsonl"]
    assert all(_mode(p) == 0o600 for p in d.iterdir())
    counts = json.loads((d / "counts.json").read_text())
    for t, n in (("users", 3), ("usage_events", 10), ("fallback_events", 4)):
        c = counts[t]
        data = (d / f"{t}.jsonl").read_bytes()
        assert c["source_count"] == c["exported"] == n == len(data.splitlines())
        assert c["sha256"] == hashlib.sha256(data).hexdigest()
        assert {"columns", "max_id", "exported_at"} <= c.keys()
    assert "token_hash" not in counts["users"]["columns"]
    assert b"token_hash" not in (d / "users.jsonl").read_bytes()
    line = (d / "users.jsonl").read_text().splitlines()[0]
    assert line == json.dumps(json.loads(line), sort_keys=True, separators=(",", ":"))


def test_second_export_same_day_gets_suffix_and_leaves_first_untouched(tmp_path: Path) -> None:
    first = sx.export_snapshot(FakeSource(), tmp_path, DAY)
    before = _tree_hash(first)
    second = sx.export_snapshot(FakeSource(), tmp_path, DAY)
    third = sx.export_snapshot(FakeSource(), tmp_path, DAY)
    assert (second.name, third.name) == (first.name + "-2", first.name + "-3")
    assert _tree_hash(first) == before


def test_all_tables_come_from_one_dump(tmp_path: Path) -> None:
    """Review fix: one consistent snapshot for every table, not separately paged reads."""
    rows = supabase_rows()
    rows["usage_events"] = [dict(rows["usage_events"][0], id=i) for i in range(1, 2501)]
    src = FakeSource(rows)
    d = sx.export_snapshot(src, tmp_path, DAY)
    assert [c for c in src.calls if c[0] == "dump"] == [("dump", ",".join(sx.TABLES))]
    ids = [json.loads(x)["id"] for x in (d / "usage_events.jsonl").read_text().splitlines()]
    assert ids == list(range(1, 2501))


class _Short(FakeSource):
    """The snapshot's count says one more row than it returned."""

    def dump(self, plan: dict[str, list[str]]) -> dict[str, sx.TableDump]:
        got = super().dump(plan)
        u = got["usage_events"]
        return {**got, "usage_events": sx.TableDump(u.count + 1, u.max_id, u.rows)}


def test_count_mismatch_fails_and_writes_no_counts(tmp_path: Path) -> None:
    with pytest.raises(ImportIntegrityError):
        sx.export_snapshot(_Short(), tmp_path, DAY)
    (d,) = tmp_path.iterdir()
    assert not (d / "counts.json").exists() and not (d / "usage_events.jsonl").exists()


def test_absent_table_exports_empty(tmp_path: Path) -> None:
    rows = supabase_rows()
    rows["fallback_events"] = []
    d = sx.export_snapshot(FakeSource(rows), tmp_path, DAY)
    c = json.loads((d / "counts.json").read_text())["fallback_events"]
    assert (c["source_count"], c["absent"]) == (0, True)


class _FakePsql:
    """Stands in for ``subprocess.run``: answers the allowlisted SELECTs from fake rows."""

    def __init__(self, stderr: str = "", code: int = 0) -> None:
        self.rows = supabase_rows()
        self.calls: list[dict[str, Any]] = []
        self.stderr, self.code = stderr, code

    def _answer(self, sql: str) -> list[str]:
        if "information_schema.columns" in sql:
            t = re.search(r"table_name = '(\w+)'", sql)[1]
            return [json.dumps(list(self.rows[t][0]))]
        if "json_build_object" in sql:
            t = re.search(r"FROM wispralt\.(\w+)", sql)[1]
            rows = self.rows[t]
            return [
                json.dumps({"table": t, "count": len(rows), "max_id": max(r["id"] for r in rows)})
            ]
        m = re.search(r"\(SELECT (.+?) FROM wispralt\.(\w+)\) p ORDER BY p\.id$", sql)
        cols = [c.strip('" ') for c in m[1].split(",")]
        return [
            json.dumps({c: r[c] for c in cols})
            for r in sorted(self.rows[m[2]], key=lambda r: r["id"])
        ]

    def __call__(self, args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        self.calls.append({"args": args, **kw})
        if self.code:
            return subprocess.CompletedProcess(args, self.code, "", self.stderr)
        stmts = [x.strip() for x in kw["input"].split(";") if x.strip().startswith("SELECT")]
        out = [line for q in stmts for line in self._answer(q)]
        return subprocess.CompletedProcess(args, 0, "\n".join(out) + "\n", "")


def test_psql_sends_only_read_only_selects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePsql()
    monkeypatch.setattr(sx.subprocess, "run", fake)
    d = sx.export_snapshot(sx.PsqlSource(URL), tmp_path, DAY)
    assert json.loads((d / "counts.json").read_text())["users"]["exported"] == 3
    data_sessions = [c for c in fake.calls if "information_schema" not in c["input"]]
    assert len(data_sessions) == 1  # every table's data from ONE psql session
    snap_sql = data_sessions[0]["input"]
    assert snap_sql.startswith(
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSELECT "
    )
    assert snap_sql.count("BEGIN") == 1 and snap_sql.count("COMMIT") == 1
    assert all(f"wispralt.{t}" in snap_sql for t in sx.TABLES)
    for call in fake.calls:
        sql = call["input"]
        begin, *stmts, commit, tail = sql.split(";")
        assert begin.startswith("BEGIN TRANSACTION ") and commit.strip() == "COMMIT"
        assert tail == "\n" and all(q.strip().startswith("SELECT ") for q in stmts)
        if "information_schema" in sql:
            assert begin == "BEGIN TRANSACTION READ ONLY" and len(stmts) == 1
        assert not WRITE_VERBS.search(sql), sql
        assert "token_hash" not in sql.split("information_schema")[0]
        args = call["args"]
        assert args[:6] == ["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1"]
        assert not any("s3cr" in a or "supabase.co" in a for a in args)  # URL never in argv
        env = call["env"]
        assert env["PGOPTIONS"] == "-c default_transaction_read_only=on"
        assert (env["PGHOST"], env["PGPORT"], env["PGPASSWORD"]) == (
            "db.example.supabase.co", "6543", "s3cr@t",
        )  # fmt: skip
        assert "WISPRALT_IMPORT_PG_URL" not in env


def test_truncated_psql_stream_is_an_integrity_failure() -> None:
    out = '{"table": "users", "count": 2, "max_id": 2}\n{"id": 1}\n'
    with pytest.raises(ImportIntegrityError):
        sx._parse_dump(out, ["users"])
    with pytest.raises(ImportIntegrityError):
        sx._parse_dump(out.replace('"count": 2', '"count": 1') + "extra\n", ["users"])


def test_wrap_refuses_anything_but_a_select() -> None:
    for bad in ("DELETE FROM wispralt.users", "SELECT 1; DROP TABLE x", "SELECT * INTO t FROM u"):
        with pytest.raises(ValueError):
            sx._wrap(bad)
    with pytest.raises(ValueError):
        sx._snapshot_script(["SELECT 1", "UPDATE wispralt.users SET label = 'x'"])
    with pytest.raises(ValueError):
        sx.PsqlSource(URL).dump({"users": ["id", "label; drop"]})
    with pytest.raises(ValueError):
        sx.PsqlSource(URL).dump({"api_keys": ["id"]})


@pytest.mark.parametrize(
    "stderr",
    [
        'psql: error: connection to server at "x" failed: Connection refused',
        'psql: error: could not translate host name "db.x.supabase.co"',
        "FATAL:  Tenant or user not found",
    ],
)
def test_unreachable_project_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    monkeypatch.setattr(sx.subprocess, "run", _FakePsql(stderr=stderr, code=2))
    with pytest.raises(ExportBlocked):
        sx.export_snapshot(sx.PsqlSource(URL), tmp_path, DAY)
    (d,) = tmp_path.iterdir()
    blocked = json.loads((d / "BLOCKED.json").read_text())
    assert blocked["reason"] and _mode(d / "BLOCKED.json") == 0o600
    assert not (d / "counts.json").exists()


def test_other_psql_errors_are_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sx.subprocess, "run", _FakePsql(stderr="ERROR: permission denied", code=3))
    with pytest.raises(ExportUnavailable):
        sx.export_snapshot(sx.PsqlSource(URL), tmp_path, DAY)


def test_url_from_env_and_missing_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WISPRALT_IMPORT_PG_URL", URL)
    assert sx._connection_url() == URL
    monkeypatch.delenv("WISPRALT_IMPORT_PG_URL")
    monkeypatch.setattr(
        sx.subprocess, "run", lambda a, **k: subprocess.CompletedProcess(a, 44, "", "not found")
    )
    with pytest.raises(ExportUnavailable):
        sx._connection_url()


def test_json_pages_source_round_trip(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    for t, rows in supabase_rows().items():
        (pages / f"{t}.json").write_text(json.dumps(rows))
    d = sx.export_snapshot(sx.JsonPagesSource(pages), tmp_path / "out", DAY)
    ref = sx.export_snapshot(FakeSource(), tmp_path / "ref", DAY)
    for t in sx.TABLES:
        assert (d / f"{t}.jsonl").read_bytes() == (ref / f"{t}.jsonl").read_bytes()


def _pages(tmp_path: Path) -> Path:
    pages = tmp_path / "pages"
    pages.mkdir()
    for t, rows in supabase_rows().items():
        (pages / f"{t}.json").write_text(json.dumps(rows))
    return pages


def test_json_pages_missing_table_file_is_an_error(tmp_path: Path) -> None:
    """R3 fix: an incomplete manual export never passes as an empty table."""
    pages = _pages(tmp_path)
    (pages / "fallback_events.json").rename(pages / "fallback_events.json.bak")
    with pytest.raises(ImportIntegrityError, match="fallback_events.json is missing"):
        sx.JsonPagesSource(pages)


@pytest.mark.parametrize("body", ["", "{}", '{"rows": []}', "[1]", "[{}]", "[tru"])
def test_json_pages_bad_file_is_an_error(tmp_path: Path, body: str) -> None:
    pages = _pages(tmp_path)
    (pages / "usage_events.json").write_text(body)
    with pytest.raises(ImportIntegrityError, match="usage_events.json"):
        sx.JsonPagesSource(pages)


def test_json_pages_explicit_empty_table(tmp_path: Path) -> None:
    pages = _pages(tmp_path)
    (pages / "fallback_events.json").write_text("[]\n")
    d = sx.export_snapshot(sx.JsonPagesSource(pages), tmp_path / "out", DAY)
    assert json.loads((d / "counts.json").read_text())["fallback_events"]["source_count"] == 0


def test_importer_never_deletes() -> None:
    """Grep gate: no rmtree anywhere in importer/, unlink only in files.py's temp cleanup."""
    pkg = Path(sx.__file__).parent
    for f in pkg.glob("*.py"):
        src = f.read_text()
        assert "rmtree" not in src, f
        if f.name != "files.py":
            assert ".unlink(" not in src and "os.remove" not in src, f
