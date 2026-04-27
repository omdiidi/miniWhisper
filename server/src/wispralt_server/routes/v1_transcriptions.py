"""OpenAI-compatible /v1/audio/transcriptions endpoint.

Drop-in replacement for any client that talks to OpenAI's audio transcription
API. Bearer token = WisprAlt token. Sync, dictate-only. Caps at 25 MB to match
OpenAI's documented limit. Returns raw Parakeet output (no smart formatting).

Spec reference:
https://platform.openai.com/docs/api-reference/audio/createTranscription
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from wispralt_server.auth import require_api_key
from wispralt_server.config import settings
from wispralt_server.constants import OPENAI_COMPAT_SIZE_CAP
from wispralt_server.dictate.parakeet import MODEL_ID
from wispralt_server.users.store import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai-compat"])

_SUPPORTED_FORMATS = {"json", "text"}
_UNSUPPORTED_FORMATS = {"srt", "vtt", "verbose_json"}


def _openai_error(
    request: Request, message: str, type_: str, code: str, status: int
) -> JSONResponse:
    """OpenAI-shaped error envelope. Includes request_id for support correlation."""
    request_id = getattr(request.state, "request_id", None)
    body: dict = {
        "error": {"message": message, "type": type_, "param": None, "code": code}
    }
    if request_id:
        body["error"]["request_id"] = request_id
    return JSONResponse(status_code=status, content=body)


@router.post("/audio/transcriptions", response_model=None)
async def create_transcription(
    request: Request,
    file: UploadFile,
    # ``response_format`` is plain ``str``, validated in-handler so we control the error
    # envelope shape. FastAPI's automatic Literal validation produces Pydantic-shape
    # errors, NOT OpenAI shape.
    response_format: str = Form(default="json"),
    model: str = Form(default="whisper-1"),  # accepted but ignored — we always route to Parakeet
    language: str | None = Form(default=None),  # accepted, ignored
    prompt: str | None = Form(default=None),  # accepted, ignored
    temperature: float | None = Form(default=None),  # accepted, ignored
    user: User = Depends(require_api_key),
) -> JSONResponse | PlainTextResponse:
    # Log unrecognized model values so admin can see what 3rd-party clients are sending.
    if model not in {"whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"}:
        logger.info("v1.transcriptions.unknown_model model=%r user=%d", model, user.id)

    response_format = response_format.lower().strip()
    if response_format in _UNSUPPORTED_FORMATS:
        return _openai_error(
            request,
            f"response_format='{response_format}' is not supported on this endpoint. "
            "Use 'json' or 'text' for sync transcription. For timestamps and segments, "
            "use the native /transcribe/meeting async API.",
            "invalid_request_error",
            "unsupported_response_format",
            422,
        )
    if response_format not in _SUPPORTED_FORMATS:
        return _openai_error(
            request,
            f"response_format='{response_format}' is not a recognized format. "
            f"Allowed: {sorted(_SUPPORTED_FORMATS)}.",
            "invalid_request_error",
            "invalid_response_format",
            422,
        )

    # Read with cap (matches dictate.py pattern). Prevents OOM from a malicious 10 GB
    # body that would otherwise be fully read before the size check below.
    cap = min(OPENAI_COMPAT_SIZE_CAP, settings.max_upload_bytes)
    audio_bytes = await file.read(cap + 1)
    if len(audio_bytes) > cap:
        return _openai_error(
            request,
            f"Audio file exceeds {cap // (1024 * 1024)} MB cap on /v1/audio/transcriptions. "
            "For longer audio, use the native /transcribe/meeting async endpoint.",
            "invalid_request_error",
            "file_too_large",
            413,
        )

    parakeet_service = request.app.state.parakeet_service
    try:
        text, _inference_ms = await parakeet_service.transcribe(audio_bytes)
    except Exception as exc:
        logger.exception("v1.transcriptions.failed user=%d", user.id)
        return _openai_error(
            request,
            f"Transcription failed: {type(exc).__name__}",
            "server_error",
            "transcription_failed",
            500,
        )

    # NOTE: smart formatting is intentionally NOT applied here. /v1 always returns raw
    # Parakeet output to match the OpenAI contract (callers expect no opinionated
    # post-processing). Native /transcribe/dictate accepts X-Smart-Format: true for
    # third-party callers that want cleanup — see docs/INTEGRATION-GUIDE.md.
    _ = MODEL_ID  # keep import used; useful for future X-Model-Id response header
    if response_format == "text":
        return PlainTextResponse(content=text, status_code=200)
    return JSONResponse(status_code=200, content={"text": text})
