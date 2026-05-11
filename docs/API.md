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

These are intentionally open so Kubernetes-style probes, Cloudflare health checks, and external monitoring can poll them without API credentials. The probes expose only ready-flag booleans + free RAM in MB — no user data, no audio, no model output.

**Bearer required:**
- `POST /transcribe/dictate`, `POST /transcribe/meeting`, `GET /transcribe/meeting/{id}`, `GET /transcribe/meeting/{id}/download/{fmt}`, `DELETE /transcribe/meeting/{id}`
- `POST /v1/audio/transcriptions` (OpenAI-compatible drop-in)
- `GET /me`, `PATCH /me`
- `GET /metrics`
- All `/admin/*` routes **except** `/admin/login`. The admin UI also accepts a `wispralt_admin_token` cookie (set by `POST /admin/login`) as a fallback for browser navigation.
- `/admin/*` (except login) additionally require `role='admin'` — an employee-role token gets **403**.

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

All authed admin routes return **503** "Admin UI unavailable: Postgres degraded." when `app.state.db_pool` is `None` — the admin UI is unusable without a pool, so it fails loudly rather than crashing on `AttributeError` deeper in. The break-glass admin path (env-var bearer when Postgres is unreachable) lets the operator authenticate to the rest of the API but **not** to the admin UI; restart the server once Postgres is back to recover.
