"""Task 7: setup client / both — DMG verify, local build, Keychain over stdin, defaults."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from wispralt.accounts.service import hash_token
from wispralt.accounts.store import AccountStore
from wispralt.cli import client
from wispralt.cli.paths import Paths
from wispralt.database import Database

from .conftest import make_app

if TYPE_CHECKING:
    import pytest

    from .conftest import CliFn, FakeHub, FakeRunner


def _dmg(tmp_path: Path, runner: FakeRunner) -> Path:
    dmg = tmp_path / "WisprAlt-0.6.0.dmg"
    dmg.write_bytes(b"dmg")
    runner.hook(
        "hdiutil", "attach", fn=lambda argv: make_app(Path(argv[argv.index("-mountpoint") + 1]) / "WisprAlt.app", "0.6.0")
    )  # fmt: skip
    return dmg


def _building(runner: FakeRunner, clone: Path, version: str = "0.5.0") -> None:
    runner.hook(
        "bash", fn=lambda _argv: make_app(clone / "client" / "build" / "WisprAlt.app", version)
    )


def _client(cli: CliFn, *extra: str) -> tuple[int, str, str]:
    return cli("setup", "--role", "client", "--non-interactive", *extra)


def test_dmg_is_verified_then_installed(cli: CliFn, runner: FakeRunner, tmp_path: Path) -> None:
    code, out, err = _client(cli, "--dmg", str(_dmg(tmp_path, runner)))
    assert code == 0, err
    programs = [c[0] for c in runner.calls]
    order = ["hdiutil", "codesign", "spctl", "osascript", "ditto", "hdiutil", "open"]
    assert [p for p in programs if p in set(order)] == order
    attach = runner.calls[programs.index("hdiutil")]
    assert attach[:4] == ["hdiutil", "attach", "-nobrowse", "-readonly"]
    assert runner.calls[programs.index("codesign")][1:4] == ["--verify", "--deep", "--strict"]
    assert runner.calls[programs.index("spctl")][1:4] == ["-a", "-t", "exec"]
    app = Paths.resolve().app
    assert app.is_relative_to(tmp_path / "root") and client.app_version(app) == "0.6.0"
    assert "xattr" not in programs and "defaults" not in programs


def test_failed_verification_still_detaches(cli: CliFn, runner: FakeRunner, tmp_path: Path) -> None:
    runner.on("spctl", rc=3, err="rejected")
    code, _, err = _client(cli, "--dmg", str(_dmg(tmp_path, runner)))
    assert code == 1 and "spctl" in err
    assert runner.calls[-1][:2] == ["hdiutil", "detach"]
    assert not Paths.resolve().app.exists()


def test_no_dmg_builds_from_the_clone(cli: CliFn, runner: FakeRunner, clone: Path) -> None:
    _building(runner, clone)
    code, _, err = _client(cli, "--source", str(clone))
    assert code == 0, err
    assert ["bash", str(clone / "scripts" / "build-client-local.sh")] in runner.calls
    assert client.app_version(Paths.resolve().app) == "0.5.0"


def test_rerun_same_version_does_not_rebuild(cli: CliFn, runner: FakeRunner, clone: Path) -> None:
    _building(runner, clone)
    assert _client(cli, "--source", str(clone))[0] == 0
    runner.calls.clear()
    code, out, _ = _client(cli, "--source", str(clone))
    assert code == 0 and "already installed" in out
    assert not any(c[0] in {"bash", "ditto"} for c in runner.calls)
    assert _client(cli, "--source", str(clone), "--rebuild")[0] == 0
    assert any(c[0] == "bash" for c in runner.calls)


def test_no_clone_and_no_dmg(cli: CliFn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, _, err = _client(cli)
    assert code == 2 and "--dmg" in err


def test_both_points_the_app_at_the_host(
    cli: CliFn, runner: FakeRunner, hub: FakeHub, clone: Path, tmp_path: Path
) -> None:
    _building(runner, clone)
    tok = tmp_path / "admin.key"
    code, _, err = cli(
        "setup", "--role", "both", "--non-interactive", "--source", str(clone),
        "--token-file", str(tok), "--no-open",
    )  # fmt: skip
    assert code == 0, err
    token = tok.read_text().strip()
    assert [
        "defaults", "write", client.DEFAULTS_DOMAIN, "serverURL", "-string", "http://127.0.0.1:8000"
    ] in runner.calls  # fmt: skip
    i = runner.calls.index(["security", "-i"])
    assert runner.inputs[i] == f"add-generic-password -U -s co.wispralt -a default -w {token}\n"
    assert token not in runner.argv_text()
    assert not any(
        c[:3] == ["defaults", "write", client.DEFAULTS_DOMAIN] and "mode" in c for c in runner.calls
    )
    admin = AccountStore(Database(Paths.resolve().db_path)).lookup_key(hash_token(token))
    assert admin is not None and admin.role == "admin"
    assert "xattr" not in {c[0] for c in runner.calls}


def test_client_mode_local_is_explicit(cli: CliFn, runner: FakeRunner, clone: Path) -> None:
    _building(runner, clone)
    assert _client(cli, "--source", str(clone), "--client-mode", "local")[0] == 0
    assert ["defaults", "write", client.DEFAULTS_DOMAIN, "mode", "-string", "local"] in runner.calls
    assert ["security", "-i"] not in runner.calls  # role client: no host key to hand over
