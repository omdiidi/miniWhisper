"""
cli/client.py — the WisprAlt menubar app on this Mac.
"""

from __future__ import annotations

import plistlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

KEYCHAIN_SERVICE = "co.wispralt"  # Local-mode key (KeychainHelper.swift)
KEYCHAIN_ACCOUNT = "default"
OPENAI_SERVICE = "co.wispralt.openai"
DEFAULTS_DOMAIN = "co.wispralt.WisprAlt"  # also the bundle id


def app_version(app: Path) -> str | None:
    try:
        with (app / "Contents" / "Info.plist").open("rb") as f:
            return str(plistlib.load(f).get("CFBundleShortVersionString") or "") or None
    except (OSError, plistlib.InvalidFileException):
        return None
