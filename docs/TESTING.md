# Testing

This file documents the regression suites run before merge / release. Unit tests live under `server/tests/` and `client/Tests/WisprAltCoreTests/` and are wired into CI by `.github/workflows/ci.yml` (server pytest on Ubuntu, client `swift build && swift test` on macOS). The **end-to-end transcription matrix** below was the gating regression suite for the MLX-Whisper swap and Phase 10 deletion of WhisperX; it now serves as the standing regression matrix for any future transcription-stack changes. See [CHANGELOG-2026-05-10.md](CHANGELOG-2026-05-10.md) for the historical swap.

## 4-Run Transcription Matrix

The matrix exercises the file-job lengths an employee can plausibly produce (meeting rows were dropped when the meeting pipeline was removed). It was originally the regression suite that gated Phase 10 of the [MLX swap plan](../tmp/done-plans/) (Phase 10 shipped on 2026-05-10; WhisperX is deleted). It remains the standing transcription-stack regression matrix.

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

`LOAD_COST = 60 s` is added to row 1 only — it exercises the cold start (lazy MLX Whisper load). Rows 2–4 inherit the warmed-up state, as long as they start before the idle-eviction window evicts the model.

### Matrix

Each row is one independent transcription kicked off via the menubar UI ("Transcribe file…") — or curl direct against `/transcribe/file` if reproducing headlessly.

| # | Size | Channels | Wall budget | Other pass criteria |
|---|---|---|---|---|
| 1 | 30 s | any (stereo is averaged to mono) | `30/R + 30 s + LOAD_COST` | json + srt + vtt + txt non-empty |
| 2 | 5 min | any | `300/R + 60 s` | outputs non-empty |
| 3 | 30 min | any | `1800/R + 120 s` | outputs non-empty |
| 4 | 105 min | any | `6300/R + 300 s` | outputs non-empty |

Per run, capture:
- Wall clock from `submit` to `status="done"` (via `/admin/active` poll).
- `phase_done` durations from the server log (`name=ffprobe`, `name=ffmpeg_decode`, `name=transcribe_load`, `name=transcribe`, `name=output_write`).
- `current_rss_mb` peak across the run (via `/admin/active` snapshots every 5 s).
- Output schema sanity: JSON has `segments[]`, every segment labelled `"Speaker 1"` with `words: []`, and `model` lists only the transcription model (see [TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md)).

### Cancel test (separate from matrix)

Run #4 (105-min file), and at ~20% through the `transcribe` phase click **Cancel** in the menubar.

Pass criteria:
1. `DELETE /transcribe/file/{id}` returns **202** (the job was running; cancel is requested). The client stops polling and clears its job state at once, so "Transcribe file…" is enabled again immediately.
2. `GET /admin/active` returns `cancel_requested: true` while the Whisper call is still running.
3. When the Whisper call returns, the server discards the result: the job ends `failed` with error `cancelled by user`, no outputs remain on disk and `transcript_text` stays NULL.
4. A new file submission during that residual window gets HTTP 429 with `code: "file_job_busy"`, which the client shows as "previous transcription still finishing".
5. After the residual window, new submissions succeed.

This is the only test that intentionally exercises the **honest limitation** documented in ARCHITECTURE.md: Whisper transcribes the whole file in one call, so a cancelled running job keeps the model and the single job slot until that call returns.

### SRT / VTT / TXT output-format regression

File mode (`request_mode=file`) emits segments **without a `words[]` array** — `mlx_whisper.transcribe` is called with `word_timestamps=False` for performance. The output formatters in `server/src/wispralt/files/output.py` must never index `seg["words"]` in the SRT/VTT/TXT branches. To pin this:

1. Run row #2 (5-min file).
2. Diff the SRT, VTT, and TXT outputs against the post-swap golden (recorded from a known-good mlx-whisper run on the same input; the original goldens were captured from the last WhisperX run before Phase 10).

Pass: byte-identical SRT, VTT, TXT. Any drift means a formatter started consuming word-level data.

### Failure handling

- **Wall-budget warning** (single row exceeded budget by < 25%): record the measured ratio, continue.
- **Wall-budget failure** (single row exceeded budget by ≥ 25%): record, continue; gate-blocking only on row 4.
- **Output-criteria failure** on any row: STOP, investigate before proceeding.
- After all 4 rows: build the summary table (measured vs budget, segments, peak RSS). This was the required artifact for the Phase 10 manual gate (delete WhisperX); for future stack changes it is the standing acceptance artifact.

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

Run with `pytest server/tests/` and `cd client && swift test`. CI runs both suites on every PR via `.github/workflows/ci.yml` (see [CONTRIBUTING.md](CONTRIBUTING.md#continuous-integration)).
