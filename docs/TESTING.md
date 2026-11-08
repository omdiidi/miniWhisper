# Testing

This file documents the regression suites run before merge / release. Unit tests live under `server/tests/` and `client/Tests/WisprAltCoreTests/` and are wired into CI by `.github/workflows/ci.yml` (server pytest on Ubuntu; client `scripts/test-client.sh` on macOS, which is also the local command). The **end-to-end transcription matrix** below was the gating regression suite for the MLX-Whisper swap and Phase 10 deletion of WhisperX; it now serves as the standing regression matrix for any future transcription-stack changes. See [CHANGELOG-2026-05-10.md](CHANGELOG-2026-05-10.md) for the historical swap.

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

File transcription emits segments **without a `words[]` array** — `mlx_whisper.transcribe` is called with `word_timestamps=False` for performance. The output formatters in `server/src/wispralt/files/output.py` must never index `seg["words"]` in the SRT/VTT/TXT branches. To pin this:

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
| `server/tests/test_dictate_route_422.py` | `/transcribe/dictate` HTTP 422/415/413/200 contract; `X-Smart-Format` / finalize `smart_format` ignored (raw text, no `smart_formatted`) |
| `server/tests/test_observability_time_window.py` | recent-window p50 + low-traffic fallback on `/metrics` |
| `server/tests/test_admin_routes_auth.py` | `/admin/*` 401/403/200 with real tokens, login cookie, Add employee, revoke; portal numbers equal `UsageStore` |
| `server/tests/test_admin_keys.py` | `/admin/keys/*` create / revoke / rotate on real SQLite |
| `server/tests/test_integration_kind_guards.py` | integration keys: 403 on `/me/*`, allowed on `/v1` |
| `server/tests/conftest.py` | shared harness: `DATA_DIR` set before import, per-test `settings.data_dir = tmp_path`, migrated `db` fixture, `accounts` / `usage` stores, `make_user`, `wire(app)` |
| `server/tests/test_accounts.py` | `AccountStore` lookup / rotate / revoke / disable, first-admin bootstrap, no token in logs |
| `server/tests/test_usage_store.py` | `UsageStore` record, overview, user totals, p50, drilldown filters, CSV rows |
| `server/tests/test_supabase_export.py` | Part 7 read-only export against `FakeSource` and a fake `psql` (`subprocess.run` patched; never contacts Supabase): modes, suffixes, paging, retry, read-only SQL proof, BLOCKED, no-delete grep gate. Shared fakes live in `tests/_import_fakes.py`. |
| `server/tests/test_supabase_import.py` | Part 7 snapshot import on the per-test DB: owner link, remaps, accounting equation, idempotent re-run, tamper refusal, dry run (DB + WAL hash unchanged), owner-mapping refusals, crash + resume from the checkpoint |
| `server/tests/test_file_import.py` | Part 7 transcript-file import in a temp tree: grouping, parsing, honest timestamps, copies, duplicates, idempotent re-run, untouched sources, dry run, copy-failure rollback |
| `server/tests/test_importer_cli.py` | Part 7 `python -m wispralt.importer` exit codes and prompts (no network) |
| `server/tests/test_imports.py` | Part 7 history writer: `import_rows` idempotency + per-row `RowError`s, input validation, `link_existing`, `import_runs` resume, offline lock, `0003` across the `0002` gap |
| `server/tests/test_database.py` | `Database` migrations (fresh, idempotent, failed, edited, missing, newer), pragmas, rollback, concurrent writers, `check()`, `DATA_DIR` validation |
| `server/tests/test_db_health.py` | `/readyz/db` ok / `schema_outdated` / `db_error`; usage middleware writes one row per tracked POST; a failing write is counted, response kept |
| `server/tests/test_auth.py` | per-request key lookup: revoked / disabled / unknown → 401, `sqlite3.Error` → 503, out-of-process revoke, disable → mint → new key works |
| `server/tests/test_job_store.py` | `JobStore` on the shared DB: transitions + cancel rules, `requeue_interrupted` refund, FK `IntegrityError`, relative→absolute `wav_path`, `recover_orphans` policy + `reenqueue_pending` on absolute paths, dictation insert under a running job, `dictation_counts`, one insight row per scope, `stats_by_user` = `compute_user_stats` |
| `server/tests/test_insights_cron.py` | the cron with a stub `InsightsClient`: a week with only a `file` transcript yields a person insight |
| `server/tests/test_history_search.py` | Part 6 FTS5: query quoting, backfill on migrate, triggers keep `'integrity-check'` clean through every mutation, 10 adversarial queries never cross users, filters, snippets |
| `server/tests/test_portal_render.py` | Part 6 shell: every page renders for admin (sidebar, stylesheet), employees 403 on admin pages and see no admin links, no `<style>` blocks |
| `server/tests/test_first_admin_setup.py` | Part 6 `/admin/setup`: 0600 code never logged, wrong code 403, one admin once, concurrent setups → one admin |
| `server/tests/test_invites.py`, `test_portal_keys.py` | Part 6 invites (shown once, hash only, atomic single-use redeem, uniform 400, 429) and people/devices (rotate vs revoke device vs disable, CSRF, roles) |
| `server/tests/test_monitoring.py` | Part 6 `/admin/system` snapshot: every key, disk walk, active vs recent clients, broken DB → `db.ok=false` |
| `server/tests/test_portal_access.py` | Part 6 search UI + record scope: highlighted and escaped snippets, 404 for another user's record, employees 403 on team records |
| `server/tests/test_measured_stats.py` | Part 6 measured stats against a hand-built fixture; tiles link to exactly their records; works with AI off |
| `server/tests/test_insights_sources.py`, `test_insights_runs.py` | Part 6 AI v2 (stub client, no network): refs validated against the prompt, render-time hiding, stale/legacy/recurring; runs, scheduler, cap, 429, backfill, estimate, settings + disclosure |
| `server/tests/test_bootstrap.py` | `python -m wispralt.bootstrap` subprocesses under a pty: TTY refusal, client config + one token, already-exists, `--check`, `--rotate-admin`, bad `DATA_DIR` |
| `server/tests/fixtures/` | `tiny.{wav,mp3,m4a,webm}`: a 1 s 440 Hz tone per container for the decode tests (regenerate with the `ffmpeg` commands in its `README.md`) |
| `server/tests/cli/` | the `wispralt` CLI in-process against a tmp `HOME` + `WISPRALT_ROOT` and a test label, with a `FakeRunner` (no real `launchctl`/`security`/`uv`/network; a direct `subprocess` call fails the test): paths and state, `.env` merge, import without MLX; launchd plist, `launchctl print` fixtures, EIO retry, stop-proof via `server_lock`; users/keys/retention and the key handoff (TTY or new 0600 `--token-file`, never a log); status sizes/JSON and doctor checks (exit 3, fix hints, read-only integrity, warm-up via an injected loader); backup/restore order (bootout → operation → bootstrap) and a real round-trip; setup host fresh run vs idempotent rerun with a fake Hugging Face hub; setup client/both (DMG verification order, local build, Keychain over stdin); update across its `execv` and uninstall limited to recorded artifacts |
| `client/Tests/WisprAltCoreTests/InjectionPredicateTests.swift` | 11-row injection truth table |
| `client/Tests/WisprAltCoreTests/SecureFieldGateTests.swift` | 5-case secure-field refusal pin |
| `client/Tests/WisprAltCoreTests/FileJobContractTests.swift` | `FileJobContract.cancelledByUserReason` matches the server's cancel reason; the Online JSON shape; `commitOutputs` (both files moved and staging empty; a failing second move removes the new folder and rethrows; a failing `makeDirectory` creates nothing). Same file: `OnlineUploadPlanTests` (cap 600: 25,000,000 B / 300 s direct, 900 s / 601 s / unknown duration / 25,000,001 B / mov·caf·aiff·aac → decode, `M4A` direct), `AudioExtractPolicyTests` (every strict / non-strict row), `ChunkPlannerTests` (seeded random envelopes: every range ≤ max, all but the last ≥ max − 20 s, deterministic; quietest window wins; no final range < 1 s) |
| `client/Tests/WisprAltCoreTests/TranscriptsFolderDefaultTests.swift` | transcripts-folder default: stored path wins, existing `Meetings` kept, else `Transcripts` |
| `client/Tests/WisprAltCoreTests/AppModeTests.swift` | route per mode/key (`DictationSnapshot`, `RouteKind`), `LaunchActions` rows (launch / switch / already polling / Online job running / `/me` once), `PollFence` table, a recording keeps its route across a switch, `ActivityState` precedence, `locksModePicker`, a live model → `.openAIRealtime` with no stream endpoint (caps 600 / 420 s), and the quick-switch race (exactly one effective poller); the default mode is Online; `.onlineJobEnded` rows (resume after the Online job ends in Local without `/me`, nothing in Online, no resume while a poller runs) |
| `client/Tests/WisprAltCoreTests/CaptureSlotTests.swift` | recorder handoff: `begin` while active fails, `end` hands the session off without aborting it, a throwing setup leaves the slot free, 2 × 1,000 concurrent begin/end never overlap |
| `client/Tests/WisprAltCoreTests/DeadlineTests.swift` | `withDeadline`: value first, timeout on work that ignores cancellation (within budget + 0.5 s), outer cancel, timeout-then-cancel (cancels only once the timeout has fired, so a busy test pool cannot race it); `onTimeout` exactly once |
| `client/Tests/WisprAltCoreTests/MonoResamplerTests.swift`, `Mono16kWAVTests.swift` | 48 kHz stereo → 16/24 kHz mono (level ±1 dB, ≥ 99 % energy at the tone, peak ≤ 1), silence, out-of-phase → 0, chunked == one-shot; 16 kHz / mono / 16-bit RIFF at ≈ 32,000 B/s, ranges, RMS profile |
| `client/Tests/WisprAltCoreTests/OpenAIModelsTests.swift` | no diarize, retire dates, one file default, `derivedSafeSeconds(2000) == 420`, every file cap 600 s and under 25 MB; `displayPrice` ("$0.0045/min", "≈$0.003/min"), `retireTag` ("retires Feb 26, 2027"); one default per kind (`gpt-transcribe`, `gpt-live-transcribe`), live caps (`gpt-realtime-whisper` 420 s with 2,000 tokens and no prompt, `gpt-live-transcribe` 600 s), a live pick resolves to `gpt-transcribe` for files |
| `client/Tests/WisprAltCoreTests/OpenAIRequestsTests.swift`, `OpenAIErrorsTests.swift` | Bearer only (key in neither URL nor body), multipart fields, `prompt` only when supported, never `language`, `usage.output_tokens` decoded; 401/403/400/`model_not_found`/`insufficient_quota` permanent, 429/5xx/`URLError` transient |
| `client/Tests/WisprAltCoreTests/LocalRequestsTests.swift`, `LocalURLPolicyTests.swift` | requests sent through `StubURLProtocol`: exact chunk / finalize / whole-file paths on the captured endpoint, `Authorization` + client version + lowercase `X-Client-Dedup-Id` on all three, finalize carries `file`, `client_dedup_id` and `speech_started_at` (equal to the input as a Double) and **no** `smart_format`; no request carries `X-Smart-Format`; `saved` decoding (absent → true); 410 `dictation_deleted` / 409 `dedup_conflict` terminal, a `detail`-shaped 409/410 not. URL policy: https ok, http only to 127.0.0.0/8 / `::1` / `localhost`, LAN IP / `ts.net` / `ftp` rejected |
| `client/Tests/WisprAltCoreTests/RealtimeProtocolTests.swift` | byte-stable `session.update`; the URL is `wss://api.openai.com/v1/realtime?intent=transcription` (no `model`, no `OpenAI-Beta`, key only in Bearer); `append` is clamped PCM16 LE base64; every server event decodes (`session.*`, `committed`, `delta`, `completed` ± `output_tokens`, `failed`, `error`, other, undecodable); error codes are sanitized |
| `client/Tests/WisprAltCoreTests/OpenAIRealtimeProviderTests.swift` | `Support/FakeTransport.swift` (scripted socket: gated or failing open, a `receive` that ignores cancellation, replies per message) + `FakeRecovery`: `session.update` is sent first and pre-open frames follow in order; frames reach the socket while FN is held; happy path (commit → `completed` by `item_id`, `output_tokens`); an `error` answering `session.update` throws (non-recoverable) or recovers (`server_error`); an error after commit throws; buffer overflow → `.notConnected` → recovery; socket never opens → recovery (`modelUsed == "gpt-transcribe"`), or `.notConnected` with recovery off; a hung `receive` → deadline → recovery, socket closed; < 100 ms → `.emptyRecording` with no commit and no recovery; `cancel` closes; the `RealtimeRecovery` table |
| `client/Tests/WisprAltCoreTests/OpenAIFileProviderTests.swift` | `StubURLProtocol` (`Support/`; every suite that uses it nests in the serialized `StubURLSuites`): 200 → text, 503 then 200 → one retry, 401 → no retry, no key → no request, a never-answering server → `.deadlineExceeded` and the task is cancelled (`stopLoading`); `transcribe(audio:…)` (the Online file path) sends its filename, content type and prompt with the same one-retry rule |
| `client/Tests/WisprAltCoreTests/ErrorLogTests.swift`, `TruncationCheckTests.swift`, `LocalFinalizeRouteTests.swift` | `ErrorLog.describe` never leaks a body or `localizedDescription`; outcomes; truncation table; Local finalize vs whole-file |
| `client/Tests/WisprAltCoreTests/SpoolDispositionTests.swift` | keep/delete table per outcome × `InjectOutcome`: delivered text → delete (`.pastedUnverified` too; `.noTarget` adds "copied"), truncated → keep (+ both toasts for `.noTarget`), Local `saved:false` → keep for every delivery, text without delivery → keep, empty → delete silently, failures → keep, cancelled → `.failed(.other)` → keep, terminal 410/409 (`.rejected`) → delete |
| `client/Tests/WisprAltCoreTests/RetryEntriesTests.swift` | 0 s entries, the 7-day boundary, a 3rd entry drops the oldest (`droppedForSpace`), orphan sidecar deleted; actions: current-mode sidecar → Retry/Export/Discard, missing / corrupt → Export/Discard, other mode → Export/Discard + "Switch to Local/Online to retry"; sidecar round-trip |
| `client/Tests/WisprAltCoreTests/LastTextTests.swift` | round-trip, file 0600 / folder 0700 (also after overwrite), 7-day expiry deletes the file; a spool CAF copied while still open for writing reads with duration > 0 (the launch-recovery rule) |
| `client/Tests/WisprAltCoreTests/MercuryPolicyTests.swift`, `MercuryClientTests.swift`, `Fixtures/mercury_vectors.json` | all 34 cases of `server/tests/test_mercury_safety.py` as JSON vectors (word count, 0.70–1.10 window, `extractText` shapes and depth limit) plus the verbatim prompt; client via `StubURLProtocol`: success (endpoint, Bearer, `X-Title`, model, prompt + text), timeout → `.failed("timeout")` with raw text, ratio → `.failed("ratio")`, < 20 words → `.skipped("short")` with no request, no key → no request, HTTP error, `reasoning` fallback |
| `client/Tests/WisprAltCoreTests/KeychainItemTests.swift` | round-trip / update / delete on a throwaway `co.wispralt.test.<uuid>` item; runs only with `WISPRALT_KEYCHAIN_TESTS=1` (set by `scripts/test-client.sh` outside CI) |

