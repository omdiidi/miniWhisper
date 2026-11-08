"""
python -m wispralt.importer — interim operator entry point (run from ``server/``).

  export   [--backups-root DIR]                         read-only Supabase → snapshot dir
  snapshot DIR [--dry-run] [--yes] [--owner-map PG=LOCAL]  snapshot → local DB
  files    PATH… --owner ID [--dry-run] [--yes]         transcript files → local DB

A real import needs the server stopped (offline lock) and confirmation: an
interactive prompt after the dry-run report, or ``--yes``. Without a TTY and
without ``--yes`` or ``--dry-run`` it refuses.
Exit codes: 0 ok, 1 error, 2 refused, 3 BLOCKED, 4 integrity failure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from wispralt import importer
from wispralt._errors import (
    ExportBlocked,
    ExportUnavailable,
    ImportIntegrityError,
    MigrationError,
    OwnerMappingRequired,
    ServerRunning,
)
from wispralt.database import Database
from wispralt.importer.lock import offline

if TYPE_CHECKING:
    from collections.abc import Callable

    from wispralt.importer.report import Report

OK, ERROR, REFUSED, BLOCKED, INTEGRITY = 0, 1, 2, 3, 4


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _owner_map(pairs: list[str] | None) -> dict[int, int] | None:
    if not pairs:
        return None
    try:
        return {int(p): int(lid) for p, lid in (x.split("=", 1) for x in pairs)}
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--owner-map takes PG_ID=LOCAL_ID") from exc


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m wispralt.importer", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="read-only Supabase export into a snapshot dir")
    ex.add_argument("--backups-root", type=Path, default=None)
    sn = sub.add_parser("snapshot", help="import a snapshot dir into the local DB")
    sn.add_argument("dir", type=Path)
    sn.add_argument("--owner-map", action="append", metavar="PG=LOCAL")
    fl = sub.add_parser("files", help="import transcript files as file jobs")
    fl.add_argument("paths", type=Path, nargs="+")
    fl.add_argument("--owner", type=int, required=True, metavar="LOCAL_USER_ID")
    for p in (sn, fl):
        p.add_argument("--dry-run", action="store_true", help="read only; print the counts")
        p.add_argument("--yes", action="store_true", help="confirm without a prompt")
    return ap


def _open_db() -> Database:
    """The existing local DB, opened without migrating or writing anything."""
    from wispralt.config import settings  # needs DATA_DIR; export does not

    db = Database(settings.db_path)
    if not db.path.exists():
        raise ExportUnavailable(
            f"{db.path} does not exist: run `wispralt setup --role host` first"
        )
    return db


def _confirmed_run(args: argparse.Namespace, run: Callable[..., Report]) -> int:
    """Dry run → print; else confirm (``--yes`` or a prompt after a dry run) → apply.

    A dry run never migrates. A real run takes the offline server lock first and
    holds it across the migration and the import (``ServerRunning`` → refused).
    """
    if args.dry_run:
        print(run(_open_db(), dry_run=True).render())
        return OK
    if not args.yes:
        if not _interactive():
            print("refused: non-interactive run needs --yes (or --dry-run)", file=sys.stderr)
            return REFUSED
        print(run(_open_db(), dry_run=True).render())
        if input("Import now (stop the server first)? [y/N] ").strip().lower() != "y":
            print("nothing imported", file=sys.stderr)
            return REFUSED
    db = _open_db()
    with offline(db.path.parent):
        db.migrate()
        report = run(db, dry_run=False)
    print(report.render())
    return OK


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "export":
            print(f"snapshot written: {importer.run_export(args.backups_root)}")
            return OK
        if args.cmd == "snapshot":
            owner_map = _owner_map(args.owner_map)

            def snap(db: Database, dry_run: bool) -> Report:
                return importer.run_snapshot_import(
                    db, args.dir, dry_run=dry_run, confirm=True, owner_map=owner_map
                )

            return _confirmed_run(args, snap)

        def files(db: Database, dry_run: bool) -> Report:
            from wispralt.config import settings

            return importer.run_file_import(
                db, args.paths, owner_id=args.owner, outputs_dir=settings.outputs_dir,
                dry_run=dry_run,
            )  # fmt: skip

        return _confirmed_run(args, files)
    except ExportBlocked as exc:
        print(
            f"BLOCKED: {exc.reason} (Supabase was not changed; BLOCKED.json records it)",
            file=sys.stderr,
        )
        return BLOCKED
    except ImportIntegrityError as exc:
        print(f"integrity failure: {exc}", file=sys.stderr)
        return INTEGRITY
    except (OwnerMappingRequired, ServerRunning, argparse.ArgumentTypeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED
    except (ExportUnavailable, MigrationError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
