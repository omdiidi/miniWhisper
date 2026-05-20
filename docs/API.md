---
title: API Reference
---

# API Reference

Complete HTTP API contract for the WisprAlt server. Implemented in `server/src/wispralt_server/routes/`.

---

## Authentication

Most routes require:

```
Authorization: Bearer <token>
```

The bearer is sha256-hashed and resolved against `wispralt.users` via the asyncpg pool, with a 60-second in-process `TokenCache` in front. See [ARCHITECTURE.md → Auth (multi-token)](ARCHITECTURE.md#auth-multi-token) for the full path.

**Unauthenticated endpoints** (probe-style + admin login, no API key required):
- `GET /healthz` — liveness probe
- `GET /readyz/dictation` — dictation-pipeline readiness probe
- `GET /readyz/meeting` — meeting-pipeline readiness probe
- `GET /admin/login` and `POST /admin/login` — admin UI login form (chicken-and-egg: must be reachable without the cookie it sets)
- `GET /me/login` and `POST /me/login` — employee portal login form (Phase 2; same chicken-and-egg reason)

These are intentionally open so Kubernetes-style probes, Cloudflare health checks, and external monitoring can poll them without API credentials. The probes expose only ready-flag booleans + free RAM in MB — no user data, no audio, no model output.

**Bearer required:**
- `POST /transcribe/dictate`, `POST /transcribe/meeting`, `GET /transcribe/meeting/{id}`, `GET /transcribe/meeting/{id}/download/{fmt}`, `DELETE /transcribe/meeting/{id}`
- `POST /v1/audio/transcriptions` (OpenAI-compatible drop-in)
- `GET /me`, `PATCH /me`, `GET /me/insights` (Phase 2 — employee self-view)
- `GET /me/history`, `GET /me/history/{kind}/{row_id}`, `DELETE /me/history/{kind}/{row_id}`, `POST /me/history/{kind}/{row_id}/restore`, `GET /me/history/{kind}/{row_id}/download/{fmt}` (Plan A — personal transcript archive)
- `GET /me/dictations/last` (v0.5.0 — caller's most recent non-deleted dictation; powers the "Copy last dictation" menubar button)
- `POST /telemetry/cloud-dictation` (Plan A — **Bearer-only**, cookie auth is explicitly rejected with 401)
- `GET /metrics`
- All `/admin/*` routes **except** `/admin/login`. The admin UI also accepts a `wispralt_admin_token` cookie (set by `POST /admin/login`) as a fallback for browser navigation.
- `/admin/*` (except login) additionally require `role='admin'` — an employee-role token gets **403**.

**Optional client-version header (always-accepted, never required):**

Every `/transcribe/*` route and `/transcribe/dictate` accept an
`X-WisprAlt-Client-Version` header. The Swift client sets it on every
server-bound request (format `"<short>+<build>"`, e.g. `"0.3.1+1"`). The
value is recorded with the persisted transcript on the row (`jobs.client_app_version`
for meeting/file, `dictations.client_app_version` for dictation) so the
Phase 2 weekly-insights cron can group captured data by shipped build.
Missing → stored as NULL. Capture is unconditional and transparent; the
header is informational only and never affects routing, auth, or rate
limits. For chunked uploads the version is captured at `/init` into
`meta.json` and re-read at `/finalize` so an in-flight client upgrade
doesn't change attribution. See [ARCHITECTURE.md →
Transcript persistence](ARCHITECTURE.md#transcript-persistence) for the
storage + 90-day retention sweep details.

**Additional auth validation:**
- A missing or invalid token returns **401**.
- An empty token after the `Bearer ` prefix returns **401**.
- Multiple `Authorization` headers in one request return **400** `"Multiple Authorization headers not allowed"`.
- When Postgres is unreachable AND the bearer doesn't match the break-glass env-var hash, the server returns **503** `"Auth temporarily unavailable"` rather than 401, so the operator can distinguish "wrong key" from "DB down".

---

## Rate Limits

Enforced by `middleware/rate_limit.py` (per-IP, in-memory rolling window):

| Route group | Limit |
|---|---|
| `POST /transcribe/dictate` | 60 requests per 60-second window (configurable: `DICTATE_RATE_PER_MIN`) |
| `POST /transcribe/meeting` | 4 requests per 3600-second window (configurable: `MEETING_RATE_PER_HOUR`) |
| `GET /readyz/*`, `GET /healthz` | 120 requests per 60-second window (configurable: `PROBE_RATE_PER_MIN`). Cloudflare's typical 5–10s health-check cadence sits well under this; the cap exists to bound probe-flood damage on the unauthenticated probe endpoints. |
| `POST /telemetry/*` | 10 requests per 60-second window (configurable: `telemetry_per_min`). Each batch carries up to 200 cloud-fallback dictations, so the ceiling is 2000 dictations/minute per IP. |

Exceeded limits return **429** with a `Retry-After` header (value = window duration in seconds).

**Client IP extraction:** When deployed behind Cloudflare Tunnel, rate limits use the real client IP from the `CF-Connecting-IP` header (set by Cloudflare, cannot be spoofed by clients). Falls back to the leftmost `X-Forwarded-For` entry, then the TCP remote address.

---

## Endpoints

### `GET /healthz`

**Auth:** None required.

Liveness probe. Always returns 200. Used by Cloudflare Tunnel health checks.

**Response 200:**
```json
{ "status": "ok" }
```

---

### `GET /readyz/dictation`

**Auth:** None required.

Readiness probe for the dictation (Parakeet) endpoint.

**Response headers:**

| Header | When present |
|---|---|
| `X-Dictation-Degraded: true` | A meeting job is currently running (`app.state.meeting_active_flag == True`). Dictation still works but unified memory is under pressure. |

**Response 200** — Parakeet is warm and ready:
```json
{ "status": "ok" }
```

**Response 503** — Parakeet model not yet loaded:
```json
{ "status": "not_ready", "detail": "Parakeet model not loaded" }
```

---

### `GET /readyz/meeting`

**Auth:** None required.

Readiness probe for the meeting pipeline. Meeting models (mlx-whisper + Pyannote) load lazily on the first meeting job — see [ARCHITECTURE.md → MeetingPipeline](ARCHITECTURE.md#components). This endpoint reports whether the load has happened yet, but **does not gate on it**: the server returns 200 as long as RAM is sufficient, because the lazy loader is wired and will succeed when invoked.

**Contract change (2026-04-29):** previously this endpoint returned 503 until eager bootstrap completed. It now returns 200 from server start onward when RAM is sufficient regardless of whether models are warm. Any external monitor that gated on 503-until-warm must be updated.

**Response body fields (always present, both on 200 and 503):**

| Field | Type | Notes |
|---|---|---|
| `available_mb` | int | `psutil.virtual_memory().available` in MiB. |
| `models_warm` | bool | `True` iff mlx-whisper + Pyannote are resident. False on cold start. |
| `models_loading` | bool | `True` iff a lazy load is currently in flight (derived from `_load_lock.locked() and not models_warm`). |

**Response 200** — RAM sufficient (≥ 2 GiB), models warm:
```json
{ "status": "ok", "available_mb": 9216, "models_warm": true, "models_loading": false }
```

**Response 200** — RAM sufficient (≥ 2 GiB), models cold (server just started; first meeting will pay the 5–30s lazy-load cost):
```json
{ "status": "ok", "available_mb": 9216, "models_warm": false, "models_loading": false }
```

**Response 503** — insufficient RAM (< 2 GiB available):
```json
{ "status": "not_ready", "detail": "Insufficient available memory", "required_mb": 2048, "available_mb": 1800, "models_warm": false, "models_loading": false }
```

**Interpretation matrix:**

| `status` | `models_warm` | `available_mb` | What it means |
|---|---|---|---|
| 200 | true | ≥ 2048 | Steady state — submit and go. |
| 200 | false | ≥ 2048 | Ready but cold — first meeting will pay 5–30s load. |
| 503 | true | < 2048 | RAM tight — `runner.submit_or_429` will reject with 429 until RAM frees. |
| 503 | false | < 2048 | Cold AND tight — first meeting will likely fail OOM guard before loading. Free RAM first. |

---

### `POST /transcribe/dictate`

**Auth:** Bearer required.

Transcribe a short audio clip using the warm Parakeet model.

**Request:** `multipart/form-data`

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | UploadFile | Yes | Must have `Content-Type: audio/*`. WAV preferred; any soundfile-supported format accepted. |

**Optional headers:**

| Header | Values | Default | Notes |
|---|---|---|---|
| `X-Smart-Format` | `true` / `1` / `yes` (case-insensitive) | absent ⇒ off | When set to a truthy value AND the server has `OPENROUTER_API_KEY` configured AND `app.state.mercury_client` is initialized AND the raw transcript is at or above `SMART_FORMAT_MIN_WORDS` (default 100), the transcript is post-processed by OpenRouter Mercury 2: punctuation, casing, paragraph breaks, light filler removal ("um"/"uh"/repeats), and bullet-list formatting where the speaker is enumerating. Meaning is preserved (no rephrasing, no summarization, no new content). A length-window safety check (cleaned ∈ [0.7×, 1.10×] of raw word count) falls back to raw on suspicious output. Fail-soft: any timeout or error returns the raw text and `smart_formatted: false`. WisprAlt-specific extension; not part of OpenAI compatibility. The native macOS client sets this header when the user toggles "Smart formatting" in Settings. The `/v1/audio/transcriptions` shim never sets it. |
| `X-Client-Dedup-Id` | UUID v4 string | absent ⇒ off | **Optional, additive (Phase 5b streaming-dictation).** When present, must be a syntactically valid UUID v4. Forwarded to `JobStore.insert_dictation(..., client_dedup_id=...)`; the row participates in the `idx_dictations_client_dedup` partial-unique index, so a subsequent retry that sends the same UUID is silently de-duped (`ON CONFLICT(client_dedup_id) DO NOTHING`). Used by the safety-buffer fallback in `DictationStreamSession` to guarantee at most one persisted row per utterance when a streaming finalize loses to its local fallback. When the header is **absent**, behavior is bit-identical to the pre-streaming contract: the column is left NULL and the partial-unique index never applies. Malformed UUIDs are rejected at the validation boundary (422). |

**Audio format flexibility (server-side resampling):**

- Sample rate: any rate libsndfile / librosa accepts (8 kHz, 16 kHz, 44.1 kHz, 48 kHz, etc.). Server resamples to 16 kHz internally before Parakeet inference.
- Bit depth: Int16, Int24, Int32, Float32 — all auto-converted to Float32 by `soundfile.read`.
- Channel count: mono or multi-channel. Server averages all channels down to mono via `np.mean(axis=1)` before Parakeet.
- The macOS client (`DictationRecorder`) uploads native-rate Int16 PCM (typically 48 kHz mono); custom integrators can send anything in this range.

**Size limit:** `Content-Length` must be ≤ `MAX_UPLOAD_BYTES` (default 2 GiB). Checked from the header before reading the body.

**Minimum duration:** Audio shorter than ~100 ms (post-resample, < `MIN_SAMPLES = 1600` at 16 kHz) returns `text=""` with `duration_ms=0.0` and HTTP 200 — not an error. Very short clips are silently no-op'd at the model layer.

**Response 200:**
```json
{
  "text": "Hello world.",
  "model_id": "mlx-community/parakeet-tdt-0.6b-v2",
  "duration_ms": 143.7,
  "smart_formatted": false
}
```

`duration_ms` is wall-clock Parakeet inference time (excludes queue wait).
`smart_formatted` is `true` only when `X-Smart-Format` was truthy AND the raw
transcript was at or above `SMART_FORMAT_MIN_WORDS` (default 100) AND the
Mercury cleanup actually replaced the text (OpenRouter responded within the
1500 ms budget, output passed the length-window safety check, no errors).
It's `false` for every other case, including header absent, header non-truthy,
raw word count below the threshold, server missing `OPENROUTER_API_KEY`,
Mercury timeout, length-window safety check failed, or Mercury HTTP error.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer token |
| 413 | Upload size exceeds `MAX_UPLOAD_BYTES` |
| 415 | `Content-Type` is not `audio/*` |
| 422 | Audio bytes cannot be decoded (corrupt file). Includes `LibsndfileError` and `RuntimeError` raised by `soundfile.read` — the decode boundary in `parakeet.py:_sync_transcribe` converts both to `CorruptAudioError` and the route handler maps them to 422. Regression-locked by `server/tests/test_dictate_corrupt_audio.py`. |
| 429 | Rate limit exceeded (60 req/min) — includes `Retry-After: 60` header |

---

### `POST /transcribe/dictate/stream/{session_id}/chunk/{index}`

**Auth:** Bearer required. **Break-glass / single-key admin callers are rejected (403)** — streaming dictation requires a real `api_key_id` for per-user session bookkeeping. **Opt-in, experimental** — see [ARCHITECTURE.md → Streaming dictation](ARCHITECTURE.md#streaming-dictation-opt-in-experimental) for the end-to-end design.

Ingest one mid-utterance, silence-cut WAV chunk into an active streaming session. The first chunk for a given `session_id` opens a server-side `StreamingSession`; subsequent chunks reuse it. Chunks run on the same single-instance Parakeet executor as `/transcribe/dictate` — no second model is loaded.

**Path params:**

| Param | Type | Notes |
|---|---|---|
| `session_id` | UUID v4 string | Client-generated. Syntactically validated (422 on malformed). The same `session_id` is reused for every chunk + the finalize call. |
| `index` | integer ≥ 0 | Zero-based monotonic chunk index. Out-of-order arrival is tolerated server-side (first arrival wins the slot via `partial_texts[index]`). |

**Request:** `multipart/form-data` with a single `file` field; `Content-Type: audio/wav` (or `audio/*`).

**Response 202:**

```json
{ "received_index": 0, "queue_depth": 1 }
```

`queue_depth` is the count of currently-pending Parakeet tasks for this session (≥1 because the just-ingested chunk is counted). The route returns as soon as the chunk is enqueued — actual Parakeet work runs asynchronously and lands in `partial_texts` when complete.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer token. |
| 403 | Break-glass admin caller, OR the bearer resolves to an `api_key_id` that does not match the session's owner (recorded at first-chunk open). |
| 409 | `per_user_busy` — this `api_key_id` already owns a different active session. Either reuse its `session_id` or wait for it to finalize/expire. |
| 410 | Session is no longer active (status ∈ {finalizing, done, aborted, expired}). The client should fall back to single-shot `/transcribe/dictate` with the safety-buffer WAV. |
| 413 | Cumulative voiced audio for this session exceeds the 270 s mid-chunk ceiling. (Finalize allows up to 300 s.) |
| 415 | `Content-Type` is not `audio/*`. |
| 422 | `session_id` is not a valid UUID v4 OR audio bytes cannot be decoded. |
| 429 | Per-session pending-task queue depth exceeded (defends against runaway clients). |
| 503 | `capacity_exceeded` — the global 2-session cap is already saturated. Retry after the other sessions drain, or fall back to single-shot. |

---

### `POST /transcribe/dictate/stream/{session_id}/finalize`

**Auth:** Bearer required; same per-user-owner gate as `/chunk`. **Opt-in, experimental.**

Close out a streaming session. The server waits up to 15 s for all pending chunk tasks to drain, joins their texts in index order, runs Mercury cleanup ONCE on the joined string (same `SMART_FORMAT_MIN_WORDS` threshold + length-window safety rail as the single-shot path), persists the row to `dictations`, and returns the final text. The chunked-receive route stops accepting new chunks for this `session_id` (returns 410) the moment finalize starts.

**Path params:** `session_id` (UUID v4) — same as `/chunk`.

**Request:** `multipart/form-data`

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | UploadFile | Yes | The tail WAV — the trailing speech segment that was still being accumulated when FN was released. `Content-Type: audio/*`. May be empty (zero-byte) if the speaker happened to finish exactly on a silence cut. |
| `smart_format` | boolean | No | Default `false`. Equivalent to the `X-Smart-Format` header on `/transcribe/dictate`; routed through the same Mercury client. |
| `client_dedup_id` | UUID v4 | Yes | The same UUID the client will reuse on the safety-buffer fallback if this finalize loses. Persisted to `dictations.client_dedup_id`; combined with the `idx_dictations_client_dedup` partial-unique index, guarantees one row per utterance. |
| `speech_started_at` | float (Unix epoch, sub-second precision) | Yes | The local wall-clock time the *user* began speaking (NOT FN press) — sent so the server can compute end-to-end latency from speech start to text injection without depending on local clock-sync. Sub-second precision is preserved (don't round to int). |

**Response 200:**

```json
{
  "text": "Hello world.",
  "model_id": "mlx-community/parakeet-tdt-0.6b-v2",
  "duration_ms": 213.4,
  "smart_formatted": false
}
```

`duration_ms` on streaming rows is **cumulative** per-chunk Parakeet inference time (sum of N chunks), not single-pass wall-clock. See [ARCHITECTURE.md → Known limitations](ARCHITECTURE.md#known-limitations) for the semantic drift versus single-shot dictation rows. The response envelope is otherwise byte-identical to `/transcribe/dictate` so the client's existing decoder is unchanged.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer token. |
| 403 | Bearer's `api_key_id` does not match the session's owner. |
| 409 | `gap_detected` — `partial_texts` contains a `None` entry between two ingested chunks (a chunk POST landed for index N but index N−1 never arrived). The client should fall back to single-shot. |
| 410 | Session is not in `status="active"` (already finalizing, done, aborted, or expired). |
| 413 | Cumulative voiced audio + tail exceeds 300 s. |
| 415 | Tail `Content-Type` is not `audio/*`. |
| 422 | `session_id` or `client_dedup_id` is not a valid UUID v4, OR tail audio cannot be decoded. |
| 502 | `inference_failed` — Parakeet raised on the tail or on a still-pending chunk during the 15 s drain. |
| 504 | `timeout` — pending chunks did not drain inside 15 s. Session marked `aborted`; client safety-buffer fallback kicks in. |

---

### `POST /v1/audio/transcriptions`

**Auth:** Bearer required.

OpenAI-compatible drop-in transcription endpoint. **For setup and SDK usage examples, see [INTEGRATION-GUIDE.md](INTEGRATION-GUIDE.md).** This section documents the wire-level constraints.

**Request:** `multipart/form-data`. Field names follow the OpenAI Audio API spec.

| Field | Required | Notes |
|---|---|---|
| `file` | Yes | Audio bytes; any libsndfile-decodable format. |
| `model` | No | Accepted; ignored. Always routed to Parakeet TDT 0.6B v2. Unknown values are logged for admin visibility. |
| `response_format` | No | `json` (default) or `text`. `srt`, `vtt`, and `verbose_json` return **422** — Parakeet doesn't emit per-segment timestamps on the dictate path; use `/transcribe/meeting` for those. |
| `language`, `prompt`, `temperature` | No | Accepted; ignored. |

**Constraints:**

- **Size cap: 25 MB** (matches OpenAI's documented limit; `OPENAI_COMPAT_SIZE_CAP` in `constants.py`). Returns **413** with the OpenAI error envelope.
- **Sync only** — blocks until Parakeet returns. Use `/transcribe/meeting` for long audio.
- **Smart formatting is never applied here.** The shim deliberately ignores `X-Smart-Format`. Third-party callers that want cleanup hit `/transcribe/dictate` directly.
- Per-IP rate limit: shares the 60 req/min window with `/transcribe/dictate`.

**Response 200 (`response_format=json`):**
```json
{ "text": "Hello world." }
```

**Response 200 (`response_format=text`):** plain `text/plain` body, no JSON wrapper.

**Errors** (OpenAI envelope shape, see [INTEGRATION-GUIDE.md](INTEGRATION-GUIDE.md#auth-failure-shape)):

| Code | `code`                       | Condition |
|------|------------------------------|-----------|
| 401  | `invalid_api_key`            | Missing/invalid/revoked bearer |
| 413  | `file_too_large`             | Body exceeds 25 MB cap |
| 422  | `unsupported_response_format`| `srt` / `vtt` / `verbose_json` requested |
| 422  | `invalid_response_format`    | Value isn't a recognized format string |
| 429  | (rate-limit envelope)        | 60 req/min window exceeded |
| 500  | `transcription_failed`       | Parakeet raised an exception (unexpected) |

Errors include `error.request_id` when the observability middleware has attached one — quote it in support requests.

Source: `server/src/wispralt_server/routes/v1_transcriptions.py`.

---

### `GET /me`

**Auth:** Bearer required (any role).

Return the calling user's own profile. Each user can only read their own row — there is no path parameter.

**Response 200:**
```json
{
  "label": "alice@example.com",
  "display_name": "Alice",
  "role": "employee",
  "created_at": "2026-04-15T19:32:14+00:00",
  "last_seen_at": "2026-04-27T17:01:09+00:00"
}
```

| Field | Type | Notes |
|---|---|---|
| `label` | string | Operator-visible identifier (typically email). Set at mint time; not user-editable. |
| `display_name` | string \| null | Self-managed friendly name. `null` until the user fills in the first-launch sheet. |
| `role` | string | `"admin"` or `"employee"`. |
| `created_at` | ISO-8601 string | Token mint time. |
| `last_seen_at` | ISO-8601 string \| null | `MAX(usage_events.ts)` for this user, or `null` if no usage yet. |

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing/invalid bearer |
| 404 | User row genuinely missing (should not happen for an authed token; indicates DB drift) |
| 503 | Postgres pool unavailable |

---

### `PATCH /me`

**Auth:** Bearer required (any role).

Update the calling user's own `display_name`. The `label` and `role` are not user-editable; admin must change those via the admin UI.

**Request body** (`application/json`):

```json
{ "display_name": "Alice" }
```

| Field | Type | Notes |
|---|---|---|
| `display_name` | string \| null | 1–40 chars after trim; no control characters (newline, tab, NUL, etc.). Pass `null` to clear. Server-side validation mirrors the SQL CHECK constraint added by `2026-04-27-v2-display-name.sql`. |

**Response 200:** same shape as `GET /me`, reflecting the new `display_name`.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing/invalid bearer |
| 422 | `display_name` length out of range or contains control chars |
| 503 | Postgres pool unavailable |

The token cache is **not** invalidated on `display_name` change because the auth `User` object only carries `(id, label, role)` — `display_name` is fetched fresh from `wispralt.users` each time the client opens its Identity section.

Source: `server/src/wispralt_server/routes/me.py`.

---

### `GET /me/dictations/last`

**Auth:** Bearer required (employee or admin token via `Authorization: Bearer <token>`).

Returns the caller's most recent dictation as JSON. Powers the menubar
"Copy last dictation" button (v0.5.0). The row is filtered by
`deleted_at IS NULL` so a soft-deleted dictation is not returned; tie-break
on identical `created_at` is `id DESC`.

**Response 200** (`application/json`):

```json
{ "id": "4217", "text": "<dictation transcript>", "created_at": 1747700123.456 }
```

| Field | Type | Notes |
|---|---|---|
| `id` | string | The `dictations.id` row id, `CAST(id AS TEXT)` server-side so the value decodes cleanly into the Swift `LastDictationResponse.id: String` shape. |
| `text` | string | Raw transcript text (no smart-format reapplication). |
| `created_at` | number | Epoch seconds (float). |

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing / invalid bearer. |
| 403 | Caller is the break-glass admin (`user.id < 0`) — break-glass identities have no personal dictation history. |
| 404 | Caller has zero non-deleted dictations. |
| 503 | SQLite job store transiently unavailable. |

Source: `server/src/wispralt_server/routes/me.py` +
`JobStore.get_most_recent_dictation(api_key_id)` in
`server/src/wispralt_server/jobs/store.py`.

---

### `GET /me/login`

**Auth:** None required.

Renders the employee token-paste form (`me_login.html.j2`). Chicken-and-egg counterpart to `/admin/login` — must be reachable without the cookie it sets. Returns `200` with `text/html`.

Source: `server/src/wispralt_server/routes/me.py` (Phase 2).

---

### `POST /me/login`

**Auth:** None required (this endpoint is what produces the auth cookie).

Validate a pasted token and set the session cookie. Mirrors `POST /admin/login` but rejects break-glass admin tokens at this surface so the break-glass identity stays scoped to emergency admin use.

**Request:** `application/x-www-form-urlencoded`

| Field | Type | Notes |
|---|---|---|
| `token` | string | 64-char hex bearer token. Validated through the same 3-stage lookup as admin login: `TokenCache` → asyncpg `wispralt.users` → break-glass env-var hash. |

**Response 303** — token valid (non-break-glass). Redirects to `/me/insights` by default, or to the value of the optional `?next=` query parameter when it's a **relative path under `/me/*`** (open-redirect guard — absolute URLs, scheme-prefixed values, and paths outside `/me/*` are silently ignored and the redirect falls back to `/me/insights`). The menubar "Open My Dictations" button (v0.5.0) uses `?next=/me/history` so first-time employees land directly on their history page after token-paste. Sets `wispralt_admin_token` cookie with `path="/"`, `HttpOnly`, `Secure`, `SameSite=Strict`, `max-age=8h`. The `path="/"` scoping lets the same cookie cover both `/admin/*` and `/me/*` surfaces; admin login was simultaneously broadened to set the cookie with `path="/"` as well.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Invalid / revoked token — re-renders `me_login.html.j2` with an error message. |
| 403 | Token resolved to the break-glass admin identity — rejected at this surface with an explanatory error. |
| 503 | Postgres pool unavailable. |

See [ARCHITECTURE.md → Data portal](ARCHITECTURE.md#data-portal) for the rationale behind sharing the cookie between admin + employee surfaces.

Source: `server/src/wispralt_server/routes/me.py` (Phase 2).

---

### `GET /me/insights`

**Auth:** Bearer required (any role, but break-glass admin is 303'd to `/admin/data`).

Per-employee weekly insights page. Returns the most recent `weekly_insights` row with `scope='person'` for the calling user, plus a time-range stats grid (transcript volume, word count, dictation/meeting split) for the selected window.

**Query parameters:**

| Field | Type | Default | Notes |
|---|---|---|---|
| `range` | enum | `7d` | One of `today`, `7d`, `30d`, `90d`, `1y`, `all`. Drives the stats grid window only — the weekly digest card always shows the most recent completed ISO week. |

**Response 200** — full page. `Content-Type: text/html` rendering `me_insights.html.j2`.

**Response 200 (HTMX partial)** — when the request carries `HX-Request: true`, the handler returns only the `_stats_grid_partial.html.j2` fragment for in-place swap. Use this for time-range tab clicks; the full page is only needed on first navigation.

**Response 303** — caller is break-glass admin (`user.id < 0`). Redirects to `/admin/data` because break-glass identities have no per-user transcript history to summarize.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer. |
| 422 | `range` is not one of the accepted values. |
| 503 | Postgres pool unavailable. |

The hallucination disclaimer ("AI-generated, may be inaccurate") is rendered inline on each insight card — see [ARCHITECTURE.md → Weekly insights](ARCHITECTURE.md#weekly-insights) for the two-tier scrub policy that backs it.

Source: `server/src/wispralt_server/routes/me.py` (Phase 2).

---

### `GET /me/history`

**Auth:** Bearer required (any role, but break-glass admin is 303'd to `/admin/data`).

Personal transcript archive — the calling user's own dictations + meetings + custom file transcriptions in one chronological list. Filterable by time range, kind, and free-text search. Per-leg cursor pagination keeps heavy-on-one-side pages from starving the other. Backed by `JobStore.transcripts_in_range_filtered` (UNION of `dictations` + `jobs`, both filtered on `api_key_id` AND `deleted_at IS NULL`).

**Query parameters:**

| Field | Type | Default | Notes |
|---|---|---|---|
| `range` | enum | `30d` | One of `today`, `7d`, `30d`, `90d`, `1y`, `all`. Reuses `insights.timewindow.epoch_for_range`. |
| `kind` | enum \| empty | unset | Either `dictation` or `meeting`. Empty string from a `<select>` is normalized to "no filter". |
| `search` | string | unset | Free-text LIKE filter on the transcript body. Strings shorter than **3 chars after `.strip()`** are silently dropped to keep the scan bounded. |
| `dict_cursor` | opaque | unset | Base64-urlsafe encoded `(created_at, row_id)` tuple for the dictations leg. Malformed → silently restart pagination. |
| `jobs_cursor` | opaque | unset | Same shape, for the jobs (meeting/file) leg. |

**Response 200** — full page (`me_history.html.j2`).

**Response 200 (HTMX nav)** — when `HX-Request: true` is present and no cursor was sent, returns `_me_history_body.html.j2` (the body fragment for in-place swap).

**Response 200 (HTMX Load-more)** — when a cursor was sent, returns `_me_history_page.html.j2` (the next batch of `<tr>` rows + the updated Load-more button). Page size is `settings.history_page_size` (default **50**) per leg.

**Response 303** — caller is break-glass admin (`user.id < 0`). Redirects to `/admin/data`.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer. |
| 422 | `range` is not one of the accepted values. |
| 503 | Postgres pool unavailable. |

Source: `server/src/wispralt_server/routes/me.py` (Plan A).

---

### `GET /me/history/{kind}/{row_id}`

**Auth:** Bearer required. Ownership-checked.

Render one row's expanded (default) or compact (`?compact=1`) partial. Unknown `kind`, unknown `row_id`, or a `row_id` owned by another user all uniformly return **404** so we never leak existence to non-owners.

**Path parameters:**

| Field | Type | Notes |
|---|---|---|
| `kind` | enum | `dictation` or `meeting`. Anything else → 404. |
| `row_id` | string | Job UUID for `meeting`, integer row id for `dictation`. |

**Query parameters:**

| Field | Type | Default | Notes |
|---|---|---|---|
| `compact` | bool | `false` | When `true`, return `_me_history_row.html.j2` (collapsed). Otherwise return `_me_history_row_expanded.html.j2` with the full transcript text + Delete + Download buttons. |

**Response 200** — partial HTML fragment ready for HTMX swap-in.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer. |
| 404 | Unknown kind, unknown row_id, soft-deleted, or owned by another user. |

Source: `server/src/wispralt_server/routes/me.py` (Plan A).

---

### `DELETE /me/history/{kind}/{row_id}`

**Auth:** Bearer required + CSRF token. Ownership-checked.

Soft-delete one owned row. Flips `deleted_at = NOW()` on the underlying row; downstream read paths (`/me/history`, `compute_user_stats`, the weekly-insights cron) all filter `deleted_at IS NULL`, so the row disappears from every surface. Reversible via the `/restore` route below until the 90-day TTL sweep zeros the transcript text.

**Request:** `application/x-www-form-urlencoded`

| Field | Type | Notes |
|---|---|---|
| `csrf_token` | string | Double-submit value matched against the cookie set by the prior `GET /me/history*` render. Constant-time compare via `hmac.compare_digest`. |

**Response 200** — body is the OOB delete fragment `<tr id="row-{kind}-{row_id}" hx-swap-oob="delete"></tr>`. HTMX removes the row from the table without re-fetching the page.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer. |
| 403 | CSRF token missing or mismatched. |
| 404 | Unknown kind, unknown row_id, already-deleted, or owned by another user. |

Source: `server/src/wispralt_server/routes/me.py` (Plan A).

---

### `POST /me/history/{kind}/{row_id}/restore`

**Auth:** Bearer required + CSRF token. Ownership-checked.

Restore one soft-deleted row by clearing `deleted_at`. Returns the compact row partial (`_me_history_row.html.j2`) so HTMX can drop the row back into the table at its original position.

**Request:** Same `csrf_token` form field as `DELETE`.

**Response 200** — `_me_history_row.html.j2` partial.

**Errors:** Same as `DELETE /me/history/{kind}/{row_id}`.

Source: `server/src/wispralt_server/routes/me.py` (Plan A).

---

### `GET /me/history/{kind}/{row_id}/download/{fmt}`

**Auth:** Bearer required. Ownership-checked.

Download one owned transcript as plain text or JSON. Returns a streaming response with `Content-Disposition: attachment; filename="{kind}-{iso-date}-{row_id}.{fmt}"`.

**Path parameter `fmt`:** `txt` or `json`. Anything else → 404.

**Response 200:**

| `fmt` | `Content-Type` | Body |
|---|---|---|
| `txt` | `text/plain; charset=utf-8` | The joined transcript text. Falls back to `transcript_text` if `text` is unset. |
| `json` | `application/json` | The full row as a JSON object (all columns; `json.dumps(row, default=str)`). |

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer. |
| 404 | Unknown kind, unknown fmt, unknown row_id, soft-deleted, or owned by another user. |

Source: `server/src/wispralt_server/routes/me.py` (Plan A).

---

### `GET /admin/data`

**Auth:** Bearer required + `role='admin'`. Router-level `require_admin` + `_require_db_pool` deps mean employee tokens get 403 and a degraded Postgres pool gets 503 — never an `AttributeError` deeper in the handler.

Admin Data tab. Renders the team `weekly_insights` row (most recent completed ISO week) on top, plus a per-user leaderboard underneath. `?user_id=N` drills into one employee using the same card layout the employee self-view (`me_insights.html.j2`) renders, so admin and employee see identical surfaces.

**Query parameters:**

| Field | Type | Default | Notes |
|---|---|---|---|
| `range` | enum | `7d` | Same set as `GET /me/insights`. Drives the stats grid window. |
| `user_id` | integer | unset | When present, scope the page to that employee's `'person'`-scope insight + their stats. Without it, the page shows the team insight + per-user leaderboard. |

**Response 200** — full page (`data.html.j2`).

**Response 200 (HTMX partial)** — when the request carries `HX-Request: true`, the handler returns only the `_stats_grid_partial.html.j2` fragment for in-place swap.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer. |
| 403 | Token is valid but role is not `admin`. |
| 404 | `user_id` was supplied but no such user exists in `wispralt.users`. |
| 422 | `range` is not one of the accepted values. |
| 503 | Postgres pool unavailable. |

Source: `server/src/wispralt_server/routes/admin_data.py` (Phase 2).

---

### `POST /telemetry/cloud-dictation`

**Auth:** Bearer required. **Bearer-only** — a cookie-only request with no `Authorization` header is rejected with **401** even if the shared `wispralt_admin_token` cookie would normally satisfy auth. This stops a browser-CSRF abuse where a malicious site that holds the session cookie triggers ingest from a logged-in browser the user didn't initiate.

Ingest a batch of cloud-fallback dictations from a Swift client. The macOS app enqueues every OpenRouter-served dictation locally (see [ARCHITECTURE.md → Cloud-fallback telemetry sync](ARCHITECTURE.md#cloud-fallback-telemetry-sync) for the queue + drain flow) and POSTs in batches once the mini is reachable again. Idempotent via `client_dedup_id` + the partial unique index on `dictations.client_dedup_id`.

**Request:** `application/json`

```json
{
  "dictations": [
    {
      "client_dedup_id": "1f5b0b71-f8e3-4f6f-9b21-2f6d7c2e9e02",
      "text": "Hello world, this was dictated while the mini was offline.",
      "dictated_at": 1747526471.0,
      "word_count": 11,
      "client_app_version": "0.3.1+1"
    }
  ]
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `dictations` | array | Yes | 1–200 items per batch. |
| `dictations[].client_dedup_id` | string (UUID) | Yes | 36-char `^[0-9a-fA-F-]{36}$`. A fresh UUIDv4 per dictation; the same id must be used on every retry of the same dictation. |
| `dictations[].text` | string | Yes | 1–200,000 chars. Whitespace-only entries are silently skipped. |
| `dictations[].dictated_at` | number | Yes | UTC epoch seconds. Bounds-checked to `[now − 365d, now + 5min]`; out-of-range entries are logged and skipped. |
| `dictations[].word_count` | integer \| null | No | Optional; defaults to `len(text.split())` when null. |
| `dictations[].client_app_version` | string \| null | No | Up to 64 chars. Stored on the row for the same Phase 2 attribution as origin-served dictations. |

**Response 200:**

```json
{ "inserted": 1, "received": 1 }
```

| Field | Description |
|---|---|
| `inserted` | Rows that actually landed (excludes `ON CONFLICT DO NOTHING` dedup conflicts and skipped entries). |
| `received` | Items that passed pydantic validation. |

A repeat batch with the same `client_dedup_id`s returns `{inserted: 0, received: N}` — the client uses this to confirm the batch is fully drained.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing bearer header (including the cookie-only case described above). |
| 403 | Caller is the break-glass admin (`user.id < 0`) — break-glass has no user row to attribute dictations to. |
| 422 | Body fails pydantic validation (malformed UUID, batch too large, text too long, etc.). |
| 429 | Per-IP telemetry rate limit (10 batches per 60-second window) exceeded. Includes `Retry-After: 60`. |
| 503 | Postgres pool unavailable. |

Source: `server/src/wispralt_server/routes/telemetry.py` (Plan A).

---

### `POST /transcribe/meeting`

**Auth:** Bearer required.

Submit a 2-channel WAV for background transcription. Returns immediately with a job ID; poll `GET /transcribe/meeting/{job_id}` for status.

**Request:** `multipart/form-data`

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | UploadFile | Yes | 2-channel 16kHz Float32 WAV (ch1=mic, ch2=system audio). |
| `Content-MD5` header | string | Yes | Base64-encoded MD5 of the WAV body. Server verifies integrity after streaming to staging. |
| `Content-Length` header | integer | Recommended | Enables fast pre-flight size check before body is read. |

**Response 202:**
```json
{ "job_id": "550e8400-e29b-41d4-a716-446655440000", "status": "pending" }
```

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer token |
| 413 | `Content-Length` exceeds `MAX_UPLOAD_BYTES` |
| 422 | WAV header invalid, or `Content-MD5` mismatch (file corrupt in transit) |
| 429 | A meeting job is already in progress OR available RAM < 2 GiB. Includes `Retry-After: 60` header. Body: `{"error": "...", "retry_after_s": 60}` |
| 507 | Insufficient storage: free disk < 1.5× upload size |

---

### `GET /transcribe/meeting/{job_id}`

**Auth:** Bearer required.

Poll job status. The client should poll every 5 seconds.

**Response 200:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "done",
  "mode": "remote",
  "error": null,
  "formats": ["json", "srt", "txt", "vtt"],
  "outputs": {
    "json": "/transcribe/meeting/550e8400-e29b-41d4-a716-446655440000/download/json",
    "srt":  "/transcribe/meeting/550e8400-e29b-41d4-a716-446655440000/download/srt",
    "txt":  "/transcribe/meeting/550e8400-e29b-41d4-a716-446655440000/download/txt",
    "vtt":  "/transcribe/meeting/550e8400-e29b-41d4-a716-446655440000/download/vtt"
  }
}
```

| Field | Present when |
|---|---|
| `status` | Always. Values: `pending`, `running`, `done`, `failed`. |
| `mode` | When `status == "done"`. Values: `remote`, `in_person`. |
| `error` | When `status == "failed"`. Human-readable error string. |
| `formats` | When `status == "done"`. Always `["json", "srt", "txt", "vtt"]`. Kept for backward compatibility. |
| `outputs` | When `status == "done"`. Maps each format name to its download URL path. **Preferred** over `formats` for new clients. |
| `attempts` | Always. Integer. Number of times this job has been set to `running`. Incremented each attempt. |

**Retry policy:** Jobs that fail are automatically retried up to 3 times by the runner. After 3 failed attempts the job is marked `failed` with `error: "max retries exceeded"` and no further attempts are made.

**Lazy model load on first meeting:** the very first meeting job after a server restart triggers `pipeline._ensure_models_loaded()` (5–30s to load mlx-whisper + Pyannote into RAM). During this window the job's `status` is `running` — the load wall-clock is included in the running duration, not surfaced as a separate state. Subsequent meetings see no extra latency. Clients can pre-flight via `GET /readyz/meeting` (`models_warm: true` means the next meeting will skip the load).

**Response 404** — job not found.

---

### `GET /transcribe/meeting/{job_id}/download/{fmt}`

**Auth:** Bearer required.

Stream a completed output file.

**Path parameter `fmt`:** `json`, `srt`, `vtt`, or `txt`.

**Response 200** — streaming file response with appropriate `Content-Type`:

| Format | Content-Type |
|---|---|
| `json` | `application/json` |
| `srt` | `application/x-subrip` |
| `vtt` | `text/vtt` |
| `txt` | `text/plain` |

**Errors:**

| Code | Condition |
|---|---|
| 400 | Unknown format string |
| 404 | Job not found or not yet complete (`status != "done"`) |
| 410 | Job is `done` but output file missing from disk (e.g. manually deleted) |

---

### `DELETE /transcribe/meeting/{job_id}`

**Auth:** Bearer required.

Delete a job record and all associated output files. Called by the client after it has successfully downloaded all four formats.

**Response 204** — success (idempotent: returns 204 even if job does not exist).

The handler deletes `{job_id}.json`, `{job_id}.srt`, `{job_id}.vtt`, and `{job_id}.txt` from the job's `output_dir`, then removes the database row.

---

### `POST /transcribe/file`

**Auth:** Bearer required.

Submit any audio or video container for transcription. The server runs ffprobe + ffmpeg to extract a canonical 16 kHz PCM WAV before queuing the job. Returns immediately with a job id; poll `GET /transcribe/meeting/{job_id}` for status (same poll route as `/transcribe/meeting`).

**Request:** `multipart/form-data`

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | UploadFile | Yes | Source container. Allowed extensions: `.m4a .mp3 .mp4 .mov .m4v .wav .aac .flac .opus .ogg .webm .caf .aiff`. |
| `mode` | string | No | `file` (default — single-speaker) or `meeting` (diarized). Explicit form field; replaces the old channel-count heuristic. |
| `Content-Length` header | integer | Recommended | Enables pre-flight 413 + disk-gate (free < `Content-Length × 2 → 507`). |

**Response 202:** `{ "job_id": "...", "status": "pending" }`

**Errors:** 413 (over `MAX_UPLOAD_BYTES`), 415 (unsupported extension), 422 (mode invalid OR ffprobe rejected the source), 429 (job already running), 503 (RAM <4 GiB), 507 (disk).

---

### `POST /transcribe/file/chunked/init`

**Auth:** Bearer required. **Files: >50 MB or any file when chunking is preferred.**

Open a chunked upload to bypass Cloudflare's 100 MB request-body cap on free / pro / business plans. The server returns an `upload_id` and a chunk size; the client then POSTs each chunk to `/transcribe/file/chunked/{upload_id}/{chunk_index}` and finally calls `/transcribe/file/chunked/{upload_id}/finalize`. The assembled file is processed by the SAME pipeline that handles single-shot `/transcribe/file` uploads — the poll route is unchanged.

**Request:** `application/json`

```json
{
  "mode": "file",
  "total_bytes": 524288000,
  "chunk_count": 10,
  "original_filename": "meeting-2025-05-13.m4a"
}
```

| Field | Type | Notes |
|---|---|---|
| `mode` | string | `file` or `meeting`. Same enum as `POST /transcribe/file`. |
| `total_bytes` | integer | Size of the full file. Must be ≤ `min(MAX_UPLOAD_BYTES, 4_000_000_000)` — the 4 GB hard ceiling keeps the finalize concat inside Cloudflare's 100 s proxy timeout window. |
| `chunk_count` | integer | `1 ≤ chunk_count ≤ 1000`. Client computes as `ceil(total_bytes / 50 MiB)`. |
| `original_filename` | string | Used to recover the source extension for the assembled staging file. Suffix must be in the same allowlist as `POST /transcribe/file`. |

**Response 200:**

```json
{ "upload_id": "abc-22-char-token", "chunk_size": 52428800 }
```

`upload_id` is a `secrets.token_urlsafe(16)` value (22 URL-safe chars). The server records the caller's API-key user id in the upload's metadata; subsequent chunk + finalize calls MUST present a bearer token resolving to the same user id (403 otherwise). Break-glass / single-key clients cannot use chunked upload — they must fall back to single-shot `POST /transcribe/file`.

**Errors:** 403 (anonymous / break-glass caller), 413 (`total_bytes` over limit), 415 (extension), 422 (`chunk_count` out of range), 503 (RAM <4 GiB), 507 (disk).

---

### `POST /transcribe/file/chunked/{upload_id}/{chunk_index:int}`

**Auth:** Bearer required. **Owner must match the user that ran `/init`.**

Upload a single chunk's raw bytes.

**Request:** `application/octet-stream` (raw chunk body, NOT multipart).

| Header | Required | Notes |
|---|---|---|
| `Content-Length` | Yes | Required. Server rejects (411) if missing and rejects (400) if bytes-written ≠ declared length. |
| `Authorization` | Yes | `Bearer <token>`. |

`chunk_index` is zero-based and must be `< chunk_count` declared at init. Indexes may arrive in any order — the server sorts on finalize. The `:int` Starlette path converter constrains this segment to a digit run so `/finalize` cannot accidentally match this route (previously did, causing every finalize to 422).

**Response 200:** `{ "ok": true, "received_bytes": N }`

**Errors:** 400 (size mismatch with Content-Length), 403 (ownership), 404 (`upload_id` unknown or swept), 411 (missing Content-Length), 413 (chunk over 50 MiB + 1 KiB slack), 422 (`chunk_index` out of range).

---

### `POST /transcribe/file/chunked/{upload_id}/finalize`

**Auth:** Bearer required. **Owner must match the user that ran `/init`.**

Verify all chunks are present, concatenate them into the staging dir, and hand the assembled file off to the job runner. Cleans up the chunked staging dir on success; on transient `MeetingInProgressError` (429) it also cleans up the assembled file so a retry starts fresh.

**Request:** Empty body (`{}`).

**Response 202:** `{ "job_id": "...", "status": "pending" }`

From this point the client polls `GET /transcribe/meeting/{job_id}` exactly as it would for a single-shot `/transcribe/file` upload.

**Errors:** 403 (ownership), 404 (upload not found), 409 (missing chunks or size mismatch), 429 (`MeetingInProgressError` — assembled file is unlinked, retry after 60 s), 503 (RAM <4 GiB), 507 (disk).

**Stale-upload TTL:** chunked staging directories whose `meta.json` mtime is older than 1 h are reaped at server startup (and on demand by `ops.staging.sweep_chunked`). Each successful chunk write touches `meta.json` so actively-progressing uploads are never reaped.

---

### `POST /admin/rotate-key`

**Auth:** Bearer required (current key — this invalidates immediately on success).

Rotate the API key. No server restart required.

**Response 200:**
```json
{ "rotated": true }
```

The new key is **not** in the response body. It is emitted to stdout (captured by launchd to `~/Library/Logs/WisprAlt/server.log`) as `NEW_API_KEY=<key>` and written to `~/Library/Application Support/WisprAlt/.last-rotation-key` (mode 0600).

Steps performed (see `routes/admin.py:rotate_key`):
1. Generate `secrets.token_hex(32)` (64 hex chars).
2. Atomically rewrite `server/.env` via `env_writer.rewrite_env_var` (tempfile-in-same-dir + `os.replace`, mode 0600 preserved).
3. Write to `.last-rotation-key` (mode 0600, using `os.O_CREAT | os.O_WRONLY | os.O_TRUNC`).
4. Print `NEW_API_KEY=<key>` to stdout.
5. Call `auth.set_current_key(new_key)` — hot-swaps the in-memory key.

**Errors:** 500 if `.env` rewrite fails.

---

### `GET /metrics`

**Auth:** Bearer required.

Structured server observability snapshot.

**Response 200:**
```json
{
  "parakeet": {
    "p50_ms": 143.0,
    "p95_ms": 198.5,
    "queue_depth": 0,
    "last_inference_at": "2026-04-24T15:43:01Z"
  },
  "meeting": {
    "active": false,
    "active_job_id": null,
    "completed_24h": 3,
    "failed_24h": 0,
    "current_eta_s": null,
    "models_warm": true,
    "models_loading": false
  },
  "memory": {
    "rss_mb": 731,
    "available_mb": 8704,
    "mlx_active_mb": 1218,
    "mlx_cache_mb": 0
  },
  "disk": {
    "free_gb": 42,
    "staging_count": 0
  },
  "requests_total": {"transcribe/dictate:200": 312, "transcribe/meeting:202": 4},
  "errors_total": {"transcribe/dictate:422": 1},
  "latencies": {
    "transcribe/dictate": {"p50": 143.0, "p95": 198.5, "p99": 240.1},
    "transcribe/meeting": {"p50": null, "p95": null, "p99": null}
  },
  "process_uptime_seconds": 3712.4
}
```

| Field | Description |
|---|---|
| `parakeet.p50_ms` / `p95_ms` | Percentiles of the last 100 Parakeet inference calls |
| `parakeet.queue_depth` | Requests currently waiting for the single-thread executor |
| `parakeet.last_inference_at` | ISO-8601 timestamp of most recent inference, or `null` |
| `meeting.active` | `true` while a meeting pipeline job is running |
| `meeting.active_job_id` | UUID of the currently running job, or `null` |
| `meeting.completed_24h` / `failed_24h` | Job counts from the last 24 hours |
| `meeting.current_eta_s` | Estimated seconds until active job completes, or `null` |
| `meeting.models_warm` | `true` iff mlx-whisper + Pyannote are resident (lazy-loaded on first meeting). See [`/readyz/meeting`](#get-readyzmeeting). |
| `meeting.models_loading` | `true` iff a lazy load is currently in flight. |
| `memory.rss_mb` | Server process RSS in MiB. **Does NOT include MLX/Metal allocations** on Apple Silicon — unified memory is tracked by Metal separately. Use `mlx_active_mb` + `mlx_cache_mb` to close the gap with what Activity Monitor's "Memory" column reports. |
| `memory.available_mb` | System available RAM in MiB |
| `memory.mlx_active_mb` | Bytes currently held by live MLX tensors (Parakeet weights ≈ 1218 MB warm, 0 if model not loaded). Returns 0 if MLX isn't initialized or the API is missing. |
| `memory.mlx_cache_mb` | MLX's allocation pool. Grows during inference, returned to OS by `mx.metal.clear_cache()` after each Parakeet call. Should be ~0 between dictations; non-zero only mid-flight. |
| `disk.free_gb` | Free disk on staging volume in GiB |
| `disk.staging_count` | Number of staging WAV files currently on disk |
| `requests_total` | Request counts keyed by `"route:status"` (last process lifetime) |
| `errors_total` | Error counts keyed by `"route:status"` for 4xx/5xx responses |
| `latencies` | Per-route p50/p95/p99 latency in ms; `null` if fewer than 2 observations. **Recent-window only**: percentiles include only observations from the last 5 minutes (`LatencyHistogram._RECENT_WINDOW_S`). This protects p50 from being poisoned by a single old outlier (e.g. a hung 197-second upload). The full deque (last 1000 entries, all-time) remains queryable via `LatencyHistogram.percentiles(route, recent_only=False)` for callers that need the legacy view. Regression-locked by `server/tests/test_observability_time_window.py`. |
| `process_uptime_seconds` | Wall time since the FastAPI lifespan started (`time.monotonic() - observability.process_started_at_monotonic`). Useful as a deploy/restart sentinel: `< 60` immediately after `bash scripts/server-launchd.sh restart`. |

---

## Admin API

The `/admin/*` routes (other than `/admin/rotate-key` and `/admin/login`) render **HTML** via Jinja2 templates, not JSON. They are intended for browser navigation; curl/Postman work too but you'll get markup back.

Source: `server/src/wispralt_server/routes/admin_ui.py` +
`server/src/wispralt_server/admin/templates/*.html.j2`.

**Auth split (be720a1):** Three routers under the same `/admin` prefix.

- `/admin/login` — public (no auth).
- `/admin/me` — any authenticated role (admin OR employee). Admins are 303'd to `/admin/`; employees see their own `user_detail` page with admin nav hidden. Universal entry point — the macOS client's "Open Portal" button targets `/admin/login` and the server figures out where to send each role.
- `/admin/`, `/admin/users`, `/admin/users/{id}`, `/admin/users/{id}/mint`, `/admin/users/{id}/revoke`, `/admin/usage`, `/admin/usage.csv` — admin-only (employee tokens get **403**).

Browser users hit `/admin/login` once to set the `wispralt_admin_token` cookie; curl/Postman users send `Authorization: Bearer <token>` on each request. See [ARCHITECTURE.md → Admin UI](ARCHITECTURE.md#admin-ui) for the three-router pattern.

| Method | Path | Auth | Returns |
|---|---|---|---|
| GET | `/admin/login` | none | HTML form (200) |
| POST | `/admin/login` | none | 303 → `/admin/` (admin) or `/admin/me` (employee) + `Set-Cookie: wispralt_admin_token=...`. 401 on invalid token. |
| GET | `/admin/me` | any role | Admin: 303 → `/admin/`. Employee: 200 with that user's `user_detail` page (admin nav hidden). |
| GET | `/admin/` | admin | Overview dashboard HTML |
| GET | `/admin/users` | admin | Users-list HTML with per-row Mint/Revoke forms |
| POST | `/admin/users/{id}/mint` | admin | HTML page showing the new plaintext token **once** |
| POST | `/admin/users/{id}/revoke` | admin | 303 → `/admin/users` (revokes + invalidates cache by hash) |
| GET | `/admin/users/{id}` | admin | Per-user detail HTML (24h/7d/30d tiles + last 50 events) |
| GET | `/admin/usage` | admin | Drill-down HTML, paginated 100/page. Filters: `kind`, `status`, `user_id`, `since`, `until`, `offset`. |
| GET | `/admin/usage.csv` | admin | `text/csv` stream (max 10000 rows) |
| GET | `/admin/data` | admin | Phase 2 — weekly insights Data tab (`data.html.j2`). HTMX partial swap returns `_stats_grid_partial.html.j2`. Documented in detail above under `GET /admin/data`. |

All authed admin routes return **503** "Admin UI unavailable: Postgres degraded." when `app.state.db_pool` is `None` — the admin UI is unusable without a pool, so it fails loudly rather than crashing on `AttributeError` deeper in. The break-glass admin path (env-var bearer when Postgres is unreachable) lets the operator authenticate to the rest of the API but **not** to the admin UI; restart the server once Postgres is back to recover.
