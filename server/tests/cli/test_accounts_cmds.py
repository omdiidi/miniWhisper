"""Task 3: users / keys / retention commands and the one-way key handoff."""

from __future__ import annotations

import json
import logging
import re
import stat
import time
from typing import TYPE_CHECKING

import pytest
from fastapi import Depends, FastAPI
from starlette.testclient import TestClient

from wispralt.accounts import service
from wispralt.accounts.service import hash_token
from wispralt.accounts.store import AccountStore, User
from wispralt.auth import require_api_key
from wispralt.cli import host
from wispralt.database import Database
from wispralt.jobs.store import JobStore

if TYPE_CHECKING:
    from pathlib import Path

    from wispralt.cli.paths import Ctx
    from wispralt.cli.system import Deps

    from .conftest import CliFn

_TOKEN = re.compile(r"\b[0-9a-f]{64}\b")


@pytest.fixture
def accounts(ctx: Ctx) -> AccountStore:
    host.bootstrap_admin(ctx.paths.data_dir, "owner")
    return AccountStore(Database(ctx.paths.db_path))


def _users(accounts: AccountStore) -> int:
    return len(accounts.list_people()) + len(accounts.list_integrations())


def test_no_tty_and_no_token_file_refuses_before_minting(
    cli: CliFn, accounts: AccountStore
) -> None:
    before = _users(accounts)
    for argv in (("users", "add", "amy"), ("keys", "rotate", "1"), ("keys", "rotate-admin")):
        code, out, err = cli(*argv)
        assert code == 2 and "--token-file" in err and not _TOKEN.search(out + err)
    assert _users(accounts) == before
    assert len(accounts.list_keys()) == 1  # the admin's key, never rotated


def test_token_file_is_new_and_private(cli: CliFn, accounts: AccountStore, tmp_path: Path) -> None:
    tok = tmp_path / "amy.key"
    code, out, _ = cli("users", "add", "amy", "--token-file", str(tok))
    assert code == 0 and str(tok) in out and not _TOKEN.search(out)
    assert stat.S_IMODE(tok.stat().st_mode) == 0o600
    token = tok.read_text().strip()
    user = accounts.lookup_key(hash_token(token))
    assert user is not None and user.label == "amy" and user.role == "employee"

    before = _users(accounts)
    code, _, err = cli("users", "add", "bob", "--token-file", str(tok))  # exists → refused
    assert code == 2 and "exists" in err
    assert _users(accounts) == before and tok.read_text().strip() == token


def test_tty_prints_the_key_once(
    cli: CliFn, accounts: AccountStore, deps: Deps, caplog: pytest.LogCaptureFixture
) -> None:
    deps.stdout_isatty = True
    with caplog.at_level(logging.DEBUG):
        code, out, _ = cli("users", "add", "cara", "--kind", "integration")
    assert code == 0
    [token] = _TOKEN.findall(out)
    assert "http://127.0.0.1:8000" in out
    assert accounts.lookup_key(hash_token(token)) is not None
    assert not any(token in r.getMessage() for r in caplog.records)


def test_rotate_admin(cli: CliFn, accounts: AccountStore, tmp_path: Path, deps: Deps) -> None:
    [admin] = accounts.list_enabled_admins()
    service.create_user(accounts, label="second", role="admin", kind="employee")
    code, _, err = cli("keys", "rotate-admin", "--token-file", str(tmp_path / "a.key"))
    assert code == 2 and "--user-id" in err and not (tmp_path / "a.key").exists()

    deps.stdout_isatty = True
    code, out, _ = cli("keys", "rotate-admin", "--user-id", str(admin.id))
    [token] = _TOKEN.findall(out)
    rotated = accounts.lookup_key(hash_token(token))
    assert code == 0 and rotated is not None and rotated.id == admin.id


