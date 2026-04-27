"""
test_dictate_route_422.py — Route-level integration test for HTTP 422 mapping.

`tests/test_dictate_corrupt_audio.py` covers the inner ``_sync_transcribe``
boundary; this file pins the ROUTE-level contract: a malformed audio body
posted to ``POST /transcribe/dictate`` returns HTTP 422 (not 500), and the
response shape carries a ``detail`` string that begins with "Cannot decode".

Uses ``starlette.testclient.TestClient`` against a minimal FastAPI app that
mounts only the dictate router with a stub ``ParakeetService``.
"""

from __future__ import annotations

import io
import wave
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from wispralt_server._errors import CorruptAudioError
from wispralt_server.config import settings
from wispralt_server.routes.dictate import router as dictate_router


def _build_test_app(transcribe_side_effect: Any) -> FastAPI:
    """Build a minimal FastAPI app with the dictate router and a stub Parakeet.

    Bypasses bearer-auth via FastAPI's dependency_overrides so the test can
    focus on the corrupt-audio mapping in isolation.
    """
    app = FastAPI()

    class _StubParakeet:
        async def transcribe(self, audio_bytes: bytes) -> tuple[str, float]:
            if isinstance(transcribe_side_effect, BaseException):
                raise transcribe_side_effect
            return transcribe_side_effect

    app.state.parakeet_service = _StubParakeet()
    app.state.parakeet_last_inference_at = None
    app.include_router(dictate_router)

    # Override the bearer-auth dependency so we can hit the route without a key.
    from wispralt_server.auth import require_api_key

    app.dependency_overrides[require_api_key] = lambda: None
    return app


def _silent_wav_bytes(duration_s: float = 0.05) -> bytes:
    """Build a tiny valid WAV with N samples of silence (under MIN_SAMPLES so
    the route succeeds without invoking the model)."""
    n = max(1, int(16_000 * duration_s))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(b"\x00\x00" * n)
    return buf.getvalue()


class TestRouteCorruptAudioContract:
    """Route-level: garbage body must yield 422, not 500."""

    def test_corrupt_audio_returns_422(self) -> None:
        app = _build_test_app(CorruptAudioError("Cannot decode audio: garbage"))
        client = TestClient(app)
        garbage = b"NOT_AN_AUDIO_FILE_AT_ALL_THIS_IS_PLAINTEXT_GARBAGE" * 4
        # send under the size limit so we don't trip 413
        # send a small WAV-typed body so we don't trip 415
        resp = client.post(
            "/transcribe/dictate",
            files={"file": ("garbage.wav", garbage, "audio/wav")},
            headers={"Authorization": "Bearer test"},
        )
        assert resp.status_code == 422, f"got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "detail" in body
        assert body["detail"].startswith("Corrupt audio: Cannot decode")

    def test_415_when_content_type_not_audio(self) -> None:
        app = _build_test_app(("ignored", 0.0))
        client = TestClient(app)
        resp = client.post(
            "/transcribe/dictate",
            files={"file": ("not-audio.txt", b"plain text body", "text/plain")},
            headers={"Authorization": "Bearer test"},
        )
        assert resp.status_code == 415

    def test_413_when_real_body_exceeds_limit(self) -> None:
        # Send an actual body larger than max_upload_bytes — the route should
        # 413 either via the Content-Length pre-check or via the
        # post-read-length check.  Use a small max via settings override.
        from wispralt_server.config import settings as live_settings
        original = live_settings.max_upload_bytes
        try:
            # Temporarily shrink the cap so we can test it without uploading GBs.
            object.__setattr__(live_settings, "max_upload_bytes", 1024)
            app = _build_test_app(("ignored", 0.0))
            client = TestClient(app)
            big_body = b"\x00" * 4096  # well above 1024
            resp = client.post(
                "/transcribe/dictate",
                files={"file": ("big.wav", big_body, "audio/wav")},
                headers={"Authorization": "Bearer test"},
            )
            assert resp.status_code == 413, f"got {resp.status_code}: {resp.text}"
        finally:
            object.__setattr__(live_settings, "max_upload_bytes", original)

    def test_200_on_valid_short_clip_with_stub(self) -> None:
        # Stub returns ("hello", 12.0); the route should pass through.
        app = _build_test_app(("hello world", 12.5))
        client = TestClient(app)
        resp = client.post(
            "/transcribe/dictate",
            files={"file": ("ok.wav", _silent_wav_bytes(0.05), "audio/wav")},
            headers={"Authorization": "Bearer test"},
        )
        assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["text"] == "hello world"
        assert body["model_id"]
        assert isinstance(body["duration_ms"], (int, float))
