# Testing

This file documents the regression suites run before merge / release. Unit tests live under `server/tests/` and `client/Tests/WisprAltCoreTests/` and are wired into CI by `.github/workflows/test-server.yml`. The **end-to-end transcription matrix** below was the gating regression suite for the MLX-Whisper swap and Phase 10 deletion of WhisperX; it now serves as the standing regression matrix for any future transcription-stack changes. See [CHANGELOG-2026-05-10.md](CHANGELOG-2026-05-10.md) for the historical swap.

## 12-Run Transcription Matrix

The matrix exercises every combination of request mode + input shape + length that an employee can plausibly produce. It was originally the regression suite that gated Phase 10 of the [MLX swap plan](../tmp/done-plans/) (Phase 10 shipped on 2026-05-10; WhisperX is deleted). It remains the standing transcription-stack regression matrix.

### Spike baseline

All wall-budget formulas are parameterized on the realtime transcribe ratio `R` measured in the Phase 0 benchmark spike on the prod-mini hardware (M4, 16 GB), with `huggingface_hub` pinned to `>=1.12.0,<1.13`:

| Metric | Value | Source |
|---|---|---|
| Input | 105-min `Sammamish Endodontics.m4a`, `mode=file` | T0.4.1 |
| `audio_duration_s` | 6341.4 | ffprobe |
| `transcribe_s` | 740.26 | `benchmark-mlx-whisper.py` |
| `R = audio_duration_s / transcribe_s` | **8.57** | derived |
| Wall clock total | ~12.3 min (738 s) | benchmark |

`R = 8.57` is the substitution value used in every "Wall budget" cell below. The matrix is rerun on each platform change (model bump, hardware swap, MLX version bump) and `R` is remeasured before the budgets bind.

`LOAD_COST = 60 s` is added to rows 1–3 only — those exercise cold-start (MLX + pyannote first-load). Runs 4–12 inherit the warmed-up state.

### Matrix

Each row is one independent transcription kicked off via the menubar UI ("Transcribe file…" for file rows; triple-tap-FN recording for meeting rows — or curl direct against `/transcribe/file` if reproducing headlessly).

| # | Size | Mode / channels | Wall budget | Other pass criteria |
|---|---|---|---|---|
| 1 | 30 s | file / any | `30/R + 30 s + LOAD_COST` | json + srt + vtt + txt non-empty |
| 2 | 30 s | meeting / mono | `(30 × 1.5)/R + 60 s + LOAD_COST` | ≥1 speaker label |
| 3 | 30 s | meeting / stereo | `(30 × 1.5)/R + 60 s + LOAD_COST` | ≥2 speakers if applicable |
| 4 | 5 min | file / any | `300/R + 60 s` | outputs non-empty |
| 5 | 5 min | meeting / mono | `(300 × 1.5)/R + 90 s` | ≥1 speaker |
| 6 | 5 min | meeting / stereo | `(300 × 1.5)/R + 90 s` | ≥2 speakers if applicable |
| 7 | 30 min | file / any | `1800/R + 120 s` | outputs non-empty |
| 8 | 30 min | meeting / mono | `(1800 × 1.5)/R + 180 s` | ≥1 speaker |
| 9 | 30 min | meeting / stereo | `(1800 × 1.5)/R + 180 s` | ≥2 speakers if applicable |
| 10 | 105 min | file / any | `6300/R + 300 s` | outputs non-empty |
| 11 | 105 min | meeting / mono | `(6300 × 1.5)/R + 600 s` | **≥2 speakers** — the "all Speaker 1" regression pin |
| 12 | 105 min | meeting / stereo | `(6300 × 1.5)/R + 600 s` | ≥2 speakers if applicable |

Per run, capture:
- Wall clock from `submit` to `status="done"` (via `/admin/active` poll).
- `phase_done` durations from the server log (`name=ffprobe`, `name=ffmpeg_decode`, `name=transcribe_load`, `name=transcribe`, `name=diarize`, `name=merge`, `name=output_write`).
- `current_rss_mb` peak across the run (via `/admin/active` snapshots every 5 s).
- Output schema sanity (file row: JSON has `segments[]` with `speaker` absent or "Unknown"; meeting row: JSON has `segments[]` with at least one non-"Unknown" speaker).

