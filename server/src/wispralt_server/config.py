"""
config.py — Pydantic Settings for WisprAlt server.

Reads from a .env file at startup (or from real environment variables).
Exposes a module-level ``settings`` singleton used throughout the package.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def verify_env_perms(env_path: Path) -> bool:
    """Return True iff *env_path* is mode 0600 and owned by the current user.

    Logs a loud WARNING (not an exception) on any violation so the server can
    still start in CI/container environments where the .env is intentionally
    world-readable via secret injection.
    """
    try:
        st = env_path.stat()
    except FileNotFoundError:
        # No .env on disk is fine — values may come from the real environment.
        return True
    except OSError as exc:
        logger.warning("Could not stat %s: %s", env_path, exc)
        return False

    mode = stat.S_IMODE(st.st_mode)
    owner_ok = st.st_uid == os.getuid()
    mode_ok = mode == 0o600

    if not owner_ok:
        logger.warning(
            "SECURITY WARNING: %s is owned by uid %d, but current uid is %d. "
            "This means other users on the system may read your secrets. "
            "Fix with: sudo chown $(whoami) %s",
            env_path,
            st.st_uid,
            os.getuid(),
            env_path,
        )
    if not mode_ok:
        logger.warning(
            "SECURITY WARNING: %s has mode %o, expected 0600. "
            "Other processes may be able to read your HF_TOKEN and API key. "
            "Fix with: chmod 600 %s",
            env_path,
            mode,
            env_path,
        )

    return owner_ok and mode_ok


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


# Module-level singleton — import as ``from wispralt_server.config import settings``
settings = Settings()
