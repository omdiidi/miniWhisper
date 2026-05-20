---
title: OpenAI Whisper API Compatibility
---

# WisprAlt — OpenAI Whisper API Drop-in

WisprAlt's `/v1/*` surface is a drop-in replacement for OpenAI's audio transcription API. Any program that talks to OpenAI's Whisper API can swap `OPENAI_BASE_URL` + `OPENAI_API_KEY` and route to WisprAlt without code changes.

This page is the canonical reference. For a quick getting-started, see [INTEGRATION-GUIDE.md](INTEGRATION-GUIDE.md).

## Setup

```bash
export OPENAI_BASE_URL=https://transcribe.integrateapi.ai/v1
export OPENAI_API_KEY=<your-integration-key>
```

Get a key from the admin UI: <https://transcribe.integrateapi.ai/admin/keys/new>. See [ADMIN.md](ADMIN.md#integration-keys) for the flow.

## Auth

`/v1/*` accepts only `Authorization: Bearer <token>`. The admin session cookie (`wispralt_admin_token`) is **NOT** consulted on `/v1/*` paths — even if a browser tab carries it. This is intentional: integration keys are for programs, not humans.

Multiple `Authorization` headers → 400.
Missing header → 401 OpenAI envelope.
Invalid token → 401 OpenAI envelope, `code: "invalid_api_key"`.

## Endpoints

### `POST /v1/audio/transcriptions`

Sync transcription. Returns text in one of: json (default), text, verbose_json, srt, vtt.

#### Form parameters

