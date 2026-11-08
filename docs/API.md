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
- `GET /readyz/db` — local SQLite DB readiness (`reason`: `db_error` / `schema_outdated`; body carries `usage_write_failures` and `dictation_save_failures`)
- `GET /admin/login` and `POST /admin/login` — admin UI login form (chicken-and-egg: must be reachable without the cookie it sets)
- `GET /me/login` and `POST /me/login` — employee portal login form (Phase 2; same chicken-and-egg reason)

These are intentionally open so Kubernetes-style probes, Cloudflare health checks, and external monitoring can poll them without API credentials. The probes expose only ready-flag booleans + free RAM in MB — no user data, no audio, no model output.

**Bearer required:**
- `POST /transcribe/dictate`, `POST /transcribe/file` (+ chunked), `GET /transcribe/file/{id}`, `GET /transcribe/file/{id}/download/{fmt}`, `DELETE /transcribe/file/{id}`
- `POST /v1/audio/transcriptions`, `GET /v1/models`, `GET /v1/models/{id}` (OpenAI-compatible drop-in surface — see [OPENAI-COMPAT.md](OPENAI-COMPAT.md))
- `GET /me`, `PATCH /me`, `GET /me/insights` (Phase 2 — employee self-view)
- `GET /me/history`, `GET /me/history/{kind}/{row_id}`, `DELETE /me/history/{kind}/{row_id}`, `POST /me/history/{kind}/{row_id}/restore`, `GET /me/history/{kind}/{row_id}/download/{fmt}` (Plan A — personal transcript archive)
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
| `X-Client-Dedup-Id` | UUID string | absent ⇒ no dedup | Optional here (required on the stream routes). RFC-4122 shape, lowercased on read; malformed → 422. Names one dictation: the server claims the id for the whole request (concurrent requests with the same id, from any user, run inference once), replays a stored dictation without inference, and stores at most one `dictations` row per id. Absent ⇒ no claim, no replay, the row is stored with a NULL id. See [Client dedup contract](#client-dedup-contract). |

#### Client dedup contract

Pinned in Part 4 (server `dedup.py`, Locked Decision 7); the macOS client (Core `LocalRequests`) implements it.

1. Every Local dictation request (whole-file, every stream chunk, finalize, Retry) sends `X-Client-Dedup-Id: <dictation id>` (lowercase UUID). Finalize may also send the `client_dedup_id` form field; header and field present and unequal → 422.
2. Every 200 dictation response carries `"saved": true|false` and `"replayed": true|false`. `"saved": false` means the server returned the text but could not store it (a `sqlite3.Error`; `dictation_save_failures_total` +1): the client delivers the text **and keeps the audio and its Retry entry**; a later Retry with the same id transcribes and saves again.
3. `409 {"error":"dedup_conflict"}` (the id belongs to another user) and `410 {"error":"dictation_deleted"}` (the owner deleted that dictation; re-running would resurrect it) are **terminal**: no inference, no row; the client discards the entry. A 409/410 without an `error` key (e.g. `{"detail":"Session is aborted"}` from a stream route) is not terminal.
4. `"replayed": true` means the stored text was returned without new inference (`duration_ms` is 0). A replay writes no `usage_events` row.

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
  "duration_ms": 143.7,
  "replayed": false,
  "saved": true
}
```

`duration_ms` is wall-clock Parakeet inference time (excludes queue wait). The row is saved synchronously before the response; blank text is not stored and reports `"saved": true` (nothing to keep).
The `smart_formatted` key was removed in Part 4 along with server smart formatting.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer token |
| 413 | Upload size exceeds `MAX_UPLOAD_BYTES` |
| 409 | `{"error":"dedup_conflict"}` — the dedup id is another user's (terminal) |
| 410 | `{"error":"dictation_deleted"}` — the owner deleted that dictation (terminal) |
| 415 | `Content-Type` is not `audio/*` |
| 422 | Malformed `X-Client-Dedup-Id`, or audio bytes cannot be decoded (corrupt file). Includes `LibsndfileError` and `RuntimeError` raised by `soundfile.read` — the decode boundary in `parakeet.py:_sync_transcribe` converts both to `CorruptAudioError` and the route handler maps them to 422. Regression-locked by `server/tests/test_dictate_corrupt_audio.py`. |
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

**Request:** `multipart/form-data` with a single `file` field; `Content-Type: audio/wav` (or `audio/*`). Header `X-Client-Dedup-Id` is **required** (422 if absent or malformed); the session records it when it opens.

**Response 202:**

```json
{ "received_index": 0, "queue_depth": 1 }
```

If a dictation with this dedup id is already stored for the caller, the chunk is not processed and no session is opened: `202 {"received_index": N, "queue_depth": 0, "finalized": true}`.

`queue_depth` is the count of currently-pending Parakeet tasks for this session (≥1 because the just-ingested chunk is counted). The route returns as soon as the chunk is enqueued — actual Parakeet work runs asynchronously and lands in `partial_texts` when complete.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer token. |
| 403 | The bearer resolves to a user who is not the session's owner (recorded at first-chunk open). |
| 409 | `per_user_busy` — this user already owns a different active session. Either reuse its `session_id` or wait for it to finalize/expire. Also: the session was opened under a different dedup id (`{"detail": …}`), or `{"error":"dedup_conflict"}` (the id is another user's). |
| 410 | `{"error":"dictation_deleted"}` for a deleted stored id; otherwise (`{"detail": …}`) session is no longer active (status ∈ {finalizing, done, aborted, expired}). The client should fall back to single-shot `/transcribe/dictate` with the safety-buffer WAV. |
| 413 | Cumulative voiced audio for this session exceeds the 270 s mid-chunk ceiling. (Finalize allows up to 300 s.) |
| 415 | `Content-Type` is not `audio/*`. |
| 422 | `session_id` is not a valid UUID, `X-Client-Dedup-Id` missing or malformed, OR audio bytes cannot be decoded. |
| 429 | Per-session pending-task queue depth exceeded (defends against runaway clients). |
| 503 | `capacity_exceeded` — the global 2-session cap is already saturated. Retry after the other sessions drain, or fall back to single-shot. |

---

### `POST /transcribe/dictate/stream/{session_id}/finalize`

**Auth:** Bearer required; same per-user-owner gate as `/chunk`. **Opt-in, experimental.**

Close out a streaming session. The dedup id is claimed first and a stored dictation is replayed **before** the session lookup, so a Retry after the session expired still gets its text (`"replayed": true`). Otherwise the server waits up to 15 s for all pending chunk tasks to drain, joins their texts in index order, persists the row to `dictations`, and returns the final text. The chunked-receive route stops accepting new chunks for this `session_id` (returns 410) the moment finalize starts.

**Path params:** `session_id` (UUID v4) — same as `/chunk`.

**Request:** `multipart/form-data`

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | UploadFile | Yes | The tail WAV — the trailing speech segment that was still being accumulated when FN was released. `Content-Type: audio/*`. May be empty (zero-byte) if the speaker happened to finish exactly on a silence cut. |
| `smart_format` | — | — | **Removed (Part 4).** Ignored if an older client still sends it; the text is never post-processed on the server. |
| `client_dedup_id` | UUID | No | Optional copy of the required `X-Client-Dedup-Id` header (the header wins; both present and unequal → 422). Must equal the id the session opened with (else 409). |
| `speech_started_at` | float (Unix epoch, sub-second precision) | Yes | The local wall-clock time the *user* began speaking (NOT FN press) — sent so the server can compute end-to-end latency from speech start to text injection without depending on local clock-sync. Sub-second precision is preserved (don't round to int). |

**Response 200:**

```json
{
  "text": "Hello world.",
  "model_id": "mlx-community/parakeet-tdt-0.6b-v2",
  "duration_ms": 213.4,
  "replayed": false,
  "saved": true
}
```

`duration_ms` on streaming rows is **cumulative** per-chunk Parakeet inference time (sum of N chunks), not single-pass wall-clock. See [ARCHITECTURE.md → Known limitations](ARCHITECTURE.md#known-limitations) for the semantic drift versus single-shot dictation rows. The response envelope is otherwise byte-identical to `/transcribe/dictate` so the client's existing decoder is unchanged.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer token. |
| 403 | The bearer's user is not the session's owner. |
| 409 | `gap_detected` — `partial_texts` contains a `None` entry between two ingested chunks (a chunk POST landed for index N but index N−1 never arrived). The client should fall back to single-shot. |
| 409 | Also: the dedup id differs from the session's, or `{"error":"dedup_conflict"}`. |
| 410 | `{"error":"dictation_deleted"}` (terminal), or `{"detail": …}`: session is not in `status="active"` (already finalizing, done, aborted, or expired). |
| 413 | Cumulative voiced audio + tail exceeds 300 s. |
| 415 | Tail `Content-Type` is not `audio/*`. |
| 422 | `session_id` not a valid UUID; dedup id missing, malformed, or header ≠ form field; OR tail audio cannot be decoded. |
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

**Auth:** Bearer required (any role), or the session cookie **plus** the CSRF token (`X-CSRF-Token` header = `wispralt_csrf` cookie; otherwise 403). A Bearer request needs no CSRF token (the macOS client sends Bearer + JSON).

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

### `GET /me/dictations/last` (removed)

Removed in Part 4 with `JobStore.get_most_recent_dictation`: since Part 3 the macOS client's "Copy last dictation" reads its own `last.json`. The route now returns 404.

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

Per-employee insights page (Part 6). Two separate parts: **Measured** stats for the selected window, computed from the caller's own rows (`insights.measured.compute`: counts, words, active days, file audio minutes, regex filler counts, time saved as a labelled 40 wpm estimate, dictation latency p50/p95, error rate, 8-week trend, hour-of-day), each count tile linking to `/me/history?since=&until=` for the same window; and the **AI interpretation** of the last full ISO week (schema v2: every item links to its source records, hidden when they were deleted; "AI interpretation is off" until an admin enables it).

**Query parameters:**

| Field | Type | Default | Notes |
|---|---|---|---|
| `range` | enum | `7d` | One of `today`, `7d`, `30d`, `90d`, `1y`, `all` (unknown → `7d`). Drives the Measured window only — the AI panel always shows the most recent completed ISO week. |

**Response 200** — full page. `Content-Type: text/html` rendering `me_insights.html.j2`.

**Response 200 (HTMX partial)** — when the request carries `HX-Request: true`, the handler returns only `_me_insights_body.html.j2` (Measured + AI panel) for in-place swap.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer. |
| 503 | Auth lookup failed (`sqlite3.Error`). |

How the AI items are tied to records is described in [ARCHITECTURE.md → Weekly insights](ARCHITECTURE.md#weekly-insights).

Source: `server/src/wispralt/routes/me.py` (Phase 2).

---

### `GET /me/history`

**Auth:** Bearer or session cookie (any role; integration keys get 403).

Personal transcript archive — the calling user's own dictations + file transcriptions in one chronological list. Filterable by time range and kind. Without `q` it is the plain list (`JobStore.transcripts_in_range_filtered`, per-leg cursors); with `q` of 2+ characters it is a **full-text search** (SQLite FTS5, `JobStore.search_history`, the caller's `user_id` bound in SQL) returning highlighted snippets (escaped first, then `<mark>`) with offset paging.

**Query parameters:**

| Field | Type | Default | Notes |
|---|---|---|---|
| `range` | enum | `30d` | One of `today`, `7d`, `30d`, `90d`, `1y`, `all`. Reuses `insights.timewindow.epoch_for_range`. |
| `kind` | enum \| empty | unset | Either `dictation` or `file` (displayed as "File transcriptions"). Empty string from a `<select>` is normalized to "no filter". The row routes `/me/history/{kind}/{row_id}` take the same two kinds; `meeting` (pre-Part 4) now 404s. |
| `q` | string | unset | Full-text query (Part 6; replaces the old `search` LIKE filter). Fewer than **2 chars after `.strip()`** → the plain list. Never passed raw to FTS: each whitespace token is quoted, tokens are ANDed, the last is a prefix match; capped at 200 chars / 12 tokens. Diacritics are folded (`cafe` finds `café`). |
| `since` / `until` | unix seconds | unset | Override the `range` tab (the Measured tiles link here with them). |
| `offset` | int ≥ 0 | 0 | Search-mode paging. |
| `dict_cursor` | opaque | unset | Base64-urlsafe encoded `(created_at, row_id)` tuple for the dictations leg. Malformed → silently restart pagination. |
| `jobs_cursor` | opaque | unset | Same shape, for the jobs (file transcription) leg. |

**Response 200** — full page (`me_history.html.j2`).

**Response 200 (HTMX nav)** — when `HX-Request: true` is present and no cursor was sent, returns `_me_history_body.html.j2` (the body fragment for in-place swap).

**Response 200 (HTMX Load-more)** — when a cursor or an `offset` was sent, returns `_me_history_page.html.j2` (the next batch of `<tr>` rows + the updated Load-more button). Page size is `settings.history_page_size` (default **50**).

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

One owned record. With `HX-Request: true` it returns the expanded row partial (or the compact one with `?compact=1`); a plain browser request returns a full **record page** (`record_detail.html.j2`, the target of AI source links). Unknown `kind`, unknown `row_id`, or a `row_id` owned by another user all uniformly return **404** so we never leak existence to non-owners.

**Path parameters:**

| Field | Type | Notes |
|---|---|---|
| `kind` | enum | `dictation` or `file` (shown as "File transcription"). Anything else (incl. the pre-Part 4 `meeting`) → 404. |
| `row_id` | string | Job UUID for `file`, integer row id for `dictation`. |

**Query parameters:**

| Field | Type | Default | Notes |
|---|---|---|---|
| `compact` | bool | `false` | When `true`, return `_me_history_row.html.j2` (collapsed). Otherwise return `_me_history_row_expanded.html.j2` with the full transcript text + Delete + Download buttons. |

**Response 200** — partial HTML fragment ready for HTMX swap-in, or the full record page.

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer. |
| 404 | Unknown kind, unknown row_id, soft-deleted, or owned by another user. |

Source: `server/src/wispralt/routes/me.py` (Plan A; record page Part 6).

---

### `DELETE /me/history/{kind}/{row_id}`

**Auth:** Session cookie + CSRF token, or a Bearer token (no CSRF token needed). Ownership-checked.

Soft-delete one owned row. Flips `deleted_at = NOW()` on the underlying row; downstream read paths (`/me/history`, `compute_user_stats`, the weekly-insights cron) all filter `deleted_at IS NULL`, so the row disappears from every surface. Reversible via the `/restore` route below until the 90-day TTL sweep zeros the transcript text.

**Request:** `application/x-www-form-urlencoded`

| Field | Type | Notes |
|---|---|---|
| `csrf_token` | string | Double-submit value matched against the cookie set by the prior `GET /me/history*` render (or send it as `X-CSRF-Token`). Constant-time compare via `hmac.compare_digest`. Not needed with a Bearer token. |

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

**Auth:** Session cookie + CSRF token, or a Bearer token (no CSRF token needed). Ownership-checked.

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

**Auth:** Bearer or session cookie + `role='admin'` (employees get 403).

Data & Insights for the team, or one person with `?user_id=N`: **Measured** stats (`insights.measured.compute`; tiles link to `/admin/records?since=&until=` [+ `user_id`]), the separate **AI interpretation** panel (team or person insight of the last full ISO week, every item linked to `/admin/records/{kind}/{id}`), the per-person table (team view) and the **AI runs** panel.

**Query parameters:** `range` (as `GET /me/insights`), `user_id` (optional).

**Response 200** — full page (`data.html.j2`); mints the CSRF token the runs form carries. **Response 200 (HTMX partial)** — with `HX-Request: true`, only `_admin_data_body.html.j2`, and the CSRF cookie is NOT rotated.

Source: `server/src/wispralt/routes/admin_data.py`.

---

### `POST /admin/data/runs`

**Auth:** admin + CSRF (form `csrf_token` / `X-CSRF-Token`, or a Bearer token). Employees 403.

Start weekly AI insights for the last full week or a backfill — estimate first.

| Field | Type | Notes |
|---|---|---|
| `weeks` | int | 1..12 (clamped). 1 = the last full ISO week (`trigger='manual'`); more = that many weeks ending with it (`trigger='backfill'`, run one after another under the insights lock). |
| `force` | bool | Regenerate people who already have an insight for the week. |
| `confirm` | bool | Without it: **no run**; returns the estimate (words × 1.35 / 1000 × the model's input rate, plus the 30-day spend vs cap) and a Start button. With `confirm=1`: starts the run(s) in a background task. |

**Response 200** — `text/html` fragment for `#run-confirm` (estimate, or "Started"). **409** — AI interpretation is off, `OPENROUTER_API_KEY` is not configured, or a run already holds the lock.

### `GET /admin/data/runs/current`

**Auth:** admin. The recent `insight_runs` list partial (status, people done / total, cost, estimate, failures `{user_id, error_class}`). While a run is going the fragment re-polls itself every 2 s. Never sets a CSRF cookie.

### `GET /admin/settings`, `POST /admin/settings`

**Auth:** admin; POST also CSRF. Form fields: `enabled`, `acknowledge` (the disclosure; required once before `enabled` can be on), `model` (OpenRouter id), `cap_30d_usd` (0..1000), `timezone` (IANA name; blank = server zone). Success → 303 `/admin/settings?saved=1`; a validation error (no ack, bad cap/model/timezone) → 400 with the form. The OpenRouter key is never accepted here.

### `GET /admin/records`, `GET /admin/records/{kind}/{row_id}`

**Auth:** admin (employees 403). Team records: `q` (2+ chars → full-text search via `search_history_team`, highlighted, owner names), `user_id`, `kind`, `range` / `since` / `until`, `offset`. The record page shows one record with its owner and states that admins can read team transcripts; 404 for an unknown or deleted record.

### `GET /admin/system`, `GET /admin/system/snapshot`

**Auth:** admin. The system page and its 5 s HTMX partial; `?format=json` on the snapshot returns `{host, cpu, mem, disk, models, queue, latency, errors, counters, db, network, clients}` (see [ADMIN.md → System](ADMIN.md#get-adminsystem--system)). A DB failure yields `db.ok: false`, never a 500.

---

### `POST /invites/redeem`

**Auth:** none (public; reachable through the tunnel). Rate limit: 10 attempts per 60 s per client IP (`CF-Connecting-IP`, else the peer) → **429** `{"error": "rate_limited"}`.

Redeems a single-use invite made at `/admin/invites`. The invite reaches the app as the join link **`wispralt://join?host=<url-encoded SERVER_URL>&invite=<token>`** (or the token pasted by hand); the app posts the token here.

**Request:** `application/json`

| Field | Type | Notes |
|---|---|---|
| `invite` | string | The invite token (1..200 chars). |
| `device_label` | string, optional | Shown as the device name on the person's page (control characters dropped, 80 chars max). |

**Response 200** (`Cache-Control: no-store`):

```json
{"token": "<64-hex API key>", "user_label": "sarah", "host": "https://transcribe.example.com"}
```

In one `BEGIN IMMEDIATE` transaction the server checks the invite exists, is unredeemed and unexpired (10-minute TTL) and that its creator is still an enabled admin; creates the person (new-person invite, kind `employee`) or adds a key to them ("add device"); and marks the invite used. Redeeming never revokes the person's other keys.

**Response 400** — `{"error": "invite_invalid"}` for **every** failure: unknown, already used, expired, revoked, creator disabled, target person disabled, malformed body. The body is identical in all cases.

Source: `server/src/wispralt/routes/admin_invites.py`, `accounts/service.py` (`redeem_invite`, `join_link`).

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

Cancel a pending or running job, or remove a finished job's output files. The client calls it to cancel, and after it has downloaded all four formats.

| Job state | Response | Effect |
|---|---|---|
| Missing, or owned by someone else | **204** | Nothing (idempotent; no existence leak). |
| `pending` or `running`, or `failed` by the watchdog while the runner is still executing it | **202** | Cancel is requested. The status is then re-read: if the job finished in the meantime (and the runner no longer executes it), its outputs are removed and the response is **204** instead. |
| `done` or `failed` | **204** | Outputs (`{job_id}.{json,srt,vtt,txt}`) are removed and `outputs_removed_at` is set. The row and its transcript text **stay** for `/me/history` and retention; on `/transcribe/file/*` the job is then invisible (poll and download 404, a repeat `DELETE` 204). |
| `done` or `failed`, but an output file could not be removed | **500** `{"error"}` | The job stays visible so a retried `DELETE` or the retention purge can finish the cleanup. |

**What a 202 means:**

- A cancel that lands while the file is being decoded stops the job right after the decode, before the file model loads (`failed: cancelled by user`). A requested cancel also wins over a phase timeout or watchdog failure: the job reads `cancelled by user`.
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

**Errors:** 403 (ownership), 404 (upload not found), 409 (a chunk index in `0…chunk_count-1` is missing — the detail names it, e.g. `Missing chunk 1 of 3`; only `chunk-NNNN.part` files with those exact names count — or a size mismatch), 429 (same body and `code` as `POST /transcribe/file`; the assembled file is unlinked, retry after 60 s), 503 (RAM <4 GiB), 507 (disk).

**Stale-upload TTL:** chunked staging directories whose `meta.json` mtime is older than 1 h are reaped at server startup (and on demand by `ops.staging.sweep_chunked`). Each successful chunk write touches `meta.json` so actively-progressing uploads are never reaped.

---

### `POST /admin/rotate-key` (removed)

Removed in Part 4 with the env admin key. The first admin comes from `wispralt setup --role host`; a lost admin key is replaced with `wispralt keys rotate-admin` (both print the token once, or write it to a new `--token-file`). Employee and integration keys are rotated from the admin UI (`/admin/people/{id}/rotate`, `/admin/integrations/{id}/rotate`) or `wispralt keys rotate <user_id>`.

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
  "dictation_save_failures_total": 0,
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
| `dictation_save_failures_total` | Dictations whose text was returned but whose row could not be saved (`sqlite3.Error`; the response said `"saved": false`). Resets on restart. Also in the `/readyz/db` body as `dictation_save_failures`. |
| `process_uptime_seconds` | Wall time since the FastAPI lifespan started (`time.monotonic() - observability.process_started_at_monotonic`). Useful as a deploy/restart sentinel: `< 60` immediately after `wispralt restart`. |

---

## Admin API

The `/admin/*` routes (other than `/admin/login`, `/admin/active`, `/admin/server-log` and `/metrics`) render **HTML** via Jinja2 templates, not JSON. They are intended for browser navigation; curl/Postman work too but you'll get markup back.

Source: `server/src/wispralt/routes/admin_ui.py` +
`server/src/wispralt/admin/templates/*.html.j2`.

**Auth split (be720a1):** Three routers under the same `/admin` prefix.

- `/admin/login` and `/admin/setup` — public (no auth; setup 404s once an enabled admin exists and needs the `setup-code` file's content).
- `/admin/me` — any authenticated role (admin OR employee). Admins are 303'd to `/admin/`; employees see their own person page without admin actions. Universal entry point — the macOS client's "Open Portal" button targets `/admin/login` and the server figures out where to send each role.
- Everything else under `/admin/` — admin-only (employee tokens get **403**). `POST /invites/redeem` is the one public non-`/admin` portal route.

Browser users hit `/admin/login` once to set the `wispralt_admin_token` cookie; curl/Postman users send `Authorization: Bearer <token>` on each request. See [ARCHITECTURE.md → Admin UI](ARCHITECTURE.md#admin-ui) for the three-router pattern.

| Method | Path | Auth | Returns |
|---|---|---|---|
| GET | `/admin/login` | none | HTML form (200) |
| POST | `/admin/login` | none | 303 → `/admin/` (admin) or `/admin/me` (employee) + `Set-Cookie: wispralt_admin_token=...`. 401 on invalid token. |
| GET | `/admin/me` | any role | Admin: 303 → `/admin/`. Employee: 200 with their own person page (no admin actions or nav). |
| GET/POST | `/admin/setup` | none (gated) | First-admin setup: code from `DATA_DIR/setup-code` + label → one admin, key shown once (`no-store`), session cookie. 403 wrong code / missing CSRF; 404 once an admin exists. |
| GET | `/admin/` | admin | Overview dashboard HTML |
| GET | `/admin/people` | admin | People list |
| GET/POST | `/admin/people/new` | admin | Add employee form / create + install command (key shown once, `no-store`) |
| GET | `/admin/people/{id}` | admin | Person page: tiles, devices (per-key last seen), recent requests, actions |
| POST | `/admin/people/{id}/rotate` | admin + CSRF | Revoke all the person's keys, issue one (re-enables); key shown once (`no-store`). 404 unknown. |
| POST | `/admin/people/{id}/disable` | admin + CSRF | 303 → person page; every key 401s from the next request. 400 for yourself. |
| POST | `/admin/keys/{key_id}/revoke` | admin + CSRF | Revoke one device; 303 → its person; 404 unknown key. |
| GET/POST | `/admin/invites` | admin (+ CSRF on POST) | Invites list / create (new person `label`+`role`, or `user_id` = add device); link + code shown once (`no-store`). |
| POST | `/admin/invites/{id}/revoke` | admin + CSRF | Expire a pending invite; 303 → `/admin/invites`. |
| GET/POST | `/admin/integrations`, `/admin/integrations/new` | admin (+ CSRF on POST) | Integration keys list / create (key + OpenAI env snippet shown once, `no-store`). |
| POST | `/admin/integrations/{id}/rotate`, `/admin/integrations/{id}/revoke` | admin + CSRF | Rotate (new key once) / revoke (disable) an integration. A non-integration user id → 404 (yourself → 400). |
| GET | `/admin/usage` | admin | Drill-down HTML, paginated 100/page. Filters: `kind`, `status`, `user_id`, `since`, `until`, `offset`. |
| GET | `/admin/usage.csv` | admin | `text/csv` stream (max 10000 rows) |
| GET | `/admin/records`, `/admin/records/{kind}/{id}` | admin | Team records search/list and one record. |
| GET | `/admin/data` | admin | Data & Insights (Measured + AI panel + runs). |
| POST | `/admin/data/runs` | admin + CSRF | Estimate, then (`confirm=1`) start a run or backfill. |
| GET | `/admin/data/runs/current` | admin | Runs list partial (polls every 2 s while running). |
| GET/POST | `/admin/settings` | admin (+ CSRF on POST) | AI enablement + disclosure, model, cap, timezone. |
| GET | `/admin/system`, `/admin/system/snapshot` | admin | System page and its partial (`?format=json`). |

All admin pages read the local SQLite DB (`AccountStore` / `UsageStore`); an auth lookup failure returns **503**.
