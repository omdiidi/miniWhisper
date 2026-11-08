"""
cli/main.py — ``wispralt``: argparse tree and dispatch only.

Exit codes: 0 ok, 1 operation failed, 2 usage error or missing non-interactive
answer, 3 doctor found failures. Every handler is ``fn(ctx, args) -> int`` and
raises ``CliError`` for anything else. Importing this module never imports MLX.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from wispralt.cli import host, launchd, ops
from wispralt.cli.paths import Ctx, InstallState, Paths
from wispralt.cli.system import EXIT_USAGE, CliError, Deps, version

if TYPE_CHECKING:
    from collections.abc import Callable

    Handler = Callable[[Ctx, argparse.Namespace], int]


def _group(_ctx: Ctx, args: argparse.Namespace) -> int:
    """Command groups (``users``, ``keys``, ``retention``) always dispatch to an action."""
    raise CliError(
        EXIT_USAGE, f"`{args.command}` needs an action; see `wispralt {args.command} -h`"
    )


def _common() -> argparse.ArgumentParser:
    """Global flags, accepted before or after any subcommand."""
    p = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--non-interactive", action="store_true", help="never prompt (exit 2 instead)")
    p.add_argument("--yes", action="store_true", help="answer yes to confirmations")
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(
        prog="wispralt",
        description="Install, run and maintain a WisprAlt host and/or client on this Mac.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"wispralt {version()}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def cmd(
        name: str, fn: Handler, help: str, into: argparse._SubParsersAction | None = None
    ) -> argparse.ArgumentParser:
        p = (into or sub).add_parser(name, help=help, description=help, parents=[common])
        p.set_defaults(func=fn)
        return p

    def token_file(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--token-file", type=Path, help="write the new key to this new 0600 file (no TTY)"
        )

    # setup
    s = cmd("setup", host.cmd_setup, "install or repair the host and/or client (safe to rerun)")
    s.add_argument("--role", choices=["host", "client", "both"])
    s.add_argument("--source", type=Path, help="the repo clone (default: recorded or ./)")
    s.add_argument("--data-dir", type=Path, help="host data directory")
    s.add_argument("--backup-dir", type=Path, help="where backups go")
    s.add_argument("--server-url", help="public URL clients use (default: loopback)")
    s.add_argument("--port", type=int, help="loopback port (default 8000)")
    s.add_argument("--admin-label", help="first admin's label (default: owner)")
    token_file(s)
    s.add_argument("--retention-days", type=int, help="0 keeps forever (asked once)")
    s.add_argument("--no-open", action="store_true", help="do not open the portal")
    s.add_argument("--skip-warmup", action="store_true", help="skip the model warm-up")
    s.add_argument("--dmg", type=Path, help="install the client from a signed DMG")
    s.add_argument("--client-mode", choices=["online", "local"], help="set the client's mode")
    s.add_argument("--rebuild", action="store_true", help="rebuild the client even if current")
    s.add_argument("--_resumed", action="store_true", dest="resumed", help=argparse.SUPPRESS)

    p = cmd("status", ops.cmd_status, "service state, URL, sizes and retention")
    p.add_argument("--client", action="store_true", help="also show the client app")
    p = cmd("doctor", ops.cmd_doctor, "check this Mac and the install (exit 3 on failures)")
    p.add_argument("--warmup", action="store_true", help="load the dictation model once")
    p.add_argument("--client", action="store_true", help="also check the client toolchain")
    cmd("start", launchd.cmd_start, "start the host service")
    cmd("stop", launchd.cmd_stop, "stop the host service")
    cmd("restart", launchd.cmd_restart, "restart the host service (re-renders its plist)")

    users = cmd("users", _group, "list, add, disable or enable users")
    us = users.add_subparsers(dest="action", required=True, metavar="ACTION")
    cmd("list", host.cmd_users_list, "list users", us)
    p = cmd("add", host.cmd_users_add, "add a user and print its key once", us)
    p.add_argument("label")
    p.add_argument("--role", choices=["employee", "admin"], default="employee")
    p.add_argument("--kind", choices=["employee", "integration"], default="employee")
    token_file(p)
    p = cmd("disable", host.cmd_users_disable, "disable a user (all its keys stop working)", us)
    p.add_argument("user_id", type=int)
    p = cmd("enable", host.cmd_users_enable, "re-enable a user with a new key", us)
    p.add_argument("user_id", type=int)
    token_file(p)

    keys = cmd("keys", _group, "list, rotate or revoke keys")
    ks = keys.add_subparsers(dest="action", required=True, metavar="ACTION")
    p = cmd("list", host.cmd_keys_list, "list keys", ks)
    p.add_argument("--user", type=int, dest="user_id")
    p = cmd("rotate", host.cmd_keys_rotate, "replace a user's key (old one stops working)", ks)
    p.add_argument("user_id", type=int)
    token_file(p)
    p = cmd("revoke", host.cmd_keys_revoke, "revoke one key", ks)
    p.add_argument("key_id", type=int)
    p = cmd("rotate-admin", host.cmd_keys_rotate_admin, "recover a lost admin key", ks)
    p.add_argument("--user-id", type=int, help="the admin, when several exist")
    token_file(p)

    ret = cmd("retention", _group, "show or set transcript retention")
    rs = ret.add_subparsers(dest="action", required=True, metavar="ACTION")
    cmd("show", host.cmd_retention_show, "current setting and what it would purge", rs)
    p = cmd("set", host.cmd_retention_set, "set retention in days (0 keeps forever)", rs)
    p.add_argument("days", type=int)

    b = cmd("backup", ops.cmd_backup, "offline backup of the host data (stops the service briefly)")
    b.add_argument("--dest", type=Path, help="new directory (default: backup dir/wispralt-<ts>)")
    bs = b.add_subparsers(dest="action", metavar="ACTION")
    cmd("list", ops.cmd_backup_list, "list backups", bs)
    p = cmd("restore", ops.cmd_restore, "replace the host data with a backup (old data kept aside)")
    p.add_argument("backup", type=Path)

    p = cmd("update", ops.cmd_update, "pull, back up, reinstall and restart")
    p.add_argument("--no-pull", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--client", action="store_true", help="also rebuild and reinstall the app")
    p.add_argument("--_phase2", action="store_true", dest="phase2", help=argparse.SUPPRESS)
    p.add_argument("--_backup", type=Path, dest="backup", help=argparse.SUPPRESS)

    p = cmd(
        "uninstall",
        ops.cmd_uninstall,
        "remove what setup installed (data kept unless --purge-data)",
    )
    p.add_argument("--client", action="store_true", help="move the app to the Trash")
    p.add_argument("--purge-data", action="store_true", help="back up, then delete host data")
    p.add_argument("--no-backup", action="store_true", help="with --purge-data: skip the backup")
    return parser


def parse(argv: list[str]) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    for flag in ("json", "non_interactive", "yes"):  # SUPPRESS defaults: shared parent actions
        if not hasattr(args, flag):
            setattr(args, flag, False)
    args.argv = argv
    return args


def make_ctx(args: argparse.Namespace, deps: Deps) -> Ctx:
    base = Paths.resolve()
    state = InstallState.load(base.state_file)
    recorded = state.paths if state else {}
    data_dir = getattr(args, "data_dir", None) or recorded.get("data_dir")
    backup_dir = getattr(args, "backup_dir", None) or recorded.get("backup_dir")
    paths = Paths.resolve(
        data_dir=Path(data_dir) if data_dir else None,
        backup_dir=Path(backup_dir) if backup_dir else None,
    )
    return Ctx(
        paths=paths,
        state=state,
        deps=deps,
        interactive=deps.stdin_isatty and not args.non_interactive,
        yes=args.yes,
        json=args.json,
        argv=list(args.argv),
    )


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parse(raw)
    try:
        ctx = make_ctx(args, deps or Deps())
        return args.func(ctx, args)
    except CliError as exc:
        print(f"wispralt: {exc.msg}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("wispralt: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
