# Brief: OpenAI Whisper API compatibility — fully dialed, end-to-end, drop-in for any program

## Why

Today, `https://transcribe.integrateapi.ai/v1/audio/transcriptions` exists and partially mimics OpenAI's `/v1/audio/transcriptions`. It's been live since some prior session. The user wants to be able to point **any** OpenAI-Whisper-compatible third-party program (Buzz, MacWhisper, Bazarr, Open WebUI voice mode, OBS Whisper plugin, the `openai-python` / `openai-node` SDKs, etc.) at our endpoint by just swapping two env vars (`OPENAI_BASE_URL` + `OPENAI_API_KEY`) and have it Just Work. Today, that promise holds for the simplest clients only (those requesting `json` or `text` from a WAV/FLAC/OGG file). Anything that asks for `verbose_json`, `srt`, `vtt`, hits us with mp3/mp4/m4a/webm, or probes `/v1/models` first will fail — and that covers a majority of real-world Whisper clients.

The user also wants the admin UI to surface an explicit **"Add API Key" flow** alongside the existing **"Add Employee"** flow. Today only the latter exists; tokens are 1:1 with users and the only label for them is "employee," which is conceptually wrong for keys handed out to programs.

Operating principle for this work: end-to-end, fully verified, documented to the level where any future agent dropped into the repo can pick it up and continue without re-discovering anything.

## Context

### Current state of `/v1/audio/transcriptions` (verified live)

- **Route**: `server/src/wispralt_server/routes/v1_transcriptions.py:44` — `POST /v1/audio/transcriptions`. Wired in `main.py:860`.
- **Auth**: `Authorization: Bearer <wispralt-token>` accepted via `require_api_key` in `server/src/wispralt_server/auth.py:134-206`. Case-insensitive `Bearer` prefix. Cookie fallback (`wispralt_admin_token`) is also accepted, which is a **side effect** of the path=/ admin session cookie — flagged as a low-priority security concern for the third-party drop-in scenario.
- **Error envelope**: `server/src/wispralt_server/middleware/openai_errors.py` re-shapes errors only for paths starting with `/v1/`. Mapping: 401→`invalid_api_key`, 403→`forbidden`, 429→`rate_limit_exceeded`, other 4xx→`bad_request`, 5xx→`internal_error`.
- **Accepted params today**: `file` (required), `response_format` (`json`/`text` only — 422 on `srt`/`vtt`/`verbose_json`), `model` (default `whisper-1`, ignored; logged when unknown), `language`, `prompt`, `temperature` (all accepted, all ignored).
- **Size cap**: `OPENAI_COMPAT_SIZE_CAP = 25 MB` (`constants.py:3`), enforced at `v1_transcriptions.py:85-95`. Returns 413 with OpenAI envelope.
- **Rate limit**: `middleware/rate_limit.py:114-118` — `/v1/audio/transcriptions` shares a 60-req/60-sec **per-IP** bucket with `/transcribe/dictate`. Per-token granularity is NOT available today.
- **Smart formatting**: Intentionally NOT applied on `/v1` (comment at `v1_transcriptions.py:110-113`). Native `/transcribe/dictate` accepts `X-Smart-Format: true`; `/v1` ignores it.
- **Live verification**: `curl -X POST -H 'Authorization: Bearer not-a-real-key' https://transcribe.integrateapi.ai/v1/audio/transcriptions` returns the correct OpenAI-shaped 401 envelope. Route is reachable on prod (mini HEAD `9c03c4a`, v0.4.6).
- **Tests**: ZERO tests for `/v1` in `server/tests/`. Adjacent `/transcribe/dictate` tests cover the inference call indirectly but not the OpenAI-envelope plumbing.
- **Docs**: `docs/INTEGRATION-GUIDE.md` and `docs/API.md:309-351` exist but have one stale fact (claims `SMART_FORMAT_MIN_WORDS=100`; actual is 80).

### Current state of admin token UX

- **Routes**: `server/src/wispralt_server/routes/admin_ui.py:334-424`.
  - `/admin/users/new` (GET form + POST) — labeled "Add Employee," mints a user via `users_store.mint`.
  - `/admin/users/{id}/revoke` — mark revoked.
  - `/admin/users/{id}/mint` — token rotation (1:1 replace, NOT issue-additional).
