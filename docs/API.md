---
title: API Reference
---

# API Reference

Complete HTTP API contract for the WisprAlt server. Implemented in `server/src/wispralt_server/routes/`.

---

## Authentication

All `/transcribe/*`, `/admin/*`, and `/readyz/*` endpoints require:

```
Authorization: Bearer <WISPRALT_API_KEY>
```

Authentication is performed by `auth.py` using `secrets.compare_digest` for constant-time comparison. A missing or wrong token returns **401**. `/healthz` is the only unauthenticated endpoint.

**Additional auth validation:**
- An empty token after the `Bearer ` prefix returns **401** `"Empty bearer token"`.
- Multiple `Authorization` headers in one request return **400** `"Multiple Authorization headers not allowed"`.

---

## Rate Limits

Enforced by `middleware/rate_limit.py` (per-IP, in-memory rolling window):

| Route group | Limit |
|---|---|
| `POST /transcribe/dictate` | 60 requests per 60-second window (configurable: `DICTATE_RATE_PER_MIN`) |
| `POST /transcribe/meeting` | 4 requests per 3600-second window (configurable: `MEETING_RATE_PER_HOUR`) |

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

**Auth:** Bearer required.

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

**Auth:** Bearer required.

Readiness probe for the meeting pipeline.

**Response 200** — all models loaded and ≥ 2 GiB RAM available:
```json
{ "status": "ok", "available_mb": 9216 }
```

**Response 503** — models not loaded:
```json
{ "status": "not_ready", "detail": "Meeting pipeline models not loaded", "available_mb": 9216 }
```

**Response 503** — insufficient RAM (< 2 GiB available):
```json
{ "status": "not_ready", "detail": "Insufficient available memory", "available_mb": 1800, "required_mb": 2048 }
```

---

### `POST /transcribe/dictate`

**Auth:** Bearer required.

Transcribe a short audio clip using the warm Parakeet model.

**Request:** `multipart/form-data`

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | UploadFile | Yes | Must have `Content-Type: audio/*`. WAV preferred; any soundfile-supported format accepted. |

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
  "duration_ms": 143.7
}
```

`duration_ms` is wall-clock Parakeet inference time (excludes queue wait).

**Errors:**

| Code | Condition |
|---|---|
| 401 | Missing or invalid bearer token |
| 413 | Upload size exceeds `MAX_UPLOAD_BYTES` |
| 415 | `Content-Type` is not `audio/*` |
| 422 | Audio bytes cannot be decoded (corrupt file) |
| 429 | Rate limit exceeded (60 req/min) — includes `Retry-After: 60` header |

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
    "current_eta_s": null
  },
  "memory": {
    "rss_mb": 7482,
    "available_mb": 8704
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
  }
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
| `memory.rss_mb` | Server process RSS in MiB |
| `memory.available_mb` | System available RAM in MiB |
| `disk.free_gb` | Free disk on staging volume in GiB |
| `disk.staging_count` | Number of staging WAV files currently on disk |
| `requests_total` | Request counts keyed by `"route:status"` (last process lifetime) |
| `errors_total` | Error counts keyed by `"route:status"` for 4xx/5xx responses |
| `latencies` | Per-route p50/p95/p99 latency in ms; `null` if fewer than 2 observations |
