"""
cli/ops.py — status, doctor and the offline operations around the host's data.

Sizes are *allocated* bytes (``st_blocks × 512`` over ``os.scandir``): ``du``
over-counts APFS clones and this is testable. ``doctor`` is read-only: the DB
check opens a ``mode=ro`` connection, Keychain checks use presence lookups
(never ``-w``), and only ``--warmup`` loads a model.
"""

from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import stat
import sys
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wispralt._errors import BackupRefused, InsufficientSpace, RestoreRefused, ServerRunning
from wispralt.accounts.store import AccountStore
from wispralt.cli import client, launchd
from wispralt.cli.host import (
    Check,
    describe_retention,
    human,
    platform_checks,
    server_url,
    verdict,
    warmup,
)
from wispralt.cli.paths import LEGACY_LABEL, env_value, private_dir
from wispralt.cli.system import EXIT_DOCTOR, CliError, version
from wispralt.database import Database
from wispralt.jobs.store import JobStore
from wispralt.ops import backup as offline

if TYPE_CHECKING:
    import argparse
    from collections.abc import Iterator

    from wispralt.cli.paths import Ctx

CLOUDFLARED_LABEL = "co.wispralt.cloudflared"
MANIFEST = "manifest.json"


# ── sizes ────────────────────────────────────────────────────────────────────