def test_users_list_disable_enable(cli: CliFn, accounts: AccountStore, tmp_path: Path) -> None:
    user, old = service.create_user(accounts, label="dave", role="employee", kind="employee")
    code, out, _ = cli("users", "list", "--json")
    rows = json.loads(out)
    assert code == 0 and {r["label"] for r in rows} == {"owner", "dave"}

    assert cli("users", "disable", str(user.id))[0] == 0
    assert accounts.lookup_key(hash_token(old)) is None
    assert cli("users", "disable", str(user.id))[0] == 1  # already disabled

    code, _, _ = cli("users", "enable", str(user.id), "--token-file", str(tmp_path / "d.key"))
    new = (tmp_path / "d.key").read_text().strip()
    assert code == 0 and accounts.lookup_key(hash_token(new)) is not None


def test_last_admin_cannot_be_disabled(cli: CliFn, accounts: AccountStore) -> None:
    [admin] = accounts.list_enabled_admins()
    code, _, err = cli("users", "disable", str(admin.id))
    assert code == 1 and "last enabled admin" in err
    assert accounts.has_enabled_admin()


def test_keys_list_rotate_revoke(cli: CliFn, accounts: AccountStore, tmp_path: Path) -> None:
    user, old = service.create_user(accounts, label="erin", role="employee", kind="employee")
    code, _, _ = cli("keys", "rotate", str(user.id), "--token-file", str(tmp_path / "e.key"))
    assert code == 0 and accounts.lookup_key(hash_token(old)) is None
    keys = json.loads(cli("keys", "list", "--user", str(user.id), "--json")[1])
    assert [k["revoked"] == "-" for k in keys] == [True, False]  # newest first
    assert cli("keys", "revoke", str(keys[0]["id"]))[0] == 0
    assert cli("keys", "revoke", str(keys[0]["id"]))[0] == 1
    assert cli("keys", "rotate", "999", "--token-file", str(tmp_path / "x.key"))[0] == 1


def test_commands_need_a_database(cli: CliFn, ctx: Ctx) -> None:
    code, _, err = cli("users", "list")
    assert code == 1 and "wispralt setup" in err
    assert not ctx.paths.db_path.exists()


def test_retention_set_needs_confirmation(cli: CliFn, accounts: AccountStore, ctx: Ctx) -> None:
    jobs = JobStore(Database(ctx.paths.db_path), ctx.paths.data_dir)
    [admin] = accounts.list_enabled_admins()
    jobs.insert_dictation(
        user_id=admin.id, text="old", duration_ms=1, client_app_version=None, dedup_id=None
    )
    with jobs.db.write() as con:
        con.execute("UPDATE dictations SET created_at = ?", (time.time() - 40 * 86400,))

    code, out, err = cli("retention", "set", "30")
    assert code == 2 and "--yes" in err and "deletes 1 dictations" in out
    assert jobs.retention_days() == 0
    assert cli("retention", "set", "30", "--yes")[0] == 0
    assert jobs.retention_days() == 30
    shown = json.loads(cli("retention", "show", "--json")[1])
    assert shown == {"retention_days": 30, "would_purge": {"dictations": 1, "jobs": 0}}
    assert "keep forever" in cli("retention", "set", "0", "--yes")[1]


def test_retention_set_interactive_no(
    cli: CliFn, accounts: AccountStore, deps: Deps, answers: list[str], ctx: Ctx
) -> None:
    deps.stdin_isatty = True
    answers.append("n")
    code, out, _ = cli("retention", "set", "7")
    assert code == 0 and "unchanged" in out
    assert JobStore(Database(ctx.paths.db_path), ctx.paths.data_dir).retention_days() == 0


def test_cli_revoke_is_seen_by_the_running_app(
    cli: CliFn, accounts: AccountStore, tmp_path: Path
) -> None:
    user, token = service.create_user(accounts, label="fay", role="employee", kind="employee")
    app = FastAPI()

    @app.get("/whoami")
    async def _whoami(u: User = Depends(require_api_key)) -> dict[str, int]:  # noqa: B008
        return {"id": u.id}

    app.state.accounts = accounts  # the server's own handle; the CLI opens its own
    client = TestClient(app)
    bearer = {"Authorization": f"Bearer {token}"}
    assert client.get("/whoami", headers=bearer).status_code == 200
    assert cli("keys", "revoke", str(user.key_id))[0] == 0
    assert client.get("/whoami", headers=bearer).status_code == 401