- **Token cardinality**: Strictly 1:1 with users. There is no "service account," no "integration," no "key purpose," no expiry, no per-token scope. `wispralt.users.role` is `{"admin","employee"}`.
- **Success template** (`employee_added.html.j2`): shows the curl install command for the macOS client only — there is no presentation for "use this in a third-party OpenAI client."

### Current state of Parakeet (the underlying ASR)

- `server/src/wispralt_server/dictate/parakeet.py:35` — `MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v2"`, via `parakeet-mlx==0.5.1`.
- The library CAN emit `AlignedToken` objects with `.start`, `.end`, `.text` (docstring at `parakeet.py:97-111` confirms). The current code only reads `.text` and discards alignment. `DecodingConfig()` is called with no args — defaults only.
- Conclusion: word-level timestamps are achievable on the dictate path; this is currently unused capability. Verifying the exact `DecodingConfig` flag and the `AlignedToken` field shape is part of the implementation work.

### Current state of file format support on `/v1`

- `/v1` uses `parakeet_service.transcribe(audio_bytes)` which internally calls `decode_wav_bytes` (`audio.py:46`) — **libsndfile only**. That decoder handles WAV / FLAC / OGG / AIFF / AU / CAF.
- **It does NOT decode**: mp3, mp4, m4a, mpga, mpeg, webm.
- This is the single biggest functional gap: real OpenAI clients send mp3 and m4a routinely. We will 500 on those today.
- ffmpeg IS available on the server — `routes/transcribe_file.py` uses it for the async `/transcribe/file` path. We need to reuse that decode logic on the `/v1` sync path.

### Meeting transcription path (relevant: it produces real segments)

- `server/src/wispralt_server/meeting/` produces transcripts with `segments[]` containing `{start, end, text, speaker, words[]}` — exactly the shape we'd need for `verbose_json`. But this path is **async, batched** (submit job → poll → download). Not usable directly for a sync `/v1` response. We can borrow the segment shape definition and the SRT/VTT writers (`meeting/output.py:127-189`), but the inference path needs to come from Parakeet, not mlx-whisper.

### Research artifact

