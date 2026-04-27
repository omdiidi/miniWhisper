"""
test_dictate_corrupt_audio.py — Regression coverage for the LibsndfileError leak.

Background
----------
Before this fix, ``ParakeetService._sync_transcribe`` called ``soundfile.read``
without try/except.  Malformed audio bodies caused ``soundfile.LibsndfileError``
to bubble up past ``routes/dictate.py``'s ``except CorruptAudioError`` handler,
producing an HTTP 500 even though the route's docstring promised a 422.

These tests pin the contract: any soundfile decode failure becomes
``CorruptAudioError`` at the audio-decode boundary, and the route maps it to 422.
"""

from __future__ import annotations

import pytest

from wispralt_server._errors import CorruptAudioError
from wispralt_server.dictate.parakeet import ParakeetService


class TestSyncTranscribeDecodeBoundary:
    """Direct unit tests on ``ParakeetService._sync_transcribe``.

    These verify the decode boundary in isolation — no FastAPI wiring needed.
    The model is never invoked because the failure happens at the soundfile
    layer, well before any tensor work.
    """

    def test_empty_bytes_raises_corrupt_audio_error(self) -> None:
        service = ParakeetService()
        with pytest.raises(CorruptAudioError) as excinfo:
            service._sync_transcribe(b"")
        # Should be the wrapped form, not raw soundfile.LibsndfileError
        assert "Cannot decode audio" in str(excinfo.value)

    def test_random_garbage_bytes_raises_corrupt_audio_error(self) -> None:
        service = ParakeetService()
        # Looks like a file but isn't a valid audio container
        garbage = b"\xff" * 1024 + b"NOT_AN_AUDIO_FILE" + b"\x00" * 256
        with pytest.raises(CorruptAudioError):
            service._sync_transcribe(garbage)

    def test_truncated_wav_header_raises_corrupt_audio_error(self) -> None:
        # RIFF magic but truncated before any usable data
        truncated = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE"
        service = ParakeetService()
        with pytest.raises(CorruptAudioError):
            service._sync_transcribe(truncated)

    def test_text_blob_raises_corrupt_audio_error(self) -> None:
        # Whatever a misconfigured client might POST instead of audio
        service = ParakeetService()
        with pytest.raises(CorruptAudioError):
            service._sync_transcribe(b"this is plaintext, not audio")
