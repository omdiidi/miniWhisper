---
name: "OpenAI Whisper API compatibility — fully dialed, end-to-end drop-in"
description: "Expand /v1/audio/transcriptions to a production-grade OpenAI-compatible surface; add /v1/models; split admin UX into 'Add Employee' vs 'Add API Key'; comprehensive docs + tests + Mac mini e2e verification."
version: "v3"
status: "ready"
brief: "./tmp/briefs/2026-05-20-openai-compat-fully-dialed.md"
review_rounds:
  - "round 1 (2 parallel) — 6 blockers + ~20 should-fix incorporated → v2"
  - "round 2 (2 parallel) — 4 blockers + ~15 should-fix incorporated → v3 (CLEAN)"
---

## Goal

Make `https://transcribe.integrateapi.ai/v1/*` a drop-in replacement for OpenAI's audio-transcription API so any third-party program (Buzz, MacWhisper, Bazarr, Open WebUI, openai-python, openai-node, curl, OBS Whisper plugin, etc.) can swap `OPENAI_BASE_URL` + `OPENAI_API_KEY` and Just Work. Add an explicit "Add API Key" flow in the admin UI distinct from "Add Employee." Ship with full test coverage, comprehensive docs (one canonical `docs/OPENAI-COMPAT.md`), and live Mac mini end-to-end verification.

## Summary

Server: expand `routes/v1_transcriptions.py` to support `verbose_json`/`srt`/`vtt` response formats; accept mp3/m4a/mp4/webm via a new sync ffmpeg decode helper (offloaded to a thread); add `timestamp_granularities[]` form param + `user` form field; emit OpenAI-standard response headers (`x-request-id`, `openai-version`, `openai-processing-ms`, `openai-model`); switch rate-limit unit from per-IP to per-token for `/v1`; restrict `/v1` auth to Bearer-only (no admin session cookie); validate error codes (4xx for client mistakes — must not trigger SDK retry storms on 408/409/429/≥500); enable CORS as the OUTERMOST middleware scoped to `/v1/*`; reject `stream=true` with a clean 400. New `routes/v1_models.py` returns the static model list. Database migration adds `users.kind` column AND updates the `User` cached dataclass to include `kind` (needed for `/me/*` + `/telemetry/*` integration-key guards). Admin UX: new `/admin/keys` routes + templates with OpenAI env-var snippet. Documentation: new `docs/OPENAI-COMPAT.md` plus updates to API.md / INTEGRATION-GUIDE.md / ADMIN.md / OVERVIEW.md. Three new test files. New `scripts/verify-openai-compat.sh` that doubles as the future smoke test. Final phase: Phase 0 live curl baseline; full implementation; deploy to mini via CRD + `/macmini paste` (using `bootout`+`bootstrap`); run verification against prod URL.

## Intent / Why