def allocated(path: Path) -> int:
    """Bytes allocated on disk under *path* (symlinks are not followed)."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_blocks * 512
    total = st.st_blocks * 512
    with os.scandir(path) as it:
        for entry in it:
            if entry.is_dir(follow_symlinks=False):
                total += allocated(Path(entry.path))
            else:
                total += entry.stat(follow_symlinks=False).st_blocks * 512
    return total


def list_backups(ctx: Ctx) -> list[Path]:
    """Backup dirs (those with a manifest) in the backup dir, oldest first."""
    root = ctx.paths.backup_dir
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir() if p.is_dir() and (p / MANIFEST).is_file()]
    return sorted(found, key=lambda p: (p.stat().st_mtime, p.name))


def pre_restore_dirs(ctx: Ctx) -> list[Path]:
    data = ctx.paths.data_dir
    return sorted(data.parent.glob(f"{data.name}.pre-restore-*")) if data.parent.is_dir() else []


def sizes(ctx: Ctx) -> dict[str, Any]:
    p = ctx.paths
    backups = list_backups(ctx)
    return {
        "data_dir": allocated(p.data_dir),
        "db": sum(allocated(p.db_path.with_name(p.db_path.name + s)) for s in ("", "-wal", "-shm")),
        "outputs": allocated(p.data_dir / "outputs"),
        "staging": allocated(p.data_dir / "staging"),
        "models": allocated(p.models_dir),
        "backups": {
            "dir": str(p.backup_dir),
            "bytes": allocated(p.backup_dir),
            "count": len(backups),
            "newest": str(backups[-1]) if backups else None,
        },
        "pre_restore": [{"path": str(d), "bytes": allocated(d)} for d in pre_restore_dirs(ctx)],
    }


# ── status ───────────────────────────────────────────────────────────────────


def _db_facts(ctx: Ctx) -> dict[str, Any] | None:
    if not ctx.paths.db_path.exists():
        return None
    db = Database(ctx.paths.db_path)
    try:
        jobs = JobStore(db, ctx.paths.data_dir)
        days = jobs.retention_days()
        return {
            "retention_days": days,
            "would_purge": jobs.retention_preview(days),
            "admins": len(AccountStore(db).list_enabled_admins()),
        }
    except sqlite3.Error as exc:
        return {"error": type(exc).__name__}


def _client_facts(ctx: Ctx) -> dict[str, Any]:
    app = ctx.paths.app
    running = ctx.runner.run(["pgrep", "-x", "WisprAlt"], check=False).returncode == 0
    url = ctx.runner.run(
        ["defaults", "read", client.DEFAULTS_DOMAIN, "serverURL"], check=False
    ).stdout.strip()
    return {
        "app": str(app),
        "installed": app.is_dir(),
        "version": client.app_version(app),
        "running": running,
        "server_url": url or None,
    }


def cmd_status(ctx: Ctx, args: argparse.Namespace) -> int:
    state = ctx.state
    svc = launchd.service(ctx)
    report: dict[str, Any] = {
        "version": version(),
        "role": state.role if state else None,
        "service": {"label": ctx.paths.label, "state": svc.state, "pid": svc.pid},
        "url": ctx.url,
        "server_url": server_url(ctx) if state else None,
        "sizes": sizes(ctx),
        "db": _db_facts(ctx),
    }
    if args.client:
        report["client"] = _client_facts(ctx)
    if ctx.json:
        print(json.dumps(report, indent=2))
        return 0
    _print_status(report)
    return 0


def _print_status(r: dict[str, Any]) -> None:
    sz = r["sizes"]
    svc = r["service"]
    pid = f" (pid {svc['pid']})" if svc["pid"] else ""
    print(f"wispralt {r['version']}  role: {r['role'] or 'not installed'}")
    print(f"service: {svc['label']} {svc['state']}{pid}  {r['url']}")
    if r["server_url"]:
        print(f"clients use: {r['server_url']}")
    print(
        f"data (allocated): {human(sz['data_dir'])}  db {human(sz['db'])}  "
        f"outputs {human(sz['outputs'])}  staging {human(sz['staging'])}  models {human(sz['models'])}"
    )
    b = sz["backups"]
    newest = f", newest {b['newest']}" if b["newest"] else ""
    print(f"backups: {b['count']} in {b['dir']} ({human(b['bytes'])}{newest})")
    for d in sz["pre_restore"]:
        print(f"pre-restore copy: {d['path']} ({human(d['bytes'])}); delete it once you are sure")
    db = r["db"]
    if db and "error" not in db:
        wp = db["would_purge"]
        print(
            f"retention: {describe_retention(db['retention_days'])} "
            f"(purges {wp['dictations']} dictations, {wp['jobs']} file jobs now)  admins: {db['admins']}"
        )
    elif db:
        print(f"database: unreadable ({db['error']})")
    if "client" in r:
        c = r["client"]
        state = "running" if c["running"] else "not running"
        where = f"{c['app']} {c['version'] or ''}".strip() if c["installed"] else "not installed"
        print(f"client: {where}, {state}, server {c['server_url'] or 'unset'}")


# ── doctor ───────────────────────────────────────────────────────────────────


def integrity(db_path: Path) -> str:
    """``PRAGMA integrity_check`` on a read-only connection ("ok" when healthy)."""
    try:
        with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as con:
            return str(con.execute("PRAGMA integrity_check").fetchone()[0])
    except sqlite3.Error as exc:
        return type(exc).__name__


def host_checks(ctx: Ctx) -> list[Check]:
    p = ctx.paths
    out: list[Check] = []
    env = p.env_file
    if env.exists():
        st = env.stat()
        good = stat.S_IMODE(st.st_mode) == 0o600 and st.st_uid == os.getuid()
        out.append(
            verdict(good, "env-file", f"{env} {oct(stat.S_IMODE(st.st_mode))}", f"chmod 600 {env}")
        )
        configured = env_value(env, "DATA_DIR")
        out.append(
            verdict(
                configured == str(p.data_dir),
                "env-data-dir",
                f"DATA_DIR={configured}",
                f"set DATA_DIR={p.data_dir} in {env} (the CLI and the service must agree)",
            )
        )
    else:
        out.append(Check("fail", "env-file", f"{env} missing", "wispralt setup --role host"))
    if p.data_dir.is_dir():
        mode = stat.S_IMODE(p.data_dir.stat().st_mode)
        out.append(
            verdict(
                mode == 0o700, "data-dir", f"{p.data_dir} {oct(mode)}", f"chmod 700 '{p.data_dir}'"
            )
        )
    else:
        out.append(Check("fail", "data-dir", f"{p.data_dir} missing", "wispralt setup --role host"))
    if p.db_path.exists():
        res = integrity(p.db_path)
        out.append(
            verdict(
                res == "ok", "db-integrity", res, "restore the newest backup: wispralt restore DIR"
            )
        )
    svc = launchd.service(ctx)
    run_at_load = _plist_flag(p.plist, "RunAtLoad")
    out.append(
        verdict(
            svc.loaded and run_at_load,
            "service",
            f"{p.label} {svc.state}" + ("" if run_at_load else " (no RunAtLoad plist)"),
            "wispralt restart",
        )
    )
    for path in ("/healthz", "/readyz/dictation", "/readyz/db"):
        code = ctx.deps.http_get(f"{ctx.url}{path}", 3.0)
        out.append(
            verdict(
                code == 200,
                path,
                f"HTTP {code}" if code else "no answer",
                f"see {p.logs_dir}/server.err.log, then wispralt restart",
            )
        )
    legacy = launchd.service(ctx, LEGACY_LABEL).loaded
    if legacy or p.legacy_plist.exists():
        out.append(
            Check(
                "fail" if legacy else "warn",
                "legacy-agent",
                f"{LEGACY_LABEL} {'loaded' if legacy else 'plist present'}",
                "wispralt setup --role host (retires it)",
            )
        )
    if (
        launchd.service(ctx, CLOUDFLARED_LABEL).loaded
        and not (p.config_dir / "cloudflare-token").exists()
    ):
        out.append(
            Check(
                "warn",
                "cloudflared",
                f"{CLOUDFLARED_LABEL} loaded but {p.config_dir / 'cloudflare-token'} is missing",
                "rerun scripts/setup-cloudflared.sh or boot the tunnel agent out",
            )
        )
    return out


def client_checks(ctx: Ctx) -> list[Check]:
    r = ctx.runner
    xcode = r.run(["xcode-select", "-p"], check=False).stdout.strip()
    ids = r.run(["security", "find-identity", "-p", "codesigning", "-v"], check=False).stdout
    app = ctx.paths.app
    running = r.run(["pgrep", "-x", "WisprAlt"], check=False).returncode == 0
    has_key = (
        r.run(
            [
                "security",
                "find-generic-password",
                "-s",
                client.KEYCHAIN_SERVICE,
                "-a",
                client.KEYCHAIN_ACCOUNT,
            ],
            check=False,
        ).returncode
        == 0
    )
    has_openai = (
        r.run(
            ["security", "find-generic-password", "-s", client.OPENAI_SERVICE], check=False
        ).returncode
        == 0
    )
    full_xcode = bool(xcode) and "CommandLineTools" not in xcode
    return [
        verdict(full_xcode, "xcode", xcode or "none", "install Xcode from the App Store (building the app needs it; a DMG install does not)", warn=True),
        verdict("Apple Development" in ids, "signing-identity", "Apple Development" if "Apple Development" in ids else "none", "Xcode > Settings > Accounts: add your Apple ID and create an Apple Development certificate", warn=True),
        verdict(app.is_dir(), "app", str(app), "wispralt setup --role client"),
        verdict(running, "app-running", "running" if running else "not running", f"open '{app}'", warn=True),
        verdict(has_key or has_openai, "keychain", f"local key: {has_key}, OpenAI key: {has_openai}", "add a key in WisprAlt > Settings", warn=True),
    ]  # fmt: skip


def info_checks(ctx: Ctx) -> list[Check]:
    fde = ctx.runner.run(["fdesetup", "status"], check=False).stdout.strip().splitlines()
    return [
        Check("info", "filevault", fde[0] if fde else "unknown"),
        Check("info", "login", "the service is a user agent: it runs only while you are logged in"),
    ]


def _plist_flag(path: Path, key: str) -> bool:
    try:
        with path.open("rb") as f:
            return bool(plistlib.load(f).get(key))
    except (OSError, plistlib.InvalidFileException):
        return False


def cmd_doctor(ctx: Ctx, args: argparse.Namespace) -> int:
    role = ctx.state.role if ctx.state else None
    host = role in {"host", "both"}
    checks = platform_checks(ctx, host=host or role is None)
    if host:
        checks += host_checks(ctx)
    if args.client or role in {"client", "both"}:
        checks += client_checks(ctx)
    if args.warmup:
        checks.append(warmup(ctx))
    checks += info_checks(ctx)
    failed = [c for c in checks if c.status == "fail"]
    if ctx.json:
        print(json.dumps({"ok": not failed, "checks": [asdict(c) for c in checks]}, indent=2))
    else:
        for c in checks:
            fix = f"  -> {c.fix}" if c.fix else ""
            print(f"{c.status.upper():4}  {c.name}: {c.detail}{fix}")
    return EXIT_DOCTOR if failed else 0


def need_host(ctx: Ctx) -> None:
    state = ctx.need_state()
    if state.role not in {"host", "both"}:
        raise CliError(1, "this Mac has no WisprAlt host installed")


# ── offline operations (backup / restore) ────────────────────────────────────

_OFFLINE_ERRORS = (ServerRunning, BackupRefused, RestoreRefused, InsufficientSpace)


@contextmanager
def service_stopped(ctx: Ctx) -> Iterator[None]:
    """Stop the service (proven by ``server_lock``) and start it again afterwards if it ran."""
    was_loaded = launchd.service(ctx).loaded
    if was_loaded:
        launchd.stop(ctx)
    try:
        yield
    except _OFFLINE_ERRORS as exc:
        raise CliError(1, f"{type(exc).__name__}: {exc}") from exc
    finally:
        if was_loaded:
            launchd.start(ctx)
            if not launchd.wait_healthy(ctx):
                print(
                    f"warning: the service restarted but {ctx.url}/healthz is not answering",
                    file=sys.stderr,
                )


def new_backup_dest(ctx: Ctx) -> Path:
    private_dir(ctx.paths.backup_dir)
    return ctx.paths.backup_dir / f"wispralt-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"


def run_backup(ctx: Ctx, dest: Path) -> Path:
    """Offline backup with the service stopped; returns the backup dir."""
    with service_stopped(ctx):
        return offline.backup(ctx.paths.data_dir, dest)


def _manifest(path: Path) -> dict[str, Any]:
    try:
        return json.loads((path / MANIFEST).read_text())
    except (OSError, ValueError):
        return {}


def _backup_row(path: Path) -> dict[str, Any]:
    m = _manifest(path)
    created = m.get("created_at")
    tables = m.get("tables") or {}
    return {
        "path": str(path),
        "created": datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M") if created else "-",
        "bytes": allocated(path),
        "users": tables.get("users", 0),
        "dictations": tables.get("dictations", 0),
        "jobs": tables.get("jobs", 0),
    }


def cmd_backup(ctx: Ctx, args: argparse.Namespace) -> int:
    need_host(ctx)
    dest = run_backup(ctx, args.dest or new_backup_dest(ctx))
    row = _backup_row(dest)
    print(
        f"backup written: {dest} ({human(row['bytes'])}; {row['users']} users, "
        f"{row['dictations']} dictations, {row['jobs']} file jobs)"
    )
    return 0


def cmd_backup_list(ctx: Ctx, _args: argparse.Namespace) -> int:
    rows = [_backup_row(p) for p in list_backups(ctx)]
    if ctx.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print(f"no backups in {ctx.paths.backup_dir}")
    for r in [] if ctx.json else rows:
        print(
            f"{r['created']}  {human(r['bytes']):>9}  {r['dictations']:>6} dictations  {r['path']}"
        )
    return 0


def cmd_restore(ctx: Ctx, args: argparse.Namespace) -> int:
    need_host(ctx)
    src = args.backup.expanduser().absolute()
    if not (src / MANIFEST).is_file():
        raise CliError(2, f"{src} is not a WisprAlt backup (no {MANIFEST})")
    print(f"restore {src} over {ctx.paths.data_dir}; the current data is kept aside")
    if not ctx.confirm("Stop the service and restore this backup?"):
        print("nothing changed")
        return 0
    with service_stopped(ctx):
        aside = offline.restore(src, ctx.paths.data_dir)
    print(f"restored {src}")
    if aside is not None:
        print(f"previous data kept at {aside} (delete it once you are sure)")
    return 0
