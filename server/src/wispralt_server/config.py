"""
config.py — Pydantic Settings for WisprAlt server.

Reads from a .env file at startup (or from real environment variables).
Exposes a module-level ``settings`` singleton used throughout the package.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Re-export verify_env_perms from ops.env_writer so callers that import it
# from config continue to work without a duplicate definition.
from wispralt_server.ops.env_writer import verify_env_perms as verify_env_perms  # noqa: F401

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Secrets — never logged or serialized
    hf_token: SecretStr
    wispralt_api_key: SecretStr

    # Supabase Postgres URL for multi-tenant auth + usage events.
    # Optional: when None, the server runs in break-glass-only mode
    # (env-var WISPRALT_API_KEY grants admin; no per-user lookups).
    supabase_database_url: SecretStr | None = None

    # Server identity
    server_url: str

    # Filesystem paths — resolved to absolute on access
    meeting_output_dir: Path
    job_db_path: Path
    staging_dir: Path

    # Audio analysis
    silence_threshold: float = 0.002

    # Upload guard (default 2 GiB)
    max_upload_bytes: int = 2_147_483_648

    # Rate-limit values (M6) — configurable from .env
    dictate_rate_per_min: int = 60
    meeting_rate_per_hour: int = 4

    # Idle-eviction for meeting models. After this many seconds with no meeting
    # activity (no submission, no in-flight job), unload mlx-whisper + Pyannote
    # AND call mx.metal.clear_cache() to actually return MLX's unified-memory
    # pool to the OS (drops RSS from ~6-9 GB to ~3 GB). Next meeting pays the
    # cold-load cost again (~3s kernel recompile + weight load).
    # Set to 0 to disable eviction (models stay warm forever — old behavior).
    # Default 60s (1 min) — aggressive because meetings are infrequent on
    # this deployment; raise to 300+ if you do meetings in clusters.
    meeting_idle_eviction_seconds: int = 60

    # Hard cap on dictation audio length. Defends against decode-amplification
    # attacks (1KB body that decodes to many minutes) and single-thread executor
    # starvation. Default 300s (5 min) — covers realistic long-form dictations
    # without letting a runaway upload pin the executor.
    dictation_max_duration_s: int = 300

    # Streaming dictation (cut-on-silence chunked transcription). Phase 1, opt-in.
    # streaming_session_ttl_s: how long an idle streaming session lives before
    #   the sweeper aborts it (matches the 15-min Phase 0 worst-case dictation).
    # streaming_max_active: server-wide cap on concurrent streaming sessions —
    #   protects the single-thread Parakeet executor from queue depth blow-up.
    # streaming_max_queue_depth: per-session pending chunk cap (excess → 429).
    # streaming_finalize_timeout_s: max wait inside /finalize for pending +
    #   tail inference to complete before raising FinalizeTimeout.
    streaming_session_ttl_s: int = 900
    streaming_max_active: int = 2
    streaming_max_queue_depth: int = 12   # v0.4.2: raised 6→12 for headroom with 5 s chunks (route now counts only in-flight tasks)
    streaming_finalize_timeout_s: int = 15

    # Trust CF-Connecting-IP / X-Forwarded-For headers for rate-limit IP extraction.
    # Set to False if exposing FastAPI directly without Cloudflare Tunnel (e.g. LAN testing)
    # to avoid IP spoofing in rate limiting.
    trust_forwarded_headers: bool = True

    # OpenRouter Mercury 2 — server-side smart-formatting post-processor.
    # Optional: when openrouter_api_key is None, smart formatting is silently disabled
    # (clients toggling the X-Smart-Format header on get raw text back).
    openrouter_api_key: str | None = None
    openrouter_model: str = "inception/mercury-2"
    # 600 ms ceiling for the Mercury cleanup (lowered from 1500 ms on
    # 2026-05-02 after measuring 7-day production latency: dictate p95
    # ran 5.4s with the old budget; this caps the disaster ceiling and
    # keeps the path fail-soft). Cross-region OpenRouter on a warm TCP
    # session is 200-400 ms; 600 ms preserves headroom while ruling out
    # the "1.5s waiting on Mercury" failure mode in the long tail.
    # If a deployment sees too many fail-overs to raw text, raise via env
    # (OPENROUTER_TIMEOUT_MS=1000) and revisit.
    openrouter_timeout_ms: int = 600
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_app_title: str = "WisprAlt"

    # Minimum dictation length (words) before smart-formatting is invoked. Below
    # this, raw Parakeet output is returned unchanged — short utterances aren't
    # worth the LLM round-trip and Parakeet's inline punctuation is good enough.
    #
    # 2026-05-13 bump 100 -> 300: with the OpenRouter timeout pinned at 600 ms
    # (see openrouter_timeout_ms above), Mercury empirically can't complete a
    # 100-300 word polish within budget. Those requests time out and fail-soft
    # to raw Parakeet text anyway, costing the user ~600 ms of dead wait. At
    # >=300 words the cleanup is both noticeable AND inside budget. The user
    # still gets correct Parakeet output (with its inline punctuation) below
    # the threshold — no transcription content is lost, only the polish step.
    #
    # 2026-05-19 drop 300 -> 80 (user pref May 19): user explicitly asked for
    # Mercury to fire on shorter dictations. Risk: at 80-300 words, polish may
    # time out at 600 ms and fail-soft to raw text, costing ~600 ms of dead
    # wait. Mitigation: revisit openrouter_timeout_ms if telemetry shows
    # excessive fail-overs at the new threshold.
    smart_format_min_words: int = 80

    # Transcript persistence TTL. After this many days, transcript text on
    # `jobs` rows is zeroed (the row stays as audit metadata) and `dictations`
    # rows are deleted entirely. Measured from COALESCE(finished_at, created_at)
    # on jobs and from created_at on dictations — i.e. the moment the text
    # actually existed, not job submission. Daily sweep runs in the lifespan
    # background task. See docs/ARCHITECTURE.md "Transcript persistence".
    transcript_retention_days: int = 90

    # Plan A /me/history pagination size. Each request returns at most this
    # many rows per leg (dictations + meetings), merged and capped to this
    # value before render. Load-more uses per-leg cursors.
    history_page_size: int = 50

    # Weekly insights (Phase 2) — Sunday-night LLM analysis of last week's transcripts.
    # Default model is grok-4.3 — verified existing + cheapest reasoning option on OpenRouter
    # (Task 0 spike 2026-05-14, ~$0.65/week projected at our employee count).
    insights_model: str = "x-ai/grok-4.3"
    insights_timeout_s: float = 30.0
    insights_max_30d_cost_usd: float = 8.0
    insights_input_word_cap: int = 30000
    insights_per_person_min_dictations: int = 5
    # ISO weekday: Mon=1...Sun=7. Default 7 = Sunday.
    insights_schedule_weekday: int = 7
    insights_schedule_hour_local: int = 23
    insights_timezone: str = "America/Los_Angeles"
    # Startup-catchup fail-closed by default. Operator opts in via .env AFTER verifying
    # OpenRouter pricing/model in Task 0 spike. Prevents accidental Wednesday-deploy charge.
    insights_catchup_enabled: bool = False


# Module-level singleton — import as ``from wispralt_server.config import settings``
settings = Settings()
