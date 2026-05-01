---
title: Integration Guide
---

# Integration Guide — Use WisprAlt as a Drop-in Transcription Provider

This guide shows how to point any third-party project at WisprAlt's self-hosted
transcription API. WisprAlt exposes an OpenAI-compatible endpoint, so any client
that speaks the OpenAI Audio API (Python SDK, Node SDK, curl, etc.) just needs
two environment variables changed.

For the full WisprAlt-native API (async meetings, /me, admin), see
[API.md](API.md).

---

## Prerequisites

- WisprAlt server is running and reachable. The default public URL is
  `https://transcribe.integrateapi.ai` (see [SETUP-SERVER.md](SETUP-SERVER.md)
  if you need to deploy your own).
- You have a WisprAlt API key. Get one from the admin dashboard at
  `https://transcribe.integrateapi.ai/admin/`, or ask your operator to mint
  one for you (see [ADMIN.md](ADMIN.md)).

---

## Setup (3 lines)

Set these two environment variables in your project. The env var names follow
OpenAI conventions so existing libraries auto-pick them up:

```bash
export OPENAI_BASE_URL=https://transcribe.integrateapi.ai/v1
export OPENAI_API_KEY=<your-wispralt-token>
```

> The value of `OPENAI_API_KEY` is your WisprAlt token. The env var name is
> what OpenAI's SDKs look for; renaming would break drop-in compatibility.

---

## Quick examples

### Python (`openai` package)

```python
from openai import OpenAI

client = OpenAI()  # picks up OPENAI_BASE_URL + OPENAI_API_KEY automatically

with open("audio.wav", "rb") as f:
    resp = client.audio.transcriptions.create(
        file=f,
        model="whisper-1",         # any value; we route to Parakeet TDT internally
        response_format="json",    # or "text"
    )
print(resp.text)
```

### Node.js (`openai` package)

```javascript
import OpenAI from "openai";
import fs from "fs";

const client = new OpenAI();  // env vars auto-picked

const resp = await client.audio.transcriptions.create({
  file: fs.createReadStream("audio.wav"),
  model: "whisper-1",
  response_format: "json",
});
console.log(resp.text);
```

### curl

```bash
curl https://transcribe.integrateapi.ai/v1/audio/transcriptions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F file=@audio.wav \
  -F model=whisper-1 \
  -F response_format=json
```

### Swift (URLSession)

Use `multipart/form-data` with field `file` pointing at your audio data. See
`client/WisprAlt/Server/DictationAPI.swift` in this repo for a reference
implementation.

---

## Supported parameters

| Parameter         | Required | Notes                                                              |
| ----------------- | -------- | ------------------------------------------------------------------ |
| `file`            | yes      | Audio bytes. Any format `ffmpeg`/`libsndfile` can decode.          |
| `model`           | no       | Accepted but ignored (always Parakeet TDT 0.6B v2).                |
| `response_format` | no       | `json` (default) or `text`. SRT/VTT/verbose_json not supported here. |
| `language`        | no       | Accepted, currently ignored.                                       |
| `prompt`          | no       | Accepted, currently ignored.                                       |
| `temperature`     | no       | Accepted, currently ignored.                                       |

---

## Limits

- **Max audio size: 25 MB.** Matches OpenAI's documented cap. Returns 413 on
  overflow.
- **Sync only.** This endpoint blocks until transcription completes (typically
  <500ms wall-clock for short clips).
- **For longer audio (meetings, calls)**, use the native async
  `/transcribe/meeting` endpoint. See [API.md](API.md).
- Per-IP rate limit: 60 requests per 60-second window (shared with
  `/transcribe/dictate`).

---

## Auth failure shape

WisprAlt returns errors in OpenAI's standard envelope on `/v1` routes:

```json
{
  "error": {
    "message": "Invalid API key",
    "type": "invalid_request_error",
    "param": null,
    "code": "invalid_api_key"
  }
}
```

The `error.request_id` field is included when available — quote it when
contacting your operator about a failed call.

---

## Smart formatting (WisprAlt-specific extension)

The WisprAlt macOS client has a "Smart formatting" toggle that calls
[OpenRouter Mercury 2](https://openrouter.ai/) on the server to add punctuation
and casing, remove obvious filler words, collapse repeats, and apply light
list formatting on dictations of at least `SMART_FORMAT_MIN_WORDS` (default
100) words. Meaning is preserved — no rephrasing, no summarization. **We
deliberately do NOT apply that on `/v1/audio/transcriptions`** — third-party
API consumers expect raw model output, not opinionated post-processing. If
you want cleanup in your own pipeline, do it yourself.

### Need cleanup but not via the WisprAlt client?

Hit the native endpoint directly with the `X-Smart-Format: true` header. Your
operator must have `OPENROUTER_API_KEY` configured server-side for this to
work; without the key, the header is silently ignored and you get raw output.

```bash
curl https://transcribe.integrateapi.ai/transcribe/dictate \
  -H "Authorization: Bearer $WISPRALT_API_KEY" \
  -H "X-Smart-Format: true" \
  -F file=@audio.wav
```

Returns:

```json
{
  "text": "Cleaned-up transcript with proper punctuation.",
  "model_id": "mlx-community/parakeet-tdt-0.6b-v2",
  "duration_ms": 142.3,
  "smart_formatted": true
}
```

The header accepts `true`, `1`, or `yes` (case-insensitive). Anything else,
or an absent header, leaves output raw and `smart_formatted: false`. This is a
WisprAlt-specific extension, **not** part of OpenAI compatibility — do not
expect upstream OpenAI clients to send it.

---

## Troubleshooting

- **401 invalid_api_key**: token wrong, expired, or revoked. Get a new one
  from `https://transcribe.integrateapi.ai/admin/` (or ask your admin).
- **413 file_too_large**: clip exceeds 25 MB. Chunk the audio client-side or
  use the native async API.
- **422 unsupported_response_format**: you asked for `srt`/`vtt`/`verbose_json`.
  Those need per-segment timestamps which Parakeet doesn't emit on the dictate
  path. Use the native `/transcribe/meeting` for timestamps.
- **422 invalid_response_format**: the value you sent isn't a recognized
  format string. Use `json` or `text`.
- **429**: per-IP rate limit hit. Honor the `Retry-After` header.
- **5xx errors**: the server may be loading models. Retry after 30s.

For server-side debugging see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Differences from upstream OpenAI

- We only support `json` and `text` response formats.
- We always route to Parakeet TDT 0.6B v2 (English, sub-200ms inference). The
  `model` field is accepted for compatibility but ignored.
- No diarization, no language hints (yet).
- No streaming (yet).

For diarization + multi-speaker meetings, use the native `/transcribe/meeting`
async API, which does WhisperX + Pyannote with full speaker attribution.

---

## See also

- [API.md](API.md) — full WisprAlt-native API reference (async meeting endpoint, `/me`, admin endpoints)
- [SETUP-SERVER.md](SETUP-SERVER.md) — deploy your own WisprAlt server
- [SETUP-CLIENT.md](SETUP-CLIENT.md) — install the macOS client
- [ADMIN.md](ADMIN.md) — minting tokens and managing users
