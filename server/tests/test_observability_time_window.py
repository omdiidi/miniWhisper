"""
test_observability_time_window.py — Regression coverage for the dictate p50 outlier skew.

Background
----------
``LatencyHistogram`` previously kept the last 1000 entries with no time
awareness. A single hung-upload outlier (e.g. 197 seconds) would dominate
``percentiles()`` for the next 1000 requests, surfacing on /metrics as an
absurd p50 even though all recent traffic was healthy.

These tests pin the new contract: percentiles default to a recent time
window so old outliers stop poisoning the metric once they age out.
"""

from __future__ import annotations

import time

from wispralt_server.observability import LatencyHistogram


class TestRecentWindowFiltering:
    """The default ``percentiles()`` should drop observations older than the window."""

    def test_recent_observations_present_old_dropped(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        h = LatencyHistogram()

        # Fake monotonic clock so we can deterministically age entries.
        fake_now = [1000.0]

        def fake_monotonic() -> float:
            return fake_now[0]

        monkeypatch.setattr("wispralt_server.observability._time.monotonic", fake_monotonic)

        # Old huge outlier — far outside the recent window
        h.record("transcribe/dictate", 197_000.0)  # 197 seconds (the historic poison value)
        # Advance clock past the recent window
        fake_now[0] = 1000.0 + h._RECENT_WINDOW_S + 60.0
        # Healthy recent observations
        for ms in (140.0, 150.0, 160.0, 170.0, 180.0):
            h.record("transcribe/dictate", ms)

        recent = h.percentiles("transcribe/dictate")  # default recent_only=True
        assert recent["p50"] is not None
        # p50 should reflect ~150ms, NOT be poisoned by the 197s outlier
        assert recent["p50"] < 1_000.0, f"p50 still poisoned by old outlier: {recent['p50']}"
        assert 100.0 <= recent["p50"] <= 200.0

        # Legacy view (full deque) still includes the old outlier
        full = h.percentiles("transcribe/dictate", recent_only=False)
        assert full["p50"] is not None
        # p50 of [140, 150, 160, 170, 180, 197000] = (170+180)/2 = 175 actually
        # Let me be more precise: median of 6 values = avg of 3rd & 4th = (160+170)/2 = 165
        # But the point is p99 should be near 197000 (the outlier sits in the tail)
        assert full["p99"] is not None
        assert full["p99"] > 100_000.0, "outlier should still be visible in full window p99"

    def test_returns_none_when_window_empty(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        h = LatencyHistogram()
        fake_now = [1000.0]
        monkeypatch.setattr(
            "wispralt_server.observability._time.monotonic", lambda: fake_now[0]
        )
        # Two observations
        h.record("transcribe/dictate", 100.0)
        h.record("transcribe/dictate", 200.0)
        # Advance past window
        fake_now[0] = 1000.0 + h._RECENT_WINDOW_S + 60.0
        result = h.percentiles("transcribe/dictate")
        assert result == {"p50": None, "p95": None, "p99": None}

    def test_real_time_recording_does_not_crash(self) -> None:
        # No monkeypatching — exercises the real time.monotonic() path.
        h = LatencyHistogram()
        for ms in (10.0, 20.0, 30.0, 40.0, 50.0):
            h.record("transcribe/dictate", ms)
            time.sleep(0.001)
        result = h.percentiles("transcribe/dictate")
        assert result["p50"] is not None
        assert 10.0 <= result["p50"] <= 50.0
