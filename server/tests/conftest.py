"""Shared test harness: env before any wispralt import, a per-test data_dir and Database."""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING

# Must run before the first ``wispralt`` import: config.py builds ``settings`` at import.
# DATA_DIR is forced (never a real host's data); SERVER_URL only defaults.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="wispralt-test-data-")
os.environ.setdefault("SERVER_URL", "http://test.local")

import pytest  # noqa: E402

from wispralt.accounts import service as account_service  # noqa: E402
from wispralt.accounts.store import AccountStore  # noqa: E402
from wispralt.config import settings  # noqa: E402
from wispralt.database import Database  # noqa: E402
from wispralt.jobs.store import JobStore  # noqa: E402
from wispralt.usage.store import UsageStore  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from fastapi import FastAPI

    from wispralt.accounts.store import User

    MakeUser = Callable[..., tuple[User, str]]
    Wire = Callable[[FastAPI], FastAPI]


@pytest.fixture(autouse=True)
def _data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Every test gets its own data_dir on the module-level ``settings``."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    yield tmp_path


@pytest.fixture
def db(_data_dir: Path) -> Database:
    database = Database(settings.db_path)
    database.migrate()
    return database


@pytest.fixture
def accounts(db: Database) -> AccountStore:
    return AccountStore(db)


@pytest.fixture
def usage(db: Database) -> UsageStore:
    return UsageStore(db)


@pytest.fixture
def job_store(db: Database, _data_dir: Path) -> JobStore:
    return JobStore(db, _data_dir)


@pytest.fixture
def make_user(accounts: AccountStore) -> MakeUser:
    """``make_user(role="employee", kind="employee", label=None) -> (User, plaintext)``."""
    counter = iter(range(1, 10_000))

    def _make(
        role: str = "employee", kind: str = "employee", label: str | None = None
    ) -> tuple[User, str]:
        return account_service.create_user(
            accounts, label=label or f"user-{next(counter)}", role=role, kind=kind
        )

    return _make


@pytest.fixture
def wire(db: Database, accounts: AccountStore, usage: UsageStore) -> Wire:
    """``wire(app)`` puts the per-test DB and stores on ``app.state`` (as the lifespan does)."""

    def _wire(app: FastAPI) -> FastAPI:
        app.state.db = db
        app.state.job_store = JobStore(db, settings.data_dir)
        app.state.accounts = accounts
        app.state.usage = usage
        return app

    return _wire
