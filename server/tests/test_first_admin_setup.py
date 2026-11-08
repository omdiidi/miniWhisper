"""Part 6 Task 3: portal first-admin setup, gated by the on-disk setup code."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import stat
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from wispralt import main, setup_code
from wispralt.accounts.service import hash_token
from wispralt.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from wispralt.config import settings
from wispralt.routes import admin_ui

if TYPE_CHECKING:
    from pathlib import Path

    from wispralt.accounts.store import AccountStore

    from .conftest import MakeUser, Wire

_CSRF = "setup-csrf"


@pytest.fixture
def app(wire: Wire) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_ui.public_router)
    app.include_router(admin_ui.authed_router)
    return wire(app)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    c = TestClient(app, base_url="https://testserver")
    c.cookies.set(CSRF_COOKIE_NAME, _CSRF)
    return c


def _code(data_dir: Path) -> str:
    return setup_code.code_path(data_dir).read_text().strip()


def _post(client: TestClient, code: str, label: str = "owner"):
    return client.post("/admin/setup", data={"code": code, "label": label, "csrf_token": _CSRF})


def test_prepare_writes_a_0600_code_and_logs_only_its_path(
    accounts: AccountStore, _data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        path = setup_code.prepare(accounts, _data_dir)
    assert path == _data_dir / "setup-code"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    code = _code(_data_dir)
    assert re.fullmatch(r"[0-9a-f]{32}", code)  # 128 bits
    assert any(str(path) in r.getMessage() for r in caplog.records)
    assert not any(code in r.getMessage() for r in caplog.records)


def test_prepare_with_an_admin_writes_nothing_and_clears_a_stale_code(
    accounts: AccountStore, make_user: MakeUser, _data_dir: Path
) -> None:
    make_user(role="admin")
    setup_code.write(_data_dir)
    assert setup_code.prepare(accounts, _data_dir) is None
    assert not setup_code.code_path(_data_dir).exists()


def test_lifespan_calls_prepare() -> None:
    assert "setup_code.prepare(app.state.accounts, settings.data_dir)" in inspect.getsource(
        main._serve
    )


def test_wrong_code_is_403_and_creates_nothing(
    client: TestClient, accounts: AccountStore, _data_dir: Path
) -> None:
    setup_code.prepare(accounts, _data_dir)
    assert client.get("/admin/setup").status_code == 200
    resp = _post(client, "0" * 32)
    assert resp.status_code == 403
    assert not accounts.has_enabled_admin()
    assert setup_code.code_path(_data_dir).exists()


def test_missing_code_file_is_403(client: TestClient, accounts: AccountStore) -> None:
    assert _post(client, "anything").status_code == 403
    assert not accounts.has_enabled_admin()


def test_missing_csrf_is_403(client: TestClient, accounts: AccountStore, _data_dir: Path) -> None:
    setup_code.prepare(accounts, _data_dir)
    client.cookies.delete(CSRF_COOKIE_NAME)
    assert _post(client, _code(_data_dir)).status_code == 403
    assert not accounts.has_enabled_admin()


def test_right_code_creates_one_admin_once(
    client: TestClient, accounts: AccountStore, _data_dir: Path
) -> None:
    setup_code.prepare(accounts, _data_dir)
    resp = _post(client, _code(_data_dir), label="dana")
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    [token] = set(re.findall(r"\b[0-9a-f]{64}\b", resp.text))
    user = accounts.lookup_key(hash_token(token))
    assert user is not None and user.role == "admin" and user.label == "dana"
    assert f"{SESSION_COOKIE_NAME}={token}" in resp.headers["set-cookie"]
    assert not setup_code.code_path(_data_dir).exists()
    # The cookie now signs the admin in; setup is gone for good.
    assert client.get("/admin/").status_code == 200
    assert client.get("/admin/setup").status_code == 404
    assert _post(client, "x").status_code == 404


def test_setup_is_404_while_an_admin_exists(client: TestClient, make_user: MakeUser) -> None:
    make_user(role="admin")
    assert client.get("/admin/setup").status_code == 404


async def test_two_concurrent_setups_make_exactly_one_admin(
    app: FastAPI, accounts: AccountStore, _data_dir: Path
) -> None:
    setup_code.prepare(accounts, _data_dir)
    code = _code(_data_dir)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as ac:
        ac.cookies.set(CSRF_COOKIE_NAME, _CSRF)
        results = await asyncio.gather(
            *(
                ac.post("/admin/setup", data={"code": code, "label": f"a{i}", "csrf_token": _CSRF})
                for i in range(2)
            )
        )
    codes = sorted(r.status_code for r in results)
    assert codes[0] == 200 and codes[1] in (403, 404), codes
    assert len(accounts.list_enabled_admins()) == 1


def test_settings_data_dir_is_the_test_dir(_data_dir: Path) -> None:
    assert settings.data_dir == _data_dir  # the code file never lands in a real data_dir