| Param | Type | Default | Notes |
|---|---|---|---|
| `file` | file | (required) | Audio bytes. See "File formats" below. ≤25 MB. |
| `model` | string | `whisper-1` | One of 5 listed models. `gpt-4o-transcribe-diarize` returns 404 — use `/transcribe/meeting` for diarization. Unknown models still route to Parakeet but log a warning. |
| `response_format` | string | `json` | One of: `json`, `text`, `verbose_json`, `srt`, `vtt`. |
| `language` | string | — | ISO-639-1 (e.g. `en`). Accepted, ignored (Parakeet is English-only). |
| `prompt` | string | — | Accepted, ignored (no Parakeet equivalent). |
| `temperature` | number | — | 0.0–1.0. Validated range, then ignored. |
| `timestamp_granularities[]` | array | `[]` | `word`, `segment`. Requires `response_format=verbose_json`. |
| `include[]` | array | `[]` | `logprobs` — silently accepted (Parakeet doesn't expose logprobs). |
| `stream` | bool | `false` | If `true`, returns 400 `streaming_unsupported`. Not supported on any model. |
| `user` | string | — | OpenAI optional end-user identifier. Accepted, debug-logged, ignored. |

#### Response formats

**`json` (default)**

```json
{ "text": "Hello, world." }
```

Content-Type: `application/json`.

**`text`**

Plain body. Content-Type: `text/plain; charset=utf-8`. **NOT** JSON-wrapped.

```
Hello, world.
```

**`verbose_json`** (full body shape)

```json
{
  "task": "transcribe",
  "language": "english",
  "duration": 4.8,
  "text": "Hello, world.",
  "segments": [
    {
      "id": 0,
      "seek": 0,
      "start": 0.0,
      "end": 1.2,
      "text": "Hello,",
      "tokens": [],
      "temperature": 0.0,
      "avg_logprob": 0.0,
      "compression_ratio": 1.0,
      "no_speech_prob": 0.0,
      "transient": false
    }
  ],
  "words": [
    { "word": "Hello", "start": 0.0, "end": 0.4 }
  ]
}
```

Notes:
- `language` is the **lowercase full English name** (`"english"`), NOT the ISO-639-1 code (`"en"`). Some strict clients break otherwise.
- `transient: false` is undocumented in OpenAI's spec but always emitted by real OpenAI. Strict TypeScript clients deserializing into typed structs fail without it.
- `tokens` is always `[]` — WisprAlt doesn't have Whisper's BPE token IDs.
- `avg_logprob` / `compression_ratio` / `no_speech_prob` are constants (0.0, 1.0, 0.0) — Parakeet doesn't produce Whisper's quality signals.
- `words[]` only present when `timestamp_granularities[]=word` is set AND aligned tokens were surfaced.
- Empty audio (silent or sub-100ms) → 200 with `text: ""`, `segments: []`.

**`srt`** (SubRip)

```
1
00:00:00,000 --> 00:00:01,200
Hello, world.

2
00:00:01,200 --> 00:00:04,800
Another segment.

```

Content-Type: `application/x-subrip`. Comma decimal separator.

**`vtt`** (WebVTT)

```
WEBVTT

00:00:00.000 --> 00:00:01.200
Hello, world.

```

Content-Type: `text/vtt`. Period decimal separator. `WEBVTT` magic line required.

### `GET /v1/models`

Returns the 5 model IDs WisprAlt advertises. Open WebUI and other clients gate on this endpoint.

```json
{
  "object": "list",
  "data": [
    { "id": "whisper-1", "object": "model", "created": 1677532384, "owned_by": "wispralt" },
    { "id": "gpt-4o-transcribe", "object": "model", "created": 1677532384, "owned_by": "wispralt" },
    { "id": "gpt-4o-mini-transcribe", "object": "model", "created": 1677532384, "owned_by": "wispralt" },
    { "id": "gpt-4o-mini-transcribe-2025-12-15", "object": "model", "created": 1677532384, "owned_by": "wispralt" },
    { "id": "gpt-4o-mini-transcribe-2025-03-20", "object": "model", "created": 1677532384, "owned_by": "wispralt" }
  ]
}
```

`gpt-4o-transcribe-diarize` is **excluded** — it's not on this endpoint, and requesting it via `/v1/audio/transcriptions` returns 404.

Cache headers: `Cache-Control: no-cache, must-revalidate` (so future model-list edits propagate immediately).

### `GET /v1/models/{id}`

Single model object. 404 with `code: "model_not_found"` for unknown IDs.

### `POST /v1/audio/translations` (NOT IMPLEMENTED)

Returns 400 with `code: "endpoint_not_supported"`. Parakeet is English-only — there's no translation path. Use `/v1/audio/transcriptions` for English audio.

## Headers

### Request headers we honor

- `Authorization: Bearer <token>` — required.
- `OpenAI-Organization`, `OpenAI-Project`, `X-Stainless-*`, `User-Agent` — accepted, ignored.

### Response headers we emit

| Header | Notes |
|---|---|
| `x-request-id` | Opaque ID; every client logs this for support correlation. |
| `openai-version` | `2024-10-01` (matches `openai-python` SDK default). |
| `openai-processing-ms` | Server-side wall-clock, including decode + inference. |
| `openai-model` | Echoes the request's `model` form field (or `whisper-1` default). |
| `Retry-After` | On 429 rate-limit responses. Seconds. |
| `Access-Control-Allow-Origin: *` | Set on every response (including error envelopes) so browser clients see real HTTP errors instead of CORS failures. |
| `Cache-Control: no-cache, must-revalidate` | On `/v1/models` responses. |

## Error envelope

All errors return the OpenAI shape:

```json
{
  "error": {
    "message": "<human-readable>",
    "type": "invalid_request_error" | "rate_limit_error" | "server_error",
    "param": null,
    "code": "<machine-readable>",
    "request_id": "<opaque>"
  }
}
```

### Error code reference

| HTTP | `code` | Trigger |
|---|---|---|
| 400 | `streaming_unsupported` | `stream=true` |
| 400 | `validation_failed` | `temperature` out of range; `timestamp_granularities[]` without verbose_json |
| 400 | `invalid_audio_data` | libsndfile decoded but produced unusable output |
| 400 | `unsupported_file_type` | ffmpeg couldn't sniff/decode the file |
| 400 | `decode_timeout` | ffmpeg exceeded 60s |
| 400 | `audio_too_long` | Audio > 15 min (after decode) |
| 400 | `endpoint_not_supported` | Hit `/v1/audio/translations` |
| 401 | `invalid_api_key` | Missing/invalid Bearer token, or cookie-only on /v1 |
| 404 | `model_not_found` | Unknown model on `/v1/models/{id}` or `gpt-4o-transcribe-diarize` on transcriptions |
| 413 | `file_too_large` | File body > 25 MB |
| 422 | `invalid_response_format` | `response_format` not in {json, text, verbose_json, srt, vtt} |
| 429 | `rate_limit_exceeded` | 60 req/min/token exceeded |
| 500 | `transcription_failed` | Unexpected inference error |
| 500 | `internal_error` | Unexpected decode error |

### Why 4xx for client errors matters

`openai-python` retries on 408, 409, 429, and ≥500 by default (up to 2 retries with exponential backoff). Returning 500 for `invalid_audio_data` would cause the SDK to silently retry the same broken upload 3 times — wasting bandwidth and rate-limit budget. WisprAlt maps decoder failures to 400 specifically to keep client retry behavior sane.

## File formats

Supported via two decoders:

| Format | Decoder |
|---|---|
| wav, flac, ogg (Vorbis/Opus), aiff, au, caf | libsndfile (`soundfile` in `audio.py`) |
| mp3, m4a, mp4, webm, aac, mpeg, mpga | ffmpeg (`sync_decode.py`) |

Note that ffmpeg sniffs by content (magic bytes), not by extension or Content-Type header — but the upload must pass libsndfile-then-ffmpeg fallthrough successfully.

## Limits

| Limit | Value | Source |
|---|---|---|
| Max file size | 25 MB | App-layer cap, matches OpenAI |
| Max duration (after decode) | 15 minutes | `dictation_max_duration_s = 900` |
| Rate limit | 60 requests / 60 seconds **per token** | Independent of `/transcribe/dictate` per-IP bucket |
| Concurrent requests | Effectively serialized | Single-thread MLX executor in ParakeetService |
| ffmpeg decode timeout | 60 seconds | `sync_decode.TIMEOUT_S` |

Uploads exceeding 100 MB hit Cloudflare's edge with a non-JSON 413 response (Cloudflare's default 413 HTML page). Size-check locally before posting.

