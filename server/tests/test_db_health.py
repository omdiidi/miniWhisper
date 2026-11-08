"""
test_db_health.py — ``/readyz/db`` on the local SQLite DB, and the in-request usage writer.

- ``/readyz/db``: 200, then 503 ``schema_outdated`` (a migration row gone),
  then 503 ``db_error`` (file unreadable); the body carries
  ``usage_write_failures``.
- Usage middleware: a tracked POST writes one ``usage_events`` row; a failing
  write bumps ``usage_write_failures_total`` and the response is unaffected.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

import pytest
from fastapi import Depends, FastAPI
from starlette.testclient import TestClient

from wispralt import observability
from wispralt.auth import require_api_key
from wispralt.main import _ObservabilityMiddleware
from wispralt.routes import health

if TYPE_CHECKING:
    from pathlib import Path

    from wispralt.accounts.store import User
    from wispralt.database import Database
    from wispralt.usage.store import UsageStore

    from .conftest import MakeUser, Wire


@pytest.fixture
def client(wire: Wire) -> TestClient:
    app = FastAPI()
    app.add_middleware(_ObservabilityMiddleware)
    app.include_router(health.router)

    @app.post("/transcribe/dictate")
    async def _dictate(user: User = Depends(require_api_key)) -> dict[str, str]:
        return {"text": "ok"}

    @app.get("/transcribe/file/x")
    async def _poll(user: User = Depends(require_api_key)) -> dict[str, str]:
        return {"status": "pending"}

    return TestClient(wire(app))


def test_readyz_db_reports_ok_then_schema_then_db_error(
    client: TestClient, db: Database, _data_dir: Path
) -> None:
    resp = client.get("/readyz/db")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok" and "usage_write_failures" in resp.json()

    with db.write() as con:
        con.execute("DELETE FROM schema_migrations")
    resp = client.get("/readyz/db")
    assert resp.status_code == 503 and resp.json()["reason"] == "schema_outdated"

    db.path.chmod(0o000)
    try:
        resp = client.get("/readyz/db")
    finally:
        db.path.chmod(0o600)
    assert resp.status_code == 503 and resp.json()["reason"] == "db_error"


def test_tracked_post_writes_one_usage_row(
    client: TestClient, make_user: MakeUser, usage: UsageStore
) -> None:
    user, token = make_user()
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/transcribe/dictate", headers=headers).status_code == 200
    assert client.get("/transcribe/file/x", headers=headers).status_code == 200  # GET: untracked
    rows = usage.recent_events(user.id)
    assert [(r["kind"], r["status"]) for r in rows] == [("dictate", 200)]


def test_failing_usage_write_is_counted_and_response_kept(
    client: TestClient,
    make_user: MakeUser,
    usage: UsageStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _user, token = make_user()

    def _boom(_event: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(usage, "record", _boom)
    before = observability.usage_write_failures_total.value
    with caplog.at_level(logging.ERROR):
        resp = client.post("/transcribe/dictate", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200 and resp.json() == {"text": "ok"}
    assert observability.usage_write_failures_total.value == before + 1
    assert any("OperationalError" in r.getMessage() for r in caplog.records)
    assert client.get("/readyz/db").json()["usage_write_failures"] == before + 1
