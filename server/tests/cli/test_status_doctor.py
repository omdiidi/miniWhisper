"""Task 4: status (sizes, JSON shape) and doctor (checks, exit 3, fix hints, warm-up)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from wispralt.cli import host, launchd, ops
from wispralt.cli.paths import merge_missing

if TYPE_CHECKING:
    from pathlib import Path

    from wispralt.cli.paths import Ctx
    from wispralt.cli.system import Deps

    from .conftest import CliFn, FakeHttp, FakeRunner


@pytest.fixture
def healthy(ctx: Ctx) -> Ctx:
    """A host whose config, data dir, DB and loaded agent are all in order."""
    merge_missing(ctx.paths.env_file, {"DATA_DIR": str(ctx.paths.data_dir), "SERVER_URL": ctx.url})
    host.bootstrap_admin(ctx.paths.data_dir, "owner")
    launchd.start(ctx)
    return ctx


def _blocks(path: Path) -> int:
    return path.lstat().st_blocks * 512


def test_allocated_size_math(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    files = [root / "x.bin", root / "a" / "y.bin", root / "a" / "b" / "z.bin"]
    for i, f in enumerate(files):
        f.write_bytes(b"\1" * (5000 * (i + 1)))
    (root / "link").symlink_to(files[0])
    expect = sum(_blocks(p) for p in [root, root / "a", root / "a" / "b", *files, root / "link"])
    assert ops.allocated(root) == expect
    assert ops.allocated(files[2]) >= 15000
    assert ops.allocated(tmp_path / "missing") == 0
    assert ops.human(1_234_567) == "1.2 MB" and ops.human(12) == "12 B"


def test_status_json_shape(healthy: Ctx, cli: CliFn) -> None:
    (healthy.paths.data_dir / "outputs").mkdir()
    (healthy.paths.data_dir / "outputs" / "a.txt").write_text("x" * 9000)
    bk = healthy.paths.backup_dir / "wispralt-1"
    bk.mkdir(parents=True)
    (bk / "manifest.json").write_text("{}")
    aside = healthy.paths.data_dir.with_name("host.pre-restore-20260101-000000-000000")
    aside.mkdir()
    code, out, _ = cli("status", "--json")
    r = json.loads(out)
    assert code == 0
    assert set(r) == {"version", "role", "service", "url", "server_url", "sizes", "db"}
    assert r["service"]["state"] == "running" and r["service"]["pid"] == 4242
    assert set(r["sizes"]) == {
        "data_dir", "db", "outputs", "staging", "models", "backups", "pre_restore",
    }  # fmt: skip
    assert r["sizes"]["outputs"] >= 9000 and r["sizes"]["db"] > 0
    assert r["sizes"]["backups"]["count"] == 1 and r["sizes"]["backups"]["newest"] == str(bk)
    assert r["sizes"]["pre_restore"][0]["path"] == str(aside)
    assert r["db"] == {
        "retention_days": 0,
        "would_purge": {"dictations": 0, "jobs": 0},
        "admins": 1,
    }


def test_status_text(healthy: Ctx, cli: CliFn, runner: FakeRunner) -> None:
    runner.on("pgrep", rc=1)
    code, out, _ = cli("status", "--client")
    assert code == 0
    assert "retention: keep forever" in out and "admins: 1" in out
    assert "running (pid 4242)" in out and "client: not installed" in out


def test_status_before_install(cli: CliFn) -> None:
    code, out, _ = cli("status")
    assert code == 0 and "not installed" in out and "not loaded" in out


def test_doctor_all_green(healthy: Ctx, cli: CliFn) -> None:
    code, out, _ = cli("doctor")
    assert code == 0, out
    assert "FAIL" not in out and "/readyz/dictation" in out and "INFO  login" in out


@pytest.mark.parametrize(
    ("breakage", "name"),
    [
        ("http", "/readyz/dictation"),
        ("env_mode", "env-file"),
        ("env_data_dir", "env-data-dir"),
        ("data_mode", "data-dir"),
        ("unloaded", "service"),
        ("arch", "apple-silicon"),
        ("macos", "macos"),
        ("disk", "disk"),
        ("ffmpeg", "ffmpeg"),
        ("legacy", "legacy-agent"),
    ],
)
def test_each_failing_check_exits_3_with_a_fix(
    healthy: Ctx,
    cli: CliFn,
    http: FakeHttp,
    runner: FakeRunner,
    deps: Deps,
    breakage: str,
    name: str,
) -> None:
    p = healthy.paths
    if breakage == "http":
        http.status["/readyz/dictation"] = 503
    elif breakage == "env_mode":
        p.env_file.chmod(0o644)
    elif breakage == "env_data_dir":
        p.env_file.write_text('DATA_DIR="/elsewhere"\n')
    elif breakage == "data_mode":
        p.data_dir.chmod(0o755)
    elif breakage == "unloaded":
        runner.loaded.clear()
    elif breakage == "arch":
        deps.machine = "x86_64"
    elif breakage == "macos":
        deps.macos = "13.6"
    elif breakage == "disk":
        deps.disk_free = lambda _p: 10**9
    elif breakage == "ffmpeg":
        deps.which = lambda n: None if n == "ffmpeg" else "/x"
    elif breakage == "legacy":
        runner.loaded.add("co.wispralt.server")
    code, out, _ = cli("doctor")
    assert code == 3
    [line] = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
    assert f"  {name}:" in line and "  -> " in line


def test_corrupt_db_fails_integrity(healthy: Ctx, cli: CliFn) -> None:
    db = healthy.paths.db_path
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    raw = bytearray(db.read_bytes())
    raw[100:4096] = b"\xff" * (4096 - 100)
    db.write_bytes(bytes(raw))
    code, out, _ = cli("doctor", "--json")
    checks = {c["name"]: c for c in json.loads(out)["checks"]}
    assert code == 3 and checks["db-integrity"]["status"] == "fail"


def test_client_checks(ctx: Ctx, cli: CliFn, runner: FakeRunner) -> None:
    runner.on("xcode-select", out="/Library/Developer/CommandLineTools\n")
    runner.on("security", "find-generic-password", rc=44)
    runner.on("pgrep", rc=1)
    code, out, _ = cli("doctor", "--client", "--json")
    checks = {c["name"]: c["status"] for c in json.loads(out)["checks"]}
    assert checks["xcode"] == "warn" and checks["signing-identity"] == "warn"
    assert checks["app"] == "fail" and code == 3
    assert not any("-w" in c for c in runner.calls if c[:1] == ["security"])


def test_warmup_uses_the_injected_loader(healthy: Ctx, cli: CliFn, deps: Deps) -> None:
    seen: list[int] = []

    def load(_model_id: str) -> object:
        def run(samples: object) -> SimpleNamespace:
            seen.append(len(samples))  # type: ignore[arg-type]
            return SimpleNamespace(text="")

        return run

    deps.load_fn = load
    code, out, _ = cli("doctor", "--warmup")
    assert code == 0 and "OK    warmup" in out and seen == [16_000]

    def broken(_model_id: str) -> object:
        raise OSError("weights missing")

    deps.load_fn = broken
    code, out, _ = cli("doctor", "--warmup")
    assert code == 3 and "weights missing" in out


def test_every_command_pins_hf_home_to_models_dir(healthy: Ctx, cli: CliFn) -> None:
    """huggingface_hub reads HF_HOME at import: main() sets it before any command runs."""
    import os

    cli("status")
    assert os.environ["HF_HOME"] == str(healthy.paths.models_dir)
