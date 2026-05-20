"""
test_v1_transcriptions.py — HTTP-shape coverage for the OpenAI-compat
``POST /v1/audio/transcriptions`` route.

The Parakeet MLX model never runs on the dev box; we stub
``app.state.parakeet_service`` with a small async-mock service that returns
canned ``(text, ms, aligned_tokens)`` triples. Every test mounts a minimal
FastAPI app composed of:

  - CORS middleware (so OPTIONS preflights resolve)
  - the request-id middleware (so ``request.state.request_id`` is set, which
    the OpenAI error envelope echoes back on errors)
  - ``v1_transcriptions.router`` (the system under test)
  - the OpenAI exception handlers installed via ``openai_errors.install``
    so 401/403/etc. ride the OpenAI envelope shape (matching production).

Auth is handled by overriding ``require_api_key_v1`` via FastAPI's
``dependency_overrides`` — we don't go through Postgres.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

# Pydantic Settings reads these at import time — set defaults BEFORE any
# wispralt_server import so the tests work on a bare dev box without a .env.
os.environ.setdefault("HF_TOKEN", "stub")
os.environ.setdefault("WISPRALT_API_KEY", "stub_api_key_for_tests_only")
os.environ.setdefault("SERVER_URL", "http://test.local")
os.environ.setdefault("MEETING_OUTPUT_DIR", "/tmp/wispralt-test-meeting")
os.environ.setdefault("JOB_DB_PATH", "/tmp/wispralt-test-jobs.db")
os.environ.setdefault("STAGING_DIR", "/tmp/wispralt-test-staging")

from typing import TYPE_CHECKING

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from wispralt_server._errors import (
    AudioTooLongError,
    CorruptAudioError,
)
from wispralt_server.auth import require_api_key_v1
from wispralt_server.middleware import openai_errors
from wispralt_server.middleware.cors import install_cors
from wispralt_server.ratelimit_per_token import (
    init_rate_limit_state,
)
from wispralt_server.routes.v1_transcriptions import router as v1_router
from wispralt_server.users.store import User

if TYPE_CHECKING:
    import pytest

FIXTURES = Path(__file__).parent / "fixtures"


# ── fixture/helper code ───────────────────────────────────────────────────────


class _RequestIdMiddleware(BaseHTTPMiddleware):
    """Minimal stand-in for main.py's _RequestIdMiddleware.

    The OpenAI error envelope reads ``request.state.request_id`` to inject the
    correlation id into ``error.request_id``; without something to set it the
    field is silently dropped (tests of the request-id header would then
    fail).
    """

    async def dispatch(self, request, call_next):  # type: ignore[override]
        rid = request.headers.get("x-request-id") or "test-rid-1234"
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response


class _AlignedTok:
    """Minimal AlignedToken stand-in: parakeet-mlx exposes ``.text/.start/.end``."""

    __slots__ = ("text", "start", "end")

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


def _aligned_hello_world() -> list[_AlignedTok]:
    """Two aligned tokens spanning 0..1s — enough to make verbose_json
    produce a single non-degenerate segment and words[] of length 2."""
    return [
        _AlignedTok("hello ", 0.0, 0.4),
        _AlignedTok("world.", 0.4, 1.0),
    ]


class _StubParakeet:
    """async mock of ParakeetService.transcribe_with_alignment.

    The route reads ``request.app.state.parakeet_service`` and calls
    ``await parakeet_service.transcribe_with_alignment(samples)``. We expose
    a single AsyncMock so each test can swap its return value or side effect.
    """

    def __init__(self) -> None:
        self.transcribe_with_alignment = AsyncMock(
            return_value=("hello world.", 12.5, _aligned_hello_world())
        )


def _override_auth(app: FastAPI, *, user: User | None = None) -> User:
    """Install a dependency override for ``require_api_key_v1`` that returns
    the supplied User (or a sensible default integration-kind one).

    NOTE: ``rate_limit_v1_per_token`` depends transitively on
    ``require_api_key_v1`` — overriding the inner dep is enough to satisfy
    both because FastAPI's dependency cache resolves each unique callable
    once per request.
    """
    if user is None:
        user = User(id=42, label="key-test-app", role="employee", kind="integration")

    async def _fake(_request: Any = None) -> User:
        return user

    app.dependency_overrides[require_api_key_v1] = _fake
    return user


def _build_app(*, override_auth: bool = True) -> FastAPI:
    """Compose the minimal app stack for /v1/audio/transcriptions tests."""
    app = FastAPI()
    install_cors(app)  # OUTERMOST — covers OPTIONS preflights + ACAO on errors
    app.add_middleware(_RequestIdMiddleware)
    app.include_router(v1_router)
    openai_errors.install(app)

    # Replace parakeet with stub — never touch MLX on the dev box.
    app.state.parakeet_service = _StubParakeet()
    app.state.parakeet_last_inference_at = None
    init_rate_limit_state(app)

    if override_auth:
        _override_auth(app)
    return app


def _client(app: FastAPI) -> TestClient:
    """Build a TestClient that triggers lifespan (with-statement)."""
    return TestClient(app)


def _tiny_wav() -> bytes:
    return (FIXTURES / "tiny.wav").read_bytes()


# ── response_format tests ─────────────────────────────────────────────────────


class TestResponseFormats:
    def test_post_json_returns_text(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"response_format": "json", "model": "whisper-1"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "text" in body
        assert isinstance(body["text"], str)

    def test_post_text_returns_plain_text(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"response_format": "text", "model": "whisper-1"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "text/plain; charset=utf-8"
        # body must be the raw text, not a JSON wrapper
        assert resp.text == "hello world."

    def test_post_verbose_json_full_shape(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"response_format": "verbose_json", "model": "whisper-1"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["task"] == "transcribe"
        assert body["language"] == "english"
        assert isinstance(body["duration"], float)
        assert "text" in body
        assert isinstance(body["segments"], list)
        assert len(body["segments"]) >= 1
        seg = body["segments"][0]
        for field in (
            "id",
            "seek",
            "start",
            "end",
            "text",
            "tokens",
            "temperature",
            "avg_logprob",
            "compression_ratio",
            "no_speech_prob",
            "transient",
        ):
            assert field in seg, f"missing {field} in segment"
        assert seg["transient"] is False

    def test_post_verbose_json_with_word_timestamps(self) -> None:
        app = _build_app()
        with _client(app) as client:
            # httpx's multipart encoder rejects `data=` as a list of tuples
            # when `files=` is also set (TypeError on stream read). Use a
            # plain dict — bracket-suffixed keys like `timestamp_granularities[]`
            # survive as-is.
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={
                    "response_format": "verbose_json",
                    "model": "whisper-1",
                    "timestamp_granularities[]": "word",
                },
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "words" in body
        assert isinstance(body["words"], list)

    def test_post_srt_format(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"response_format": "srt", "model": "whisper-1"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("application/x-subrip")
        # SubRip blocks start with "1\n00:00:..."
        assert resp.text.startswith("1\n00:00:"), resp.text[:60]

    def test_post_vtt_format(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"response_format": "vtt", "model": "whisper-1"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/vtt")
        assert resp.text.startswith("WEBVTT"), resp.text[:60]


# ── validation errors ────────────────────────────────────────────────────────


class TestValidationErrors:
    def test_post_invalid_response_format(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"response_format": "garbage", "model": "whisper-1"},
                headers={"Authorization": "Bearer test"},
            )
        # Route returns 422 with code=invalid_response_format
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["error"]["code"] == "invalid_response_format"
        assert body["error"]["type"] == "invalid_request_error"

    def test_post_diarize_model_returns_404(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"model": "gpt-4o-transcribe-diarize"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["error"]["code"] == "model_not_found"

    def test_post_temperature_out_of_range(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"model": "whisper-1", "temperature": "2.0"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["error"]["code"] == "validation_failed"

    def test_post_stream_true_rejected(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"model": "whisper-1", "stream": "true"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["error"]["code"] == "streaming_unsupported"

    def test_post_timestamp_granularities_without_verbose_json(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={
                    "response_format": "json",
                    "model": "whisper-1",
                    "timestamp_granularities[]": "word",
                },
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["error"]["code"] == "validation_failed"


# ── silent-accept fields ──────────────────────────────────────────────────────


class TestSilentlyAcceptedFields:
    def test_post_include_logprobs_silently_accepted(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"model": "whisper-1", "include[]": "logprobs"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 200, resp.text

    def test_post_user_field_silently_accepted(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"model": "whisper-1", "user": "alice@example.com"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 200, resp.text

    def test_post_openai_sdk_headers_accepted(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"model": "whisper-1"},
                headers={
                    "Authorization": "Bearer test",
                    "OpenAI-Organization": "org-test",
                    "OpenAI-Project": "proj-test",
                    "X-Stainless-Lang": "python",
                },
            )
        assert resp.status_code == 200, resp.text


# ── payload + decode failure paths ────────────────────────────────────────────


class TestPayloadAndDecodeFailures:
    def test_post_oversized_file_returns_413(self) -> None:
        app = _build_app()
        # 26 MB blob — well above OPENAI_COMPAT_SIZE_CAP (25 MB).
        big = b"\x00" * (26 * 1024 * 1024)
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("big.wav", big, "audio/wav")},
                data={"model": "whisper-1"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 413, resp.text
        body = resp.json()
        assert body["error"]["code"] == "file_too_large"

    def test_post_audio_too_long_returns_400(self) -> None:
        app = _build_app()
        app.state.parakeet_service.transcribe_with_alignment.side_effect = (
            AudioTooLongError("Audio too long: 1000.0s (max 900s)")
        )
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"model": "whisper-1"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["error"]["code"] == "audio_too_long"

    def test_post_empty_audio_returns_empty_text(self) -> None:
        app = _build_app()
        app.state.parakeet_service.transcribe_with_alignment.return_value = (
            "",
            0.0,
            None,
        )
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"response_format": "verbose_json", "model": "whisper-1"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["text"] == ""
        assert body["segments"] == []

    def test_post_corrupt_audio_returns_400_not_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force the decode helper to raise CorruptAudioError. Patch the
        # `decode_to_pcm` symbol imported into the route module (not the
        # source module) — `from X import Y` rebinds Y locally.
        from wispralt_server.routes import v1_transcriptions as v1_route_mod

        def _raise(_b: bytes) -> Any:
            raise CorruptAudioError("Cannot decode audio: garbage")

        monkeypatch.setattr(v1_route_mod, "decode_to_pcm", _raise)
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("bogus.wav", b"not actually audio", "audio/wav")},
                data={"model": "whisper-1"},
                headers={"Authorization": "Bearer test"},
            )
        # MUST be 400 — NOT 500 (which would trigger the openai-python retry).
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["error"]["code"] == "invalid_audio_data"


# ── auth ─────────────────────────────────────────────────────────────────────


class TestAuth:
    def test_no_bearer_returns_401_envelope(self) -> None:
        # Don't override auth — let the real require_api_key_v1 run.
        app = _build_app(override_auth=False)
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"model": "whisper-1"},
            )
        assert resp.status_code == 401, resp.text
        body = resp.json()
        # OpenAI envelope, not native {"detail": ...}
        assert "error" in body
        assert body["error"]["code"] == "invalid_api_key"

    def test_cookie_only_auth_rejected_on_v1(self) -> None:
        # Setting the admin session cookie WITHOUT a Bearer header MUST 401 on
        # /v1 — cookie auth is intentionally not consulted by require_api_key_v1.
        app = _build_app(override_auth=False)
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"model": "whisper-1"},
                cookies={"wispralt_admin_token": "deadbeef" * 8},
            )
        assert resp.status_code == 401, resp.text


# ── response headers + CORS ───────────────────────────────────────────────────


class TestResponseHeadersAndCors:
    def test_response_headers_present(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"model": "whisper-1"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 200, resp.text
        # Header names are case-insensitive in starlette; case-folded lookup
        assert resp.headers.get("x-request-id")
        assert resp.headers.get("openai-version")
        assert resp.headers.get("openai-processing-ms")
        assert resp.headers.get("openai-model") == "whisper-1"

    def test_cors_options_preflight(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.options(
                "/v1/audio/transcriptions",
                headers={
                    "Origin": "https://other.example.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Authorization, Content-Type",
                },
            )
        assert resp.status_code == 200, resp.text
        # CORSMiddleware echoes the wildcard or the request Origin into ACAO.
        acao = resp.headers.get("access-control-allow-origin")
        assert acao is not None


# ── /v1/audio/translations stub ───────────────────────────────────────────────


class TestTranslationsStub:
    def test_translations_stub_returns_400(self) -> None:
        app = _build_app()
        with _client(app) as client:
            resp = client.post(
                "/v1/audio/translations",
                files={"file": ("tiny.wav", _tiny_wav(), "audio/wav")},
                data={"model": "whisper-1"},
                headers={"Authorization": "Bearer test"},
            )
        # Stub returns 400 / endpoint_not_supported. Auth dep
        # (rate_limit_v1_per_token -> require_api_key_v1) is resolved via
        # the override installed by _build_app().
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["error"]["code"] == "endpoint_not_supported"
