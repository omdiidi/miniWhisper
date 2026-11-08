"""
cli/client.py — the WisprAlt menubar app on this Mac.

``setup --role client|both`` installs the app from a signed DMG (verified with
``codesign`` + ``spctl``; quarantine is never stripped) or builds it from the
clone with ``scripts/build-client-local.sh``, then swaps it into place. Role
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


def install_app(ctx: Ctx, built: Path) -> None:
    """Quit the running app, copy *built* next to the target with ``ditto``, swap it in."""
    app = ctx.paths.app
    app.parent.mkdir(parents=True, exist_ok=True)
    staged = app.with_name(f".{app.name}.new")
    old = app.with_name(f".{app.name}.old")
    for leftover in (staged, old):
        shutil.rmtree(leftover, ignore_errors=True)
    quit_app(ctx)
    ctx.runner.run(["ditto", str(built), str(staged)])
    if app.exists():
        os.replace(app, old)
    os.replace(staged, app)
    shutil.rmtree(old, ignore_errors=True)


def install_dmg(ctx: Ctx, dmg: Path) -> None:
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


def build_and_install(ctx: Ctx, source: Path, *, rebuild: bool) -> None:
    current = app_version(ctx.paths.app)
    wanted = plist_version(source / "client" / "WisprAlt" / "Info.plist")
    if not rebuild and current and current == wanted:
        print(f"client {current} already installed (pass --rebuild to rebuild)")
        return
    print("building the client (needs Xcode and an Apple Development identity) …")
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