The full OpenAI Whisper API spec audit (every param, every response field, every error code, every quirk, every real client's behavior) was performed and is summarized in this brief. Sources cover platform.openai.com (mirrored via developers.openai.com), the openai-python SDK source, openai-node issue tracker, community threads about `transient` and the `language` long-form quirk, and the behavior of Groq, Deepgram, faster-whisper-server, MacWhisper, Buzz, OBS, Open WebUI, Bazarr, Pipecat. Highlights:
- 6 known model IDs real clients send: `whisper-1`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, `gpt-4o-mini-transcribe-2025-12-15`, `gpt-4o-mini-transcribe-2025-03-20`, `gpt-4o-transcribe-diarize`.
- `verbose_json` consumers tolerate unknown fields but require `segments[].{start,end,text}` at minimum. `transient: false` is undocumented but always emitted by OpenAI.
- `language` field in `verbose_json` is the **lowercase full English name** (`"english"`), not the ISO-639-1 code (`"en"`) — clones that emit the latter break.
- `text` response is `Content-Type: text/plain; charset=utf-8`, not JSON-wrapped.
- `srt` uses comma decimal separator (`00:00:02,500`); `vtt` uses period (`00:00:02.500`) plus the `WEBVTT` magic line.
- Almost no client probes `/v1/models` first — Open WebUI is the loud exception.
- Whisper-1 returns 400 on `stream=true`; only the gpt-4o-transcribe family streams. We should mirror this.
- `x-request-id` response header is expected by every client.
- Most clients hardcode `whisper-1` as the model string.

## Decisions

### Server: extend `/v1/audio/transcriptions` to handle the real-world client mix

1. **Add ffmpeg decode path on `/v1`** — reuse the decoder logic from `routes/transcribe_file.py` so mp3, m4a, mp4, mpeg, mpga, webm, aac all work. Sniff the format (libsndfile first; fall through to ffmpeg). Why: this is the single biggest gap for "any program." How to apply: keep the 25 MB cap; reject non-`audio/*`-and-non-`video/*` Content-Type with a clean OpenAI envelope.

2. **Add `verbose_json` response format** — investigate `parakeet-mlx` `DecodingConfig` to enable word-level alignment via `AlignedToken`. Synthesize `segments[]` by grouping aligned tokens on sentence boundaries (or simple silence gaps if Parakeet doesn't surface boundaries directly). Include the full segment schema: `id`, `seek` (we'll emit `0` as a constant — Whisper internals don't map to Parakeet, but presence matters), `start`, `end`, `text`, `tokens` (empty array — we don't have BPE token IDs), `temperature` (`0.0`), `avg_logprob` (omit or `0.0`), `compression_ratio` (`1.0`), `no_speech_prob` (`0.0`), `transient` (`false`). Top-level: `task: "transcribe"`, `language: "english"` (lowercase full word), `duration`, `text`, `segments`, optionally `words`. **Why**: this unblocks Buzz, MacWhisper, Bazarr, subgen, Pipecat — the entire subtitle-export tier. **How to apply**: if `parakeet-mlx` won't expose alignment, fall back to a single segment spanning the whole transcript with degenerate `start=0, end=duration`. Still valid `verbose_json`, just no segmentation. Document the degradation clearly.

3. **Add `srt` and `vtt` response formats** — reuse `meeting/output.py:127-189` writers, adapt to the in-memory segments produced by step 2. Comma decimal for SRT, period for VTT, `WEBVTT` magic line. `Content-Type: application/x-subrip` and `text/vtt` respectively.

4. **Add `timestamp_granularities[]` form param** — accept `["word"]`, `["segment"]`, or both. Require `response_format=verbose_json`; else 400 with OpenAI envelope (matches real OpenAI behavior).

5. **Add `GET /v1/models` and `GET /v1/models/{id}`** — static list with 6 entries: `whisper-1`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, `gpt-4o-mini-transcribe-2025-12-15`, `gpt-4o-mini-transcribe-2025-03-20`, `gpt-4o-transcribe-diarize`. All `owned_by: "wispralt"`, `created` = some fixed epoch. Why: Open WebUI and enterprise probers reject backends that don't expose this. How to apply: pure read-only handler, no DB, ~10 lines of code. Same auth + envelope as the rest of `/v1`.

6. **Reject `stream=true` on `whisper-1`** explicitly with 400 / `invalid_request_error` / `code: "streaming_unsupported"`. Don't implement SSE streaming on `/v1` in this round — we don't currently have a Parakeet streaming path that fits the OpenAI event shape. Document as "future work; native `/transcribe/dictate/stream` is the WisprAlt-native streaming path." **Why**: matching OpenAI's actual behavior (whisper-1 returns 400) is better than silently returning a single-chunk SSE — that breaks clients waiting for `transcript.text.delta`. **How to apply**: read `stream` form field; if truthy and model is `whisper-1` (or default), 400.

7. **Honor `include[]=logprobs` by silently no-op'ing** (since we don't have logprobs from Parakeet, and the field is optional in the response). Don't 400 — that breaks newer SDKs that always send it. Document as "accepted, ignored."