Client tests use **Swift Testing** (`import Testing`, `@Test`, `#expect`); the test target builds in Swift 5 language mode. Run them with `scripts/test-client.sh` (`swift build` + `swift test`; on a CommandLineTools-only Mac it adds the `-F`/`-rpath` flags for the CLT `Testing.framework` and turns off cross-import overlays, because the CLT copy of `_Testing_Foundation` has no module; it exports `WISPRALT_KEYCHAIN_TESTS=1` unless `CI` is set). Server tests: `pytest server/tests/` (locally `tmp/mission/part2/run-pytest.sh`).

### Log-privacy gate

`scripts/test-client.sh` ends with `scripts/check-log-privacy.py --self-test && scripts/check-log-privacy.py`. The scan reads every `Log.info/debug/warning/error(` call under `client/WisprAlt` and `client/WisprAltCore` and fails (printing `file:line`) on any interpolation that could carry transcript text, an error's text, a response body, a server reason, a path, a URL, a host or a file name. Allowed: `….count`, `….statusCode`, `….code`, `….rawValue`, and `ErrorLog.describe(…)`. The self-test runs the checker over known leaks (a three-line `Log.warning(` leak, `\(err)`, `\(folderURL.path)`, `\(decodeError)`, `\(stem)`, `\(name)`) and known-safe lines, and exits 1 on any wrong result. Release builds also log every message `.private` (`Util/Logger.swift`). CI runs both suites on every PR via `.github/workflows/ci.yml` (see [CONTRIBUTING.md](CONTRIBUTING.md#continuous-integration)).