## Concurrency model

WisprAlt's Parakeet inference runs in a single-thread `ThreadPoolExecutor` (see `dictate/parakeet.py:39-40`). Concurrent `/v1/audio/transcriptions` requests serialize naturally — there is one transcription in flight at a time across the entire server, regardless of how many tokens or IPs are calling.

This means:
- You can paste your token into multiple programs simultaneously, but they'll process in series.
- Per-token rate limit (60/min) is the upstream gate; throughput is bounded by inference wall-clock (~200ms for short clips on M4, scaling with audio length).

## Rate limits

Per-token: **60 requests per 60-second sliding window**, keyed on the token's internal user id. Independent of the per-IP rate limit on `/transcribe/dictate` — your native WisprAlt client and your `/v1` traffic count against different buckets.

`Retry-After` header on 429 responses indicates seconds to wait. `openai-python` SDK consumes it automatically.

Break-glass admin tokens bypass the per-token limit.

## Key kinds: employee vs integration

WisprAlt tokens have a `kind` field:
- **`kind='employee'`** — minted via `/admin/users/new`. For human employees using the macOS client. Has access to `/v1/*`, `/me/*`, `/telemetry/*`, and (if `role='admin'`) `/admin/*`.
- **`kind='integration'`** — minted via `/admin/keys/new`. For third-party programs. Has access to `/v1/*` ONLY. Blocked from `/me/*` and `/telemetry/*` with a 403 (those surfaces are for humans, not programs).

For drop-in use with `openai-python` or similar, mint a `kind='integration'` key.

## Tested clients

The following clients have been verified against WisprAlt's `/v1` (or are expected to work based on documented Whisper-API compatibility):

| Client | Verified | Notes |
|---|---|---|
| `openai-python` SDK | yes | `client.audio.transcriptions.create(...)` works for all 5 response_formats. |
| `openai-node` SDK | expected | Same multipart contract as Python. |
| curl | yes | Used by `scripts/verify-openai-compat.sh`. |
| Buzz (subtitle generator) | expected | Requests `verbose_json` for SRT export. |
| MacWhisper | expected | Custom-endpoint MDM config. |
| Open WebUI voice mode | expected | Probes `/v1/models` then picks first; works because diarize is excluded from list. |
| Bazarr / subgen | expected | `verbose_json` consumer. |
| OBS Whisper plugin | expected | Only needs `response_format=text`. |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 401 `invalid_api_key` | Token revoked or wrong. Mint a new one via `/admin/keys/new`. |
| 401 with valid token | Sending cookie instead of `Authorization: Bearer ...` |
| 400 `unsupported_file_type` | ffmpeg can't decode the file. Pre-convert to wav/mp3 locally. |
| 400 `audio_too_long` | Audio > 15 min. Use `/transcribe/meeting` for longer clips. |
| 429 in rapid succession | Hit the 60/min/token cap. Honor `Retry-After`. |
| Browser client fails silently with no error | CORS preflight failing. Check browser console for the actual error. |
| 413 with HTML body (not JSON) | Cloudflare edge rejection on > 100 MB. Size-check locally. |

## What's not supported

| Feature | Status |
|---|---|
| SSE streaming (`stream=true`) | Not supported on any model. Matches real OpenAI's whisper-1 behavior. |
| Diarization (`response_format=diarized_json`, `model=gpt-4o-transcribe-diarize`) | Use `/transcribe/meeting` (async, native WisprAlt). |
| Translation (`/v1/audio/translations`) | Parakeet is English-only. |
| `include[]=logprobs` (returning logprobs) | Silently accepted; not surfaced (Parakeet doesn't produce logprobs). |
| `language` parameter (forcing a non-English language) | Accepted, ignored. Output is always English-detected. |

## Getting an API key

See [ADMIN.md](ADMIN.md#integration-keys) for the `/admin/keys/new` flow.
