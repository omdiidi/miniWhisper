"""
test_app_wiring.py — the REAL application route table.

Imports ``wispralt.main`` and calls the real ``create_app()``. There is
deliberately no fallback app: if the import graph is broken (e.g. a deleted
module is still imported somewhere), this file fails at collection.

The lifespan is NOT run (it installs signal handlers, needs ffmpeg, and
loads Parakeet), so this only proves the route table and import graph.

Forbidden paths are built by concatenation so the repo-wide grep gate for
removed features stays clean.
"""
from __future__ import annotations

import os

os.environ.setdefault("SERVER_URL", "http://test.local")

import pytest
from fastapi.routing import APIRoute

from wispralt.main import create_app

_MEETING_PREFIX = "/transcribe/" + "meeting"
_MEETING_READYZ = "/readyz/" + "meeting"


@pytest.fixture
def routes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, frozenset[str]]]:
    # The removed dev-faults router read this env var at create_app() time;
    # setting it keeps the "no /dev/*" assertion meaningful against a regrowth.
    monkeypatch.setenv("WISPRALT_DEV_FAULTS", "1")
    app = create_app()
    return [
        (r.path, frozenset(r.methods or ()))
        for r in app.routes
        if isinstance(r, APIRoute)
    ]


def test_file_submit_routes_present(routes: list[tuple[str, frozenset[str]]]) -> None:
    table = {path: methods for path, methods in routes}
    assert "POST" in table["/transcribe/file"]
    assert "POST" in table["/transcribe/file/chunked/init"]
    assert "GET" in table["/readyz/dictation"]


def test_meeting_submit_route_absent(routes: list[tuple[str, frozenset[str]]]) -> None:
    offenders = [
        (path, sorted(methods))
        for path, methods in routes
        if path.startswith(_MEETING_PREFIX) and "POST" in methods
    ]
    assert offenders == []


def test_meeting_readyz_absent(routes: list[tuple[str, frozenset[str]]]) -> None:
    assert [p for p, _ in routes if p.startswith(_MEETING_READYZ)] == []


def test_file_job_routes_present(routes: list[tuple[str, frozenset[str]]]) -> None:
    by_path: dict[str, set[str]] = {}
    for path, methods in routes:
        by_path.setdefault(path, set()).update(methods)
    assert {"GET", "DELETE"} <= by_path["/transcribe/file/{job_id}"]
    assert "GET" in by_path["/transcribe/file/{job_id}/download/{fmt}"]


def test_no_meeting_routes_remain(routes: list[tuple[str, frozenset[str]]]) -> None:
    assert [p for p, _ in routes if p.startswith(_MEETING_PREFIX)] == []


_TELEMETRY_PREFIX = "/" + "telemetry"
_DEV_PREFIX = "/" + "dev"


def test_no_telemetry_or_dev_routes(routes: list[tuple[str, frozenset[str]]]) -> None:
    # The fixture sets WISPRALT_DEV_FAULTS=1, so a lingering dev-faults mount
    # would show up here.
    offenders = [
        p for p, _ in routes
        if p.startswith(_TELEMETRY_PREFIX) or p.startswith(_DEV_PREFIX + "/")
        or p == _DEV_PREFIX
    ]
    assert offenders == []


def test_removed_admin_key_routes_absent(routes: list[tuple[str, frozenset[str]]]) -> None:
    # Break-glass key rotation is gone: the first admin comes from wispralt.bootstrap.
    assert ("/admin/" + "rotate-key") not in {p for p, _ in routes}


def test_readyz_db_present(routes: list[tuple[str, frozenset[str]]]) -> None:
    assert "GET" in dict(routes)["/readyz/db"]


# ── MLX is optional at import (the [host] extra) ──────────────────────────────

_BLOCKED_IMPORT_SMOKE = r"""
import importlib.abc, sys

BLOCKED = ("mlx", "mlx_whisper", "parakeet_mlx")

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"blocked: {name}")
        return None

sys.meta_path.insert(0, Block())
for mod in [m for m in sys.modules if m.split(".")[0] in BLOCKED]:
    del sys.modules[mod]

import wispralt.main as main
app = main.create_app()
assert app.routes
leaked = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
assert not leaked, leaked
print("ok")
"""


def test_main_imports_and_builds_without_mlx(tmp_path) -> None:
    import subprocess
    import sys

    env = {**os.environ, "DATA_DIR": str(tmp_path), "SERVER_URL": "http://test.local"}
    result = subprocess.run(
        [sys.executable, "-c", _BLOCKED_IMPORT_SMOKE],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "ok"


def test_parakeet_load_fn_runs_without_parakeet_mlx() -> None:
    import io
    import sys
    import wave

    from wispralt.dictate.parakeet import MODEL_ID, ParakeetService

    seen: list[tuple[str, int]] = []

    def load_fn(model_id: str):
        def run(samples: object) -> object:
            seen.append((model_id, len(samples)))  # type: ignore[arg-type]
            return type("Hyp", (), {"text": " stub text "})()

        return run

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(b"\x01\x00" * 16_000)
    had_parakeet = "parakeet_mlx" in sys.modules

    service = ParakeetService(load_fn=load_fn)
    assert service.model_id == MODEL_ID
    service.load()
    text, ms = service._sync_transcribe(buf.getvalue())

    assert service.ready and text == "stub text" and ms >= 0
    assert seen == [(MODEL_ID, 16_000)]
    assert ("parakeet_mlx" in sys.modules) == had_parakeet  # never imported by this path
