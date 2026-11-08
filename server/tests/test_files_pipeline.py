"""
test_files_pipeline.py — mono Whisper file pipeline + whisper-only eviction.

The mlx-whisper loader is stubbed (``load`` / ``transcribe_channel`` /
``reset`` / ``release_cache``), so these tests exercise the real pipeline body: WAV decode,
stereo downmix, segment labelling, the four atomic writers, and the
eviction path including its memory-delta self-check.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

os.environ.setdefault("SERVER_URL", "http://test.local")

import numpy as np
import pytest
import soundfile as sf

from wispralt import observability
from wispralt.config import settings
from wispralt.files import mlx_whisper_loader
from wispralt.files import pipeline as p

_SEGMENTS = [
    {"start": 0.0, "end": 1.5, "text": " hello there"},
    {"start": 1.5, "end": 3.25, "text": " general kenobi "},
]


class _LoaderSpy:
    def __init__(self) -> None:
        self.load_calls = 0
        self.reset_calls = 0
        self.release_calls = 0
        self.transcribe_calls: list[dict] = []

    def load(self) -> None:
        self.load_calls += 1

    def reset(self) -> None:
        self.reset_calls += 1

    def release_cache(self) -> None:
        self.release_calls += 1

    def transcribe_channel(self, audio, *, word_timestamps, progress_cb=None, cancel_cb=None):
        self.transcribe_calls.append({"audio": audio, "word_timestamps": word_timestamps})
        return {"segments": [dict(s) for s in _SEGMENTS]}


@pytest.fixture
def loader(monkeypatch: pytest.MonkeyPatch) -> _LoaderSpy:
    spy = _LoaderSpy()
    monkeypatch.setattr(p._mlx_mod, "load", spy.load)
    monkeypatch.setattr(p._mlx_mod, "reset", spy.reset)
    monkeypatch.setattr(p._mlx_mod, "release_cache", spy.release_cache)
    monkeypatch.setattr(p._mlx_mod, "transcribe_channel", spy.transcribe_channel)
    monkeypatch.setattr(p, "_models_ready", False)
    monkeypatch.setattr(p, "_loading_in_flight", False)
    return spy


def _write_wav(path: Path, channels: int) -> None:
    n = 16_000 * 3
    if channels == 1:
        data = np.full(n, 0.2, dtype=np.float32)
    else:
        data = np.zeros((n, 2), dtype=np.float32)
        data[:, 0] = 0.2  # left only; downmix must average to 0.1
    sf.write(str(path), data, 16_000, subtype="FLOAT")


@pytest.mark.parametrize("channels", [1, 2])
def test_transcribe_file_writes_all_outputs(
    tmp_path: Path, loader: _LoaderSpy, channels: int
) -> None:
    wav = tmp_path / "in.wav"
    _write_wav(wav, channels)
    out = tmp_path / "out"
    jid = "00000000-0000-0000-0000-000000000001"

    transcript = p.transcribe_file(wav, out, jid)

    # Loaded once, transcribed once on a 1-D mono stream without word timestamps.
    assert loader.load_calls == 1
    assert len(loader.transcribe_calls) == 1
    call = loader.transcribe_calls[0]
    assert call["word_timestamps"] is False
    audio = call["audio"]
    assert audio.ndim == 1
    expected_level = 0.2 if channels == 1 else 0.1
    assert float(np.mean(audio)) == pytest.approx(expected_level, abs=1e-4)

    assert transcript["mode"] == "single"
    assert transcript["model"] == {"transcription": mlx_whisper_loader.MODEL_ID}
    assert transcript["duration_s"] == pytest.approx(3.0)
    assert [s["speaker"] for s in transcript["segments"]] == ["Speaker 1", "Speaker 1"]
    assert transcript["speakers"] == {"mic": {"display_name": "Speaker 1", "channel": None}}

    for fmt in ("json", "srt", "vtt", "txt"):
        assert (out / f"{jid}.{fmt}").is_file(), fmt

    data = json.loads((out / f"{jid}.json").read_text())
    assert data["model"] == {"transcription": mlx_whisper_loader.MODEL_ID}
    assert [s["text"] for s in data["segments"]] == ["hello there", "general kenobi"]

    srt = (out / f"{jid}.srt").read_text()
    assert "00:00:00,000 --> 00:00:01,500\nhello there" in srt
    assert "00:00:01,500 --> 00:00:03,250\ngeneral kenobi" in srt

    vtt = (out / f"{jid}.vtt").read_text()
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.500 --> 00:00:03.250\ngeneral kenobi" in vtt

    assert (out / f"{jid}.txt").read_text() == "hello there\ngeneral kenobi"
    assert loader.release_calls == 1  # per-job cache release
    assert mlx_whisper_loader.MODEL_ID == "mlx-community/whisper-large-v3-turbo"


def test_failed_warmup_still_stamps_idle_and_releases_cache(
    tmp_path: Path, loader: _LoaderSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise RuntimeError("warmup failed")

    monkeypatch.setattr(p._mlx_mod, "load", boom)
    monkeypatch.setattr(p, "_last_job_finished_at", time.monotonic() - 999.0)
    wav = tmp_path / "in.wav"
    _write_wav(wav, 1)
    with pytest.raises(RuntimeError, match="warmup failed"):
        p.transcribe_file(wav, tmp_path / "out", "00000000-0000-0000-0000-000000000002")
    assert p.idle_seconds() < 5.0
    assert loader.release_calls == 1
    assert loader.reset_calls == 1  # the failed load dropped its references
    assert p.is_ready() is False


def test_evict_skips_while_a_job_is_inside_transcribe_file(
    tmp_path: Path, loader: _LoaderSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    real = loader.transcribe_channel

    def gated(audio, **kw):
        entered.set()
        release.wait(10)
        return real(audio, **kw)

    monkeypatch.setattr(p._mlx_mod, "transcribe_channel", gated)
    wav = tmp_path / "in.wav"
    _write_wav(wav, 1)
    job = threading.Thread(
        target=p.transcribe_file, args=(wav, tmp_path / "out", "00000000-0000-0000-0000-000000000003")
    )
    job.start()
    try:
        assert entered.wait(10)
        # Model warm, idle clock long past the threshold — but a job holds _load_lock.
        monkeypatch.setattr(p, "_last_job_finished_at", time.monotonic() - 999.0)
        assert p.evict_if_idle(1.0) is False
        assert loader.reset_calls == 0 and p.is_ready() is True
    finally:
        release.set()
        job.join(10)
    assert loader.release_calls == 1


def test_release_cache_clears_and_resets_peak(monkeypatch: pytest.MonkeyPatch) -> None:
    import mlx.core as mx

    calls: list[str] = []
    monkeypatch.setattr(mx, "clear_cache", lambda: calls.append("clear"), raising=False)
    monkeypatch.setattr(mx, "reset_peak_memory", lambda: calls.append("peak"), raising=False)
    mlx_whisper_loader.release_cache()
    assert calls == ["clear", "peak"]


def test_evict_if_idle_skips_when_not_idle(
    monkeypatch: pytest.MonkeyPatch, loader: _LoaderSpy
) -> None:
    monkeypatch.setattr(p, "_models_ready", True)
    monkeypatch.setattr(p, "_last_job_finished_at", time.monotonic())

    assert p.evict_if_idle(300.0) is False
    assert loader.reset_calls == 0
    assert loader.release_calls == 0
    assert p.is_ready() is True


def test_evict_if_idle_releases_whisper_and_runs_self_check(
    monkeypatch: pytest.MonkeyPatch, loader: _LoaderSpy
) -> None:
    monkeypatch.setattr(p, "_models_ready", True)
    monkeypatch.setattr(p, "_last_job_finished_at", time.monotonic() - 600.0)
    # Memory reader reports a loaded model that did NOT drop after release:
    # the self-check must fire (pre above the gate, delta below the floor).
    high = settings.file_eviction_pre_active_gate_mb + 500
    monkeypatch.setattr(observability, "mlx_active_mb", lambda: high)
    before = observability.eviction_failures_total.value

    assert p.evict_if_idle(60.0) is True

    assert loader.reset_calls == 1
    assert loader.release_calls == 1
    assert p.is_ready() is False
    assert observability.eviction_failures_total.value == before + 1


def test_evict_if_idle_self_check_quiet_on_real_release(
    monkeypatch: pytest.MonkeyPatch, loader: _LoaderSpy
) -> None:
    monkeypatch.setattr(p, "_models_ready", True)
    monkeypatch.setattr(p, "_last_job_finished_at", time.monotonic() - 600.0)
    readings = iter([settings.file_eviction_pre_active_gate_mb + 1500, 100])
    monkeypatch.setattr(observability, "mlx_active_mb", lambda: next(readings))
    before = observability.eviction_failures_total.value

    assert p.evict_if_idle(60.0) is True
    assert loader.reset_calls == 1
    assert observability.eviction_failures_total.value == before
