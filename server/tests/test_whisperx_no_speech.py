"""Regression tests for transcribe_channel handling of no-speech audio.

WhisperX's VAD pre-pass crashes the underlying transformers pipeline with
IndexError when no speech is detected (transformers does ``inputs[0]`` on an
empty list). transcribe_channel must catch that and return an empty result
so the meeting pipeline produces a valid (empty-segments) transcript instead
of failing the whole job.
"""
from unittest.mock import MagicMock, patch
import numpy as np
import pytest
from wispralt_server.meeting import whisperx_loader as wx


def _seed_loaded() -> None:
    """Pretend load() succeeded so transcribe_channel proceeds past the guard."""
    wx._model = MagicMock()
    wx._align_model = MagicMock()
    wx._align_metadata = {"language": "en"}


def teardown_function(_):
    wx.reset()


def test_indexerror_returns_empty_segments():
    _seed_loaded()
    wx._model.transcribe.side_effect = IndexError("list index out of range")
    out = wx.transcribe_channel(np.zeros(16_000, dtype=np.float32))
    assert out == {"segments": []}


def test_empty_segments_skips_align():
    _seed_loaded()
    wx._model.transcribe.return_value = {"segments": []}
    with patch.object(wx, "whisperx") as mock_wx:
        out = wx.transcribe_channel(np.zeros(16_000, dtype=np.float32))
    assert out == {"segments": []}
    mock_wx.align.assert_not_called()


def test_align_indexerror_returns_empty():
    """If transcribe returns segments but align crashes on degenerate input,
    fall back to empty rather than failing the meeting job."""
    _seed_loaded()
    wx._model.transcribe.return_value = {"segments": [{"start": 0.0, "end": 0.01, "text": ""}]}
    with patch.object(wx, "whisperx") as mock_wx:
        mock_wx.align.side_effect = IndexError("degenerate align")
        out = wx.transcribe_channel(np.zeros(16_000, dtype=np.float32))
    assert out == {"segments": []}


def test_real_segments_still_align():
    _seed_loaded()
    wx._model.transcribe.return_value = {"segments": [{"start": 0.0, "end": 1.0, "text": "hi"}]}
    with patch.object(wx, "whisperx") as mock_wx:
        mock_wx.align.return_value = {"segments": [{"start": 0.0, "end": 1.0, "text": "hi", "words": []}]}
        out = wx.transcribe_channel(np.zeros(16_000, dtype=np.float32))
    assert out["segments"][0]["text"] == "hi"
    mock_wx.align.assert_called_once()


def test_not_loaded_raises():
    wx.reset()
    with pytest.raises(RuntimeError, match="not loaded"):
        wx.transcribe_channel(np.zeros(16_000, dtype=np.float32))
