---
title: API Reference
---

# API Reference

Complete HTTP API contract for the WisprAlt server. Implemented in `server/src/wispralt/routes/`.

---

## Authentication

Most routes require:

```
Authorization: Bearer <token>
```

The bearer is sha256-hashed and looked up in the local SQLite DB (`api_keys ⋈ users`: key not revoked, user not disabled) on **every** request — there is no cache, so a revoke applies to the next request. See [ARCHITECTURE.md → Auth](ARCHITECTURE.md#auth-local-sqlite-no-cache) for the full path.

**Unauthenticated endpoints** (probe-style + admin login, no API key required):
- `GET /healthz` — liveness probe
- `GET /readyz/dictation` — dictation-pipeline readiness probe
- `GET /readyz/db` — local SQLite DB readiness (`reason`: `db_error` / `schema_outdated`; body carries `usage_write_failures`)
- `GET /admin/login` and `POST /admin/login` — admin UI login form (chicken-and-egg: must be reachable without the cookie it sets)
- `GET /me/login` and `POST /me/login` — employee portal login form (Phase 2; same chicken-and-egg reason)

These are intentionally open so Kubernetes-style probes, Cloudflare health checks, and external monitoring can poll them without API credentials. The probes expose only ready-flag booleans + free RAM in MB — no user data, no audio, no model output.

**Bearer required:**
- `POST /transcribe/dictate`, `POST /transcribe/file` (+ chunked), `GET /transcribe/file/{id}`, `GET /transcribe/file/{id}/download/{fmt}`, `DELETE /transcribe/file/{id}`
- `POST /v1/audio/transcriptions`, `GET /v1/models`, `GET /v1/models/{id}` (OpenAI-compatible drop-in surface — see [OPENAI-COMPAT.md](OPENAI-COMPAT.md))
- `GET /me`, `PATCH /me`, `GET /me/insights` (Phase 2 — employee self-view)
- `GET /me/history`, `GET /me/history/{kind}/{row_id}`, `DELETE /me/history/{kind}/{row_id}`, `POST /me/history/{kind}/{row_id}/restore`, `GET /me/history/{kind}/{row_id}/download/{fmt}` (Plan A — personal transcript archive)
- `GET /me/dictations/last` (v0.5.0 — caller's most recent non-deleted dictation; powers the "Copy last dictation" menubar button)
- `GET /metrics`
- All `/admin/*` routes **except** `/admin/login`. The admin UI also accepts a `wispralt_admin_token` cookie (set by `POST /admin/login`) as a fallback for browser navigation.
- `/admin/*` (except login) additionally require `role='admin'` — an employee-role token gets **403**.

**Optional client-version header (always-accepted, never required):**

Every `/transcribe/*` route and `/transcribe/dictate` accept an
`X-WisprAlt-Client-Version` header. The Swift client sets it on every
server-bound request (format `"<short>+<build>"`, e.g. `"0.3.1+1"`). The
value is recorded with the persisted transcript on the row (`jobs.client_app_version`
for file jobs, `dictations.client_app_version` for dictation) so the
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
- When the auth lookup itself fails (`sqlite3.Error`), the server returns **503** `"Auth temporarily unavailable"` rather than 401, so the operator can distinguish "wrong key" from "DB down". There is no fallback admin.

---

## Rate Limits

Enforced by `middleware/rate_limit.py` (per-IP, in-memory rolling window):

| Route group | Limit |
|---|---|
| `POST /transcribe/dictate` | 60 requests per 60-second window (configurable: `DICTATE_RATE_PER_MIN`) |
| `GET /readyz/*`, `GET /healthz` | 120 requests per 60-second window (configurable: `PROBE_RATE_PER_MIN`). Cloudflare's typical 5–10s health-check cadence sits well under this; the cap exists to bound probe-flood damage on the unauthenticated probe endpoints. |

Exceeded limits return **429** with a `Retry-After` header (value = window duration in seconds). These rate-limit 429s carry **no** `code` field; the file-job busy 429s (see [`POST /transcribe/file`](#post-transcribefile)) do, which is how clients tell them apart.

`/transcribe/file` has no per-IP rate limit: the runner allows one file job at a time and rejects the rest with a coded 429.

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
| `X-Dictation-Degraded: true` | A file job is running (`app.state.file_runner.active`) or the file model is lazy-loading (`files.pipeline.is_loading()`). Dictation still works but unified memory is under pressure. |

**Response 200** — Parakeet is warm and ready:
```json
{ "status": "ok" }
```

**Response 503** — Parakeet model not yet loaded:
```json
{ "status": "not_ready", "detail": "Parakeet model not loaded" }
```

---

There is no readiness probe for file transcription: the file model (MLX Whisper) loads lazily on the first file job and is evicted when idle, so it is normally cold. `GET /metrics` → `file.models_warm` reports its current state. (The meeting readiness probe was removed with the meeting pipeline and now returns 404.)

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
| `X-Smart-Format` | — | — | **Removed (Part 4).** Server smart formatting (OpenRouter Mercury) is gone; the header is ignored and the raw transcript is always returned. Any formatting now happens in the client. |
| `X-Client-Dedup-Id` | UUID v4 string | absent ⇒ off | **Optional, additive (Phase 5b streaming-dictation).** When present, must be a syntactically valid UUID v4. Forwarded to `JobStore.insert_dictation(..., client_dedup_id=...)`; the row participates in the `idx_dictations_client_dedup` partial-unique index, so a subsequent retry that sends the same UUID is silently de-duped (`ON CONFLICT(client_dedup_id) DO NOTHING`). Used by the safety-buffer fallback in `DictationStreamSession` to guarantee at most one persisted row per utterance when a streaming finalize loses to its local fallback. When the header is **absent**, behavior is bit-identical to the pre-streaming contract: the column is left NULL and the partial-unique index never applies. Malformed UUIDs are rejected at the validation boundary (422). |

**Audio format flexibility (server-side resampling):**

- Sample rate: any rate libsndfile / librosa accepts (8 kHz, 16 kHz, 44.1 kHz, 48 kHz, etc.). Server resamples to 16 kHz internally before Parakeet inference.
- Bit depth: Int16, Int24, Int32, Float32 — all auto-converted to Float32 by `soundfile.read`.
- Channel count: mono or multi-channel. Server averages all channels down to mono via `np.mean(axis=1)` before Parakeet.
- The macOS client in **Local** mode uploads native-rate Int16 PCM (typically 48 kHz mono, `LocalCapture.wholeWAV` via `DictationAPI.transcribe(capture:)`); custom integrators can send anything in this range. In **Online** mode the client never calls this server: it posts 16 kHz mono WAV straight to OpenAI.
- Client error logging: `ServerError` conforms to `LogSafeError`, so a log line names only the status or error kind (`server(status: 500)`, `transport(...)`), never the response body.

**Size limit:** `Content-Length` must be ≤ `MAX_UPLOAD_BYTES` (default 2 GiB). Checked from the header before reading the body.

**Minimum duration:** Audio shorter than ~100 ms (post-resample, < `MIN_SAMPLES = 1600` at 16 kHz) returns `text=""` with `duration_ms=0.0` and HTTP 200 — not an error. Very short clips are silently no-op'd at the model layer.

**Response 200:**
```json
{
  "text": "Hello world.",
  "model_id": "mlx-community/parakeet-tdt-0.6b-v2",
  "duration_ms": 143.7
}
```

`duration_ms` is wall-clock Parakeet inference time (excludes queue wait).
The `smart_formatted` key was removed in Part 4 along with server smart formatting.

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

**Auth:** Bearer required. Sessions are owned by the authenticated user. **Opt-in, experimental** — see [ARCHITECTURE.md → Streaming dictation](ARCHITECTURE.md#streaming-dictation-opt-in-experimental) for the end-to-end design.

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
| 403 | The bearer resolves to a user who is not the session's owner (recorded at first-chunk open). |
| 409 | `per_user_busy` — this user already owns a different active session. Either reuse its `session_id` or wait for it to finalize/expire. |
| 410 | Session is no longer active (status ∈ {finalizing, done, aborted, expired}). The client should fall back to single-shot `/transcribe/dictate` with the safety-buffer WAV. |
| 413 | Cumulative voiced audio for this session exceeds the 270 s mid-chunk ceiling. (Finalize allows up to 300 s.) |
| 415 | `Content-Type` is not `audio/*`. |
| 422 | `session_id` is not a valid UUID v4 OR audio bytes cannot be decoded. |
| 429 | Per-session pending-task queue depth exceeded (defends against runaway clients). |
| 503 | `capacity_exceeded` — the global 2-session cap is already saturated. Retry after the other sessions drain, or fall back to single-shot. |

---

### `POST /transcribe/dictate/stream/{session_id}/finalize`

**Auth:** Bearer required; same per-user-owner gate as `/chunk`. **Opt-in, experimental.**

Close out a streaming session. The server waits up to 15 s for all pending chunk tasks to drain, joins their texts in index order, persists the row to `dictations`, and returns the final text. The chunked-receive route stops accepting new chunks for this `session_id` (returns 410) the moment finalize starts.

**Path params:** `session_id` (UUID v4) — same as `/chunk`.

**Request:** `multipart/form-data`

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | UploadFile | Yes | The tail WAV — the trailing speech segment that was still being accumulated when FN was released. `Content-Type: audio/*`. May be empty (zero-byte) if the speaker happened to finish exactly on a silence cut. |
| `smart_format` | — | — | **Removed (Part 4).** Ignored if an older client still sends it; the text is never post-processed on the server. |
| `client_dedup_id` | UUID v4 | Yes | The same UUID the client will reuse on the safety-buffer fallback if this finalize loses. Persisted to `dictations.client_dedup_id`; combined with the `idx_dictations_client_dedup` partial-unique index, guarantees one row per utterance. |
| `speech_started_at` | float (Unix epoch, sub-second precision) | Yes | The local wall-clock time the *user* began speaking (NOT FN press) — sent so the server can compute end-to-end latency from speech start to text injection without depending on local clock-sync. Sub-second precision is preserved (don't round to int). |

**Response 200:**

```json
{
  "text": "Hello world.",
  "model_id": "mlx-community/parakeet-tdt-0.6b-v2",
  "duration_ms": 213.4
}
```

`duration_ms` on streaming rows is **cumulative** per-chunk Parakeet inference time (sum of N chunks), not single-pass wall-clock. See [ARCHITECTURE.md → Known limitations](ARCHITECTURE.md#known-limitations) for the semantic drift versus single-shot dictation rows. The response envelope is otherwise byte-identical to `/transcribe/dictate` so the client's existing decoder is unchanged.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer token. |
| 403 | The bearer's user is not the session's owner. |
| 409 | `gap_detected` — `partial_texts` contains a `None` entry between two ingested chunks (a chunk POST landed for index N but index N−1 never arrived). The client should fall back to single-shot. |
| 410 | Session is not in `status="active"` (already finalizing, done, aborted, or expired). |
| 413 | Cumulative voiced audio + tail exceeds 300 s. |
| 415 | Tail `Content-Type` is not `audio/*`. |
| 422 | `session_id` or `client_dedup_id` is not a valid UUID v4, OR tail audio cannot be decoded. |
| 502 | `inference_failed` — Parakeet raised on the tail or on a still-pending chunk during the 15 s drain. |
| 504 | `timeout` — pending chunks did not drain inside 15 s. Session marked `aborted`; client safety-buffer fallback kicks in. |

---

### `/v1/*` — OpenAI-compatible surface

The `/v1/*` routes are a drop-in replacement for OpenAI's audio
transcription API. The full wire-level contract — every form parameter,
every response format (`json`, `text`, `verbose_json`, `srt`, `vtt`),
every response header (`x-request-id`, `openai-version`,
`openai-processing-ms`, `openai-model`), every error `code`, file format
support, rate limits, integration vs employee key kinds — lives in the
canonical reference: **[OPENAI-COMPAT.md](OPENAI-COMPAT.md)**.

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/v1/audio/transcriptions` | Bearer | Sync transcription. 5 response formats. ≤25 MB / ≤15 min. |
| GET  | `/v1/models` | Bearer | Lists the 5 advertised model IDs. Open WebUI gates on this. `Cache-Control: no-cache, must-revalidate`. |
| GET  | `/v1/models/{id}` | Bearer | Single model object. 404 `model_not_found` for unknown IDs. |
| POST | `/v1/audio/translations` | Bearer | NOT IMPLEMENTED. Returns 400 `endpoint_not_supported`. |

**Key constraints that matter at the API contract level:**

- **Bearer-only auth on `/v1/*`** — the `wispralt_admin_token` session
  cookie is NOT consulted, even if the browser carries it. Programs use
  Bearer; humans use cookies; the two surfaces stay separate.
- **Per-token rate limit** (60 req/min/token), independent of the
  per-IP bucket on `/transcribe/dictate`.
- **4xx for client errors, 5xx for server errors** — categorization
  matters because `openai-python` retries 408 / 409 / 429 / ≥500 by
  default. Decoder failures map to 400 so the SDK doesn't retry-storm a
  broken upload.
- **CORS** — `Access-Control-Allow-Origin: *` on every `/v1/*`
  response including 429 envelopes, so browser clients see real HTTP
  errors instead of CORS failures.
- **No server-side smart formatting** on any route (removed in Part 4);
  `/v1/*` returns the raw transcript.

Sources: `server/src/wispralt/routes/v1_transcriptions.py`,
`server/src/wispralt/routes/v1_models.py`.

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
| 503 | Auth lookup failed (`sqlite3.Error`) |

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
| 503 | Auth lookup failed (`sqlite3.Error`) |

`display_name` is read fresh from `users` each time (auth never caches the `User`).

Source: `server/src/wispralt/routes/me.py`.

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
| 404 | Caller has zero non-deleted dictations. |
| 503 | SQLite job store transiently unavailable. |

Source: `server/src/wispralt/routes/me.py` +
`JobStore.get_most_recent_dictation(user_id)` in
`server/src/wispralt/jobs/store.py`.

---

### `GET /me/login`

**Auth:** None required.

Renders the employee token-paste form (`me_login.html.j2`). Chicken-and-egg counterpart to `/admin/login` — must be reachable without the cookie it sets. Returns `200` with `text/html`.

Source: `server/src/wispralt/routes/me.py` (Phase 2).

---

### `POST /me/login`

**Auth:** None required (this endpoint is what produces the auth cookie).

Validate a pasted token and set the session cookie. Mirrors `POST /admin/login`.

**Request:** `application/x-www-form-urlencoded`

| Field | Type | Notes |
|---|---|---|
| `token` | string | 64-char hex bearer token. Validated with the same lookup as every request (`auth.resolve_token`). |

**Response 303** — token valid. Redirects to `/me/insights` by default, or to the value of the optional `?next=` query parameter when it's a **relative path under `/me/*`** (open-redirect guard — absolute URLs, scheme-prefixed values, and paths outside `/me/*` are silently ignored and the redirect falls back to `/me/insights`). The menubar "Open My Dictations" button (v0.5.0) uses `?next=/me/history` so first-time employees land directly on their history page after token-paste. Sets `wispralt_admin_token` cookie with `path="/"`, `HttpOnly`, `Secure`, `SameSite=Strict`, `max-age=8h`. The `path="/"` scoping lets the same cookie cover both `/admin/*` and `/me/*` surfaces; admin login was simultaneously broadened to set the cookie with `path="/"` as well.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Invalid / revoked token — re-renders `me_login.html.j2` with an error message. |
| 503 | Auth lookup failed (`sqlite3.Error`). |

See [ARCHITECTURE.md → Data portal](ARCHITECTURE.md#data-portal) for the rationale behind sharing the cookie between admin + employee surfaces.

Source: `server/src/wispralt/routes/me.py` (Phase 2).

---

### `GET /me/insights`

**Auth:** Bearer or session cookie (any role; integration keys get 403).

Per-employee weekly insights page. Returns the most recent `weekly_insights` row with `scope='person'` for the calling user, plus a time-range stats grid (transcript volume, word count, dictation/file-transcription split) for the selected window.

**Query parameters:**

| Field | Type | Default | Notes |
|---|---|---|---|
| `range` | enum | `7d` | One of `today`, `7d`, `30d`, `90d`, `1y`, `all`. Drives the stats grid window only — the weekly digest card always shows the most recent completed ISO week. |

**Response 200** — full page. `Content-Type: text/html` rendering `me_insights.html.j2`.

**Response 200 (HTMX partial)** — when the request carries `HX-Request: true`, the handler returns only the `_stats_grid_partial.html.j2` fragment for in-place swap. Use this for time-range tab clicks; the full page is only needed on first navigation.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer. |
| 422 | `range` is not one of the accepted values. |
| 503 | Auth lookup failed (`sqlite3.Error`). |

The hallucination disclaimer ("AI-generated, may be inaccurate") is rendered inline on each insight card — see [ARCHITECTURE.md → Weekly insights](ARCHITECTURE.md#weekly-insights) for the two-tier scrub policy that backs it.

Source: `server/src/wispralt/routes/me.py` (Phase 2).

---

### `GET /me/history`

**Auth:** Bearer or session cookie (any role; integration keys get 403).

Personal transcript archive — the calling user's own dictations + file transcriptions in one chronological list. Filterable by time range, kind, and free-text search. Per-leg cursor pagination keeps heavy-on-one-side pages from starving the other. Backed by `JobStore.transcripts_in_range_filtered` (UNION of `dictations` + `jobs`, both filtered on `user_id` AND `deleted_at IS NULL`).

**Query parameters:**

| Field | Type | Default | Notes |
|---|---|---|---|
| `range` | enum | `30d` | One of `today`, `7d`, `30d`, `90d`, `1y`, `all`. Reuses `insights.timewindow.epoch_for_range`. |
| `kind` | enum \| empty | unset | Either `dictation` or `file` (displayed as "File transcriptions"). Empty string from a `<select>` is normalized to "no filter". The row routes `/me/history/{kind}/{row_id}` take the same two kinds; `meeting` (pre-Part 4) now 404s. |
| `search` | string | unset | Free-text LIKE filter on the transcript body. Strings shorter than **3 chars after `.strip()`** are silently dropped to keep the scan bounded. |
| `dict_cursor` | opaque | unset | Base64-urlsafe encoded `(created_at, row_id)` tuple for the dictations leg. Malformed → silently restart pagination. |
| `jobs_cursor` | opaque | unset | Same shape, for the jobs (file transcription) leg. |

**Response 200** — full page (`me_history.html.j2`).

**Response 200 (HTMX nav)** — when `HX-Request: true` is present and no cursor was sent, returns `_me_history_body.html.j2` (the body fragment for in-place swap).

**Response 200 (HTMX Load-more)** — when a cursor was sent, returns `_me_history_page.html.j2` (the next batch of `<tr>` rows + the updated Load-more button). Page size is `settings.history_page_size` (default **50**) per leg.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer. |
| 422 | `range` is not one of the accepted values. |
| 503 | Auth lookup failed (`sqlite3.Error`). |

Source: `server/src/wispralt/routes/me.py` (Plan A).

---

### `GET /me/history/{kind}/{row_id}`

**Auth:** Bearer required. Ownership-checked.

Render one row's expanded (default) or compact (`?compact=1`) partial. Unknown `kind`, unknown `row_id`, or a `row_id` owned by another user all uniformly return **404** so we never leak existence to non-owners.

**Path parameters:**

| Field | Type | Notes |
|---|---|---|
| `kind` | enum | `dictation` or `file` (shown as "File transcription"). Anything else (incl. the pre-Part 4 `meeting`) → 404. |
| `row_id` | string | Job UUID for `file`, integer row id for `dictation`. |

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

Source: `server/src/wispralt/routes/me.py` (Plan A).

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

Source: `server/src/wispralt/routes/me.py` (Plan A).

---

### `POST /me/history/{kind}/{row_id}/restore`

**Auth:** Bearer required + CSRF token. Ownership-checked.

Restore one soft-deleted row by clearing `deleted_at`. Returns the compact row partial (`_me_history_row.html.j2`) so HTMX can drop the row back into the table at its original position.

**Request:** Same `csrf_token` form field as `DELETE`.

**Response 200** — `_me_history_row.html.j2` partial.

**Errors:** Same as `DELETE /me/history/{kind}/{row_id}`.

Source: `server/src/wispralt/routes/me.py` (Plan A).

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

Source: `server/src/wispralt/routes/me.py` (Plan A).

---

### `GET /admin/data`

**Auth:** Bearer required + `role='admin'`. Handler-level `require_admin`: employee tokens get 403.

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
| 404 | `user_id` was supplied but no such user exists. |
| 422 | `range` is not one of the accepted values. |
| 503 | Auth lookup failed (`sqlite3.Error`). |

Source: `server/src/wispralt/routes/admin_data.py` (Phase 2).

---

### `POST /admin/data/insights/run-now`

**Auth:** Bearer + `wispralt_admin_token` session cookie + `role='admin'`. Handler-level `require_admin`: an employee token gets **403**.

Admin-only manual trigger for `insights.cron.run_weekly_insights` against the **current in-progress ISO week**. Fire-and-forget — the handler returns immediately with a tiny HTML body (toast + OOB-swap button fragment) for HTMX to render in place. The spawned task is strong-referenced in `app.state.weekly_insights_manual_tasks` (set of `asyncio.Task`) and removed by a closure-based done-callback that also logs any unhandled exception.

Per-week double-billing is prevented by `cron.py`'s per-user idempotency skip — if a `weekly_insights` row already exists for `(iso_year, iso_week, scope_key='user:<id>')`, the OpenRouter call is skipped. The in-flight lock pre-check is purely UX.

**Request:** `application/x-www-form-urlencoded`

| Field | Type | Notes |
|---|---|---|
| `csrf_token` | string | Double-submit value matched against the `wispralt_csrf` cookie set by the prior `GET /admin/data` full-page render. Constant-time compare via `hmac.compare_digest`. 30-minute cookie TTL. |

**Response 200** — `text/html`. Body composes a toast (`<div class="admin-toast">…</div>`) with one of:

| Toast | Trigger |
|---|---|
| `Insights run started for Wxx. Refresh page in ~1 minute to see results.` | Happy path — task spawned. Body also includes an OOB-swap `#admin-run-insights-button` fragment that paints the button as `Running…` (disabled). |
| `Insights unavailable — OPENROUTER_API_KEY not configured.` | `app.state.insights_client is None`. No task spawned. |
| `Budget exceeded ($X.XX / $Y.YY cap). Run skipped.` | Rolling 30-day insights spend exceeds `settings.insights_max_30d_cost_usd` (default $8). No task spawned. |
| `A run is already in progress. Refresh in ~1 minute.` | `app.state.weekly_insights_lock.locked()` is True. Body also includes the OOB-swap disabled button. No task spawned. |

**Response 403** — `text/html`. Body is the toast `Session expired. Refresh the page and try again.` so HTMX's `hx-target-403` swaps it into `#admin-run-insights-toast`. Triggered by missing / mismatched / expired `csrf_token`.

**Response 403** — empty body. Triggered by non-admin role (the standard `require_admin` 403 path; employees never reach this surface via the UI).

**Response 503** — auth lookup failed (`sqlite3.Error`).

**Notes:**

- Targets the **current** in-progress ISO week (NOT last completed). The Sunday 23:00 cron will UPSERT-replace these rows on its next fire — no orphan rows.
- Server logs one `INFO` line per trigger: `Manual insights run started by admin_id=<id> for W<nn> (user=<label>)`. Operators distinguish manual vs scheduled runs by grepping for this prefix.
- Fire-and-forget tasks are NOT durable across SIGTERM — see `docs/ADMIN.md` for the re-click recovery path.

Source: `server/src/wispralt/routes/admin_data.py`.

---

### `POST /transcribe/file`

**Auth:** Bearer required.

Submit any audio or video container for background transcription with MLX Whisper (one mono stream; stereo is averaged). The route streams the upload to staging in its original container and returns immediately; the runner then runs ffprobe + ffmpeg and the Whisper pipeline. Poll `GET /transcribe/file/{job_id}` for status.

There are no meetings and no `mode` field: every job is a single-speaker file job. An old client that still sends a `mode` form field is ignored (FastAPI drops undeclared fields).

**Request:** `multipart/form-data`

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | UploadFile | Yes | Source container. Allowed extensions: `.m4a .mp3 .mp4 .mov .m4v .wav .aac .flac .opus .ogg .webm .caf .aiff`. |
| `Content-Length` header | integer | Recommended | Enables pre-flight 413 + disk gate (free < `Content-Length × 2` → 507). |

**Response 202:** `{ "job_id": "...", "status": "pending" }`

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer token |
| 413 | Over `MAX_UPLOAD_BYTES` |
| 415 | Unsupported extension |
| 422 | ffprobe / ffmpeg rejected the source (no audio stream, decode failed) |
| 429 | Runner busy. Body `{"error": "...", "retry_after_s": 60, "code": "..."}` plus `Retry-After: 60`. `code` is `file_job_busy` (a file job is already running, or still finishing after a cancel; only one runs at a time) or `insufficient_memory` (< 2 GiB available to start a job). |
| 503 | Server low on memory (< 4 GiB available at the route boundary) |
| 507 | Insufficient disk for upload + transcode |

The `code` field is what the macOS client branches on (`file_job_busy` → "previous transcription still finishing"; `insufficient_memory` → the server's message). The per-IP rate limiter's 429s have no `code`.

**Model load:** the file model (MLX Whisper) loads lazily on the first file job and is evicted after `FILE_MODEL_IDLE_EVICTION_SECONDS` (default 60) of idleness, so the first job after an idle spell pays the load cost. The load wall-clock is part of the `running` time (phase `transcribe_load`), not a separate status.

---

### `GET /transcribe/file/{job_id}`

**Auth:** Bearer required. The caller sees a job only if it owns it (`job.user_id == user.id`) or is an admin. Anything else is **404**, exactly like a missing job, so existence never leaks.

Poll job status. The client polls every few seconds.

**Response 200:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "done",
  "mode": "single",
  "formats": ["json", "srt", "txt", "vtt"],
  "outputs": {
    "json": "/transcribe/file/550e8400-e29b-41d4-a716-446655440000/download/json",
    "srt":  "/transcribe/file/550e8400-e29b-41d4-a716-446655440000/download/srt",
    "txt":  "/transcribe/file/550e8400-e29b-41d4-a716-446655440000/download/txt",
    "vtt":  "/transcribe/file/550e8400-e29b-41d4-a716-446655440000/download/vtt"
  }
}
```

| Field | Present when |
|---|---|
| `status` | Always. Values: `pending`, `running`, `done`, `failed`. |
| `mode` | When recorded on the row (`single`). `request_mode` was removed in Part 4 (the client never read it). |
| `error` | When `status == "failed"`. A cancelled job reads `cancelled by user`. |
| `progress` | When `status == "running"`: `phase`, `phase_label`, `chunk_index`, `total_chunks`, optional `phase_elapsed_s`, `audio_duration_s`. |
| `serverFinishing` | `true` when `status == "running"` and a cancel was requested: the Whisper call is still running and its result will be discarded. |
| `formats` | When `status == "done"`. Always `["json", "srt", "txt", "vtt"]`. Kept for backward compatibility. |
| `outputs` | When `status == "done"`. Maps each format to its download URL. **Preferred** over `formats`. |

**Retry policy:** a job re-enqueued at startup is skipped and marked `failed` (`max retries exceeded`) once it has 3 attempts. Each run counts one attempt.

**Errors:** 400 (malformed `job_id`), 404 (not found or not visible to the caller).

---

### `GET /transcribe/file/{job_id}/download/{fmt}`

**Auth:** Bearer required. Same ownership rule as the poll route.

Stream a completed output file.

**Path parameter `fmt`:** `json`, `srt`, `vtt`, or `txt`.

**Response 200** — streaming file response:

| Format | Content-Type |
|---|---|
| `json` | `application/json` |
| `srt` | `application/x-subrip` |
| `vtt` | `text/vtt` |
| `txt` | `text/plain` |

**Errors:**

| Code | Condition |
|---|---|
| 400 | Unknown format string, or malformed `job_id` |
| 404 | Job not found, not visible to the caller, or not yet `done` |
| 410 | Job is `done` but the output file is missing from disk (e.g. swept by retention) |

---

### `DELETE /transcribe/file/{job_id}`

**Auth:** Bearer required. Same ownership rule as the poll route.

Cancel a pending or running job, or remove a finished one. The client calls it to cancel, and after it has downloaded all four formats.

| Job state | Response | Effect |
|---|---|---|
| Missing, or owned by someone else | **204** | Nothing (idempotent; no existence leak). |
| `pending` or `running` | **202** | Cancel is requested. The status is then re-read: if the job finished in the meantime, its outputs and row are removed and the response is **204** instead. |
| `done` or `failed` | **204** | Outputs (`{job_id}.{json,srt,vtt,txt}`) and the row are removed. |
| `done` or `failed`, but an output file could not be removed | **500** `{"error"}` | The row is kept so a retried `DELETE` or the retention sweep can finish the cleanup. |

**What a 202 means:**

- A **pending** job never starts: the runner's conditional `set_running` refuses a cancelled row, deletes the staged source and marks the job `failed: cancelled by user`.
- A **running** job cannot be interrupted mid-transcribe. Whisper transcribes the whole file in one call, so the job keeps the model and the single job slot until that call returns. Only then is the result discarded: `set_done` refuses a cancelled row, the outputs are removed, the job ends `failed: cancelled by user` and `transcript_text` stays NULL. A new submission during that window gets 429 `code=file_job_busy`.

The client treats 202 and 204 the same: it stops polling and clears its job state at once.

---

### `POST /transcribe/file/chunked/init`

**Auth:** Bearer required. **Files: >50 MB or any file when chunking is preferred.**

Open a chunked upload to bypass Cloudflare's 100 MB request-body cap on free / pro / business plans. The server returns an `upload_id` and a chunk size; the client then POSTs each chunk to `/transcribe/file/chunked/{upload_id}/{chunk_index}` and finally calls `/transcribe/file/chunked/{upload_id}/finalize`. The assembled file is processed by the SAME pipeline that handles single-shot `/transcribe/file` uploads — the poll route is unchanged.

**Request:** `application/json`

```json
{
  "total_bytes": 524288000,
  "chunk_count": 10,
  "original_filename": "call-2025-05-13.m4a"
}
```

| Field | Type | Notes |
|---|---|---|
| `total_bytes` | integer | Size of the full file. Must be ≤ `min(MAX_UPLOAD_BYTES, 4_000_000_000)` — the 4 GB hard ceiling keeps the finalize concat inside Cloudflare's 100 s proxy timeout window. |
| `chunk_count` | integer | `1 ≤ chunk_count ≤ 1000`. Client computes as `ceil(total_bytes / 50 MiB)`. |
| `original_filename` | string | Used to recover the source extension for the assembled staging file. Suffix must be in the same allowlist as `POST /transcribe/file`. |

**Response 200:**

```json
{ "upload_id": "abc-22-char-token", "chunk_size": 52428800 }
```

`upload_id` is a `secrets.token_urlsafe(16)` value (22 URL-safe chars). The server records the caller's API-key user id in the upload's metadata; subsequent chunk + finalize calls MUST present a bearer token resolving to the same user id (403 otherwise). Break-glass / single-key clients cannot use chunked upload — they must fall back to single-shot `POST /transcribe/file`.

**Errors:** 401 (unauthenticated), 413 (`total_bytes` over limit), 415 (extension), 422 (`chunk_count` out of range), 503 (RAM <4 GiB), 507 (disk).

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

Verify all chunks are present, concatenate them into the staging dir, and hand the assembled file off to the job runner. Cleans up the chunked staging dir on success; on a busy 429 (`FileJobInProgressError`) it also cleans up the assembled file so a retry starts fresh. A `mode` key left in a pre-upgrade `meta.json` is ignored, so such uploads finish as ordinary file jobs.

**Request:** Empty body (`{}`).

**Response 202:** `{ "job_id": "...", "status": "pending" }`

From this point the client polls `GET /transcribe/file/{job_id}` exactly as it would for a single-shot `/transcribe/file` upload.

**Errors:** 403 (ownership), 404 (upload not found), 409 (missing chunks or size mismatch), 429 (same body and `code` as `POST /transcribe/file`; the assembled file is unlinked, retry after 60 s), 503 (RAM <4 GiB), 507 (disk).

**Stale-upload TTL:** chunked staging directories whose `meta.json` mtime is older than 1 h are reaped at server startup (and on demand by `ops.staging.sweep_chunked`). Each successful chunk write touches `meta.json` so actively-progressing uploads are never reaped.

---

### `POST /admin/rotate-key` (removed)

Removed in Part 4 with the env admin key. The first admin comes from `python -m wispralt.bootstrap --label NAME`; a lost admin key is replaced with `python -m wispralt.bootstrap --rotate-admin` (both run from `server/` in a terminal and print the token once). Employee and integration keys are rotated from the admin UI (`/admin/users/{id}/mint`, `/admin/keys/{id}/rotate`).

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
  "file": {
    "active": false,
    "active_job_id": null,
    "completed_24h": 3,
    "failed_24h": 0,
    "current_eta_s": null,
    "models_warm": true,
    "models_loading": false,
    "idle_seconds": 12.4,
    "idle_eviction_threshold_s": 60,
    "eviction_failures_total": 0
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
  "requests_total": {"transcribe/dictate:200": 312, "transcribe/file:202": 4},
  "errors_total": {"transcribe/dictate:422": 1},
  "latencies": {
    "transcribe/dictate": {"p50": 143.0, "p95": 198.5, "p99": 240.1},
    "transcribe/file": {"p50": null, "p95": null, "p99": null}
  },
  "usage_write_failures_total": 0,
  "process_uptime_seconds": 3712.4
}
```

| Field | Description |
|---|---|
| `parakeet.p50_ms` / `p95_ms` | Percentiles of the last 100 Parakeet inference calls |
| `parakeet.queue_depth` | Requests currently waiting for the single-thread executor |
| `parakeet.last_inference_at` | ISO-8601 timestamp of most recent inference, or `null` |
| `file.*` | The **file-job** block (renamed from `meeting` in Part 4). |
| `file.active` | `true` while a file job is queued or running |
| `file.active_job_id` | UUID of the currently running job, or `null` |
| `file.completed_24h` / `failed_24h` | Job counts from the last 24 hours |
| `file.current_eta_s` | Estimated seconds until active job completes, or `null` |
| `file.models_warm` | `true` iff the file model (MLX Whisper) is resident. It loads lazily on the first file job and is evicted when idle. |
| `file.models_loading` | `true` iff a lazy load is currently in flight. |
| `file.idle_seconds` / `idle_eviction_threshold_s` | Seconds since the file model was last used, and the eviction threshold (`FILE_MODEL_IDLE_EVICTION_SECONDS`). |
| `file.eviction_failures_total` | Count of eviction self-checks that found memory not returned. |
| `memory.rss_mb` | Server process RSS in MiB. **Does NOT include MLX/Metal allocations** on Apple Silicon — unified memory is tracked by Metal separately. Use `mlx_active_mb` + `mlx_cache_mb` to close the gap with what Activity Monitor's "Memory" column reports. |
| `memory.available_mb` | System available RAM in MiB |
| `memory.mlx_active_mb` | Bytes currently held by live MLX tensors (Parakeet weights ≈ 1218 MB warm, 0 if model not loaded). Returns 0 if MLX isn't initialized or the API is missing. |
| `memory.mlx_cache_mb` | MLX's allocation pool. Grows during inference, returned to OS by `mx.metal.clear_cache()` after each Parakeet call. Should be ~0 between dictations; non-zero only mid-flight. |
| `disk.free_gb` | Free disk on staging volume in GiB |
| `disk.staging_count` | Number of staging WAV files currently on disk |
| `requests_total` | Request counts keyed by `"route:status"` (last process lifetime) |
| `errors_total` | Error counts keyed by `"route:status"` for 4xx/5xx responses |
| `latencies` | Per-route p50/p95/p99 latency in ms; `null` if fewer than 2 observations. **Recent-window only**: percentiles include only observations from the last 5 minutes (`LatencyHistogram._RECENT_WINDOW_S`). This protects p50 from being poisoned by a single old outlier (e.g. a hung 197-second upload). The full deque (last 1000 entries, all-time) remains queryable via `LatencyHistogram.percentiles(route, recent_only=False)` for callers that need the legacy view. Regression-locked by `server/tests/test_observability_time_window.py`. |
| `usage_write_failures_total` | Usage rows lost because `UsageStore.record` raised `sqlite3.Error` (usage is best-effort; the request itself succeeded). Resets on restart. Also in the `/readyz/db` body as `usage_write_failures`. |
| `process_uptime_seconds` | Wall time since the FastAPI lifespan started (`time.monotonic() - observability.process_started_at_monotonic`). Useful as a deploy/restart sentinel: `< 60` immediately after `bash scripts/server-launchd.sh restart`. |

---

## Admin API

The `/admin/*` routes (other than `/admin/login`, `/admin/active`, `/admin/server-log` and `/metrics`) render **HTML** via Jinja2 templates, not JSON. They are intended for browser navigation; curl/Postman work too but you'll get markup back.

Source: `server/src/wispralt/routes/admin_ui.py` +
`server/src/wispralt/admin/templates/*.html.j2`.

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
| POST | `/admin/users/{id}/mint` | admin | Revokes the user's keys, issues one new key (re-enabling a revoked user); HTML page shows it **once**. 404 for an unknown user. |
| POST | `/admin/users/{id}/revoke` | admin | 303 → `/admin/users` (disables the user; its keys 401 from the next request) |
| GET | `/admin/users/{id}` | admin | Per-user detail HTML (24h/7d/30d tiles + last 50 events) |
| GET | `/admin/usage` | admin | Drill-down HTML, paginated 100/page. Filters: `kind`, `status`, `user_id`, `since`, `until`, `offset`. |
| GET | `/admin/usage.csv` | admin | `text/csv` stream (max 10000 rows) |
| GET | `/admin/data` | admin | Phase 2 — weekly insights Data tab (`data.html.j2`). HTMX partial swap returns `_stats_grid_partial.html.j2`. Documented in detail above under `GET /admin/data`. |
| POST | `/admin/data/insights/run-now` | admin | Manual trigger for the weekly-insights cron against the current in-progress ISO week. Form-style CSRF. Fire-and-forget; returns HTML toast + OOB-swap button fragment. Documented in detail above under `POST /admin/data/insights/run-now`. |

All admin pages read the local SQLite DB (`AccountStore` / `UsageStore`); an auth lookup failure returns **503**.