### Cancel test (separate from matrix)

Run #10 (105-min file), and at ~20% through the `transcribe` phase click **Cancel** in the menubar.

Pass criteria:
1. Within 2 s, the UI shows the "Previous transcription still finishing on server" banner.
2. `GET /admin/active` returns `cancel_requested: true`.
3. Server log shows the watchdog/cancel handling (`cancel_requested=1` and either `set_failed jid=... reason=cancelled by user` for the mid-ffmpeg case, or the advisory log line for the mid-transcribe case).
4. New file submissions during the executor's residual run window either receive HTTP 429 from the server **or** are client-side-blocked by the banner.
5. After the executor returns naturally (typically the remaining `transcribe_s` for that file at the measured `R`), the banner clears and new submissions succeed.

This is the only test that intentionally exercises the **honest limitation** documented in ARCHITECTURE.md → Honest Limitations: cancel mid-transcribe is advisory; the semaphore is held until the executor returns.

### SRT / VTT / TXT output-format regression

File mode (`request_mode=file`) emits segments **without a `words[]` array** — `mlx_whisper.transcribe` is called with `word_timestamps=False` for performance. The output formatters in `server/src/wispralt_server/meeting/output.py` must never index `seg["words"]` in the SRT/VTT/TXT branches. To pin this:

1. Run row #4 (5-min file mode).
2. Diff the SRT, VTT, and TXT outputs against the post-swap golden (recorded from a known-good mlx-whisper run on the same input; the original goldens were captured from the last WhisperX run before Phase 10).

Pass: byte-identical SRT, VTT, TXT. Any drift means a formatter started consuming word-level data.

### Failure handling

- **Wall-budget warning** (single row exceeded budget by < 25%): record the measured ratio, continue.
- **Wall-budget failure** (single row exceeded budget by ≥ 25%): record, continue; gate-blocking only on rows 10–12.
- **Output-criteria failure** on any row: STOP, investigate before proceeding.
- After all 12 rows: build the summary table (measured vs budget, segments, speakers, peak RSS). This was the required artifact for the Phase 10 manual gate (delete WhisperX); for future stack changes it is the standing acceptance artifact.

### Rollback path (historical)

This section describes the rollback strategy that was in place during Phases 1–9 of the MLX swap. Phase 10 has shipped (2026-05-10): WhisperX, ctranslate2, and faster-whisper are removed from `pyproject.toml` and `whisperx_loader.py` is deleted. Rolling back today requires a `git revert` of the Phase 10 commit in addition to the swap commit, plus a `pip install -e .` to restore the deps. See [CHANGELOG-2026-05-10.md](CHANGELOG-2026-05-10.md) "Roll-back plan" for the original procedure.

## Unit / integration tests

| File | What it pins |
|---|---|
| `server/tests/test_dictate_corrupt_audio.py` | LibsndfileError → CorruptAudioError boundary |
| `server/tests/test_dictate_route_422.py` | `/transcribe/dictate` HTTP 422/415/413/200 contract |
| `server/tests/test_observability_time_window.py` | recent-window p50 + low-traffic fallback on `/metrics` |
| `server/tests/test_token_cache.py` | LRU + 60 s TTL on `TokenCache` |
| `server/tests/test_usage_writer.py` | UsageEventQueue overflow + drainer batch + FK-violation retry |
| `server/tests/test_admin_routes_auth.py` | `/admin/*` 403 employee / 200 admin |
| `server/tests/test_db_health.py` | `db.health_check` + `db.recreate_pool` |
| `server/tests/test_auth_break_glass.py` | Postgres-unreachable → env-var bearer → admin path |
| `client/Tests/WisprAltCoreTests/InjectionPredicateTests.swift` | 11-row injection truth table |
| `client/Tests/WisprAltCoreTests/SecureFieldGateTests.swift` | 5-case secure-field refusal pin |

Run with `pytest server/tests/` and `cd client && swift test`. CI runs the Python suite on every PR via `.github/workflows/test-server.yml`.
