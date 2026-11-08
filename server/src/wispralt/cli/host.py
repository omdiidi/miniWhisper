"""
cli/host.py — the host side: ``setup`` (preflight, ``[host]`` extra, ``.env``,
models, database, first admin, retention, warm-up, LaunchAgent) and the account
handoff (``users``, ``keys``, ``retention``).

Setup is safe to rerun: it adds only absent ``.env`` keys, downloads nothing
when the cached models verify (sha256 against the Hub's LFS metadata at the
resolved commit), never creates a second admin or rotates a key, and asks the
retention question once.

Every key the CLI mints leaves through ``secret_sink`` + ``announce_secret``:
printed only to a terminal, else written to a new 0600 ``--token-file``
(``O_EXCL``, created before anything is minted; a rotation writes it inside its
transaction, so a failed write leaves the old key working), else the command
refuses (exit 2) *before* anything is minted. A key never reaches a log,
an argv or a non-terminal stdout. Accounts and retention are the server's own
functions (``accounts.service``, ``AccountStore``, ``JobStore``).
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from wispralt._errors import MigrationError
from wispralt.accounts import service
from wispralt.accounts.store import AccountStore
from wispralt.cli import client, launchd
from wispralt.cli.paths import (
    DB_NAME,
    InstallState,
    env_value,
    find_source,
    merge_missing,
    private_dir,
    set_values,
)
from wispralt.cli.system import CliError, version
from wispralt.database import Database
from wispralt.jobs.store import JobStore

_LOOPBACK_URL = re.compile(r"^http://(127\.0\.0\.1|localhost):\d+/?$")

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Iterator
    from datetime import datetime

    from wispralt.accounts.store import User
    from wispralt.cli.paths import Ctx

GB = 1_000_000_000
MIN_FREE_HOST = 6 * GB
MIN_MACOS = 14


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} GB"


@dataclass(frozen=True, slots=True)
class Check:
    status: str  # "ok" | "warn" | "fail" | "info"
    name: str
    detail: str
    fix: str = ""


def verdict(cond: bool, name: str, detail: str, fix: str, *, warn: bool = False) -> Check:
    return Check("ok" if cond else ("warn" if warn else "fail"), name, detail, "" if cond else fix)


def platform_checks(ctx: Ctx, *, host: bool) -> list[Check]:
    d = ctx.deps
    major = int(d.macos.split(".")[0]) if d.macos else 0
    checks = [
        verdict(
            d.machine == "arm64",
            "apple-silicon",
            d.machine,
            "the host needs Apple Silicon (MLX); use this Mac as a client only",
            warn=not host,
        ),
        verdict(major >= MIN_MACOS, "macos", d.macos or "unknown", f"update to macOS {MIN_MACOS}+"),
    ]
    if host:
        free = d.disk_free(ctx.paths.models_dir)
        checks += [
            verdict(free >= MIN_FREE_HOST, "disk", f"{human(free)} free", "free at least 6 GB"),
            verdict(bool(d.which("ffmpeg")), "ffmpeg", "on PATH", "brew install ffmpeg"),
            verdict(bool(d.which("uv")), "uv", "on PATH", "brew install uv"),
        ]
    return checks


def tone_wav(seconds: float = 1.0, hz: float = 440.0, rate: int = 16_000) -> bytes:
    import numpy as np
    import soundfile as sf

    t = np.arange(int(seconds * rate), dtype=np.float32) / rate
    buf = io.BytesIO()
    sf.write(buf, 0.2 * np.sin(2 * np.pi * hz * t), rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def warmup(ctx: Ctx) -> Check:
    """Load the dictation model in-process and transcribe a 1 s tone (text may be empty)."""
    import asyncio

    os.environ["HF_HOME"] = str(ctx.paths.models_dir)
    os.environ.setdefault("DATA_DIR", str(ctx.paths.data_dir))  # parakeet imports Settings
    os.environ.setdefault("SERVER_URL", ctx.url)
    from wispralt.dictate.parakeet import ParakeetService

    svc = ParakeetService(ctx.deps.load_fn)
    try:
        svc.load()
        asyncio.run(svc.transcribe(tone_wav()))
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        fix = "wispralt setup --role host (re-verifies the models)"
        return Check("fail", "warmup", f"{type(exc).__name__}: {exc}", fix)
    return Check("ok", "warmup", f"{svc.model_id} transcribed a test tone")


# ── database + admin bootstrap (also used by ``python -m wispralt.bootstrap``) ──


def prepare_db(data_dir: Path) -> Database:
    """Create ``data_dir`` (0700) if missing and migrate its database."""
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = Database(data_dir / DB_NAME)
    try:
        db.migrate()
    except MigrationError as exc:
        raise CliError(1, f"database migration refused: {exc}") from exc
    return db


def bootstrap_admin(data_dir: Path, label: str) -> tuple[User, str]:
    """The first admin and its one-time key; ``AlreadyBootstrapped`` if one exists."""
    return service.bootstrap_first_admin(AccountStore(prepare_db(data_dir)), label)


def rotate_admin(
    data_dir: Path, user_id: int | None = None, *, deliver: Callable[[str], None] | None = None
) -> tuple[User, str]:
    """A new key for an enabled admin (the only one, or *user_id*); old keys stop working.

    *deliver* gets the key inside the rotation's transaction (see ``secret_sink``).
    """
    accounts = AccountStore(prepare_db(data_dir))
    admins = accounts.list_enabled_admins()
    if not admins:
        raise CliError(
            1,
            "No enabled admin exists: create one first (wispralt setup, or run without --rotate-admin).",
        )
    if user_id is None:
        if len(admins) > 1:
            ids = ", ".join(f"{a.id} ({a.label})" for a in admins)
            raise CliError(2, f"Several enabled admins ({ids}): pass --user-id N.")
        user_id = admins[0].id
    elif user_id not in {a.id for a in admins}:
        raise CliError(1, f"User {user_id} is not an enabled admin.")
    return service.rotate_user_key(accounts, user_id, deliver=deliver)


# ── the one way a key leaves the CLI ─────────────────────────────────────────


def check_secret_sink(ctx: Ctx, token_file: Path | None) -> None:
    """Refuse (exit 2) up front when a key would have nowhere safe to go."""
    if token_file is None:
        if not ctx.deps.stdout_isatty:
            raise CliError(2, "refusing to print a key to a non-terminal; pass --token-file PATH")
        return
    if token_file.exists() or token_file.is_symlink():
        raise CliError(2, f"{token_file} exists; --token-file must name a new file")
    if not token_file.parent.is_dir():
        raise CliError(2, f"{token_file.parent} does not exist")


@contextmanager
def secret_sink(ctx: Ctx, token_file: Path | None) -> Iterator[Callable[[str], None] | None]:
    """Create ``--token-file`` (0600, ``O_EXCL``) *before* a key is minted; yield its writer.

    Mint inside the block and hand the writer to the mint: a rotation calls it inside
    its transaction, so a failed write rolls the rotation back and the old key keeps
    working. Any failure in the block removes the file. Yields ``None`` for a terminal
    (the key is printed by ``announce_secret`` once the mint has committed).
    """
    check_secret_sink(ctx, token_file)
    if token_file is None:
        yield None
        return
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    f = os.fdopen(os.open(token_file, flags, 0o600), "w")

    def write(token: str) -> None:
        f.write(token + "\n")
        f.flush()
        os.fsync(f.fileno())

    try:
        with f:
            yield write
    except BaseException:
        token_file.unlink(missing_ok=True)
        raise


def announce_secret(ctx: Ctx, token: str, token_file: Path | None, *, heading: str) -> None:
    """Tell the operator where the key went (or print it, on a terminal)."""
    if token_file is not None:
        print(f"{heading} The key is in {token_file} (0600); delete it once it is in use.")
        return
    print(client_config_text(server_url(ctx), token, heading=heading))


def client_config_text(url: str, token: str, *, heading: str) -> str:
    """The one wording for handing over a key (the CLI and ``python -m wispralt.bootstrap``)."""
    url = url or "(not set yet: the https:// URL you set as SERVER_URL)"
    return (
        f"\n{heading}\nEnter these in the WisprAlt client (Settings > Server). The key is shown ONCE:\n\n"
        f"  Server URL: {url}\n"
        f"  API key:    {token}\n"
    )


def server_url(ctx: Ctx) -> str:
    return env_value(ctx.paths.env_file, "SERVER_URL") or ctx.url


# ── helpers ──────────────────────────────────────────────────────────────────


def open_db(ctx: Ctx) -> Database:
    """The installed host's database (never created here)."""
    if not ctx.paths.db_path.exists():
        raise CliError(1, f"no database at {ctx.paths.db_path}; run `wispralt setup --role host`")
    return Database(ctx.paths.db_path)


