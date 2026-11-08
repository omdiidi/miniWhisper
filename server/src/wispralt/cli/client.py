"""
cli/client.py — the WisprAlt menubar app on this Mac.

``setup --role client|both`` installs the app from a signed DMG (verified with
``codesign`` + ``spctl``; quarantine is never stripped) or builds it from the
clone with ``scripts/build-client-local.sh``, then swaps it into place (an
interrupted swap's ``.old`` copy is restored on the next run, never erased). Role
``both`` also points the app at the local host (``defaults``) and stores the
admin key in the Keychain through ``security -i`` on **stdin**, so the key is
never in an argv; ``-T`` trusts the installed app so it reads the key without a prompt. The OpenAI key is never handled here.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from wispralt.cli.paths import find_source
from wispralt.cli.system import CliError

if TYPE_CHECKING:
    import argparse

    from wispralt.cli.paths import Ctx

KEYCHAIN_SERVICE = "co.wispralt"  # Local-mode key (KeychainHelper.swift)
KEYCHAIN_ACCOUNT = "default"
OPENAI_SERVICE = "co.wispralt.openai"
DEFAULTS_DOMAIN = "co.wispralt.WisprAlt"  # also the bundle id
BUILD_SCRIPT = "scripts/build-client-local.sh"


def plist_version(plist: Path) -> str | None:
    try:
        with plist.open("rb") as f:
            return str(plistlib.load(f).get("CFBundleShortVersionString") or "") or None
    except (OSError, plistlib.InvalidFileException):
        return None


def app_version(app: Path) -> str | None:
    return plist_version(app / "Contents" / "Info.plist")


def quit_app(ctx: Ctx) -> None:
    ctx.runner.run(
        ["osascript", "-e", f'tell application id "{DEFAULTS_DOMAIN}" to quit'], check=False
    )


def _swap_paths(app: Path) -> tuple[Path, Path]:
    return app.with_name(f".{app.name}.new"), app.with_name(f".{app.name}.old")


def recover_interrupted_install(app: Path) -> bool:
    """An install that died after moving the app to ``.old``: put that copy back."""
    _staged, old = _swap_paths(app)
    if app.exists() or app.is_symlink() or not old.is_dir():
        return False
    os.replace(old, app)
    print(f"restored {app} from an interrupted install")
    return True


def install_app(ctx: Ctx, built: Path) -> None:
    """Quit the running app, copy *built* next to the target with ``ditto``, swap it in.

    Crash-safe: an interrupted swap's ``.old`` copy is restored first (never deleted
    while it is the only app), and ``.old`` goes only once the new app is in place.
    """
    app = ctx.paths.app
    app.parent.mkdir(parents=True, exist_ok=True)
    staged, old = _swap_paths(app)
    recover_interrupted_install(app)
    shutil.rmtree(staged, ignore_errors=True)
    quit_app(ctx)
    ctx.runner.run(["ditto", str(built), str(staged)])
    if app.exists():
        shutil.rmtree(old, ignore_errors=True)  # stale: the installed app is the newer copy
        os.replace(app, old)
    try:
        os.replace(staged, app)
    except OSError:
        recover_interrupted_install(app)
        raise
    shutil.rmtree(old, ignore_errors=True)


def install_dmg(ctx: Ctx, dmg: Path) -> None:
    recover_interrupted_install(ctx.paths.app)  # even if this attempt fails, an app is back
    if not dmg.is_file():
        raise CliError(2, f"no DMG at {dmg}")
    mount = Path(tempfile.mkdtemp(prefix="wispralt-dmg-"))
    r = ctx.runner
    try:
        r.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount), str(dmg)])
        apps = sorted(mount.glob("*.app"))
        if len(apps) != 1:
            raise CliError(1, f"expected one .app in {dmg}, found {len(apps)}")
        r.run(["codesign", "--verify", "--deep", "--strict", str(apps[0])])
        r.run(["spctl", "-a", "-t", "exec", str(apps[0])])
        install_app(ctx, apps[0])
    finally:
        r.run(["hdiutil", "detach", str(mount)], check=False)
        shutil.rmtree(mount, ignore_errors=True)


def app_commit(app: Path) -> str | None:
    """The clone commit ``build-client-local.sh`` stamped as ``WisprAltSourceCommit``."""
    try:
        with (app / "Contents" / "Info.plist").open("rb") as f:
            return str(plistlib.load(f).get("WisprAltSourceCommit") or "") or None
    except (OSError, plistlib.InvalidFileException):
        return None


def build_and_install(ctx: Ctx, source: Path, *, rebuild: bool) -> None:
    """Build and install unless the installed app is this version built from this commit.

    The version alone is not enough: it rarely changes between builds, so an old
    app with the same version would be kept. A clone that is not a git checkout
    falls back to the version check.
    """
    recover_interrupted_install(ctx.paths.app)
    current = app_version(ctx.paths.app)
    wanted = plist_version(source / "client" / "WisprAlt" / "Info.plist")
    head = ctx.runner.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=False)
    commit = head.stdout.strip() if head.returncode == 0 else ""
    same_build = not commit or app_commit(ctx.paths.app) == commit
    if not rebuild and current and current == wanted and same_build:
        print(f"client {current} already installed (pass --rebuild to rebuild)")
        return
    print("building the client (needs the Command Line Tools and an Apple Development identity) …")
    ctx.runner.run(["bash", str(source / BUILD_SCRIPT)])
    install_app(ctx, source / "client" / "build" / "WisprAlt.app")


def _arg(value: str) -> str:
    """One ``security -i`` word: quoted when it holds whitespace (its parser handles that)."""
    if '"' in value or "\n" in value:
        raise CliError(2, f"unsupported character in {value!r}")
    return f'"{value}"' if any(c.isspace() for c in value) else value


def store_local_key(ctx: Ctx, token: str) -> None:
    """Keychain ``co.wispralt`` via ``security -i`` (stdin): the key never enters an argv.

    ``-T`` puts the installed app (and ``/usr/bin/security``) on the item's access
    list, so the app's ``SecItemCopyMatching`` read does not raise a password prompt.
    The list pins the app's code signature: a client re-signed by another identity
    gets one "Always Allow" prompt.
    """
    line = (
        f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a {KEYCHAIN_ACCOUNT}"
        f" -T {_arg(str(ctx.paths.app))} -T /usr/bin/security -w {token}\n"
    )
    ctx.runner.run(["security", "-i"], input=line)


def setup_client(ctx: Ctx, args: argparse.Namespace, *, host_token: str | None) -> None:
    if args.dmg:
        install_dmg(ctx, args.dmg.expanduser().absolute())
    else:
        source = find_source(ctx, args.source)
        if source is None:
            raise CliError(2, "pass --dmg PATH, or run from a WisprAlt clone (--source DIR)")
        build_and_install(ctx, source, rebuild=args.rebuild)
    r = ctx.runner
    if ctx.state is not None and ctx.state.role == "both":
        r.run(["defaults", "write", DEFAULTS_DOMAIN, "serverURL", "-string", ctx.url])
        if host_token is not None:
            store_local_key(ctx, host_token)
            print("the client now uses this host with the admin key (Keychain co.wispralt)")
    if args.client_mode:
        r.run(["defaults", "write", DEFAULTS_DOMAIN, "mode", "-string", args.client_mode])
    r.run(["open", str(ctx.paths.app)], check=False)
    print(f"client installed at {ctx.paths.app}")
