"""
bootstrap.py — first-admin bootstrap and lost-admin-key recovery (interim CLI).

Run from ``server/`` (``Settings`` reads ``.env`` from the cwd)::

    python -m wispralt.bootstrap --label owner          # first admin + client config
    python -m wispralt.bootstrap --rotate-admin [--user-id N]
    python -m wispralt.bootstrap --check                 # exit 0 iff an enabled admin exists

The first two print a one-time token, so they refuse (exit 2) unless stdout is
a terminal: a launchd or CI log must never capture a credential. They create
``DATA_DIR`` (0700) if it is missing and migrate the DB first. ``--check``
prints no secret and never creates anything. Part 5's CLI replaces this module
and calls the same ``accounts.service`` functions.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from typing import TYPE_CHECKING

from pydantic import ValidationError

from wispralt._errors import AlreadyBootstrapped, MigrationError
from wispralt.accounts import service
from wispralt.accounts.store import AccountStore
from wispralt.database import Database

if TYPE_CHECKING:
    from wispralt.config import Settings

EXIT_OK, EXIT_FAIL, EXIT_NOT_TTY = 0, 1, 2


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _load_settings() -> Settings | None:
    try:
        from wispralt.config import settings
    except ValidationError as exc:
        fields = ", ".join(str(e["loc"][0]).upper() for e in exc.errors())
        _err(f"Invalid server settings ({fields}); check server/.env (DATA_DIR must be absolute).")
        return None
    return settings


def _print_client_config(server_url: str, token: str, *, heading: str) -> None:
    server_url = server_url or "(not set yet: the https:// URL you set as SERVER_URL)"
    print(
        f"\n{heading}\n"
        "Enter these in the WisprAlt client (Settings > Server). The key is shown ONCE:\n\n"
        f"  Server URL: {server_url}\n"
        f"  API key:    {token}\n"
    )


def _check(settings: Settings) -> int:
    if not settings.db_path.exists():
        _err("No database yet: run `python -m wispralt.bootstrap --label owner`.")
        return EXIT_FAIL
    try:
        ok = AccountStore(Database(settings.db_path)).has_enabled_admin()
    except sqlite3.Error as exc:
        _err(f"Database check failed: {type(exc).__name__}")
        return EXIT_FAIL
    print("admin: ok" if ok else "admin: none")
    return EXIT_OK if ok else EXIT_FAIL


def _pick_admin(accounts: AccountStore, user_id: int | None) -> int | None:
    admins = accounts.list_enabled_admins()
    if not admins:
        _err("No enabled admin exists: run without --rotate-admin to create one.")
        return None
    if user_id is not None:
        if user_id not in {a.id for a in admins}:
            _err(f"User {user_id} is not an enabled admin.")
            return None
        return user_id
    if len(admins) > 1:
        ids = ", ".join(f"{a.id} ({a.label})" for a in admins)
        _err(f"Several enabled admins ({ids}): pass --user-id N.")
        return None
    return admins[0].id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m wispralt.bootstrap")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rotate-admin", action="store_true", help="issue a new key for an admin")
    mode.add_argument("--check", action="store_true", help="exit 0 iff an enabled admin exists")
    parser.add_argument("--label", default="owner", help="first admin's label (default: owner)")
    parser.add_argument("--user-id", type=int, help="admin to rotate when several exist")
    args = parser.parse_args(argv)

    if not args.check and not sys.stdout.isatty():
        _err("Refusing to print a token to a non-terminal stdout; run this in a terminal.")
        return EXIT_NOT_TTY

    settings = _load_settings()
    if settings is None:
        return EXIT_FAIL
    if args.check:
        return _check(settings)

    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = Database(settings.db_path)
    try:
        db.migrate()
    except MigrationError as exc:
        _err(f"Database migration refused: {exc}")
        return EXIT_FAIL
    accounts = AccountStore(db)

    if args.rotate_admin:
        user_id = _pick_admin(accounts, args.user_id)
        if user_id is None:
            return EXIT_FAIL
        _user, token = service.rotate_user_key(accounts, user_id)
        _print_client_config(settings.server_url, token, heading="Admin key rotated.")
        return EXIT_OK

    try:
        _user, token = service.bootstrap_first_admin(accounts, args.label)
    except AlreadyBootstrapped:
        _err("An enabled admin already exists. Lost its key? Use --rotate-admin.")
        return EXIT_FAIL
    _print_client_config(settings.server_url, token, heading="First admin created.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