def _when(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "-"


def _emit_rows(ctx: Ctx, rows: list[dict[str, Any]], cols: list[str]) -> None:
    if ctx.json:
        print(json.dumps(rows, indent=2, default=str))
        return
    widths = [max([len(c), *(len(str(r[c])) for r in rows)]) for c in cols]
    for line in [dict(zip(cols, cols, strict=True)), *rows]:
        print("  ".join(str(line[c]).ljust(w) for c, w in zip(cols, widths, strict=True)).rstrip())


def _mint(ctx: Ctx, args: argparse.Namespace, accounts: AccountStore, user_id: int) -> int:
    if accounts.get_user(user_id) is None:
        raise CliError(1, f"no user {user_id}")
    with secret_sink(ctx, args.token_file) as deliver:
        user, token = service.rotate_user_key(accounts, user_id, deliver=deliver)
    announce_secret(
        ctx, token, args.token_file, heading=f"New key for user {user.id} ({user.label})."
    )
    return 0


# ── users ────────────────────────────────────────────────────────────────────


def cmd_users_list(ctx: Ctx, _args: argparse.Namespace) -> int:
    accounts = AccountStore(open_db(ctx))
    people = accounts.list_people() + accounts.list_integrations()
    _emit_rows(
        ctx,
        [
            {
                "id": u.id,
                "label": u.label,
                "role": u.role,
                "kind": u.kind,
                "created": _when(u.created_at),
                "last_seen": _when(u.last_seen_at),
                "disabled": _when(u.disabled_at),
            }
            for u in sorted(people, key=lambda u: u.id)
        ],
        ["id", "label", "role", "kind", "created", "last_seen", "disabled"],
    )
    return 0


def cmd_users_add(ctx: Ctx, args: argparse.Namespace) -> int:
    accounts = AccountStore(open_db(ctx))
    with secret_sink(ctx, args.token_file) as deliver:
        user, token = service.create_user(
            accounts, label=args.label, role=args.role, kind=args.kind
        )
        if deliver is not None:  # a new user: a lost write is recovered with `keys rotate`
            deliver(token)
    announce_secret(ctx, token, args.token_file, heading=f"User {user.id} ({user.label}) added.")
    return 0


def cmd_users_disable(ctx: Ctx, args: argparse.Namespace) -> int:
    accounts = AccountStore(open_db(ctx))
    admins = accounts.list_enabled_admins()
    if [a.id for a in admins] == [args.user_id]:
        raise CliError(1, "refusing to disable the last enabled admin")
    if not accounts.disable_user(args.user_id):
        raise CliError(1, f"user {args.user_id} does not exist or is already disabled")
    print(f"user {args.user_id} disabled; none of its keys work now")
    return 0


def cmd_users_enable(ctx: Ctx, args: argparse.Namespace) -> int:
    """Re-enabling issues a new key (``rotate_user_key`` clears ``disabled_at``)."""
    return _mint(ctx, args, AccountStore(open_db(ctx)), args.user_id)


# ── keys ─────────────────────────────────────────────────────────────────────


def cmd_keys_list(ctx: Ctx, args: argparse.Namespace) -> int:
    keys = AccountStore(open_db(ctx)).list_keys(args.user_id)
    _emit_rows(
        ctx,
        [
            {
                "id": k.id,
                "user_id": k.user_id,
                "user": k.user_label,
                "created": _when(k.created_at),
                "revoked": _when(k.revoked_at),
            }
            for k in keys
        ],
        ["id", "user_id", "user", "created", "revoked"],
    )
    return 0


def cmd_keys_rotate(ctx: Ctx, args: argparse.Namespace) -> int:
    return _mint(ctx, args, AccountStore(open_db(ctx)), args.user_id)


def cmd_keys_revoke(ctx: Ctx, args: argparse.Namespace) -> int:
    if not AccountStore(open_db(ctx)).revoke_key(args.key_id):
        raise CliError(1, f"key {args.key_id} does not exist or is already revoked")
    print(f"key {args.key_id} revoked")
    return 0


def cmd_keys_rotate_admin(ctx: Ctx, args: argparse.Namespace) -> int:
    open_db(ctx)
    with secret_sink(ctx, args.token_file) as deliver:
        user, token = rotate_admin(ctx.paths.data_dir, args.user_id, deliver=deliver)
    announce_secret(ctx, token, args.token_file, heading=f"Admin key rotated for {user.label}.")
    return 0


# ── retention ────────────────────────────────────────────────────────────────


def _jobs(ctx: Ctx) -> JobStore:
    return JobStore(open_db(ctx), ctx.paths.data_dir)


def describe_retention(days: int) -> str:
    return "keep forever" if days == 0 else f"{days} days"


def cmd_retention_show(ctx: Ctx, _args: argparse.Namespace) -> int:
    jobs = _jobs(ctx)
    days = jobs.retention_days()
    preview = jobs.retention_preview(days)
    if ctx.json:
        print(json.dumps({"retention_days": days, "would_purge": preview}))
    else:
        print(f"retention: {describe_retention(days)}")
        print(
            f"a purge now would delete {preview['dictations']} dictations, {preview['jobs']} file jobs"
        )
    return 0


def cmd_retention_set(ctx: Ctx, args: argparse.Namespace) -> int:
    if args.days < 0:
        raise CliError(2, "DAYS must be 0 (keep forever) or more")
    jobs = _jobs(ctx)
    preview = jobs.retention_preview(args.days)
    print(
        f"retention {describe_retention(args.days)}: the next daily purge deletes "
        f"{preview['dictations']} dictations and {preview['jobs']} file jobs "
        "(imported history is never purged)"
    )
    if not ctx.confirm("Apply this retention setting?"):
        print("unchanged")
        return 0
    jobs.set_retention_days(args.days)
    print(f"retention set to {describe_retention(args.days)}")
    return 0


# ── setup host ───────────────────────────────────────────────────────────────

UV_TOOL = "uv-tool:wispralt"  # the artifact name for the installed CLI itself


@dataclass(frozen=True, slots=True)
class RemoteFile:
    name: str
    size: int
    sha256: str | None  # LFS files only; others are checked by size


class Hub(Protocol):
    def resolve(self, repo: str) -> tuple[str, list[RemoteFile]]: ...

    def snapshot(
        self, repo: str, commit: str, *, download: bool, force: bool = False
    ) -> Path | None: ...


class HubError(Exception):
    """Hugging Face unreachable or refusing."""


class HfHub:
    """Hugging Face Hub, with the cache under ``models_dir/hub`` (the service's ``HF_HOME``)."""

    def __init__(self, models_dir: Path) -> None:
        self.cache = models_dir / "hub"

    def resolve(self, repo: str) -> tuple[str, list[RemoteFile]]:
        from huggingface_hub import HfApi

        try:
            info = HfApi().model_info(repo, revision="main", files_metadata=True)
        except OSError as exc:  # every hub error (HTTP, offline, not found) is an OSError
            raise HubError(f"{type(exc).__name__}: {exc}") from exc
        files = [
            RemoteFile(s.rfilename, int(s.size or 0), s.lfs.sha256 if s.lfs else None)
            for s in info.siblings or []
        ]
        return str(info.sha), files

    def snapshot(
        self, repo: str, commit: str, *, download: bool, force: bool = False
    ) -> Path | None:
        from huggingface_hub import snapshot_download

        try:
            path = snapshot_download(
                repo,
                revision=commit,
                cache_dir=self.cache,
                local_files_only=not download,
                force_download=force,
            )
        except OSError as exc:
            if not download:
                return None
            raise HubError(f"{type(exc).__name__}: {exc}") from exc
        return Path(str(path))


def model_ids(ctx: Ctx) -> list[str]:
    """The host's models, named by the server code itself (dictation, then file)."""
    os.environ.setdefault("DATA_DIR", str(ctx.paths.data_dir))  # parakeet imports Settings
    os.environ.setdefault("SERVER_URL", ctx.url)
    from wispralt.dictate.parakeet import MODEL_ID as DICTATION
    from wispralt.files.mlx_whisper_loader import MODEL_ID as FILE

    return [DICTATION, FILE]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def verify_snapshot(snap: Path, files: list[RemoteFile]) -> list[str]:
    """Names that are missing, the wrong size, or (LFS) the wrong sha256."""

    def ok(f: RemoteFile) -> bool:
        local = snap / f.name
        if not local.is_file() or local.stat().st_size != f.size:
            return False
        return f.sha256 is None or _sha256(local) == f.sha256

    return [f.name for f in files if not ok(f)]


def ensure_models(ctx: Ctx, state: InstallState) -> None:
    """Download (if needed) and verify every model; record the verified commits."""
    private_dir(ctx.paths.models_dir)
    hub: Hub = ctx.deps.hub or HfHub(ctx.paths.models_dir)
    for repo in model_ids(ctx):
        try:
            commit, files = hub.resolve(repo)
        except HubError as exc:
            recorded = state.models.get(repo)
            if recorded and hub.snapshot(repo, recorded, download=False):
                print(
                    f"{repo}: Hugging Face unreachable; keeping the verified copy {recorded[:12]}"
                )
                continue
            raise CliError(
                1, f"cannot fetch {repo} ({exc}) and no verified copy is cached"
            ) from exc
        snap = hub.snapshot(repo, commit, download=False)
        if snap is not None and not verify_snapshot(snap, files):
            print(f"{repo}: verified {commit[:12]}")
        else:
            bad: list[str] = []
            for force in (False, True) if snap is None else (True,):
                print(f"{repo}: downloading {commit[:12]}{' again' if force else ''} …")
                try:
                    snap = hub.snapshot(repo, commit, download=True, force=force)
                except HubError as exc:
                    raise CliError(1, f"download of {repo} failed: {exc}") from exc
                bad = verify_snapshot(snap, files) if snap else ["snapshot"]
                if not bad:
                    break
            if bad:
                raise CliError(1, f"{repo}: {bad[0]} failed verification after a re-download")
            print(f"{repo}: verified {commit[:12]}")
        state.models[repo] = commit


def has_host_extra() -> bool:
    return importlib.util.find_spec("parakeet_mlx") is not None


def reinstall_cli(ctx: Ctx, source: Path, *, extra: bool = True, reinstall: bool = False) -> None:
    """``uv tool install --force`` from the clone (never ``sudo``)."""
    target = f"{source}/server[host]" if extra else f"{source}/server"
    argv = ["uv", "tool", "install", "--force", "--python", "3.11"]
    ctx.runner.run([*argv, *(["--reinstall"] if reinstall else []), target])


def installed_exe() -> str:
    """The ``wispralt`` entry point beside this interpreter (what ``uv tool install`` wrote)."""
    return str(Path(sys.executable).with_name("wispralt"))


def exec_self(ctx: Ctx, *extra: str) -> None:
    """Replace this process with the freshly installed ``wispralt`` (never half-imported code)."""
    ctx.deps.execv([installed_exe(), *ctx.argv, *extra])


def preflight(ctx: Ctx) -> None:
    if os.geteuid() == 0:
        raise CliError(1, "do not run setup as root or with sudo")
    failed = [c for c in platform_checks(ctx, host=True) if c.status == "fail"]
    if failed:
        lines = "\n".join(f"  {c.name}: {c.detail} -> {c.fix}" for c in failed)
        raise CliError(1, f"this Mac cannot be a host yet:\n{lines}")


def _ask(ctx: Ctx, question: str, default: str) -> str:
    return (ctx.deps.ask(f"{question} [{default}] ") if ctx.interactive else "").strip() or default


def _retention(ctx: Ctx, state: InstallState, flag: int | None, jobs: JobStore) -> None:
    if state.retention_prompted:
        if flag is not None and flag != jobs.retention_days():
            print(f"retention is already set; change it with `wispralt retention set {flag}`")
        return
    if flag is None:
        answer = _ask(ctx, "Keep transcripts forever (0) or for how many days?", "0")
        if not answer.isdigit():
            raise CliError(2, "retention must be 0 (keep forever) or a number of days")
        flag = int(answer)
    if flag < 0:
        raise CliError(2, "--retention-days must be 0 (keep forever) or more")
    jobs.set_retention_days(flag)
    state.retention_prompted = True
    print(f"retention: {describe_retention(flag)} (change it with `wispralt retention set N`)")


def check_data_dir(data_dir: Path) -> None:
    """Refuse a data dir that already holds other things and no ``wispralt.db``.

    So everything in a data dir is WisprAlt's: ``uninstall --purge-data`` may then
    delete its ``outputs/`` and ``staging/`` whole. A missing or empty dir (Finder's
    ``.DS_Store`` aside) is fine; so is an existing install's dir.
    """
    if (data_dir / DB_NAME).exists() or not data_dir.is_dir():
        return
    other = sorted(e.name for e in data_dir.iterdir() if e.name != ".DS_Store")
    if other:
        shown = ", ".join(other[:3]) + (", …" if len(other) > 3 else "")
        raise CliError(
            2,
            f"{data_dir} already holds other files ({shown}) and no {DB_NAME}; "
            "WisprAlt needs a data dir of its own: pick a new or empty --data-dir",
        )


def setup_host(ctx: Ctx, args: argparse.Namespace, state: InstallState) -> str | None:
    """Install or repair the host; returns the new admin key when one was minted."""
    preflight(ctx)
    source = find_source(ctx, args.source)
    if not has_host_extra():
        if args.resumed or source is None:
            raise CliError(1, "the MLX stack is missing; run ./install.sh from a WisprAlt clone")
        print("installing the Apple Silicon inference stack (wispralt[host]) …")
        reinstall_cli(ctx, source)
        exec_self(ctx, "--_resumed")
        return None  # only reached when execv is faked
    p = ctx.paths
    configured = env_value(p.env_file, "DATA_DIR")
    if configured and Path(configured) != p.data_dir:
        if args.data_dir:
            raise CliError(2, f"--data-dir disagrees with DATA_DIR={configured} in {p.env_file}")
        ctx.paths = p = replace(p, data_dir=Path(configured))
    check_data_dir(p.data_dir)
    if source:
        state.source_dir = str(source)
    state.paths = {"data_dir": str(p.data_dir), "backup_dir": str(p.backup_dir)}
    state.add("artifacts", p.plist, p.models_dir, p.logs_dir)
    state.add("data", p.data_dir, p.env_file, p.backup_dir)

    has_admin = p.db_path.exists() and AccountStore(prepare_db(p.data_dir)).has_enabled_admin()
    if not has_admin:
        check_secret_sink(ctx, args.token_file)  # refuse before downloading anything
    label = None if has_admin else (args.admin_label or _ask(ctx, "First admin's label", "owner"))

    merge_missing(
        p.env_file,
        {
            "DATA_DIR": str(p.data_dir),
            "SERVER_URL": args.server_url or ctx.url,
            "SERVER_LOG_PATH": str(p.logs_dir / "wispralt.log"),
        },
    )
    # Defaults never clobber a value; an explicitly passed flag does.
    url = env_value(p.env_file, "SERVER_URL") or ""
    if args.server_url:
        set_values(p.env_file, {"SERVER_URL": args.server_url})
    elif args.port and _LOOPBACK_URL.match(url):  # a loopback URL follows a new --port
        set_values(p.env_file, {"SERVER_URL": ctx.url})
    state.save(p.state_file)
    ensure_models(ctx, state)
    state.save(p.state_file)

    db = prepare_db(p.data_dir)
    token = None
    if AccountStore(db).has_enabled_admin():
        print("admin exists; use `wispralt keys rotate-admin` if its key is lost")
    else:
        with secret_sink(ctx, args.token_file) as deliver:
            _user, token = bootstrap_admin(p.data_dir, label or "owner")
            if deliver is not None:  # a lost write is recovered with `keys rotate-admin`
                deliver(token)
        announce_secret(ctx, token, args.token_file, heading="First admin created.")
    _retention(ctx, state, args.retention_days, JobStore(db, p.data_dir))
    state.save(p.state_file)

    if not args.skip_warmup:
        check = warmup(ctx)
        if check.status != "ok":
            raise CliError(1, f"warm-up failed: {check.detail} -> {check.fix}")
        print(f"warm-up: {check.detail}")
    if note := launchd.retire_legacy(ctx):
        print(note)
    launchd.restart(ctx)
    if not launchd.wait_healthy(ctx):
        raise CliError(1, f"the service did not become healthy; see {p.logs_dir}/server.err.log")
    print(f"host running at {ctx.url}")
    if not args.no_open:
        ctx.runner.run(["open", f"{ctx.url}/admin"], check=False)
    return token


def cmd_setup(ctx: Ctx, args: argparse.Namespace) -> int:
    state = ctx.state or InstallState(label=ctx.paths.label)
    role = args.role or state.role
    if role is None:
        if not ctx.interactive:
            raise CliError(2, "--role host|client|both is required with --non-interactive")
        role = _ask(ctx, "Install the host, the client, or both? (host/client/both)", "both")
        if role not in {"host", "client", "both"}:
            raise CliError(2, f"unknown role {role!r}")
    if state.role and role != state.role:
        print(f"note: changing role {state.role} -> {role}")
    state.role = role
    state.add("artifacts", UV_TOOL)
    state.version = version()
    state.label = ctx.paths.label
    state.port = args.port or state.port
    ctx.state = state
    token = None
    if role in {"host", "both"}:
        token = setup_host(ctx, args, state)
    else:
        failed = [c for c in platform_checks(ctx, host=False) if c.status == "fail"]
        if failed:
            raise CliError(1, f"{failed[0].name}: {failed[0].detail} -> {failed[0].fix}")
    if role in {"client", "both"}:
        client.setup_client(ctx, args, host_token=token)
    state.save(ctx.paths.state_file)
    print(f"setup complete ({role}); check it any time with `wispralt doctor`")
    return 0
