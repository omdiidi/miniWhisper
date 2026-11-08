"""
test_integration_kind_guards.py — :func:`forbid_integration_kind` coverage.

The /me/* surface is human-employee-only — integration
keys (``kind='integration'``) must be 403'd. /v1/* is the OPPOSITE: it's the
surface integration keys are FOR, so we also exercise the negative
invariant (integration kind allowed through to /v1).

Auth is real: each request carries a token minted by ``make_user`` on the
per-test SQLite DB. The JobStore is a stub; tests focus on the dep gate.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from wispralt.middleware import openai_errors
from wispralt.middleware.cors import install_cors
from wispralt.ratelimit_per_token import init_rate_limit_state
from wispralt.routes import me as me_routes
from wispralt.routes.v1_transcriptions import router as v1_router

if TYPE_CHECKING:
    from .conftest import MakeUser, Wire

# ── helpers ───────────────────────────────────────────────────────────────────


class _RequestIdMiddleware(BaseHTTPMiddleware):
    """Same stand-in as the other v1 tests — sets request_id on request.state."""

    async def dispatch(self, request, call_next):  # type: ignore[override]
        rid = request.headers.get("x-request-id") or "test-rid"
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response


class _AlignedTok:
    __slots__ = ("text", "start", "end")

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


class _StubParakeet:
    """Async-mock ParakeetService for the /v1 reach-through test."""

    def __init__(self) -> None:
        self.transcribe_with_alignment = AsyncMock(
            return_value=(
                "hi.",
                1.0,
                [_AlignedTok("hi.", 0.0, 0.5)],
            )
        )


def _token(make_user: MakeUser, kind: str) -> dict[str, str]:
    _user, token = make_user(kind=kind)
    return {"Authorization": f"Bearer {token}"}


# ── /me/history ──────────────────────────────────────────────────────────────


class TestMeHistoryGuard:
    def test_me_history_blocks_integration_kind(self, make_user: MakeUser, wire: Wire) -> None:
        app = FastAPI()
        app.add_middleware(_RequestIdMiddleware)
        app.include_router(me_routes.router)

        wire(app)
        app.state.job_store = MagicMock()

        with TestClient(app) as client:
            resp = client.get("/me/history?range=30d", headers=_token(make_user, "integration"))
        assert resp.status_code == 403, resp.text

    def test_me_history_allows_employee_kind(self, make_user: MakeUser, wire: Wire) -> None:
        app = FastAPI()
        app.add_middleware(_RequestIdMiddleware)
        app.include_router(me_routes.router)
        wire(app)
        # JobStore.transcripts_in_range_filtered is invoked via asyncio.to_thread;
        # return a benign empty page so render proceeds.
        store = MagicMock()
        store.transcripts_in_range_filtered = MagicMock(return_value=([], None, None))
        app.state.job_store = store

        with TestClient(app) as client:
            resp = client.get("/me/history?range=30d", headers=_token(make_user, "employee"))
        # Anything not-403 indicates the guard let the request through.
        # 200 is the happy path (empty rows render the base template).
        assert resp.status_code != 403, resp.text
        assert resp.status_code == 200, resp.text


# ── /me/login: must remain unauthenticated ────────────────────────────────────


class TestMeLoginUnauthenticated:
    def test_me_login_get_still_unauthenticated(self, wire: Wire) -> None:
        # /me/login MUST NOT require auth — otherwise nobody can log in.
        # No deps overridden; if the route depended on require_api_key
        # transitively, no-auth would return 401.
        app = FastAPI()
        app.add_middleware(_RequestIdMiddleware)
        app.include_router(me_routes.router)
        wire(app)
        app.state.job_store = MagicMock()

        with TestClient(app) as client:
            resp = client.get("/me/login")
        assert resp.status_code == 200, resp.text

    def test_me_login_post_still_unauthenticated(self, wire: Wire) -> None:
        # POST /me/login with a bogus form should hit the route body's CSRF
        # check (not an auth dep). Expect NOT 401 — actual outcome will be a
        # rendered login form with an error (403 for CSRF failure, or 401 for
        # invalid token format). The point is the route body executes.
        app = FastAPI()
        app.add_middleware(_RequestIdMiddleware)
        app.include_router(me_routes.router)
        wire(app)
        app.state.job_store = MagicMock()

        with TestClient(app) as client:
            resp = client.post(
                "/me/login",
                data={"token": "abc", "csrf_token": "x"},
            )
        # 401 (token format check) and 403 (CSRF failure) both indicate the
        # route body ran — neither implies auth-dep rejection. Reject only an
        # unexpected 5xx or some other status.
        assert resp.status_code in (401, 403), resp.text


# ── /v1 reach-through: integration kind MUST be allowed ───────────────────────


class TestV1AllowsIntegrationKind:
    def test_v1_audio_transcriptions_allows_integration_kind(
        self, make_user: MakeUser, wire: Wire
    ) -> None:
        app = FastAPI()
        install_cors(app)
        app.add_middleware(_RequestIdMiddleware)
        app.include_router(v1_router)
        openai_errors.install(app)

        app.state.parakeet_service = _StubParakeet()
        app.state.parakeet_last_inference_at = None
        init_rate_limit_state(app)

        wire(app)

        # tiny WAV body — just enough to clear the multipart parser.
        from pathlib import Path

        wav = (Path(__file__).parent / "fixtures" / "tiny.wav").read_bytes()
        with TestClient(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", wav, "audio/wav")},
                data={"model": "whisper-1"},
                headers=_token(make_user, "integration"),  # what integration keys are for
            )
        assert resp.status_code == 200, resp.text