Online file jobs (`App/OnlineFileJob.swift`) are app code; their decisions are tested in Core above. On 2026-11-07 the real `OnlineFileJob` + `AudioExtractor` sources were compiled against Core in a throwaway harness and run on two short clips (a 3.7 s m4a through the direct path, a 4.8 s aiff through the decode path): both transcripts exact, TXT + JSON committed, no temp dir left, ≈ $0.0006 (logged in the spend ledger).

## Real-API assumption gates (OpenAI)

`scripts/online-assumptions/run-all.sh` checks Online-mode assumptions against the **real** OpenAI API, so it **costs money** and is never part of CI or `test-client.sh`. It builds the `WisprAltProbe` developer tool (`client/Probe/main.swift`, never bundled into the app) and runs:

| Step | Probe | What it checks | Est. $ |
|---|---|---|---|
| `preflight` | P5 | the Keychain read of `co.wispralt.openai` / `default` (no spend) | 0 |
| `preflight` | P1 (report-only) | `say -r 212` renders `fixtures/speech.txt` (re-checked to 590-600 s, else **probe FAIL (fixture)** with no API call), then **one** `gpt-transcribe` call on the 16 kHz mono WAV. Reports spoken words, returned words, `usage.output_tokens` and the tail marker (`OK` or `TRUNCATED(measured=N)`). Both marker outcomes PASS; the model catalog is never changed from it (A1) | ≈ 0.045 |
| `suite-file` | per file model | every file model in `OpenAIModels.all` on one ≈ 5 s clip through the shipped `OpenAIFileProvider` (≤ 2 requests per model, so ≤ 3 calls), `gpt-4o-mini-transcribe-2025-03-20` with exactly one call (`model_not_found` is reported, not failed: A7), plus one direct m4a upload to `gpt-transcribe` | ≈ 0.003 |
| `preflight` | P3 | `gpt-live-transcribe`, the P1 fixture (590-600 s) streamed at 5× real time through the shipped `OpenAIRealtimeProvider` with recovery off, then **one** commit: PASS on `completed` (reports words, `output_tokens`, tail marker, commit→`completed` ms). On a commit refusal (`realtime(type:)`) P3 is replaced by **P3b** (the first 210 s, ≈ $0.06); a P3b pass means `realtimeCommitLimit = 210` (A2) | ≈ 0.170 |
| `preflight` | P4 (report-only) | a 20-40 s clip (the fixture's first 105 words) streamed in real time through both live models (FN-up → text = `finish`, i.e. drain + commit → `completed`), and one `gpt-transcribe` POST (POST → response); latency is reported, never gated (A10) | ≈ 0.019 |
| `suite-live` | per live model + recovery | each live model on the ≈ 5 s clip (one session each, recovery off, keyword check), plus one recovery case: a socket that never opens → the same clip through the shipped `gpt-transcribe` provider (`modelUsed == "gpt-transcribe"`) | ≈ 0.003 |

- **Key.** Read inside the probe with `KeychainItem`; the shell script never touches it, and it never appears in argv, env, a log or a file. Missing key, `errSecAuthFailed`, `errSecInteractionNotAllowed` or a denied prompt → **BLOCKED**. `--keychain-service NAME` points the probe at another item (e.g. an absent one, to check the no-key path).
- **Spend.** The probe projects each call's cost (`seconds / 60 × catalog price`) and refuses it (BLOCKED) if it would pass the pre-flight cap ($0.30 per run), the suites cap ($1 per run, combined) or the ledger cap ($5 ever). Every billable call is appended **before** it is sent to `tmp/mission/part3/openai-spend.jsonl` (override with `WISPRALT_SPEND_LEDGER`) as `{ts, run, probe, model, seconds, est_usd}`; a provider retry adds a second line.
- **Audio.** Generated AIFF / WAV / m4a files live in a `wispralt-probe-<uuid>` temp dir that is removed on exit; only the text fixture is in git.
- **Keychain prompt.** The probe reads `co.wispralt.openai` itself. `run-all.sh` signs it with your first code-signing identity (or `WISPRALT_SIGN_IDENTITY`), so one **Always Allow** survives rebuilds; with no identity macOS re-prompts after every build.
- **Exit codes.** 0 all PASS; 1 any FAIL (`run-all.sh` halts at the failing step); 2 BLOCKED with no FAIL (continue, results UNVERIFIED).
- **Fixture.** If a new `say` voice drifts outside 590-600 s, add or remove a sentence in `fixtures/speech.txt` and update its header.

Last real run (2026-11-07, run `20261108T001338Z-47742`, $0.0471, exit 0):

- P5 PASS. P1 PASS: clip 594.8 s (19.0 MB WAV), spoken 2,360 words, returned 2,359, `output_tokens` n/a (`usage` was `{type: duration, seconds: 595}`), marker **OK** (heard as "zebra chords"), 18.3 s latency. So `gpt-transcribe` returned a full 10-minute transcript, but it reports duration-only usage, so its output-token ceiling stays unverified (A1).
- `suite-file`: all 6 file models PASS with one call each (`gpt-transcribe` and `whisper-1` report duration usage; the `gpt-4o*` models report `output_tokens`); `gpt-4o-mini-transcribe-2025-03-20` is usable (A7 verified on this date); the direct m4a upload PASS.
- P3 / P3b / P4 / `suite-live` (live models): **not yet run** (UNVERIFIED). On 2026-11-07 the rebuilt probe's Keychain prompt was not answered, so no call was made. Until P3 runs, `realtimeCommitLimit` stays unset (live caps 600 s, `gpt-realtime-whisper` 420 s) and no live latency is recorded here.

