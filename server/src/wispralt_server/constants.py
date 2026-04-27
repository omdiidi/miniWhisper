"""Shared constants used across routes, validators, and DB checks."""
MAX_DISPLAY_NAME_LEN: int = 40
OPENAI_COMPAT_SIZE_CAP: int = 25 * 1024 * 1024  # 25 MB — matches OpenAI's documented /v1/audio/transcriptions limit