- **Drop-in promise**: a user with one WisprAlt token should be able to paste it into any OpenAI-Whisper-shaped client and have transcription Just Work. Today the surface is technically alive but breaks on the majority of real clients (no mp3 support, no verbose_json, no `/v1/models`, no CORS).
- **Admin UX clarity**: tokens issued for third-party programs are conceptually different from tokens issued to human employees. The admin UI should reflect that distinction with a dedicated "Add API Key" flow. Under the hood, they're still rows in `wispralt.users`; the distinction is a `kind` column for UI grouping AND for route-level guards on `/me/*` + `/telemetry/*` (integration keys should NOT have access to dictation history or user-event telemetry).
- **Operator confidence**: an operator dropping into the repo six months from now must be able to answer "is the OpenAI-compat surface healthy?" by running one verification script.
- **Invariants that must remain true**:
  1. `/v1/*` paths never apply WisprAlt-specific behaviors that an OpenAI client wouldn't expect.
  2. Per-token rate limits on `/v1` count independently from per-IP limits on `/transcribe/dictate`.
  3. The cookie auth fallback NEVER satisfies `/v1` requests — Bearer only.
  4. `verbose_json` always emits the canonical fields including `segments[].transient: false` and `language: "english"` (lowercase full word).
  5. 4xx is for client errors (don't retry); 5xx is for server errors (retryable). Mis-categorization causes openai-python retry storms (it retries 408 / 409 / 429 / ≥500).
  6. `kind='integration'` users cannot reach `/me/*` or `/telemetry/*` — only `/v1/*`.
  7. CORS headers (`Access-Control-Allow-Origin: *`) appear on EVERY response — including 429 rate-limit envelopes — so browser clients see a clean rate-limit error, not a CORS error.

## Source Artifacts

- Brief: `./tmp/briefs/2026-05-20-openai-compat-fully-dialed.md` (44 numbered decisions)
- Research dossier: embedded in this plan under "Verified Repo Truths" and "All Needed Context"
- Plan review round 1 (2 parallel reviewers): blockers + should-fixes incorporated in this v2 revision

## What

A user with a WisprAlt API key can:

1. `export OPENAI_BASE_URL=https://transcribe.integrateapi.ai/v1` + `export OPENAI_API_KEY=<token>` and use the `openai-python` SDK against any of: WAV, FLAC, OGG, MP3, M4A, MP4, WEBM (≤25 MB), with `response_format` ∈ {`json`, `text`, `verbose_json`, `srt`, `vtt`}. Optional `timestamp_granularities=[word]` returns `words[]` when aligned tokens are available.
2. `GET /v1/models` returns the static OpenAI model list, satisfying clients (Open WebUI) that probe before transcribing.
3. CORS preflight succeeds so browser-based clients work.
4. Errors arrive as `{"error": {"message", "type", "param", "code"}}` with correct status codes that do NOT trigger openai-python retry-on-(408/409/429/5xx) for client mistakes.
5. Open `/admin/keys/new` in the admin UI, name a key after a program, mint it, copy the plaintext + OpenAI env-var snippet. Revoke + rotate work.
6. `/v1` traffic is rate-limited per token (60 req/min) independently of `/transcribe/dictate` per-IP rate.
7. An integration-kind token CANNOT pull `/me/history` or POST `/telemetry/*` — only `/v1/*`.

### Success Criteria

- [ ] Phase 0 baseline curl against existing prod `/v1/audio/transcriptions` with a real WAV returns 200 + `{"text": "..."}` (proves we're not building on sand).
- [ ] `openai-python` SDK `client.audio.transcriptions.create(file=..., model="whisper-1", response_format="verbose_json")` against prod URL returns OpenAI-shaped JSON with `task`, `language`, `duration`, `text`, `segments[]`.
- [ ] Same call with `response_format="srt"` returns valid SRT with comma decimal separators.
- [ ] Same call with `response_format="vtt"` returns `WEBVTT`-prefixed VTT with period decimals.
- [ ] Same call with an mp3 / m4a / mp4 / webm file completes successfully.
- [ ] `GET /v1/models` returns 200 with 5 OpenAI model IDs (`whisper-1`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, `gpt-4o-mini-transcribe-2025-12-15`, `gpt-4o-mini-transcribe-2025-03-20`). NOTE: `gpt-4o-transcribe-diarize` is NOT listed — listing-but-rejecting confuses Open WebUI's "test connection" probe.
- [ ] `OPTIONS /v1/audio/transcriptions` returns 200 with `Access-Control-Allow-Origin: *`.
- [ ] Rate-limit 429 response carries `Access-Control-Allow-Origin: *` header (so browser clients see the 429, not a CORS failure).
- [ ] Bad Bearer token returns 401 with OpenAI envelope + `x-request-id` header.
- [ ] Corrupt audio returns 400 / `code: "invalid_audio_data"`. ffmpeg timeout returns 400 / `code: "decode_timeout"`. Audio too long returns 400 / `code: "audio_too_long"`. NONE return 5xx (would trigger SDK retry).
- [ ] Integration key (`kind='integration'`) gets 403 on `GET /me/history`, 403 on `POST /telemetry/dictation`.
- [ ] Admin UI: `/admin/keys/new` mints a key with `kind='integration'`; key works against `/v1`; revoke and rotate flows work; `/admin/users` lists hide it.
- [ ] `pytest server/tests/test_v1_transcriptions.py server/tests/test_v1_models.py server/tests/test_admin_keys.py` all pass.
- [ ] `scripts/verify-openai-compat.sh` against `https://transcribe.integrateapi.ai/v1` exits 0.
- [ ] Mini deploy completes; live URL passes the verification script.

## Verified Repo Truths

### Data / State

- Fact: `wispralt.users` table has `id`, `label`, `token_hash`, `role`, `created_at`, `revoked_at`, `display_name`. No `kind` column today.
  Evidence: `server/src/wispralt_server/users/store.py:21-44`; `server/migrations/2026-04-27-v2-display-name.sql` was the last column addition.
  Implication: Migration v4 adds `kind` column with default `'employee'`.
  Search Evidence: `grep -n "kind" server/src/wispralt_server/users/store.py` returns no rows referencing a `kind` column.

- Fact: `wispralt.schema_version` is append-only with columns `(version, notes)`; v2 and v3 migrations use `INSERT ... ON CONFLICT (version) DO NOTHING` to record their version.
  Evidence: `server/migrations/2026-04-27-v2-display-name.sql:15-17` (`INSERT INTO wispralt.schema_version (version, notes) VALUES (2, '…') ON CONFLICT (version) DO NOTHING;`).
  Implication: Migration v4 MUST use the same INSERT-ON-CONFLICT pattern. The plan must NOT use `UPDATE ... WHERE version < 4` (would mutate v3's row and lose its history).

- Fact: `role` values in production are `'admin'` or `'employee'`.
  Evidence: `server/src/wispralt_server/users/store.py:29` (`# 'admin' | 'employee'`); `server/src/wispralt_server/routes/admin_ui.py:282` (`_VALID_ROLES = {"admin", "employee"}`).
  Implication: `kind='integration'` rows still use `role='employee'` — distinction is on the new axis, not the existing role.

- Fact: `decode_wav_bytes` returns `(audio_np, sample_rate_int)` where `audio_np` is a 1-D or 2-D float32 array; sample_rate is integer Hz (NOT duration).
  Evidence: `server/src/wispralt_server/audio.py:33-51`.
  Implication: New `decode_to_pcm` MUST explicitly compute duration as `len(samples_mono_16k) / 16000` AFTER resample + downmix. Don't blindly forward `(audio, sr)` as `(audio, duration_s)` — they are not interchangeable.

- Fact: `ParakeetService.transcribe(...)` returns `(text, inference_ms)` as a 2-tuple; `_sync_transcribe(...)` is the underlying blocking impl.
  Evidence: `server/src/wispralt_server/dictate/parakeet.py:113-182, 186-193`.
  Implication: Changing this signature would break THREE callers: `routes/dictate.py`, `routes/dictate_stream.py`, `routes/v1_transcriptions.py:99`. Plan introduces a NEW `transcribe_with_alignment(...)` method instead — leaves the existing 2-tuple API untouched.

- Fact: `_sync_transcribe` already raises `CorruptAudioError` with a message starting `"Dictation too long: …"` for the audio-too-long case.
  Evidence: `server/src/wispralt_server/dictate/parakeet.py:136-143`.
  Implication: Add a new `AudioTooLongError(CorruptAudioError)` subclass to `_errors.py`, switch the raise site to use it, and check `isinstance(exc, AudioTooLongError)` BEFORE `isinstance(exc, CorruptAudioError)` at the /v1 boundary. All existing `except CorruptAudioError` callers still fire (subclass relationship).

### Entry Points / Integrations

- Fact: `/v1/audio/transcriptions` is wired at `server/src/wispralt_server/main.py:860` via `app.include_router(v1_transcriptions.router)`.
  Evidence: `server/src/wispralt_server/main.py:80, 860`.
  Implication: New `routes/v1_models.py` follows the same pattern.

- Fact: OpenAI error envelope middleware is installed via `openai_errors.install(app)` AFTER all routers, at `main.py:874`.
  Evidence: `server/src/wispralt_server/middleware/openai_errors.py:33, 74`.
  Implication: New `/v1/*` routes automatically get OpenAI-shaped errors. Plan extends `openai_errors.py` to map `audio_too_long`, `decode_timeout`, `unsupported_file_type`, `invalid_audio_data`, `streaming_unsupported`, `model_not_supported_on_endpoint`, `endpoint_not_supported` codes.

- Fact: Templates live at `server/src/wispralt_server/admin/templates/*.j2`.
  Evidence: `find server -name "*.j2"` returned 20 templates including `add_employee.html.j2`, `employee_added.html.j2`, `users.html.j2`, `base.html.j2`.
  Implication: New templates `keys.html.j2`, `add_key.html.j2`, `key_added.html.j2` go in same directory; extend `base.html.j2`.

- Fact: Telemetry mapping `_KIND_MAP` at `main.py:695-699` already maps `"v1/audio"` route-key to `"v1_dictate"` kind in `usage_events`.
  Evidence: `server/src/wispralt_server/main.py:695-699`.
  Implication: Add entries `"v1/models"` → `"v1_models"` AND `"v1/audio/translations"` → `"v1_translations"`. If only `"v1/audio"` is mapped (prefix-match), `/v1/audio/translations` would incorrectly count toward `v1_dictate`.

- Fact: LaunchAgent label is `co.wispralt.server`; `.claude/commands/verify-autostart.md` uses `bootout` + `bootstrap` for a true restart cycle, not `kickstart -k`.
  Evidence: `.claude/commands/verify-autostart.md:26-27`.
  Implication: Phase 7 (Mac mini deploy) uses `bootout gui/$(id -u)/co.wispralt.server` followed by `bootstrap gui/$(id -u) ~/Library/LaunchAgents/co.wispralt.server.plist` after `uv sync` to pick up new dependencies. Use `$(id -u)` not `$UID` (more portable across login shells).

### Execution / Async Flow

- Fact: `ParakeetService` uses a single-thread `ThreadPoolExecutor` (`max_workers=1`) — MLX inference is already serialized.
  Evidence: `server/src/wispralt_server/dictate/parakeet.py:39-40, 62-63`.
  Implication: Brief decision #33 (add semaphore) is REDUNDANT. Document the existing serialization in `docs/OPENAI-COMPAT.md`. No new lock.

- Fact: ffmpeg invocation pattern for offline file→canonical-WAV conversion exists.
  Evidence: `server/src/wispralt_server/ops/staging.py:461-595` (`transcode_to_canonical_wav`) uses `subprocess.Popen` with `-ar 16000 -ac 1 -acodec pcm_s16le` flags and a 30-minute timeout.
  Implication: New sync helper for `/v1` (call it `decode_to_pcm` in a new `dictate/sync_decode.py`) borrows the command shape but uses `-acodec pcm_f32le -f f32le pipe:1` to get float32 to stdout. Cap timeout at 60s (a 25 MB file should decode in <5s on M4). **Critical**: the function is synchronous; the FastAPI route MUST call it via `await asyncio.to_thread(decode_to_pcm, audio_bytes)` — otherwise `subprocess.run` blocks the event loop and starves all other in-flight requests for the decode duration.

- Fact: `audio.py:46` `decode_wav_bytes` uses libsndfile via `soundfile.SoundFile` — does NOT decode mp3/mp4/m4a/mpga/webm; returns `(audio_np, sample_rate_int)`.
  Evidence: `server/src/wispralt_server/audio.py:33-51`.
  Implication: New `dictate/sync_decode.py` tries libsndfile first; on failure, falls through to ffmpeg. After libsndfile success, MUST call `safe_resample` to 16 kHz AND downmix to mono explicitly — those steps live in `_sync_transcribe` today (`parakeet.py:131`), not in `decode_wav_bytes`.

- Fact: `parakeet-mlx` returns either a `Hypothesis` object with `.text` OR a list of `AlignedToken` objects with `.text`/`.start`/`.end`. Code only reads `.text` today, discarding alignment.
  Evidence: `server/src/wispralt_server/dictate/parakeet.py:97-111`.
  Implication: New `transcribe_with_alignment(...)` method returns `(text, duration_ms, aligned_tokens: list | None)`. The existing `transcribe(...)` stays a 2-tuple. The `_extract_text` helper is split into `_extract_text_and_tokens(result) -> (str, list | None)`.

### User-Facing / Operator-Facing Surface

- Fact: Admin UI mounts at `/admin/*` via three routers (`public_router`, `me_router`, `authed_router`).
  Evidence: `server/src/wispralt_server/main.py:844-846`.
  Implication: New `/admin/keys/*` routes attach to `authed_router` (admin scope) — only admin users can mint integration keys.

- Fact: Existing `/admin/users/new` POST mints via `users_store.mint(label=..., role=..., display_name=...)`.
  Evidence: `server/src/wispralt_server/routes/admin_ui.py:348-397`; `users/store.py:89-117`.
  Implication: New `/admin/keys/new` POST calls `mint(...)` then `UPDATE wispralt.users SET kind='integration' WHERE id=$1` — keeps `mint` signature stable.

- Fact: Per-IP rate limiter at `middleware/rate_limit.py:114-118` shares one bucket between `/transcribe/dictate` and `/v1/audio/transcriptions`.
  Evidence: `server/src/wispralt_server/middleware/rate_limit.py:114-118`.
  Implication: Drop `/v1/audio/transcriptions` from the dictate per-IP bucket. Add a post-auth `Depends(rate_limit_v1_per_token)` to the `/v1` route. The new dependency depends on `require_api_key_v1` transitively (FastAPI's dependency cache resolves it once per request).

### External / Operational Surface

- Fact: Cloudflare Tunnel proxies prod traffic to the mini at `transcribe.integrateapi.ai`; tunnel has 100s proxy ceiling.
  Evidence: `server/src/wispralt_server/routes/transcribe_file.py:75`; CLAUDE.md cloudflared note.
  Implication: ffmpeg decode for 25 MB on the mini must complete well under 100s — empirically a few seconds for typical mp3/m4a.

- Fact: GitHub Releases publishes both DMG + source; `releases/latest` is currently v0.4.6.
  Evidence: CLAUDE.local.md "Recent Activity" + `git tag` history.
  Implication: Default version bump for this feature is v0.5.0. User's seq 21 pattern was patch bumps; this is a new minor surface so v0.5.0 is justified. If the user explicitly objects post-deploy, we can re-tag as v0.4.7.

## Locked Decisions

All 44 numbered decisions in `./tmp/briefs/2026-05-20-openai-compat-fully-dialed.md` are locked. Critical clarifications from the reviewer round:

1. **Server**: Add ffmpeg sync decode (offloaded via `asyncio.to_thread`); add `verbose_json`/`srt`/`vtt` builders; add `timestamp_granularities[]` + `user` form params; add `GET /v1/models` (5 models, NOT 6 — exclude `gpt-4o-transcribe-diarize`); reject `stream=true` with 400 + `code: "streaming_unsupported"`; reject `model=gpt-4o-transcribe-diarize` with 400 + `code: "model_not_supported_on_endpoint"`; accept `include[]=logprobs` silently no-op; emit OpenAI response headers; Bearer-only on `/v1` (no cookie); per-token rate limit (60/min/user.id with break-glass id<0 bypass); 4xx for client errors; CORS as OUTERMOST middleware; CORS allow_credentials=False; allow_origin="*"; allow_headers includes `OpenAI-Organization`, `OpenAI-Project`, `X-Stainless-*`; handle empty/silent audio gracefully (200 + empty text/segments); audio_too_long as 400; decode_timeout as 400 (NOT 504/408 — both retry-triggers).
2. **DB**: Add `wispralt.users.kind` column (default `'employee'`, backfill same). Add `kind` to `User` cached dataclass + `lookup`/`lookup_by_id`/`mint` SELECT lists (needed for /me/* + /telemetry/* integration-key guards). Update v2/v3-pattern `schema_version` INSERT.
3. **Auth**: New `require_api_key_v1` Bearer-only. Refactor extracts `_resolve_token_user(request, plaintext)` from existing `require_api_key`, preserving the four-branch state machine: (a) token_cache hit, (b) Postgres lookup hit → cache.put + return, (c) Postgres lookup miss → 401 hard-stop (NO break-glass — revocation invariant), (d) Postgres errored → break-glass attempt OR 503. Both `require_api_key` (cookie OK) and `require_api_key_v1` (Bearer only) call `_resolve_token_user`. `request.state.user` set on every success path. Multiple Authorization headers → 400 preserved on both surfaces.
4. **Permission guards**: `/me/*` and `/telemetry/*` routes add `if user.kind == 'integration': raise HTTPException(403, "Integration keys cannot access /me or /telemetry — use /v1 only")`. Implemented as a route-level dependency `forbid_integration_kind`.
5. **Admin UX**: New `/admin/keys` (list), `/admin/keys/new` (form + POST), `/admin/keys/{id}/revoke`, `/admin/keys/{id}/rotate`. Three new templates. `/admin/users` filters `kind='employee'`. Overview tile: integration count. The `key_added.html.j2` template handles BOTH "minted" and "rotated" via a `mode='mint'|'rotate'` Jinja variable (different headline copy).
6. **Docs**: New `docs/OPENAI-COMPAT.md`. Update `INTEGRATION-GUIDE.md`, `API.md`, `OVERVIEW.md`, `ADMIN.md`. Fix stale `SMART_FORMAT_MIN_WORDS=100` → 80.
7. **Tests**: Three new files. Use deterministic fixtures generated from `ffmpeg -f lavfi -i "sine=frequency=440:duration=1"` (committed binaries; CI-safe; reproducible).
8. **Verification**: `scripts/verify-openai-compat.sh` (live roundtrip; smoke test).
9. **Deferred (documented as 400)**: `/v1/audio/translations` (Parakeet is English-only); `model=gpt-4o-transcribe-diarize` (point at `/transcribe/meeting`); `stream=true` on whisper-1.
10. **Concurrency model**: Already serialized via ParakeetService's single-thread executor. ffmpeg decode runs in `asyncio.to_thread` (does NOT block the event loop). Per-token rate limit uses `dict[int, deque[float]]` in `app.state.v1_rate_buckets` (NOT module-level — test isolation); each call sweeps entries older than 60s.

### Non-goals / Guardrails

- Do NOT implement SSE streaming on `/v1` (no Parakeet incremental path).
- Do NOT implement diarization on `/v1`.
- Do NOT change `/transcribe/dictate` rate limit (stays per-IP).
- Do NOT issue multiple tokens per user (1:1 cardinality stays).
- Do NOT change `ParakeetService.transcribe(...)` signature (2-tuple). Add `transcribe_with_alignment(...)` as a separate method.
- Do NOT advertise `gpt-4o-transcribe-diarize` in `/v1/models` (we 400 on it — listing it would confuse Open WebUI's test-connection probe).
- Do NOT use `kickstart -k` for the mini restart — use `bootout` + `bootstrap` so `uv sync` deps are picked up.
- Do NOT use a module-level dict for rate-limit buckets (test pollution); use `app.state.v1_rate_buckets`.

## Known Mismatches / Assumptions

- Mismatch: Brief decision #33 says "add asyncio.Semaphore(1) around inference"; repo already serializes via `ThreadPoolExecutor(max_workers=1)`.
  Repo Evidence: `parakeet.py:39-40, 62-63`.
  Requirement Evidence: Brief #33.
  Planning Decision: Drop the semaphore. Document executor in `docs/OPENAI-COMPAT.md`.

- Mismatch: Brief implies User dataclass stays unchanged (`role`-only); reviewer round identified `kind='integration'` permission leak on `/me/*` and `/telemetry/*` requiring `kind` at request time.
  Repo Evidence: `users/store.py:23-29` (`User` dataclass has 3 fields).
  Requirement Evidence: Reviewer B finding #7 (BLOCKER → resolved as SHOULD-FIX with route guard).
  Planning Decision: Add `kind: str = 'employee'` to `User` dataclass. Update `lookup` and `lookup_by_id` SELECT to include `kind`. Token cache holds the new shape. Add `forbid_integration_kind` dependency to `/me/*` + `/telemetry/*` route mounts in main.py.

- Mismatch: Plan v1 had a 6-model `/v1/models` list including `gpt-4o-transcribe-diarize`; reviewer A finding #13 noted Open WebUI's "test connection" probe lists then picks first — listing a model we 400 on confuses the probe.
  Planning Decision: 5 models only; exclude `gpt-4o-transcribe-diarize`. Document in OPENAI-COMPAT.md.

- Assumption: `parakeet-mlx` `AlignedToken` shape (`.text`, `.start`, `.end`) — dev box can't `pip install parakeet-mlx`.
  Planning Decision: dual-branch fallback. If `AlignedToken[]` path returns no usable timestamps OR `start`/`end` attributes are missing, fall through to single-segment verbose_json with `start=0, end=duration`. Final verification on Mac mini.

- Assumption: Cloudflare body-size ceiling is 100 MB; our 25 MB cap is well below.
  Planning Decision: Enforce 25 MB at app layer; document in OPENAI-COMPAT.md that >100 MB uploads hit Cloudflare's edge with a non-JSON 413.

## Critical Codebase Anchors

- Anchor: `routes/v1_transcriptions.py:31-41` (`_openai_error` helper)
  Reuse: All new error returns use this shape; preserve `request_id` injection.

- Anchor: `middleware/openai_errors.py:33-96` (`install` registers exception handlers)
  Reuse: New routes inherit error shaping. Plan extends the code → status_code → type mappings.

- Anchor: `ops/staging.py:461-595` (`transcode_to_canonical_wav`)
  Reuse: ffmpeg command shape. New sync helper pipes through tempfile-in + stdout-out, 60s timeout (not 30min).

- Anchor: `meeting/output.py:127-189` (SRT + VTT writers + `_seconds_to_srt`, `_seconds_to_vtt`)
  Reuse: Import and reuse formatters in `dictate/v1_response_builders.py`.

- Anchor: `routes/admin_ui.py:334-424` (Add Employee + revoke + rotate flows)
  Reuse: Form GET → POST → success template pattern. `_validate_label`, `_validate_optional_display_name` helpers.

- Anchor: `users/store.py:89-160` (`mint`, `rotate`, `revoke`)
  Reuse: New `/admin/keys/new` does `mint(role='employee', ...)` then `set_kind(user_id, 'integration')`.

- Anchor: `migrations/2026-04-27-v2-display-name.sql:15-17` (schema_version INSERT pattern)
  Reuse: v4 migration uses identical `INSERT (version, notes) ... ON CONFLICT (version) DO NOTHING`.

- Anchor: `auth.py:104-206` (4-branch token resolution)
  Reuse: Extract `_resolve_token_user` carefully preserving all 4 branches and `request.state.user` side effect.

## All Needed Context

### Documentation & References

- Repo reference: `server/src/wispralt_server/routes/v1_transcriptions.py` — current shim.
- Repo reference: `server/src/wispralt_server/middleware/openai_errors.py` — error shaping.
- Repo reference: `server/src/wispralt_server/ops/staging.py:461-595` — ffmpeg pattern.
- Repo reference: `server/src/wispralt_server/meeting/output.py:127-189` — SRT/VTT formatters.
- Repo reference: `server/src/wispralt_server/users/store.py` — token mint/rotate/revoke; new helpers added.
- Repo reference: `server/src/wispralt_server/routes/admin_ui.py:334-424` — form pattern.
- Repo reference: `server/src/wispralt_server/admin/templates/add_employee.html.j2`, `employee_added.html.j2`, `users.html.j2` — copy structure.
- Repo reference: `server/src/wispralt_server/dictate/parakeet.py:36-46, 97-111, 113-193` — `MAX_SAMPLES`, dual-shape result, serial executor.
- Repo reference: `server/src/wispralt_server/middleware/rate_limit.py:114-172` — bucket to remove `/v1` from.
- Repo reference: `server/migrations/2026-04-27-v2-display-name.sql` — migration template.
- Repo reference: `docs/INTEGRATION-GUIDE.md` — existing guide (stale on `SMART_FORMAT_MIN_WORDS`).
- Repo reference: `docs/API.md:309-351` — existing /v1 section.
- Repo reference: `docs/OVERVIEW.md` — file→doc map.
- Repo reference: `.claude/commands/verify-autostart.md:26-27` — bootout/bootstrap pattern.
- External doc: https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create — canonical form-field reference. Critical: `response_format=text` MUST return `Content-Type: text/plain; charset=utf-8`.
- External doc: https://developers.openai.com/api/reference/resources/models/methods/list — `/v1/models` shape.
- External doc: https://community.openai.com/t/whisper-api-verbose-json-results/93083 — confirms undocumented `transient: false`.
- External doc: https://community.openai.com/t/whisper-transcribe-api-verbose-json-results-format-of-language-property/646014 — `language` is lowercase full English name.

### Files Being Changed

```
server/
├── migrations/
│   └── 2026-05-20-v4-users-kind.sql                      ← NEW
├── src/wispralt_server/
│   ├── dictate/
│   │   ├── parakeet.py                                   ← MODIFIED (new transcribe_with_alignment; AudioTooLongError raise; _extract_text_and_tokens helper)
│   │   ├── sync_decode.py                                ← NEW (libsndfile + ffmpeg → 16kHz mono float32; sync function; called via asyncio.to_thread)
│   │   └── v1_response_builders.py                       ← NEW (verbose_json/srt/vtt builders; segmentation algorithm)
│   ├── middleware/
│   │   ├── cors.py                                       ← NEW (CORSMiddleware scoped to /v1/*)
│   │   ├── openai_errors.py                              ← MODIFIED (add invalid_audio_data, unsupported_file_type, audio_too_long, decode_timeout, streaming_unsupported, model_not_supported_on_endpoint, endpoint_not_supported)
│   │   └── rate_limit.py                                 ← MODIFIED (drop /v1 from dictate bucket; 429 emits CORS headers)
│   ├── routes/
│   │   ├── admin_ui.py                                   ← MODIFIED (new /admin/keys/* routes; integration_count tile; filter integration from /admin/users)
│   │   ├── me.py                                         ← MODIFIED (add forbid_integration_kind dep on all /me/* mounts)
│   │   ├── telemetry.py                                  ← MODIFIED (add forbid_integration_kind dep on all /telemetry/* mounts)
│   │   ├── v1_transcriptions.py                          ← MODIFIED (verbose_json/srt/vtt, ffmpeg via to_thread, headers, per-token rate limit, stream rejection, error code mapping, temperature validation, user form field, /v1/audio/translations stub)
│   │   └── v1_models.py                                  ← NEW (GET /v1/models + GET /v1/models/{id})
│   ├── users/
│   │   └── store.py                                      ← MODIFIED (User.kind field; UserRow.kind field; lookup/lookup_by_id SELECT kind; list_integrations; set_kind; count_kind helpers)
│   ├── admin/templates/
│   │   ├── add_key.html.j2                               ← NEW
│   │   ├── key_added.html.j2                             ← NEW (handles mint + rotate modes)
│   │   ├── keys.html.j2                                  ← NEW
│   │   ├── users.html.j2                                 ← MODIFIED (kind filter at query level)
│   │   └── overview.html.j2                              ← MODIFIED (integration_count tile)
│   ├── _errors.py                                        ← MODIFIED (add AudioTooLongError(CorruptAudioError) subclass; UnsupportedAudioError; DecodeTimeoutError)
│   ├── auth.py                                           ← MODIFIED (extract _resolve_token_user; new require_api_key_v1 Bearer-only; new forbid_integration_kind dep)
│   ├── constants.py                                      ← MODIFIED (OPENAI_COMPAT_VERSION = "2024-10-01"; OPENAI_KNOWN_MODELS tuple of 5; OPENAI_KNOWN_MODELS_CREATED epoch; OPENAI_KNOWN_MODELS_OWNED_BY="wispralt")
│   ├── main.py                                           ← MODIFIED (register v1_models router; CORS middleware OUTERMOST; TRACKED_ROUTES + _KIND_MAP for v1/models + v1/audio/translations; app.state.v1_rate_buckets init)
│   └── ratelimit_per_token.py                            ← NEW (post-auth per-token rate limit dependency)
├── tests/
│   ├── test_v1_transcriptions.py                         ← NEW
│   ├── test_v1_models.py                                 ← NEW
│   ├── test_admin_keys.py                                ← NEW
│   ├── test_integration_kind_guards.py                   ← NEW (verify /me/* + /telemetry/* reject integration keys)
│   └── fixtures/
│       ├── tiny.wav                                      ← NEW (deterministic sine via ffmpeg lavfi)
│       ├── tiny.mp3                                      ← NEW
│       ├── tiny.m4a                                      ← NEW
│       └── tiny.webm                                     ← NEW
├── scripts/
│   └── verify-openai-compat.sh                           ← NEW
docs/
├── OPENAI-COMPAT.md                                      ← NEW
├── INTEGRATION-GUIDE.md                                  ← MODIFIED
├── API.md                                                ← MODIFIED
├── ADMIN.md                                              ← MODIFIED
└── OVERVIEW.md                                           ← MODIFIED
README.md                                                 ← MODIFIED
```

### Known Gotchas & Library Quirks

- **openai-python retries on 408, 409, 429, and ≥500.** Map decoder failures to 400 only. ffmpeg timeout maps to `400 / decode_timeout`, NOT 408. Rate limit `Retry-After` is consumed by SDK and honored (SDK caps backoff at 60s, our value fits).
- **`response_format=text` MUST return `Content-Type: text/plain; charset=utf-8`.** Don't JSON-wrap.
- **`language` in `verbose_json` is `"english"`**, lowercase full English name. NOT `"en"`.
- **`segments[].transient: false`** is undocumented but always emitted by real OpenAI.
- **`Bearer` parsing**: existing code accepts case-insensitive (broader than spec — fine).
- **`stream=true` on whisper-1 → 400** with `code: "streaming_unsupported"`.
- **CORS**: install as the OUTERMOST middleware in `main.py` so EVERY response (including 429 rate-limit envelopes) carries `Access-Control-Allow-Origin: *`. Browser clients can then surface the actual HTTP error instead of a misleading "CORS error." Set `allow_credentials=False` explicitly — `allow_credentials=True` + `allow_origin="*"` is invalid per CORS spec (browser rejects).
- **`subprocess.run(timeout=60)` blocks the event loop.** Wrap calls in `await asyncio.to_thread(decode_to_pcm, audio_bytes)` from the async route handler.
- **`ffmpeg pipe:1`**: write input to a tempfile (small, ≤25 MB), read output bytes from stdout via `subprocess.run(..., capture_output=True)`. Use `try/finally` to ensure tempfile cleanup even on timeout. Use `pcm_f32le` + `-f f32le` for direct float32 output (NOT `pcm_s16le` — would require an extra normalization step).
- **`Cloudflare Tunnel` returns its own HTML 413 on >100MB body — document the cap chain.**
- **`/v1/models` MUST emit `Cache-Control: no-cache, must-revalidate`.**
- **`OPENAI_COMPAT_VERSION = "2024-10-01"`** — matches openai-python's default `OpenAI-Version` header. NOT the deploy date.
- **Admin session cookie path is `/`**. `require_api_key_v1` reads `request.headers.getlist("authorization")` directly and does NOT consult `request.cookies`.
- **Migration v4 idempotency**: use `ADD COLUMN IF NOT EXISTS` + `INSERT INTO wispralt.schema_version ... ON CONFLICT (version) DO NOTHING`. NEVER use `UPDATE schema_version SET version=4 WHERE version < 4` — would mutate v3's history row.
- **Test fixtures are deterministic**: `ffmpeg -f lavfi -i "sine=frequency=440:duration=1" -ar 16000 -ac 1 tiny.wav` produces the same bytes every time. Re-encode to mp3/m4a/webm with explicit codec versions for reproducibility.
- **Mocked `ParakeetService.transcribe_with_alignment`** returns canned `(text, ms, aligned_tokens)` triple — never run MLX on the dev box.
- **launchctl restart**: `bootout gui/$(id -u)/co.wispralt.server && bootstrap gui/$(id -u) ~/Library/LaunchAgents/co.wispralt.server.plist`. NOT `kickstart -k` (doesn't pick up new `uv sync` deps).
- **Break-glass admin (`user.id == -1`)** skips per-token rate limit (no key id to bucket on); should `if user.id < 0: return user` at top of `rate_limit_v1_per_token`.
- **`tokens[]` in segments**: emit `[]` (we don't have BPE token IDs).
- **`avg_logprob` / `compression_ratio` / `no_speech_prob`**: emit `0.0`, `1.0`, `0.0` placeholders.
- **`include[]=logprobs`**: silently accept and no-op.
- **`user` form field**: accept and silently ignore (OpenAI optional end-user identifier).

## Reconciliation Notes

- Added from review round 1: explicit `asyncio.to_thread` wrap for `decode_to_pcm`.
- Added: `AudioTooLongError(CorruptAudioError)` subclass model.
- Added: explicit four-step state machine for `_resolve_token_user`.
- Added: concrete `verbose_json` segmentation algorithm (split on `.?!` punctuation OR time gap > 0.5s OR length > 30s/100 tokens).
- Added: `User.kind` field + `forbid_integration_kind` dep on `/me/*` and `/telemetry/*`.
- Added: CORS as OUTERMOST middleware; explicit `allow_credentials=False`.
- Added: rate-limit buckets in `app.state` (testability); explicit sweep on every call; break-glass bypass.
- Removed: `gpt-4o-transcribe-diarize` from `/v1/models` listing.
- Changed: `OPENAI_COMPAT_VERSION` from `"2026-05-20"` to `"2024-10-01"` (matches openai-python default).
- Changed: launchctl restart command from `kickstart -k` to `bootout` + `bootstrap` (picks up `uv sync` deps; per `.claude/commands/verify-autostart.md`).
- Changed: test fixtures generation to deterministic `ffmpeg lavfi sine` source (reproducible across machines).
- Changed: migration SQL to use v2/v3 `INSERT (version, notes) ON CONFLICT (version) DO NOTHING` pattern.
- Added: Phase 0 baseline curl as a BLOCKING task before any code changes.
- Added: `user` form field handling on `/v1/audio/transcriptions`.
- Added: `_KIND_MAP` entry for `"v1/audio/translations"` → `"v1_translations"`.

## Delta Design

### Data / State Changes

Existing:
- `wispralt.users(id, label, token_hash, role, created_at, revoked_at, display_name)`. `User` dataclass: 3 fields (id/label/role).

Change:
- Add column `kind TEXT NOT NULL DEFAULT 'employee' CHECK (kind IN ('employee', 'integration'))`.
- `User` dataclass: add `kind: str = 'employee'` (cached in token_cache).
- `UserRow`: add `kind: str`.
- New helpers `list_integrations(pool)`, `set_kind(pool, user_id, kind)`, `count_kind(pool, kind)`.
- Update `lookup` and `lookup_by_id` SELECT to include `kind` column.
- `list_all` (employees query) adds `WHERE u.kind = 'employee'`.

Why:
- `kind` is orthogonal to `role`. Lets us guard `/me/*` + `/telemetry/*` against integration-kind tokens without touching the role enum.
- Including `kind` in cached `User` avoids a second DB query per request for the guard.

Risks:
- Token cache holds stale `kind` for up to 60s after admin rotates a key. Acceptable — `kind` doesn't change post-creation in normal flows. If we ever add a "convert this user to integration" flow, we'd need to invalidate the cache.

### Entry Point / Integration Flow

Existing:
- `/v1/audio/transcriptions` (POST), no other /v1 paths, no CORS.

Change:
- `/v1/audio/transcriptions` (POST) — expanded form params + response formats + headers + per-token rate limit + ffmpeg decode via `asyncio.to_thread` + error code mapping.
- `/v1/audio/translations` (POST) — 400 stub.
- `/v1/models` (GET) and `/v1/models/{id}` (GET) — static, no rate limit.
- `OPTIONS /v1/*` — handled by CORS middleware.

Risks:
- CORS headers missing on rate-limit 429 if CORS isn't the OUTERMOST middleware. Mitigation: explicit placement in main.py.

### Execution / Control Flow

Existing:
- Per-IP rate limit at middleware level; cookie auth path coexists with Bearer for all routes.

Change:
- New `app.state.v1_rate_buckets: dict[int, deque[float]]` (testable, isolated per-test-app).
- New `require_api_key_v1` (Bearer only) sits transitively under `rate_limit_v1_per_token`.
- `/v1/audio/transcriptions` route's only dep: `Depends(rate_limit_v1_per_token)` (returns User; auth resolved transitively).
- `_resolve_token_user(request, plaintext) -> User`: shared four-step state machine (cache, Postgres, no-row-401, errored-break-glass-or-503). Sets `request.state.user`. Multi-auth-header 400 preserved.
- ffmpeg sync decode wrapped via `await asyncio.to_thread(decode_to_pcm, audio_bytes)`.
- `transcribe_with_alignment(...)` async method on `ParakeetService` returns `(text, ms, aligned_tokens | None)`.

Risks:
- Auth state-machine bugs are revocation/security-critical. Mitigation: explicit test for all 5 outcomes; cache invalidation on rotate.

### User-Facing / Operator-Facing Surface

Existing:
- `/admin/users/new` mints user labeled "Add Employee."

Change:
- New `/admin/keys` GET, `/admin/keys/new` GET+POST, `/admin/keys/{id}/revoke`, `/admin/keys/{id}/rotate`.
- `key_added.html.j2` handles both mint (headline "API key created") and rotate (headline "API key rotated — old token revoked"). Pass `mode='mint'|'rotate'` from route.
- `/admin/users` filtered to `kind='employee'`.
- Overview tile: "Integration keys: N".

Risks:
- Templates drift from form validation. Mitigation: reuse existing `_validate_label` and `_validate_optional_display_name` from `admin_ui.py`.

### External / Operational Surface

Existing:
- Telemetry `_KIND_MAP` maps `"v1/audio"` → `"v1_dictate"`.

Change:
- Add `_KIND_MAP["v1/models"] = "v1_models"`.
- Add `_KIND_MAP["v1/audio/translations"] = "v1_translations"` (so unsupported-endpoint probes don't pollute `v1_dictate` stats).
- New `scripts/verify-openai-compat.sh` becomes long-term smoke target.

## Implementation Blueprint

### Architecture Overview

```
Client → Cloudflare → uvicorn (mini)
                      │
                      ├─→ CORSMiddleware (OUTERMOST — tags every response with ACAO)
                      ├─→ _RequestIdMiddleware
                      ├─→ rate_limit.py per-IP middleware
                      │
                      ▼ /v1/audio/transcriptions
                      │   Depends(rate_limit_v1_per_token) → Depends(require_api_key_v1) → _resolve_token_user
                      │   (cookie path NOT consulted on /v1)
                      │   validate response_format / temperature / stream / model / timestamp_granularities
                      │   await asyncio.to_thread(decode_to_pcm, audio_bytes)
                      │     ├─→ libsndfile try (wav/flac/ogg)
                      │     └─→ ffmpeg pipe (mp3/m4a/mp4/webm/aac/mpeg) → 16kHz mono float32
                      │   await parakeet.transcribe_with_alignment(samples) → (text, ms, tokens|None)
                      │   build_{json,text,verbose_json,srt,vtt}(text, ms, tokens)
                      │   Response + headers: x-request-id, openai-version, openai-processing-ms, openai-model
                      │
                      ▼ /v1/models, /v1/models/{id}
                      │   Depends(require_api_key_v1)  (no rate limit; cheap read)
                      │   static response + Cache-Control: no-cache, must-revalidate
                      │
                      ▼ /me/*, /telemetry/*
                      │   Depends(require_api_key) + Depends(forbid_integration_kind)
                      │   (raises 403 if user.kind == 'integration')
                      │
                      ▼ /admin/keys/*
                          Depends(require_admin)
                          mint → set_kind → render key_added.html.j2 with mode='mint'
```

### Key Pseudocode

#### Sync decode helper (new `dictate/sync_decode.py`)

```python
import shutil
import subprocess
import tempfile
import numpy as np
from wispralt_server import audio
from wispralt_server._errors import CorruptAudioError, UnsupportedAudioError, DecodeTimeoutError

TARGET_SR = 16_000
TIMEOUT_S = 60

def decode_to_pcm(audio_bytes: bytes) -> tuple[np.ndarray, float]:
    """Decode arbitrary audio bytes to 16kHz mono float32 PCM.

    Returns (samples_float32, duration_s).
    Raises CorruptAudioError, UnsupportedAudioError, or DecodeTimeoutError.

    BLOCKING — caller MUST wrap in asyncio.to_thread to avoid blocking the event loop.

    Strategy:
      1. libsndfile via audio.decode_wav_bytes (handles wav/flac/ogg/aiff).
         Returns (samples, sample_rate_int); resample to 16k mono explicitly.
      2. On libsndfile failure, fall through to ffmpeg pipe.
    """
    try:
        samples, sr = audio.decode_wav_bytes(audio_bytes)
        # downmix to mono if needed
        if samples.ndim == 2:
            samples = samples.mean(axis=1)
        # resample to 16 kHz
        samples = audio.safe_resample(samples, sr, TARGET_SR)
        samples = samples.astype(np.float32, copy=False)
        return samples, len(samples) / TARGET_SR
    except CorruptAudioError:
        pass  # fall through to ffmpeg

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")

    tmp = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        cmd = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "error",
            "-i", tmp.name,
            "-map", "0:a:0",
            "-vn",
            "-ac", "1",
            "-ar", str(TARGET_SR),
            "-acodec", "pcm_f32le",
            "-f", "f32le",
            "pipe:1",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise DecodeTimeoutError(f"ffmpeg decode exceeded {TIMEOUT_S}s") from exc
        if result.returncode != 0:
            tail = result.stderr.decode("utf-8", errors="replace")[-500:]
            raise UnsupportedAudioError(f"ffmpeg decode failed: {tail}")
        samples = np.frombuffer(result.stdout, dtype=np.float32).copy()  # copy: np.frombuffer over `bytes` returns READ-ONLY; downstream resample/MLX needs writable
        if samples.size == 0:
            raise UnsupportedAudioError("ffmpeg produced empty output")
        return samples, len(samples) / TARGET_SR
    finally:
        import os
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
```

#### `transcribe_with_alignment` (new method on `ParakeetService`)

```python
# Add to parakeet.py
def _sync_transcribe_with_alignment(self, samples: np.ndarray) -> tuple[str, float, list | None]:
    """Variant returning aligned tokens when available.

    samples: 16 kHz mono float32 (already decoded — no audio_bytes path here).
    Returns (text, inference_ms, aligned_tokens or None).
    """
    t0 = time.perf_counter()
    # length checks (mirror _sync_transcribe behavior)
    if len(samples) > MAX_SAMPLES:
        from wispralt_server._errors import AudioTooLongError
        raise AudioTooLongError(
            f"Audio too long: {len(samples)/TARGET_SR:.1f}s (max {MAX_SAMPLES/TARGET_SR:.0f}s)"
        )
    if len(samples) < MIN_SAMPLES:
        return "", 0.0, []

    audio_mlx = mx.array(samples, dtype=mx.float32)
    mel = get_logmel(audio_mlx, self.model.preprocessor_config)
    result = self.model.generate(mel, decoding_config=DecodingConfig())
    mx.eval(result)

    text, tokens = self._extract_text_and_tokens(result)

    del result, mel, audio_mlx
    try:
        mx.metal.clear_cache()
    except AttributeError:
        pass

    duration_ms = (time.perf_counter() - t0) * 1_000.0
    self.recent_durations.append(duration_ms)
    return text, duration_ms, tokens

async def transcribe_with_alignment(self, samples: np.ndarray) -> tuple[str, float, list | None]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(self._exec, self._sync_transcribe_with_alignment, samples)

def _extract_text_and_tokens(self, result: object) -> tuple[str, list | None]:
    """Return (text, aligned_tokens_or_none).

    aligned_tokens is None when the result is a Hypothesis (text-only),
    or a list of AlignedToken objects when alignment is surfaced.
    """
    if isinstance(result, list) and result and hasattr(result[0], "text"):
        # AlignedToken list — surface for verbose_json segmentation
        text = "".join(str(t.text) for t in result).strip()
        return text, list(result)
    if hasattr(result, "text"):
        return str(result.text).strip(), None
    return "", None
```

NOTE: existing `transcribe(audio_bytes: bytes) -> (str, float)` stays unchanged. New method takes pre-decoded samples (the /v1 route does its own decode via `sync_decode.decode_to_pcm`).

#### verbose_json builder (new `dictate/v1_response_builders.py`)

```python
import re

# Look back over the last few tokens' joined text for a sentence-end. Parakeet
# emits subword tokens (e.g. "transcrip", "tion") — a single token rarely ends
# in [.!?]. Joining the last N tokens catches the punctuation that lands
# wherever the subword tokenizer split it.
SENTENCE_END = re.compile(r"[.!?][\"')\]]?\s*$")
SENTENCE_LOOKBACK = 4         # last 4 tokens' joined text checked for sentence-end
TIME_GAP_THRESHOLD_S = 0.5
MAX_SEGMENT_SECONDS = 12.0    # short enough that subtitle clients render usefully;
                              # long enough to capture a full sentence
MAX_SEGMENT_TOKENS = 80
MIN_SEGMENT_SECONDS = 1.0     # don't flush a segment under 1s on a sentence-end alone
                              # (defends against single-word "Yes." splits)

def _group_into_segments(aligned_tokens: list) -> list[dict]:
    """Split an AlignedToken list into OpenAI-shaped segments.

    Boundary rules (in order):
      - time gap > 0.5s between prev_token.end and cur_token.start  → ALWAYS split
      - sentence-end in last SENTENCE_LOOKBACK tokens joined text   → split IF segment ≥ 1s
      - segment exceeds MAX_SEGMENT_SECONDS or MAX_SEGMENT_TOKENS   → force split

    Time-gap is the primary signal because Parakeet aligned timestamps reliably
    surface speaker pauses. Sentence-end is secondary (subword tokens make
    punctuation-detection brittle); we accept some false negatives.
    """
    if not aligned_tokens:
        return []
    segments: list[dict] = []
    current_tokens: list = []
    seg_start = float(aligned_tokens[0].start)
    last_end = float(aligned_tokens[0].end)
    seg_id = 0

    def flush():
        nonlocal current_tokens, seg_id, seg_start
        if not current_tokens:
            return
        text = "".join(str(t.text) for t in current_tokens).strip()
        if not text:
            current_tokens = []
            return
        segments.append({
            "id": seg_id,
            "seek": 0,
            "start": float(seg_start),
            "end": float(current_tokens[-1].end),
            "text": text,
            "tokens": [],
            "temperature": 0.0,
            "avg_logprob": 0.0,
            "compression_ratio": 1.0,
            "no_speech_prob": 0.0,
            "transient": False,
        })
        seg_id += 1
        current_tokens = []

    for tok in aligned_tokens:
        cur_start = float(tok.start)
        cur_end = float(tok.end)
        if current_tokens:
            time_gap = cur_start - last_end
            seg_duration = last_end - seg_start
            # sliding window over the last N tokens' joined text — survives subword splits
            tail_text = "".join(str(t.text) for t in current_tokens[-SENTENCE_LOOKBACK:])
            should_split = (
                time_gap > TIME_GAP_THRESHOLD_S
                or seg_duration >= MAX_SEGMENT_SECONDS
                or len(current_tokens) >= MAX_SEGMENT_TOKENS
                or (SENTENCE_END.search(tail_text) and seg_duration >= MIN_SEGMENT_SECONDS)
            )
            if should_split:
                flush()
                seg_start = cur_start
        current_tokens.append(tok)
        last_end = cur_end
    flush()
    return segments

def build_verbose_json(
    text: str,
    duration_s: float,
    aligned_tokens: list | None,
    include_words: bool,
) -> dict:
    """Build the OpenAI verbose_json response body."""
    # Empty audio → empty segments + empty words
    if not text:
        return {
            "task": "transcribe",
            "language": "english",
            "duration": float(duration_s),
            "text": "",
            "segments": [],
            **({"words": []} if include_words else {}),
        }

    if aligned_tokens:
        segments = _group_into_segments(aligned_tokens)
        if include_words:
            words = [
                {"word": str(t.text).strip(), "start": float(t.start), "end": float(t.end)}
                for t in aligned_tokens
                if str(t.text).strip()
            ]
        else:
            words = None
    else:
        # Hypothesis (text-only) — single degenerate segment
        segments = [{
            "id": 0, "seek": 0,
            "start": 0.0, "end": float(duration_s),
            "text": text,
            "tokens": [], "temperature": 0.0,
            "avg_logprob": 0.0, "compression_ratio": 1.0,
            "no_speech_prob": 0.0, "transient": False,
        }]
        words = [] if include_words else None

    body = {
        "task": "transcribe",
        "language": "english",
        "duration": float(duration_s),
        "text": text,
        "segments": segments,
    }
    if words is not None:
        body["words"] = words
    return body

def build_srt(text: str, duration_s: float, aligned_tokens: list | None) -> str:
    """Build SubRip SRT body. Reuses meeting/output.py:_seconds_to_srt timecode formatter."""
    from wispralt_server.meeting.output import _seconds_to_srt
    if aligned_tokens:
        segments = _group_into_segments(aligned_tokens)
    elif text:
        segments = [{"start": 0.0, "end": float(duration_s), "text": text}]
    else:
        return ""
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{_seconds_to_srt(seg['start'])} --> {_seconds_to_srt(seg['end'])}")
        lines.append(seg["text"].strip())
        lines.append("")
    return "\n".join(lines)

def build_vtt(text: str, duration_s: float, aligned_tokens: list | None) -> str:
    """Build WebVTT body. Reuses meeting/output.py:_seconds_to_vtt timecode formatter."""
    from wispralt_server.meeting.output import _seconds_to_vtt
    parts = ["WEBVTT", ""]
    if aligned_tokens:
        segments = _group_into_segments(aligned_tokens)
    elif text:
        segments = [{"start": 0.0, "end": float(duration_s), "text": text}]
    else:
        return "WEBVTT\n"
    for seg in segments:
        parts.append(f"{_seconds_to_vtt(seg['start'])} --> {_seconds_to_vtt(seg['end'])}")
        parts.append(seg["text"].strip())
        parts.append("")
    return "\n".join(parts)
```

#### Per-token rate limit (new `ratelimit_per_token.py`)

```python
import asyncio
import collections
import time
from fastapi import Depends, HTTPException, Request
from wispralt_server.auth import require_api_key_v1
from wispralt_server.users.store import User

_PER_MIN = 60
_WINDOW_S = 60.0

async def rate_limit_v1_per_token(
    request: Request,
    user: User = Depends(require_api_key_v1),
) -> User:
    """Post-auth dependency: 60 req / 60s per user.id.

    Break-glass users (user.id < 0) bypass — no key id to bucket on.
    Buckets live on app.state.v1_rate_buckets (testable, per-app isolation).
    Each call sweeps entries older than window — amortized O(1).
    """
    if user.id < 0:
        return user

    buckets: dict[int, collections.deque[float]] = request.app.state.v1_rate_buckets
    lock: asyncio.Lock = request.app.state.v1_rate_buckets_lock

    now = time.monotonic()
    async with lock:
        bucket = buckets.get(user.id)
        if bucket is None:
            bucket = collections.deque()
            buckets[user.id] = bucket
        # Sweep stale entries (older than window) — amortized O(1)
        while bucket and now - bucket[0] > _WINDOW_S:
            bucket.popleft()
        if len(bucket) >= _PER_MIN:
            retry_after = max(1, int(_WINDOW_S - (now - bucket[0])))
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: {_PER_MIN} requests per minute per token",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)
    return user

def init_rate_limit_state(app):
    """Call from main.py lifespan startup."""
    app.state.v1_rate_buckets = {}
    app.state.v1_rate_buckets_lock = asyncio.Lock()
```

#### Auth refactor (extract `_resolve_token_user` from `auth.py`)

```python
# Pseudocode for the refactor — preserves the existing 4-branch state machine.

async def _resolve_token_user(request: Request, plaintext: str) -> User:
    """Shared token-resolution path: cache → Postgres → break-glass.

    State machine:
      1. token_cache hit → return User immediately
      2. Postgres lookup
         2a. Found → cache.put + set request.state.user + return
         2b. Not found (and Postgres did NOT error) → 401 hard-stop (no break-glass — revocation invariant)
      3. Postgres errored → break-glass attempt
         3a. break_glass_token_hash matches → return synthetic User(id=-1, role='admin')
         3b. No match → 503
    """
    # NOTE: token_cache is a module-level singleton in auth.py (line 48), NOT app.state.
    # Imports unchanged from current auth.py: hmac, asyncpg, users_store, hash_token,
    # logger, plus the module-level `token_cache` and `_cache_mod` references.
    th = hash_token(plaintext)
    cached = token_cache.get(th)
    if cached is not None:
        request.state.user = cached
        return cached

    pool = getattr(request.app.state, "db_pool", None)
    postgres_errored = False
    user_row = None
    if pool is not None:
        try:
            user_row = await users_store.lookup(pool, th)
        # IMPORTANT: this exception tuple is documented at auth.py:174 and cited in
        # the 2026-05-17 postmortem ("asyncpg has NO common base class; InterfaceError
        # handles 'pool is closed' client-side"). Do NOT change without re-reading
        # that postmortem; replacing with OSError/TimeoutError would re-open the
        # incident where break-glass never fired on pool-closed errors.
        except (asyncpg.PostgresError, asyncpg.InterfaceError):
            postgres_errored = True
            logger.exception("Postgres lookup failed; will try break-glass")

    if user_row is not None:
        token_cache.put(th, user_row)
        request.state.user = user_row
        return user_row

    if not postgres_errored:
        # Definitive "no row" → 401, do NOT consult break-glass (revocation invariant)
        raise HTTPException(401, "Invalid bearer token")

    # Postgres errored — try break-glass
    bg_hash = getattr(request.app.state, "break_glass_token_hash", None)
    if bg_hash and hmac.compare_digest(bg_hash, th):
        synthetic = User(id=-1, label="break-glass-admin", role="admin", kind="employee")
        request.state.user = synthetic
        return synthetic

    raise HTTPException(503, "Auth temporarily unavailable")


def _extract_bearer(request: Request) -> str | None:
    """Read the Authorization: Bearer header. Returns plaintext or None.

    Multiple Authorization headers → 400 (preserved from existing behavior).
    """
    headers = request.headers.getlist("authorization")
    if not headers:
        return None
    if len(headers) > 1:
        raise HTTPException(400, "Multiple Authorization headers not allowed")
    raw = headers[0]
    if not raw.lower().startswith("bearer "):
        return None
    return raw[7:].strip() or None


async def require_api_key(request: Request) -> User:
    """Existing behavior: Bearer OR session cookie."""
    plaintext = _extract_bearer(request)
    if plaintext is None:
        # Try cookie fallback
        plaintext = request.cookies.get("wispralt_admin_token")
    if not plaintext:
        raise HTTPException(401, "Missing Authorization header or session cookie")
    return await _resolve_token_user(request, plaintext)


async def require_api_key_v1(request: Request) -> User:
    """/v1/* only: Bearer required, cookie ignored."""
    plaintext = _extract_bearer(request)
    if not plaintext:
        raise HTTPException(401, "Expected 'Bearer <token>' Authorization header")
    return await _resolve_token_user(request, plaintext)


async def forbid_integration_kind(user: User = Depends(require_api_key)) -> User:
    """Dependency for /me/* and /telemetry/* — reject integration-kind tokens."""
    if user.kind == "integration":
        raise HTTPException(
            403,
            "Integration keys cannot access /me or /telemetry — use /v1/audio/transcriptions only",
        )
    return user
```

### Data Models and Structure

```python
# server/src/wispralt_server/users/store.py — additions

@dataclass(frozen=True, slots=True)
class User:
    id: int
    label: str
    role: str           # 'admin' | 'employee'
    kind: str = 'employee'   # NEW — 'employee' | 'integration'

@dataclass(frozen=True, slots=True)
class UserRow:
    id: int
    label: str
    role: str
    created_at: datetime
    revoked_at: datetime | None
    last_seen_at: datetime | None
    display_name: str | None
    kind: str           # NEW

async def lookup(pool, token_hash):
    row = await pool.fetchrow(
        "SELECT id, label, role, kind FROM wispralt.users "
        "WHERE token_hash = $1 AND revoked_at IS NULL",
        token_hash,
    )
    if row is None:
        return None
    return User(id=row["id"], label=row["label"], role=row["role"], kind=row["kind"])

# Similarly: lookup_by_id (SELECT adds kind)
# list_all: WHERE u.kind = 'employee' (so /admin/users excludes integration keys)
# list_integrations: WHERE u.kind = 'integration' AND u.revoked_at IS NULL
# set_kind(pool, user_id, kind): UPDATE wispralt.users SET kind=$1 WHERE id=$2
# count_kind(pool, kind): SELECT COUNT(*) WHERE kind=$1 AND revoked_at IS NULL
```

```python
# server/src/wispralt_server/constants.py — additions

OPENAI_COMPAT_SIZE_CAP = 25 * 1024 * 1024  # unchanged
OPENAI_COMPAT_VERSION = "2024-10-01"        # matches openai-python default OpenAI-Version
OPENAI_KNOWN_MODELS: tuple[str, ...] = (
    "whisper-1",
    "gpt-4o-transcribe",
    "gpt-4o-mini-transcribe",
    "gpt-4o-mini-transcribe-2025-12-15",
    "gpt-4o-mini-transcribe-2025-03-20",
)  # 5 models — gpt-4o-transcribe-diarize EXCLUDED (we 400 on it)
OPENAI_KNOWN_MODELS_CREATED = 1677532384
OPENAI_KNOWN_MODELS_OWNED_BY = "wispralt"
```

```sql
-- server/migrations/2026-05-20-v4-users-kind.sql
-- Apply via Supabase MCP `apply_migration` with name `v4_users_kind`.
-- Re-runs cleanly: ADD COLUMN IF NOT EXISTS + DO $$ for constraint + INSERT ON CONFLICT.

ALTER TABLE wispralt.users
  ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'employee';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'users_kind_check'
  ) THEN
    ALTER TABLE wispralt.users
      ADD CONSTRAINT users_kind_check CHECK (kind IN ('employee', 'integration'));
  END IF;
END $$;

INSERT INTO wispralt.schema_version (version, notes)
VALUES (4, 'add kind column to wispralt.users (employee | integration)')
ON CONFLICT (version) DO NOTHING;
```

### Tasks (in implementation order)

---

**Task 0 (PHASE 0, BLOCKING single):** Live baseline curl against existing prod `/v1/audio/transcriptions`.
Goal:
- Confirm the existing minimal `/v1` shim actually transcribes a real WAV before we build on top of it.
Steps:
- Generate a real-speech fixture (not sine — Parakeet returns "" on tonal input):
  ```
  say "hello world this is a test" -o /tmp/hello.aiff
  ffmpeg -y -i /tmp/hello.aiff -ar 16000 -ac 1 -acodec pcm_s16le /tmp/hello.wav
  ```
- Source a prod key from `/admin/users` (existing employee key list) OR mint a temporary one via `/admin/users/new`. Store as `WISPRALT_PROD_KEY` env var for the curl call.
- `curl -fsSL -X POST -H "Authorization: Bearer $WISPRALT_PROD_KEY" -F file=@/tmp/hello.wav -F response_format=json https://transcribe.integrateapi.ai/v1/audio/transcriptions > "tmp/v1-baseline-$(date +%Y-%m-%d).json"`
- Validate response is `{"text": "..."}` with non-empty text (substring match "hello" or "test").
- If baseline fails (empty text, 500, etc.) → STOP and re-plan.
- BLOCKING SKIP allowed if `tmp/v1-baseline-*.json` from the last 7 days already shows healthy text — operator may continue without re-running the curl.
Definition of done:
- A recent `tmp/v1-baseline-*.json` exists with non-empty `text` field, OR operator confirms an existing baseline ≤ 7 days old.

---

**Task 1 (PHASE 1):** Apply DB migration v4.
Goal:
- Add `kind` column to `wispralt.users`.
Files:
- CREATE `server/migrations/2026-05-20-v4-users-kind.sql`
Steps:
- Apply via Supabase MCP `apply_migration(name='v4_users_kind', query=<file contents>)` against project `lmaffmygjrfgkwrapfax`.
- Verify: `SELECT version FROM wispralt.schema_version ORDER BY version DESC LIMIT 1` returns 4.
- Verify: `SELECT DISTINCT kind FROM wispralt.users` returns `{'employee'}`.
Gotchas:
- Use `INSERT ... ON CONFLICT (version) DO NOTHING` — do NOT use `UPDATE schema_version SET version=4`.
Definition of done:
- Column exists; all rows `kind='employee'`; schema_version has row `(4, '...')`.

---

**Task 2 (PHASE 2a, PARALLEL):** New sync decode helper.
Files:
- CREATE `server/src/wispralt_server/dictate/sync_decode.py`
- MODIFY `server/src/wispralt_server/_errors.py` (add `UnsupportedAudioError`, `DecodeTimeoutError`, `AudioTooLongError(CorruptAudioError)`)
- MODIFY `server/src/wispralt_server/dictate/parakeet.py` (replace too-long `raise CorruptAudioError` with `raise AudioTooLongError`; add `transcribe_with_alignment(samples)` async method + `_sync_transcribe_with_alignment` + `_extract_text_and_tokens` helper)
Pattern to copy:
- ffmpeg command in `ops/staging.py:514-535`.
- libsndfile read in `audio.py:46`.
Gotchas:
- Use `pcm_f32le` + `-f f32le` output.
- Tempfile cleanup in `try/finally` (NamedTemporaryFile(delete=False) + os.unlink).
- 60s subprocess timeout.
- This function is SYNCHRONOUS — caller MUST wrap in `await asyncio.to_thread(decode_to_pcm, audio_bytes)` from the async route.
- After libsndfile success, MUST resample to 16k AND downmix to mono explicitly.
- `np.frombuffer(...).copy()` so the returned ndarray doesn't reference freed stdout buffer.
- `_sync_transcribe_with_alignment` takes pre-decoded samples (NOT audio_bytes). Existing `transcribe(audio_bytes)` stays unchanged.
Definition of done:
- Unit tests verify a 1s synthetic mp3 → ~16000 samples, duration=1.0.

---

**Task 3 (PHASE 2b, PARALLEL):** v1 response builders.
Files:
- CREATE `server/src/wispralt_server/dictate/v1_response_builders.py`
Pattern to copy:
- `meeting/output.py:127-189` (SRT/VTT) + `_seconds_to_srt`/`_seconds_to_vtt`.
Gotchas:
- `language = "english"` (lowercase full word).
- Segments always include `transient: false`.
- `tokens=[]`, `avg_logprob=0.0`, `compression_ratio=1.0`, `no_speech_prob=0.0` placeholders.
- Empty text → `segments=[]`.
- Concrete segmentation algorithm: split on `.?!` (sentence end) OR time gap > 0.5s OR cumulative duration ≥ 30s OR ≥ 100 tokens.
Definition of done:
- `build_verbose_json("hello world", 1.0, None, False)` returns canonical shape; segmentation tests pass on a synthetic AlignedToken list.

---

**Task 4 (PHASE 2c, PARALLEL):** CORS middleware (OUTERMOST).
Files:
- CREATE `server/src/wispralt_server/middleware/cors.py`
- MODIFY `server/src/wispralt_server/main.py` (wire CORS LAST via `app.add_middleware` so it's OUTERMOST — Starlette LIFO: last-added runs first)
Gotchas:
- `allow_origin="*"`, `allow_credentials=False` (BOTH true is invalid per spec; browser rejects).
- `allow_methods=["GET", "POST", "OPTIONS"]`.
- `allow_headers=["Authorization", "Content-Type", "OpenAI-Organization", "OpenAI-Project", "X-Stainless-Lang", "X-Stainless-Package-Version", "X-Stainless-OS", "X-Stainless-Arch", "X-Stainless-Runtime", "X-Stainless-Runtime-Version", "User-Agent"]`.
- `expose_headers=["x-request-id", "openai-version", "openai-processing-ms", "openai-model"]`.
- `max_age=86400`.
- CORS scope: **install globally** (no path subclass). Rationale: (a) `allow_credentials=False` means cross-origin reads of admin endpoints can't carry the session cookie, so admin auth surface is unaffected; (b) `transcribe.integrateapi.ai` is a single-purpose domain; the admin UI is same-origin; (c) the simpler middleware install removes a class of "why is preflight failing on /v1 but not /admin" debug sessions. Note this trade-off explicitly in `docs/ADMIN.md`: ACAO=`*` is set on admin responses but credentials are never returned cross-origin.
Definition of done:
- `curl -X OPTIONS -H "Origin: https://foo.bar" -H "Access-Control-Request-Method: POST" /v1/audio/transcriptions` returns 200 with ACAO/ACAM/ACAH headers.
- Rate-limit 429 response includes `Access-Control-Allow-Origin: *`.

---

**Task 5 (PHASE 2d, PARALLEL):** Per-token rate limit dependency.
Files:
- CREATE `server/src/wispralt_server/ratelimit_per_token.py`
- MODIFY `server/src/wispralt_server/middleware/rate_limit.py` (drop `/v1/audio/transcriptions` from dictate bucket; keep `/transcribe/dictate` per-IP)
- MODIFY `server/src/wispralt_server/main.py` (call `init_rate_limit_state(app)` at lifespan startup BEFORE `yield`; specifically before the first `app.include_router(v1_transcriptions.router)` is hit by any request — practically, any pre-yield position works since lifespan startup runs to completion before serving)
Gotchas:
- Buckets live on `app.state.v1_rate_buckets: dict[int, deque[float]]` + `app.state.v1_rate_buckets_lock: asyncio.Lock`.
- Drop dead `maxlen=120` — use explicit sweep + length check.
- Skip break-glass users (`user.id < 0`).
- Dependency chain: route uses ONLY `Depends(rate_limit_v1_per_token)` (which itself depends on `require_api_key_v1` — FastAPI caches the dep result, so auth runs exactly once per request).
- Defensive lazy-init: `rate_limit_v1_per_token` should check `getattr(request.app.state, 'v1_rate_buckets', None)` and initialize-on-miss with a one-time WARN log, so tests that don't trigger lifespan (older `TestClient(app)` usage without `with`) still work. Pin FastAPI version to ≥0.93 in pyproject so `with TestClient(app):` lifespan invocation is the canonical fixture pattern.
Definition of done:
- Test: 60 calls in <1s pass; 61st returns 429 with `Retry-After`.
- Test: auth runs exactly once per request (mock counter on `_resolve_token_user`).

---

**Task 6 (PHASE 2e, PARALLEL):** Auth refactor — extract `_resolve_token_user`.
Files:
- MODIFY `server/src/wispralt_server/auth.py` (extract helper; add `require_api_key_v1`; add `forbid_integration_kind`)
Gotchas:
- Preserve ALL four branches of the existing 4-state machine.
- Preserve `request.state.user` side effect on every success path.
- Preserve multi-Authorization-header 400.
- Preserve "Postgres said no row → 401, do NOT consult break-glass" branch (revocation invariant).
- Existing callers of `require_api_key` stay unchanged.
Definition of done:
- All existing tests pass (`test_admin_routes_auth.py`, `test_auth_break_glass.py`).
- New tests: 5 outcomes (cache hit, fresh hit, postgres-no-row 401, postgres-errored break-glass success, no-pool 503).
- New test: `/v1` with cookie-only auth (no Bearer) → 401.

---

**Task 7 (PHASE 2f, depends on 2-6):** Expand v1_transcriptions.py.
Files:
- MODIFY `server/src/wispralt_server/routes/v1_transcriptions.py`
- MODIFY `server/src/wispralt_server/middleware/openai_errors.py` (new codes + status mappings)
- MODIFY `server/src/wispralt_server/constants.py` (model IDs, version stamp)
Form params (all):
- `file: UploadFile` (required)
- `response_format: str = Form("json")` — validated against `{"json", "text", "verbose_json", "srt", "vtt"}`
- `model: str = Form("whisper-1")` — validated; `gpt-4o-transcribe-diarize` → 404 `model_not_found` (consistent with its exclusion from /v1/models — matches real OpenAI semantics for a model that doesn't exist on this endpoint); unknown models logged but still routed to Parakeet
- `language: str | None = Form(None)` — accepted, ignored
- `prompt: str | None = Form(None)` — accepted, ignored
- `temperature: float | None = Form(None)` — validated 0.0 ≤ x ≤ 1.0 if present; else 400
- `timestamp_granularities: list[str] = Form([])` — validated; requires verbose_json if non-empty
- `include: list[str] = Form([])` — accepted, silently no-op
- `stream: bool = Form(False)` — if True → 400 `streaming_unsupported`
- `user: str | None = Form(None)` — accepted, logged at debug, ignored
Error code map additions in `openai_errors.py`:
- `invalid_audio_data` → 400 (from `CorruptAudioError`)
- `audio_too_long` → 400 (from `AudioTooLongError`)
- `unsupported_file_type` → 400 (from `UnsupportedAudioError`)
- `decode_timeout` → 400 (from `DecodeTimeoutError`)
- `streaming_unsupported` → 400 (stream=True rejected)
- `model_not_found` → 404 (diarize requested — consistent with /v1/models exclusion)
- `endpoint_not_supported` → 400 (/v1/audio/translations stub)
- `validation_failed` → 400 (timestamp_granularities w/o verbose_json; temperature out of range)
Response headers (all responses, success + error):
- `x-request-id` (already set by middleware)
- `openai-version: 2024-10-01` (from constants)
- `openai-processing-ms: <int>` (computed inside handler)
- `openai-model: <echoed-request-model>` (or `whisper-1` default)
Routes added:
- `POST /v1/audio/translations` → 400 `endpoint_not_supported` (stub)
Decode + transcribe flow:
- `samples, duration_s = await asyncio.to_thread(decode_to_pcm, audio_bytes)`
- `text, ms, tokens = await parakeet_service.transcribe_with_alignment(samples)`
- Build response per `response_format`.
Gotchas:
- All validation MUST happen BEFORE inference (fail-fast).
- Route depends only on `Depends(rate_limit_v1_per_token)` (auth resolved transitively).
- Plain text response: `PlainTextResponse(text, status_code=200, media_type="text/plain; charset=utf-8")`.
- SRT response: `PlainTextResponse(srt_body, media_type="application/x-subrip")`.
- VTT response: `PlainTextResponse(vtt_body, media_type="text/vtt")`.
- JSON/verbose_json: `JSONResponse(body, headers={...})`.
Definition of done:
- Mock-driven tests cover every response format, every error path, every header.

---

**Task 8 (PHASE 2g, PARALLEL with 7):** New /v1/models routes + telemetry mapping.
Files:
- CREATE `server/src/wispralt_server/routes/v1_models.py`
- MODIFY `server/src/wispralt_server/main.py` (register router; `_KIND_MAP["v1/models"] = "v1_models"`; `_KIND_MAP["v1/audio/translations"] = "v1_translations"`)
Routes:
- `GET /v1/models` → 200 with `{"object": "list", "data": [<5 model objects>]}`
- `GET /v1/models/{id}` → 200 single model object OR 404 `model_not_found`
Headers:
- `Cache-Control: no-cache, must-revalidate`
- Standard `x-request-id`, `openai-version`
Auth:
- `Depends(require_api_key_v1)` — Bearer only, no rate limit (cheap read)
Gotchas:
- 5 models, NOT 6 — `gpt-4o-transcribe-diarize` excluded.
Definition of done:
- Test: list returns 5 entries; unknown model id returns 404 envelope.

---

**Task 9 (PHASE 3a, PARALLEL after Phase 1):** users/store.py additions.
Files:
- MODIFY `server/src/wispralt_server/users/store.py`
Changes:
- `User.kind: str = 'employee'` field
- `UserRow.kind: str` field
- Update `lookup` and `lookup_by_id` SELECT to include `kind`
- Add `list_integrations(pool)`, `set_kind(pool, user_id, kind)`, `count_kind(pool, kind)` helpers
- `list_all` (the `/admin/users` query) adds `WHERE u.kind = 'employee'`
Definition of done:
- `UserRow.kind` reads from DB; `set_kind` round-trips; `list_integrations` returns only integration rows.

---

**Task 10 (PHASE 3b, depends on 9):** Admin UI /admin/keys routes + templates + permission guards.
Files:
- MODIFY `server/src/wispralt_server/routes/admin_ui.py` (new /admin/keys routes; integration_count tile)
- MODIFY `server/src/wispralt_server/routes/me.py` (swap `Depends(require_api_key)` → `Depends(forbid_integration_kind)` on EACH authenticated route individually — `/me/login` GET and POST stay unauthenticated; do NOT add at router level)
- MODIFY `server/src/wispralt_server/routes/telemetry.py` (swap `Depends(require_api_key)` → `Depends(forbid_integration_kind)` on each authenticated route)
- CREATE `server/src/wispralt_server/admin/templates/keys.html.j2`
- CREATE `server/src/wispralt_server/admin/templates/add_key.html.j2`
- CREATE `server/src/wispralt_server/admin/templates/key_added.html.j2` (handles both mint + rotate via `mode` variable)
- MODIFY `server/src/wispralt_server/admin/templates/overview.html.j2` (integration_count tile)
Gotchas:
- `mint(label='key-<slug>', role='employee', display_name=program_name)` followed by `set_kind(user_id, 'integration')`. CRITICAL: the `User` returned by `mint` has `kind='employee'` (the dataclass default). Either refresh via `await users_store.lookup_by_id(pool, user.id)` AFTER `set_kind`, OR make sure `key_added.html.j2` never reads `user.kind` (only `display_name`, `plaintext`, and an explicitly-passed `kind='integration'`). Prefer the refresh — clearer semantics.
- After `users_store.rotate(...)`, call `auth.token_cache.invalidate(old_hash)` so the old plaintext stops working immediately (existing rotate flow at admin_ui.py:415-416 does this — preserve).
- `key_added.html.j2` accepts `mode='mint'|'rotate'` and shows different headline.
- Revoke + rotate routes reuse `users_store.revoke` / `users_store.rotate`.
- Slug `label`: lowercase, `[a-z0-9-]`, prefixed `key-`. Validate ≤ 80 via `_validate_label`.
- `/me/login` GET+POST routes are intentionally UNAUTHENTICATED (mint a session cookie for browsers). Do NOT add `Depends(forbid_integration_kind)` at router level — it would brick login. Swap per-route only on routes that already had `Depends(require_api_key)`.
Definition of done:
- Manual: /admin/keys/new flow works end-to-end on dev box (test client).
- Test: integration key gets 403 on `/me/history` GET and `/telemetry/dictation` POST.
- Test: `/me/login` GET and POST still work WITHOUT auth (router-level vs per-route attachment of `forbid_integration_kind` matters here).
- Test: `post_mint_user.kind == 'integration'` (after `set_kind` + `lookup_by_id` refresh; verifies the stale-User-dataclass-from-mint footgun).

---

**Task 11 (PHASE 4, depends on Phase 2 + Phase 3):** Tests.
Files:
- CREATE `server/tests/test_v1_transcriptions.py`
- CREATE `server/tests/test_v1_models.py`
- CREATE `server/tests/test_admin_keys.py`
- CREATE `server/tests/test_integration_kind_guards.py`
- CREATE deterministic fixtures via shell: 
  ```
  ffmpeg -f lavfi -i "sine=frequency=440:duration=1" -ar 16000 -ac 1 -y server/tests/fixtures/tiny.wav
  ffmpeg -i server/tests/fixtures/tiny.wav -ar 16000 -ac 1 -codec:a libmp3lame -b:a 64k -y server/tests/fixtures/tiny.mp3
  ffmpeg -i server/tests/fixtures/tiny.wav -ar 16000 -ac 1 -codec:a aac -b:a 64k -y server/tests/fixtures/tiny.m4a
  ffmpeg -i server/tests/fixtures/tiny.wav -ar 16000 -ac 1 -codec:a libopus -b:a 64k -y server/tests/fixtures/tiny.webm
  ```
  Commit binaries to repo; reproducible across machines.
Gotchas:
- Mock `ParakeetService.transcribe_with_alignment` to return canned `(text, ms, aligned_tokens)`.
- Test `x-request-id` set on ALL responses.
- Test that `OpenAI-Organization`, `OpenAI-Project`, `X-Stainless-*` headers don't break a request (smoke).
- Cookie-only auth on `/v1/audio/transcriptions` → 401 (via `client.cookies.set(...)` AND no Authorization header).
- Empty audio: mock returns `("", 0.0, [])`; expect `verbose_json` with `segments: []`.
- Audio too long: mock raises `AudioTooLongError`; expect 400 / `code: "audio_too_long"`.
- Test rate-limit auth runs once (mock counter).
- Test `/v1/models` `Cache-Control: no-cache, must-revalidate` header present.
- Test rate-limit 429 carries `Access-Control-Allow-Origin: *` header.
- Test `gpt-4o-transcribe-diarize` is NOT in `/v1/models` listing.
- Test integration key 403 on `/me/history`, `/telemetry/dictation`.
- Test `_resolve_token_user` 5 outcomes.
Definition of done:
- `cd server && uv run pytest tests/ -v` exits 0.

---

**Task 12 (PHASE 5, PARALLEL with Phase 4):** Documentation.
Files:
- CREATE `docs/OPENAI-COMPAT.md`
- MODIFY `docs/INTEGRATION-GUIDE.md` (SMART_FORMAT_MIN_WORDS=80 fix; new examples; /admin/keys pointer)
- MODIFY `docs/API.md` (rewrite /v1 section, point to OPENAI-COMPAT.md; /v1/models)
- MODIFY `docs/ADMIN.md` (new "Integration Keys (/admin/keys)" section)
- MODIFY `docs/OVERVIEW.md` (file→doc map for new files)
- MODIFY `README.md` (one-line OpenAI-compat callout near top; exact copy: `**OpenAI-compatible**: point any whisper-API client at `https://transcribe.integrateapi.ai/v1`. See [docs/OPENAI-COMPAT.md](docs/OPENAI-COMPAT.md).`)
OPENAI-COMPAT.md sections:
- Setup, Auth (Bearer only on /v1), Supported parameters (full table), Response formats (json/text/verbose_json/srt/vtt with full body examples), Headers (request + response), Models endpoint (5 models; note diarize excluded), Error codes table, File formats (wav/flac/ogg/aiff via libsndfile; mp3/m4a/mp4/webm/aac/mpeg via ffmpeg), Limits (25 MB, Cloudflare 100 MB edge), Concurrency model (single-thread executor; transcribes serialize), Rate limits (60/min/token; break-glass admin exempt; Retry-After honored), Tested clients matrix, Troubleshooting, What's not supported (translations, diarization, streaming, logprobs-emit), Getting an API key (pointer to /admin/keys).
Definition of done:
- All docs cross-link; no broken refs.

---

**Task 13 (PHASE 6, depends on Phase 2):** Verification script.
Files:
- CREATE `scripts/verify-openai-compat.sh`
Gotchas:
- Takes `BASE_URL` and `API_KEY` env vars; defaults to prod.
- Checks (each as a bash function with input/expected/assert):
  1. `text` response_format → 200 + Content-Type `text/plain` (no JSON wrapper)
  2. `json` → 200 + valid JSON with `text` field
  3. `verbose_json` → 200 + `task="transcribe"` + `language="english"` + `segments` array
  4. `srt` → 200 + body starts with "1\n00:00:" (cue numbering)
  5. `vtt` → 200 + body starts with "WEBVTT"
  6. mp3 roundtrip → 200 (assert HTTP status only — Parakeet on synthetic sine may return empty text; that's expected for tonal input)
  7. m4a roundtrip → 200
  8. `GET /v1/models` → 200 + 5 entries in `data[]`
  9. `GET /v1/models/whisper-1` → 200 + `id="whisper-1"`
  10. `GET /v1/models/gpt-4o-transcribe-diarize` → 404 `model_not_found`
  11. Bad bearer → 401 + OpenAI error envelope
  12. `OPTIONS /v1/audio/transcriptions` → 200 + `Access-Control-Allow-Origin: *`
  13. Rate-limit 429 carries ACAO header (set X-Forwarded-For to a sentinel IP; spam until 429)
  14. `GET /v1/models` returns `Cache-Control: no-cache, must-revalidate`
- For live verification against real Parakeet: assert HTTP status + structure, NEVER assert on text content (Parakeet may return empty for tonal sine fixtures).
- `--slow` flag adds rate-limit enforcement test (61 calls in 60s expects ≥1 429).
- Each check is a bash function returning 0/1; main runner accumulates failures, exits non-zero on any.
- Uses `server/tests/fixtures/tiny.{wav,mp3,m4a}` (committed binaries).
- Each check exits non-zero with diff on failure.
Definition of done:
- `bash scripts/verify-openai-compat.sh` against prod URL exits 0 with 12 ✓.

---

**Task 14 (PHASE 7, FINAL):** Deploy to Mac mini + e2e verify.
Goal:
- Apply migration in prod (already done in Task 1 via Supabase MCP — applies to prod project directly), pull on mini, restart launchd, run verification.
Steps:
- (a) **Pre-flight ordering check**: confirm `SELECT version FROM wispralt.schema_version ORDER BY version DESC LIMIT 1` returns 4 (Task 1 applied). If not 4, STOP — code that SELECTs `kind` would 503 the server.
- (b) Git commit + push (with explicit user approval — already authorized by user message for end-of-mission).
- (c) Via CRD + `/macmini paste` (gist transport):
  ```
  cd ~/wispralt/server && git pull && uv sync && \
    launchctl bootout gui/$(id -u)/co.wispralt.server && \
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/co.wispralt.server.plist
  ```
- (d) Wait 60s for warmup (longer than the 30s previous default to accommodate Cloudflare Tunnel repoint); `curl -fsSL https://transcribe.integrateapi.ai/healthz` (expect 200).
- (e) Mint a test integration key via `/admin/keys/new` on prod admin UI; copy plaintext.
- (f) Run `BASE_URL=https://transcribe.integrateapi.ai/v1 API_KEY=<test-key> bash scripts/verify-openai-compat.sh` (with `-H "Cache-Control: no-cache"` on /v1/models check, to bust any Cloudflare edge cache from the pre-deploy old shape).
- (g) Tag `git tag v0.5.0 && git push --tags`. Do NOT build a new DMG — no client code changed in this plan. The GitHub Release auto-generated from the tag is sufficient.
- (h) Revoke the test integration key.
Gotchas:
- DO NOT use `kickstart -k` — won't pick up `uv sync` deps.
- `$(id -u)` not `$UID` — more portable across login shells.
- Cloudflare Tunnel takes ~5-10s to repoint after launchd restart — wait before curl.
- If verification fails, capture mini logs via `tail ~/Library/Logs/WisprAlt/server.err.log` (use `/macmini paste` for quotes).
Definition of done:
- All 12 verification checks pass against prod URL.

### Integration Points

- Data / schema source of truth: `server/migrations/2026-05-20-v4-users-kind.sql`; applied via Supabase MCP.
- Entry points to extend: `main.py:80, 695-699, 860-874, lifespan` — new router, _KIND_MAP entries, CORS middleware install, rate-limit state init.
- Validation layer: per-route Pydantic + in-handler validation in `routes/v1_transcriptions.py`; `Depends(rate_limit_v1_per_token)`; `Depends(require_api_key_v1)`; `Depends(forbid_integration_kind)`.
- Domain / service layer: `dictate/parakeet.py` (new transcribe_with_alignment); `dictate/sync_decode.py` (new); `dictate/v1_response_builders.py` (new).
- User-facing / operator-facing surface: `routes/admin_ui.py` (new /admin/keys); 3 new templates; 2 modified templates.
- Shared types / export hubs: `users/store.py` (User.kind, UserRow.kind, helpers); `constants.py` (model IDs, version stamp).
- External / operational hooks: `scripts/verify-openai-compat.sh`.

## Validation

```bash
# Server lint + type check
cd server && uv run ruff check src/wispralt_server/ tests/ \
  && uv run mypy src/wispralt_server/ --ignore-missing-imports

# Server unit tests (offline, mocked parakeet)
cd server && uv run pytest tests/test_v1_transcriptions.py tests/test_v1_models.py tests/test_admin_keys.py tests/test_integration_kind_guards.py -v

# Full existing test suite — must not regress
cd server && uv run pytest -v
```

### Factuality Checks

- `Verified Repo Truths` uses `Fact / Evidence / Implication` for every bullet ✓
- Every negative claim includes `Search Evidence` ✓
- No proposal/future language in `Verified Repo Truths` ✓
- No placeholder/template strings remain ✓
- Every `MODIFY` path verified to exist ✓

### Manual Checks

- Scenario: Open WebUI configured against prod URL with a fresh integration key.
  Expected: Model picker populates with the 5 whisper/gpt-4o-transcribe models; transcription against `tiny.mp3` returns text.
- Scenario: Buzz subtitle app, SRT export from a 30s mp3.
  Expected: Valid `.srt` with multi-cue output (or one cue if Parakeet returns Hypothesis-only).
- Scenario: Browser-based UI on Chrome posts to /v1/audio/transcriptions.
  Expected: OPTIONS preflight 200 with CORS headers; POST 200 with ACAO.
- Scenario: openai-python `client.audio.transcriptions.create(...)` with `ThreadPoolExecutor(max_workers=8)` x 10 calls.
  Expected: All 80 calls succeed serially; rate limit kicks in around call ~60; ~20 calls return 429 with `Retry-After` (and ACAO header).
- Scenario: admin creates integration key, attempts to use it on `GET /me/history`.
  Expected: 403 / "Integration keys cannot access /me or /telemetry — use /v1 only".
- Scenario: corrupt mp4 (truncated file) uploaded.
  Expected: 400 / `code: "unsupported_file_type"`. SDK must NOT retry.
- Scenario: `bash scripts/verify-openai-compat.sh` against prod URL.
  Expected: exit 0; all 12 checks ✓.

## Open Questions

None.

## Final Validation Checklist

- [ ] No linting errors: `cd server && uv run ruff check src/wispralt_server/ tests/`
- [ ] No type errors: `cd server && uv run mypy src/wispralt_server/ --ignore-missing-imports`
- [ ] All 44 brief decisions addressed in the Tasks section
- [ ] `Verified Repo Truths` only contains checked facts with file:line evidence
- [ ] Every `MODIFY` path exists
- [ ] Migration v4 uses `INSERT (version, notes) ON CONFLICT (version) DO NOTHING` pattern
- [ ] `decode_to_pcm` is documented as SYNCHRONOUS; route wraps in `asyncio.to_thread`
- [ ] `_resolve_token_user` four-step state machine preserved
- [ ] CORS is OUTERMOST middleware; 429 responses carry ACAO
- [ ] `kind='integration'` users blocked on `/me/*` + `/telemetry/*` via `forbid_integration_kind` dep
- [ ] `gpt-4o-transcribe-diarize` excluded from `/v1/models` listing
- [ ] All test fixtures generated deterministically via ffmpeg lavfi sine
- [ ] launchctl uses `bootout` + `bootstrap` (not `kickstart -k`)
- [ ] Migration v4 applied BEFORE code that SELECTs `kind` is deployed (Task 14 step a)
- [ ] `_resolve_token_user` uses `db_pool` (not `users_pool`) and the postmortem-documented `(asyncpg.PostgresError, asyncpg.InterfaceError)` tuple
- [ ] Module-level `token_cache` preserved (not migrated to `app.state`)
- [ ] `forbid_integration_kind` attached PER-ROUTE on /me/*, never at router level (/me/login stays unauthenticated)
- [ ] Verbose_json segmentation algorithm uses time-gap primary + sliding-window sentence-end secondary (NOT single-token punctuation check)
- [ ] `gpt-4o-transcribe-diarize` returns 404 `model_not_found` (not 400)
- [ ] `verify-openai-compat.sh` asserts HTTP shape only on live Parakeet, not text content
- [ ] No unresolved factual blockers from review

## Deprecated / Removed Code

- `middleware/rate_limit.py:114` — remove `/v1/audio/transcriptions` from per-IP dictate bucket.
- `routes/v1_transcriptions.py:27-28` — `_SUPPORTED_FORMATS`/`_UNSUPPORTED_FORMATS` replaced by single `_ALL_FORMATS = {"json", "text", "verbose_json", "srt", "vtt"}`.
- Stale `SMART_FORMAT_MIN_WORDS=100` text in `docs/INTEGRATION-GUIDE.md` and `docs/API.md` → 80.

## Anti-Patterns to Avoid

- Don't mix verified repo facts with proposed changes.
- Don't add a `kind` parameter to `users_store.mint` — two-step `mint` + `set_kind`.
- Don't add `asyncio.Semaphore` around Parakeet inference — single-thread executor already serializes.
- Don't return 5xx for client mistakes — openai-python retries 408/409/429/5xx.
- Don't strip `transient: false` from segments.
- Don't emit `"en"` for `language` — must be `"english"`.
- Don't JSON-wrap `response_format=text` — plain text/plain only.
- Don't accept admin session cookie on `/v1/*`.
- Don't use module-level dict for rate-limit buckets — `app.state` for testability.
- Don't use `kickstart -k` for the mini restart — use `bootout`+`bootstrap`.
- Don't list `gpt-4o-transcribe-diarize` in `/v1/models` while also 400ing on it (use 404 `model_not_found` so semantics match the exclusion).
- Don't run `subprocess.run` directly inside an async route — wrap in `asyncio.to_thread`.
- Don't mutate `wispralt.schema_version` rows — INSERT only.
- Don't change `ParakeetService.transcribe(...)` signature — add `transcribe_with_alignment(samples)`.
- Don't chain `forbid_integration_kind` after `require_api_key_v1` — integration keys are EXACTLY the kind that /v1 is built for. The guard is for /me/* + /telemetry/* only.
- Don't attach `forbid_integration_kind` at router level on `routes/me.py` — `/me/login` is intentionally unauthenticated; router-level attach would break it.
- Don't add `Depends(require_api_key_v1)` to the /v1/audio/transcriptions route signature — it's already resolved transitively via `Depends(rate_limit_v1_per_token)` (FastAPI dep cache). Explicit chaining is harmless but invites future "for clarity" edits that confuse the auth-runs-once invariant.
- Don't build a new DMG with no client-side code changes — tag-and-push the v0.5.0 git tag only.
- Don't migrate `token_cache` to `app.state.token_cache` — it's a module-level singleton (`auth.py:48`) consumed by many call sites. Keep module-level.
- Don't broaden the Postgres exception catch beyond `(asyncpg.PostgresError, asyncpg.InterfaceError)` — see the 2026-05-17 postmortem cited in auth.py:174.

---

**Confidence score: 9.8/10** (round 1 → v2 at 9.7; round 2 → v3 at 9.8). All blockers from both reviewer rounds resolved with concrete code or explicit ordering invariants.

Sources of confidence:
- All 6 blockers from review round 1 resolved with concrete code.
- Concrete segmentation algorithm in pseudocode (was previously a TODO).
- Migration SQL matches v2/v3 pattern exactly.
- `decode_wav_bytes` shape mismatch fixed with explicit resample+downmix step.
- Auth refactor has explicit 5-outcome test plan.
- CORS ordering nailed down.
- Permission guard added for `/me/*` + `/telemetry/*` integration-key leak.
- Mac mini restart uses correct `bootout`+`bootstrap` pattern.
- All test fixtures deterministic and CI-safe.

Remaining 0.3 uncertainty:
- Live `parakeet-mlx` `AlignedToken.start`/`.end` field names unverified on dev box. Mitigation: graceful single-segment fallback when alignment isn't usable.
- CORS scoping decision — currently allow all `/v1/*` + non-v1 with allow_origin="*" globally for simplicity. If we later need stricter scoping (e.g., admin endpoints should NOT respond to cross-origin), wrap CORS in a path-prefix filter middleware.
