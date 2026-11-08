"""Part 6 Task 5: the /admin/system snapshot, clients last seen, queue and error summaries."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from wispralt.auth import CSRF_COOKIE_NAME
from wispralt.database import Database
from wispralt.jobs.store import JobStore
from wispralt.main import create_app
from wispralt.ops import monitoring
from wispralt.usage.events import UsageEvent
from wispralt.usage.store import UsageStore

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from wispralt.settings_store import SettingsStore

    from .conftest import MakeUser, Wire

SECTIONS = {
    "host": {"name", "version", "git_sha", "uptime_s", "starts"},
    "cpu": {"pct", "load"},
    "mem": {"used", "total", "rss"},
    "disk": {"free", "total", "db_bytes", "wal_bytes", "outputs_bytes", "staging_bytes"},
    "models": {"dictation_ready", "file_loaded"},
    "queue": {"pending", "running", "failed_24h", "oldest_pending_age_s"},
    "latency": set(),
    "errors": {"rate_5xx_1h"},
    "counters": {"usage_write_failures", "dictation_save_failures", "eviction_failures", "stream_aborts"},
    "db": {"ok", "schema_version", "journal_mode"},
    "network": {"server_url", "reachable"},
    "clients": {"active", "recent"},
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monitoring._cache.clear()

    async def fake_probe(url: str) -> bool:
        return True

    monkeypatch.setattr(monitoring, "probe_reachable", fake_probe)


@pytest.fixture
def app(wire: Wire) -> FastAPI:
    app = wire(create_app())
    monitoring.on_startup(app, app.state.settings_store)
    return app


@pytest.fixture
def admin_headers(make_user: MakeUser) -> dict[str, str]:
    _u, token = make_user(role="admin")
    return {"Authorization": f"Bearer {token}"}


async def test_snapshot_has_every_section_with_sane_types(app: FastAPI, _data_dir: Path) -> None:
    (_data_dir / "outputs" / "sub").mkdir(parents=True)
    (_data_dir / "outputs" / "a.txt").write_bytes(b"x" * 10)
    (_data_dir / "outputs" / "sub" / "b.srt").write_bytes(b"x" * 7)
    (_data_dir / "staging").mkdir()
    (_data_dir / "staging" / "c.wav").write_bytes(b"x" * 5)

    snap = await monitoring.snapshot(app)
    json.dumps(snap)  # JSON-serialisable
    for section, keys in SECTIONS.items():
        assert keys <= set(snap[section]), section
    assert snap["disk"]["outputs_bytes"] == 17 and snap["disk"]["staging_bytes"] == 5
    assert snap["disk"]["db_bytes"] > 0 and snap["disk"]["free"] > 0
    assert snap["host"]["starts"] == 1 and snap["host"]["uptime_s"] >= 0
    assert 0 <= snap["cpu"]["pct"] <= 100 * 64
    assert snap["mem"]["total"] >= snap["mem"]["used"] > 0
    assert snap["db"] == {**snap["db"], "ok": True, "journal_mode": "wal"}
    assert snap["db"]["schema_version"] == snap["db"]["code_schema_version"]
    assert snap["network"]["reachable"] is True
    assert snap["models"] == {"dictation_ready": False, "file_loaded": False, "file_loading": False}


def test_server_starts_counts_up(app: FastAPI, settings_store_for: SettingsStore) -> None:
    monitoring.on_startup(app, settings_store_for)
    assert app.state.server_starts == 2


@pytest.fixture
def settings_store_for(app: FastAPI) -> SettingsStore:
    return app.state.settings_store


async def test_clients_active_within_two_minutes_else_recent(
    app: FastAPI, make_user: MakeUser, usage: UsageStore
) -> None:
    now = time.time()
    alice, _ = make_user(label="alice")
    bob, _ = make_user(label="bob")
    usage.record(UsageEvent(user_id=alice.id, ts=now - 60, kind="dictate", status=200, key_id=alice.key_id))
    usage.record(UsageEvent(user_id=bob.id, ts=now - 600, kind="dictate", status=200, key_id=bob.key_id))
    usage.record(UsageEvent(user_id=bob.id, ts=now - 30, kind="dictate", status=200))  # no key: skipped
    clients = (await monitoring.snapshot(app, now=now))["clients"]
    assert [c["user_label"] for c in clients["active"]] == ["alice"]
    assert [c["user_label"] for c in clients["recent"]] == ["bob"]


def test_latency_error_summary_and_queue_counts(
    usage: UsageStore, job_store: JobStore, make_user: MakeUser
) -> None:
    now = time.time()
    alice, _ = make_user()
    for ms, status in ((100.0, 200), (200.0, 200), (300.0, 500), (400.0, 404)):
        usage.record(UsageEvent(user_id=alice.id, ts=now - 10, kind="dictate", status=status, duration_ms=ms))
    usage.record(UsageEvent(user_id=alice.id, ts=now - 7200, kind="dictate", status=500))
    s = usage.latency_error_summary(now - 3600, now)
    assert (s["total"], s["errors"], s["server_errors"]) == (4, 2, 1)
    assert s["rate_5xx"] == 0.25 and s["error_rate"] == 0.5
    assert s["by_kind"]["dictate"] == {"count": 4, "p50_ms": 250.0, "p95_ms": pytest.approx(385.0)}

    pending = job_store.create("/tmp/a.wav", user_id=alice.id)
    running = job_store.create("/tmp/b.wav", user_id=alice.id)
    assert job_store.set_running(running)
    failed = job_store.create("/tmp/c.wav", user_id=alice.id)
    job_store.set_failed(failed, "boom")
    q = job_store.queue_counts(time.time())
    assert (q["pending"], q["running"], q["failed_24h"]) == (1, 1, 1)
    assert q["oldest_pending_age_s"] >= 0 and pending


def test_page_is_admin_only_and_polls_without_csrf(
    app: FastAPI, admin_headers: dict[str, str], make_user: MakeUser
) -> None:
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/admin/system").status_code == 401
    _e, emp = make_user()
    assert client.get("/admin/system", headers={"Authorization": f"Bearer {emp}"}).status_code == 403
    page = client.get("/admin/system", headers=admin_headers)
    assert page.status_code == 200 and 'hx-trigger="every 5s"' in page.text
    poll = client.get("/admin/system/snapshot", headers=admin_headers)
    assert poll.status_code == 200 and "Failure counters" in poll.text
    assert CSRF_COOKIE_NAME not in poll.headers.get("set-cookie", "")
    raw = client.get("/admin/system/snapshot?format=json", headers=admin_headers).json()
    assert set(SECTIONS) <= set(raw)


def test_a_broken_db_renders_db_not_ok_instead_of_500(
    app: FastAPI, admin_headers: dict[str, str], _data_dir: Path
) -> None:
    broken = Database(_data_dir / "missing" / "wispralt.db")
    app.state.db = broken
    app.state.job_store = JobStore(broken, _data_dir)
    app.state.usage = UsageStore(broken)
    client = TestClient(app, base_url="https://testserver")
    resp = client.get("/admin/system/snapshot?format=json", headers=admin_headers)
    assert resp.status_code == 200
    snap = resp.json()
    assert snap["db"]["ok"] is False and snap["db"]["reason"] == "db_error"
    assert snap["queue"]["pending"] is None and snap["clients"] == {"active": [], "recent": []}
    assert client.get("/admin/system", headers=admin_headers).status_code == 200
