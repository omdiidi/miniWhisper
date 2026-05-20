"""Shared constants used across routes, validators, and DB checks."""
MAX_DISPLAY_NAME_LEN: int = 40
OPENAI_COMPAT_SIZE_CAP: int = 25 * 1024 * 1024  # 25 MB — matches OpenAI's documented /v1/audio/transcriptions limit

# ── OpenAI compat — /v1 surface ───────────────────────────────────────────────

OPENAI_COMPAT_VERSION = "2024-10-01"  # matches openai-python's default OpenAI-Version header
OPENAI_KNOWN_MODELS: tuple[str, ...] = (
    "whisper-1",
    "gpt-4o-transcribe",
    "gpt-4o-mini-transcribe",
    "gpt-4o-mini-transcribe-2025-12-15",
    "gpt-4o-mini-transcribe-2025-03-20",
)  # 5 models — gpt-4o-transcribe-diarize EXCLUDED (we 404 on it; matches /v1/models exclusion)
OPENAI_KNOWN_MODELS_CREATED = 1677532384  # Feb 27 2023 — OpenAI's whisper-1 created epoch
OPENAI_KNOWN_MODELS_OWNED_BY = "wispralt"