8. **Add OpenAI response headers**: at minimum `openai-version: 2026-05-20` (today's date as a stable version stamp), `openai-processing-ms: <int>`, `openai-model: whisper-1` (echo whatever the caller sent), plus the existing `x-request-id`. Skip `x-ratelimit-*` for now since rate limiting is per-IP not per-token; documenting a meaningful per-IP "remaining" value is confusing. **Why**: every real client logs `x-request-id`; the others are bonus polish that costs nothing. **How to apply**: set in the response object before return; add to error responses too.

9. **Tighten `Authorization` cookie fallback for `/v1`** — `auth.py:122-123` accepts the `wispralt_admin_token` cookie. For `/v1/*` paths, require `Authorization: Bearer` only (no cookie). Why: the cookie is for the admin web UI; a third-party drop-in client should never carry a `wispralt_admin_token` cookie, and accepting it broadens the attack surface for nothing. How to apply: add a `request_path` check in `_extract_bearer`, or split `require_api_key_v1` from `require_api_key`. Prefer the latter — clearer surface.

10. **Per-token rate limiting on `/v1`** — the per-IP shared bucket is wrong for the drop-in model. A user with one API key running 4 parallel jobs from one machine will hit the cap; meanwhile the limit doesn't actually protect us from abuse since the IP is the limit unit. Switch `/v1` to **per-token** (60 req/min per `user.id`, NOT per-IP). Native `/transcribe/dictate` stays per-IP. **Why**: clients running `openai-python` with concurrency will exceed per-IP trivially; per-token is the correct unit for "drop-in". **How to apply**: in `rate_limit.py`, switch the `/v1` branch to look up the bearer token's user ID after auth resolves (or do it in a post-auth dependency, sidestepping the middleware). Tradeoff: this means rate limiting happens after auth instead of before, so auth failures are not rate-limited. That's fine — invalid tokens get a fast 401 and don't burn inference resources.

### Server: validation polish

11. **Enforce `timestamp_granularities[]` requires `verbose_json`** — 400 with `invalid_request_error` / `code: "validation_failed"` (or `incompatible_parameters`) and clear message.

12. **Validate `model` against the known list** — if unknown, return 200 still (because real-world clients send variations and we want to be lenient), but log it. Already does this; keep behavior. Document explicitly.

13. **Accept and normalize `response_format` case** — already lowercased at `v1_transcriptions.py:62`. Keep.

14. **`temperature` range validation** — accept `0.0-1.0`, else 400. Match OpenAI behavior. Today this is ignored, which is fine (we don't use temperature). But validating the range prevents weird clients from sending `temperature=2.0` and getting a successful response that wasn't actually controlled.

### Admin UI: separate "Add API Key" flow

15. **Add `wispralt.users.kind` column** with values `'employee'` (default, backfill) and `'integration'`. Why: tokens are still 1:1 with users (no change to mint/rotate/revoke), but the *type* of user is now distinguished. **How to apply**: Supabase migration via MCP; backfill all existing rows to `'employee'`; default for new rows is also `'employee'`.

16. **New routes**:
   - `GET /admin/keys` — list all `kind='integration'` users, similar to `/admin/users` but separate.
   - `GET /admin/keys/new` — form: `program_name` (display_name), `notes` (optional).
   - `POST /admin/keys/new` — mints a user with `kind='integration'`, `role='employee'` (under the hood — same auth scope), `label = "key-" + slug(program_name)`, `display_name = program_name`. Shows the plaintext token AND the OpenAI client snippet (`OPENAI_BASE_URL=...`, `OPENAI_API_KEY=...`) on success.
   - `POST /admin/keys/{id}/revoke` — same revoke path; reuse existing `users_store.revoke`.
   - `POST /admin/keys/{id}/rotate` — same rotate path; reuse `users_store.rotate`.

17. **`/admin/users` filter**: hide `kind='integration'` users by default from the "Employees" list. They appear only under `/admin/keys`. Why: keeps the mental model clean — employees are humans, keys are programs.

18. **Templates**: new templates `keys.html.j2`, `add_key.html.j2`, `key_added.html.j2`. `key_added.html.j2` shows:
   - The plaintext token (one-time view, copy button)
   - A code block with `export OPENAI_BASE_URL=https://transcribe.integrateapi.ai/v1` + `export OPENAI_API_KEY=<token>`
   - A "What now?" link to `docs/INTEGRATION-GUIDE.md`
   - No curl install command (that's for the macOS client only)

19. **Admin overview tile**: add a small "Integration keys: N" tile alongside the existing user count tile. Why: easy at-a-glance visibility for the operator.

### Documentation

20. **New `docs/OPENAI-COMPAT.md`** — single canonical reference. Covers:
   - Setup (env vars)
   - Every accepted parameter, with type, default, validation, and example
   - Every response format with full body shape (json, text, verbose_json, srt, vtt)
   - Every error code we can emit, with HTTP status and example envelope
   - `/v1/models` endpoint
   - Known limitations vs upstream OpenAI (no streaming on whisper-1, no diarization on /v1, no translations, no logprobs, no token usage object)
   - "Tested with" matrix: openai-python, openai-node, curl, Buzz, MacWhisper, Open WebUI, Bazarr — each with version + minimal example
   - Troubleshooting (auth, file size, format compatibility, rate limit)
   - Pointer to `/admin/keys` for getting a token

21. **Update `docs/INTEGRATION-GUIDE.md`** — fix the stale `SMART_FORMAT_MIN_WORDS` reference. Add `verbose_json`/`srt`/`vtt` examples. Add pointer to `/admin/keys` flow. Add file-format support note (now: WAV/FLAC/OGG/MP3/M4A/MP4/MPEG/MPGA/WEBM).

22. **Update `docs/API.md` /v1 section** — full re-write of lines 309-351. Include all new params, all new response formats, all new error codes, /v1/models entry.

23. **Update `docs/OVERVIEW.md`** file→doc map:
   - `routes/v1_transcriptions.py` → `OPENAI-COMPAT.md`, `INTEGRATION-GUIDE.md`, `API.md`
   - `routes/v1_models.py` (new) → `OPENAI-COMPAT.md`, `API.md`
   - `routes/admin_ui.py` (additions for `/admin/keys`) → `ADMIN.md` (new section) + `OPENAI-COMPAT.md`

24. **Update `docs/ADMIN.md`** with a new section on `/admin/keys` — how to mint, rotate, revoke; what the user sees post-mint.

25. **README badge update** — add a tiny "OpenAI-compatible /v1 audio API" line near the top.

### Tests

26. **New `server/tests/test_v1_transcriptions.py`**:
   - Auth: 401 with proper envelope on missing/bad bearer
   - Auth: cookie-only auth REJECTED on `/v1` (after decision #9)
   - Size cap: 413 at >25 MB
   - `response_format` matrix: json, text, verbose_json, srt, vtt — all return 200 with correct Content-Type and body shape
   - `response_format=invalid` → 422
   - `timestamp_granularities=word` without `verbose_json` → 400
   - `stream=true` → 400 (whisper-1 model)
   - `include[]=logprobs` → silently accepted
   - `model` unknown → 200 (lenient), logged
   - `temperature` out of range → 400
   - File format: send tiny mp3, m4a, mp4, webm → 200 (ffmpeg path)
   - Roundtrip: real Parakeet (or mocked at the service boundary) with a known WAV → text matches expected substring
   - `x-request-id` present on all responses (success and error)
   - `openai-processing-ms`, `openai-version`, `openai-model` headers present

27. **New `server/tests/test_v1_models.py`**:
   - `GET /v1/models` → 200, list with 6 entries
   - `GET /v1/models/whisper-1` → 200, single model object
   - `GET /v1/models/nonexistent` → 404 with OpenAI envelope, `code: "model_not_found"`
   - Auth required (401 without bearer)

28. **New `server/tests/test_admin_keys.py`**:
   - `GET /admin/keys/new` requires admin auth
   - `POST /admin/keys/new` creates a `kind='integration'` user
   - Created key works against `/v1/audio/transcriptions`
   - Revoke flow works
   - Listing on `/admin/users` excludes `kind='integration'` rows
   - Listing on `/admin/keys` excludes `kind='employee'` rows

### End-to-end verification

29. **Real-world client roundtrip script** at `scripts/verify-openai-compat.sh`:
   - Uses `openai-python` SDK directly
   - Tests: text, json, verbose_json, srt, vtt — each with a tiny canned WAV in `tests/fixtures/`
   - Tests: mp3, m4a roundtrip (via ffmpeg path)
   - Tests: `/v1/models` listing
   - Tests: bad-auth path returns the OpenAI envelope
   - Exits 0 on full pass, non-zero with diff on any miss
   - This script becomes the smoke test for any future agent picking up the project

30. **Mac mini deploy** — once the server changes land, deploy via the existing CRD + `/macmini paste` gist pattern. Run the verification script against `https://transcribe.integrateapi.ai/v1` AFTER deploy.

31. **Manual UI verification** — log into `/admin/`, click "Add API Key", create a test key, copy it, run the verification script with that key, then revoke it.

### Migration plan (one-time DB change)

32. **Migration**: add `kind` column. Safe, additive, default `'employee'`. Use Supabase MCP `apply_migration`. Roll back: drop column (harmless).

### Additional gotchas surfaced after deeper review (not in original list)

33. **Concurrent Parakeet inference is not obviously safe.** MLX models are typically not reentrant — two concurrent `/v1/audio/transcriptions` calls landing on the same `ParakeetService.transcribe(...)` may corrupt internal state or simply serialize implicitly with surprising latency. Today we have no explicit lock, semaphore, or queue around inference. **Decision**: add an `asyncio.Semaphore(1)` (or `2` if benchmarks show MLX handles it) around the actual `model.generate(...)` call inside `_sync_transcribe`. Document the concurrency model in `docs/OPENAI-COMPAT.md` ("concurrent requests are serialized; 60 req/min per token is the rate cap; throughput is bounded by model wall-clock"). **Why**: without this, a single user pasting their key into 4 parallel jobs (very common with `openai-python` `ThreadPoolExecutor` workflows) can crash the model or hang the server. **How to apply**: wrap the call in `dictate/parakeet.py` not in the route — applies uniformly to /v1 and /transcribe/dictate. Don't apply to meeting jobs (those have their own async runner).

34. **Audio resampling on the ffmpeg path must enforce 16 kHz mono float32.** Parakeet requires that exact shape (`parakeet.py:36` — `TARGET_SR = 16000`). mp4/m4a/webm sources are almost always 48 kHz stereo. The new ffmpeg decode path must include explicit resample + downmix flags (`-ar 16000 -ac 1 -f f32le` or equivalent), not just decode. If we forget this, mp3/m4a files will either fail outright or produce garbage transcripts. **Decision**: build the ffmpeg invocation explicitly with these flags; test against a multi-channel 48 kHz m4a fixture; document the canonical ffmpeg command in `docs/OPENAI-COMPAT.md` "How decode works" section so future agents don't break it.

35. **CORS / OPTIONS preflight for browser-based clients.** Open WebUI's voice mode runs in a browser; so do most web-based Whisper wrappers. Without `Access-Control-Allow-Origin` on `/v1/*` responses + a working OPTIONS preflight, browser clients fail before the first POST. **Decision**: enable `CORSMiddleware` scoped to `/v1/*` paths only, allow methods `GET, POST, OPTIONS`, allow headers `Authorization, Content-Type, OpenAI-Organization, OpenAI-Project, X-Stainless-*`, allow origin `*` (this is an auth-gated API; relying on Bearer for security, not Origin). **Why**: silent breakage class — devs spend hours debugging "why does my web app fail with no error" before discovering CORS. Document in OPENAI-COMPAT.md.

36. **Empty-audio + audio-too-long-after-decode behavior.** Two cases:
    - **Empty/silent audio**: Parakeet returns `""`. Real OpenAI returns `{"text": ""}` with 200. Today our path probably returns 200 with `{"text": ""}` already — verify and add a test. For `verbose_json` it must be `{"task":"transcribe","language":"english","duration":0.0,"text":"","segments":[]}`.
    - **Too-long-after-decode**: a 25 MB mp3 can decode to hours of audio. The 25 MB byte cap is enforced pre-decode; the duration cap (`dictation_max_duration_s = 900`) is enforced inside Parakeet at the sample level (`parakeet.py:46`). We need to return `400 / invalid_request_error / code: "audio_too_long"` with the OpenAI envelope when this trips — not let Parakeet raise a generic exception that maps to our existing `transcription_failed` 500. **Decision**: catch the duration-exceeded condition in `parakeet.py` or in `v1_transcriptions.py` and convert to the proper 400.

37. **OpenAI SDK auto-retry semantics — don't return 5xx for client errors.** `openai-python` default retry policy: retries 5xx and 429 with exponential backoff up to 2 times. If we return 500 for "corrupt audio file" (currently `v1_transcriptions.py:100-108` maps any inference exception to 500/server_error), the SDK will retry the same broken upload 3 times — wasting bandwidth and bunding requests against the rate limit. **Decision**: split the exception handling. `CorruptAudioError` from `audio.py:47-48` → 400 / `invalid_request_error` / `code: "invalid_audio_data"`. Decode/format errors from ffmpeg → 400 / `code: "unsupported_file_type"`. Only true unexpected exceptions (MLX crash, OOM) stay as 500. **Why**: getting status code semantics right is foundational to client-side reliability; mis-categorized errors trigger silent retry storms.

38. **Accept and silently ignore the modern OpenAI SDK header set.** `openai-python` ≥ 1.0 sends `OpenAI-Organization`, `OpenAI-Project`, `X-Stainless-Lang`, `X-Stainless-Package-Version`, `X-Stainless-OS`, `X-Stainless-Arch`, `X-Stainless-Runtime`, `X-Stainless-Runtime-Version`, plus the `user` form field. None of these should cause rejection. FastAPI's default behavior is permissive on unknown headers, but we should verify our auth path doesn't strip-and-fail on multi-header edge cases. **Decision**: add a test that sends a full openai-python-shaped request (all the headers + `user` form field) and confirms 200. No code change expected — this is verification + a regression test.

39. **Cloudflare body-size and custom error page interactions.** Cloudflare on the integrateapi.ai zone has its own request body size limit (100 MB on the current tier per `docs/DEPLOYMENT-NOTES.md`, well above our 25 MB cap). But if a client uploads >100 MB, Cloudflare returns its own HTML 413, NOT our OpenAI envelope. Clients parsing JSON will choke. **Decision**: document this in `docs/OPENAI-COMPAT.md` "limits" section explicitly: "uploads larger than the documented 25 MB cap will be rejected by our edge with a non-JSON 413 — your client should size-check locally first." Not a code change, but the most common silent breakage cause and worth a clear note.

40. **`x-request-id` echo on errors (including auth failures).** Today we set it on success and on the in-handler `_openai_error` path, but verify the middleware-level errors (HTTPException from `require_api_key`, RequestValidationError) also carry it. The `openai_errors.py:49` reads `request.state.request_id`, which depends on `_RequestIdMiddleware` (`main.py:760-772`) running BEFORE the exception is raised. Ordering matters. Add a test: bad auth must return both the OpenAI envelope AND a non-empty `x-request-id` response header.

41. **Concurrent integration-key rate-limit fairness.** Per decision #10 we switch `/v1` to per-token rate limit (60 req/min per `user.id`). But the existing `/transcribe/dictate` bucket is per-IP. If a user logs in from one machine with native client (per-IP bucket) AND uses `/v1` from the same machine (per-token bucket), they could double-spend. **Decision**: keep the buckets separate. Document explicitly: "Native dictation and /v1 traffic count against DIFFERENT rate limits." This is actually a feature for power users.

42. **`/v1/models` must NOT be cached at edge.** Add `Cache-Control: no-cache, must-revalidate` to the `/v1/models` response so that when we add/remove model IDs (e.g., when the gpt-4o-mini-transcribe-2026-XX snapshot drops), clients see the update immediately. Otherwise Cloudflare may cache for hours. Tiny but easy-to-forget detail.

43. **Audit `usage_events` for `/v1`.** `main.py:699` writes `kind="v1_dictate"`. Verify: are `bytes_in`, `duration_ms`, `status` populated correctly for the new code paths (verbose_json, srt, vtt)? Verify the `user_id` is the integration-key user. This data feeds `/admin/usage` and the admin tile counts; if it's broken, operators won't see integration-key activity. Add an integration test that creates a key, hits /v1, then queries usage_events.

44. **Test the OpenAI SDK in CI with VCR-style cassettes.** The verification script (decision #29) runs against live infra. Tests in `server/tests/` need to run without network. **Decision**: in `server/tests/test_v1_transcriptions.py`, monkeypatch `openai.OpenAI` (or use httpx.MockTransport) for unit tests, and mark the live-roundtrip script as a separate smoke target (not pytest). This keeps `pytest server/tests/` fast and offline-safe.

## Rejected Alternatives

- **Implement `stream=true` SSE on `/v1`** — rejected for this round. We don't have a Parakeet streaming path that maps cleanly to OpenAI's `transcript.text.delta` event shape. Real OpenAI 400s on `whisper-1 + stream=true` so mirroring that is conformant. WisprAlt's native `/transcribe/dictate/stream` already exists for the WisprAlt client; it doesn't need to leak into the `/v1` surface. Defer to future work if a real client demands it (none do today).

- **Implement `/v1/audio/translations`** — rejected. Parakeet is English-only; faking translation by identity-passthrough on English-in/English-out is awkward and breaks if anyone tries with Spanish input. Document as "not supported" in `OPENAI-COMPAT.md` and return 400 with `code: "endpoint_not_supported"` if called.

- **Implement `/v1/audio/transcriptions` with diarization (`response_format=diarized_json`)** — rejected. We don't have Parakeet diarization; the native `/transcribe/meeting` path uses pyannote + mlx-whisper, which is asynchronous and not a fit for a sync `/v1` response. Return 400 on `model=gpt-4o-transcribe-diarize` with a clear error pointing to `/transcribe/meeting`.

- **`include[]=logprobs` actually emitting logprobs** — rejected. Parakeet doesn't expose per-token logprobs through `parakeet-mlx`. Silently no-op is good enough — clients that always send it (newer openai-python defaults) won't fail.

- **Per-user usage quotas and `x-ratelimit-*` response headers** — deferred. Per-IP rate limiting is what we have today; reporting "remaining" against a per-IP bucket on a per-token response would be misleading. Add real per-token quotas in a future round.

- **Server-side WAV chunking for very long files** — rejected. OpenAI's 25 MB cap handles this implicitly. Anything longer goes through `/transcribe/file` (async, ffmpeg + meeting pipeline).

- **Adding a new `role` value** (e.g. `'integration'`) instead of a `kind` column — rejected. `role` controls authorization (admin vs employee). The integration-key distinction is about *what* the token is for, not *what it can do*. They should be employee-scoped for permissions but separated in the UI. `kind` is the cleaner axis.

- **Allowing multiple tokens per user** — rejected. Stays 1:1 for now. The "Add API Key" flow just creates a new (kind='integration') user. No data model change beyond the kind column.

- **Removing the cookie auth path entirely** — rejected. The admin web UI relies on it. Just scope the cookie out of `/v1/*`.

## Where Reasoning Clashed

- **`segments[].seek`, `tokens`, `avg_logprob`, `compression_ratio` in `verbose_json`**: these are Whisper-internals that don't map to Parakeet. Two valid positions:
  1. Emit them with placeholder values (`0`, `[]`, `0.0`, `1.0`) so strictly-typed deserializers (some Pydantic-based clients) accept the response. This is what the OpenAI Python SDK does — it tolerates missing fields but warns. Strict TypeScript clients with full Pydantic-equivalent types break on missing fields.
  2. Omit them entirely. Most clients tolerate it. Cleaner semantically.

  Recommend (1) — emit placeholders. Worth the 30 lines of code to maximize drop-in compatibility. Documented as "WisprAlt does not produce these signals — values are constants."

- **Per-IP vs per-token rate limit unit on `/v1`**: a reasonable engineer could argue per-IP is fine because it's simple and the use case is "one user, one key, one machine." But the drop-in promise breaks when a user runs `openai-python` with `Pool(concurrency=8)` and gets 429ed after 60 requests in a minute despite their key having a much higher implied budget. Going per-token is the right call for the contract; the cost is that rate limiting happens post-auth. Worth flagging in the plan review.

- **`role` vs `kind` on the users table**: there's a real argument that an integration key SHOULD have reduced permissions (e.g., no `/admin/*` access, even if technically employee scope today doesn't grant that). I'm choosing the simpler `kind` column for now, but a future-tightening item is "explicitly deny `kind='integration'` tokens from cookie-auth-only paths if any are ever added." Worth a comment in code.

## One Thing to Do First

Before any code, **run `/v1/audio/transcriptions` end-to-end against the live mini with a real `.wav` file and a real key** to establish a baseline that the existing path actually transcribes correctly. The route reachability check earlier in this discussion confirmed 401 envelope shape, but never confirmed an actual successful transcription path. If even that's broken, everything else in this brief is built on sand. The simplest form: `curl -X POST -H "Authorization: Bearer <key>" -F file=@tests/fixtures/hello.wav -F response_format=json https://transcribe.integrateapi.ai/v1/audio/transcriptions` and confirm `{"text": "..."}` comes back.

## Direction

Take the existing `/v1/audio/transcriptions` from "barely-compatible shim" to "production-grade drop-in." The work is server-side feature expansion (verbose_json/srt/vtt/models endpoint/ffmpeg decode/per-token rate limit/response headers), an admin UX split ("Add API Key" alongside "Add Employee" via a `kind` column on users), and a comprehensive documentation pass (new `docs/OPENAI-COMPAT.md` plus updates to API, INTEGRATION-GUIDE, ADMIN, and OVERVIEW). Ship with full test coverage (three new test files, ~30+ test cases) and an end-to-end verification script that doubles as the future smoke test. Deploy to the mini and confirm against the real prod URL with the openai-python SDK before declaring done. No streaming on /v1 in this round; no translations; no diarization — all explicitly deferred with clear "not supported" responses where a client tries them.
