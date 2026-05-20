"""OpenAI-compatible /v1/audio/transcriptions endpoint.

Drop-in replacement for any client that talks to OpenAI's audio transcription
API. Bearer token = WisprAlt token (Bearer only — admin session cookie is
ignored on /v1). Sync, dictate-only. Caps at 25 MB to match OpenAI's documented
limit.

Response formats: json (default), text, verbose_json, srt, vtt.
Supported input formats: wav/flac/ogg/aiff (libsndfile) + mp3/m4a/mp4/webm/aac/mpeg (ffmpeg).

Rate limit: per-token, 60 req/min, enforced via Depends(rate_limit_v1_per_token).
This unit differs from /transcribe/dictate's per-IP bucket — see plan
`2026-05-20-openai-compat-fully-dialed.md` for rationale.

Spec reference:
https://platform.openai.com/docs/api-reference/audio/createTranscription
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from wispralt_server._errors import (
    AudioTooLongError,
    CorruptAudioError,
    DecodeTimeoutError,
    UnsupportedAudioError,
)
from wispralt_server.config import settings
from wispralt_server.constants import (
    OPENAI_COMPAT_SIZE_CAP,
    OPENAI_COMPAT_VERSION,
)
from wispralt_server.dictate.sync_decode import decode_to_pcm
from wispralt_server.dictate.v1_response_builders import (
    build_srt,
    build_verbose_json,
    build_vtt,
)
from wispralt_server.ratelimit_per_token import rate_limit_v1_per_token
from wispralt_server.users.store import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai-compat"])

_ALL_FORMATS = {"json", "text", "verbose_json", "srt", "vtt"}
_KNOWN_MODELS = {
    "whisper-1",
    "gpt-4o-transcribe",
    "gpt-4o-mini-transcribe",
    "gpt-4o-mini-transcribe-2025-12-15",
    "gpt-4o-mini-transcribe-2025-03-20",
}
# gpt-4o-transcribe-diarize: NOT in _KNOWN_MODELS AND NOT advertised in /v1/models.
# Requests with this model id return 404 model_not_found (matches OpenAI semantics
# for a model that doesn't exist on this endpoint).
_DIARIZE_MODEL = "gpt-4o-transcribe-diarize"


def _openai_error(
    request: Request,
    message: str,
    type_: str,
    code: str,
    status: int,
    headers: dict | None = None,
) -> JSONResponse:
    """OpenAI-shaped error envelope. Includes request_id and standard openai-* headers."""
    request_id = getattr(request.state, "request_id", None)
    body: dict = {
        "error": {"message": message, "type": type_, "param": None, "code": code}
    }
    if request_id:
        body["error"]["request_id"] = request_id
    resp_headers = {
        "openai-version": OPENAI_COMPAT_VERSION,
    }
    if headers:
        resp_headers.update(headers)
    return JSONResponse(status_code=status, content=body, headers=resp_headers)


def _attach_openai_response_headers(
    response: Response,
    processing_ms: int,
    model: str,
) -> None:
    """Set the OpenAI-standard response headers on a successful response."""
    response.headers["openai-version"] = OPENAI_COMPAT_VERSION
    response.headers["openai-processing-ms"] = str(processing_ms)
    response.headers["openai-model"] = model


@router.post("/audio/transcriptions", response_model=None)
async def create_transcription(
    request: Request,
    file: UploadFile,
    # `response_format` is plain str so we control the error envelope shape
    # (FastAPI's Literal validation produces Pydantic errors, not OpenAI shape).
    response_format: str = Form(default="json"),
    model: str = Form(default="whisper-1"),
    language: str | None = Form(default=None),       # accepted, ignored
    prompt: str | None = Form(default=None),         # accepted, ignored
    temperature: float | None = Form(default=None),  # validated range, then ignored
    # OpenAI optional end-user identifier. Accepted, debug-logged, ignored.
    user_field: str | None = Form(default=None, alias="user"),
    # Aggregated list params — FastAPI parses repeated `timestamp_granularities[]` form fields.
    # OpenAI's spec uses `timestamp_granularities[]` (with brackets) in form encoding.
    stream: bool = Form(default=False),
    # Whether the route's auth + rate-limit dep chain is satisfied:
    user: User = Depends(rate_limit_v1_per_token),
) -> Response:
    t_start = time.perf_counter()

    # Read these list-shaped form fields manually — FastAPI's Form() doesn't
    # handle `name[]` array syntax natively.
    raw_form = await request.form()
    timestamp_granularities = [
        v for k, v in raw_form.multi_items()
        if k in ("timestamp_granularities[]", "timestamp_granularities")
    ]
    include = [
        v for k, v in raw_form.multi_items()
        if k in ("include[]", "include")
    ]

    # ── 1. Validate form fields (fail-fast before any inference) ──────────────
    response_format = response_format.lower().strip()
    if response_format not in _ALL_FORMATS:
        return _openai_error(
            request,
            f"response_format='{response_format}' is not supported. "
            f"Allowed: {sorted(_ALL_FORMATS)}.",
            "invalid_request_error",
            "invalid_response_format",
            422,
        )

    if model == _DIARIZE_MODEL:
        return _openai_error(
            request,
            f"Model '{_DIARIZE_MODEL}' is not available on this endpoint. "
            "For diarization, use the native /transcribe/meeting async API.",
            "invalid_request_error",
            "model_not_found",
            404,
        )

    if model not in _KNOWN_MODELS:
        logger.info(
            "v1.transcriptions.unknown_model model=%r user=%d", model, user.id
        )

    if temperature is not None and (temperature < 0.0 or temperature > 1.0):
        return _openai_error(
            request,
            f"temperature={temperature} must be between 0.0 and 1.0 inclusive.",
            "invalid_request_error",
            "validation_failed",
            400,
        )

    if stream:
        return _openai_error(
            request,
            "Streaming SSE responses are not supported on /v1/audio/transcriptions "
            "for any model — WisprAlt only ships the non-streaming endpoint.",
            "invalid_request_error",
            "streaming_unsupported",
            400,
        )

    if timestamp_granularities and response_format != "verbose_json":
        return _openai_error(
            request,
            "timestamp_granularities[] requires response_format='verbose_json'.",
            "invalid_request_error",
            "validation_failed",
            400,
        )

    include_words = "word" in timestamp_granularities

    if include:
        # include[]=logprobs is the only valid value upstream; we silently
        # no-op — Parakeet doesn't expose logprobs. Other values likewise ignored.
        logger.debug("v1.transcriptions.include_ignored include=%r user=%d", include, user.id)

    if user_field:
        logger.debug("v1.transcriptions.user_field user=%r account=%d", user_field, user.id)

    # ── 2. Read with cap ──────────────────────────────────────────────────────
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

    # ── 3. Decode (off the event loop) ────────────────────────────────────────
    try:
        samples, duration_s = await asyncio.to_thread(decode_to_pcm, audio_bytes)
    except AudioTooLongError as exc:
        return _openai_error(
            request, str(exc), "invalid_request_error", "audio_too_long", 400
        )
    except UnsupportedAudioError as exc:
        return _openai_error(
            request, str(exc), "invalid_request_error", "unsupported_file_type", 400
        )
    except DecodeTimeoutError as exc:
        return _openai_error(
            request, str(exc), "invalid_request_error", "decode_timeout", 400
        )
    except CorruptAudioError as exc:
        return _openai_error(
            request, str(exc), "invalid_request_error", "invalid_audio_data", 400
        )
    except Exception:
        logger.exception("v1.transcriptions.decode_failed user=%d", user.id)
        return _openai_error(
            request,
            "Audio decode failed unexpectedly.",
            "server_error",
            "internal_error",
            500,
        )

    # ── 4. Transcribe (offloaded to single-thread executor in ParakeetService) ─
    parakeet_service = request.app.state.parakeet_service
    try:
        text, _inference_ms, aligned_tokens = await parakeet_service.transcribe_with_alignment(samples)
    except AudioTooLongError as exc:
        # Sample-count check in Parakeet may also raise this if our decode produced
        # > MAX_SAMPLES (Parakeet has its own re-check inside _sync_transcribe_with_alignment).
        return _openai_error(
            request, str(exc), "invalid_request_error", "audio_too_long", 400
        )
    except Exception as exc:
        logger.exception("v1.transcriptions.inference_failed user=%d", user.id)
        return _openai_error(
            request,
            f"Transcription failed: {type(exc).__name__}",
            "server_error",
            "transcription_failed",
            500,
        )

    # ── 5. Build response per format ──────────────────────────────────────────
    processing_ms = int((time.perf_counter() - t_start) * 1_000.0)

    if response_format == "text":
        resp: Response = PlainTextResponse(
            content=text,
            status_code=200,
            media_type="text/plain; charset=utf-8",
        )
    elif response_format == "srt":
        resp = PlainTextResponse(
            content=build_srt(text, duration_s, aligned_tokens),
            status_code=200,
            media_type="application/x-subrip",
        )
    elif response_format == "vtt":
        resp = PlainTextResponse(
            content=build_vtt(text, duration_s, aligned_tokens),
            status_code=200,
            media_type="text/vtt",
        )
    elif response_format == "verbose_json":
        body = build_verbose_json(text, duration_s, aligned_tokens, include_words)
        resp = JSONResponse(status_code=200, content=body)
    else:
        # default "json"
        resp = JSONResponse(status_code=200, content={"text": text})

    _attach_openai_response_headers(resp, processing_ms, model)
    return resp


@router.post("/audio/translations", response_model=None)
async def create_translation(
    request: Request,
    user: User = Depends(rate_limit_v1_per_token),
) -> JSONResponse:
    """OpenAI /v1/audio/translations stub. NOT IMPLEMENTED — Parakeet is English-only.

    Returns 400 with a clear pointer to /v1/audio/transcriptions.
    """
    return _openai_error(
        request,
        "/v1/audio/translations is not supported. WisprAlt's underlying model "
        "(Parakeet TDT) is English-only — there is no translation path. "
        "Use /v1/audio/transcriptions instead.",
        "invalid_request_error",
        "endpoint_not_supported",
        400,
    )
