---
title: Integration Guide
---

# Integration Guide — Use WisprAlt as a Drop-in Transcription Provider

This guide shows how to point any third-party project at WisprAlt's self-hosted
transcription API. WisprAlt exposes an OpenAI-compatible endpoint, so any client
that speaks the OpenAI Audio API (Python SDK, Node SDK, curl, etc.) just needs
two environment variables changed.

> For the full /v1 surface reference (all response formats, headers, error
> codes, file formats, key kinds), see [OPENAI-COMPAT.md](OPENAI-COMPAT.md).

For the full WisprAlt-native API (async meetings, /me, admin), see
[API.md](API.md).

---

## Prerequisites

- WisprAlt server is running and reachable. The default public URL is
  `https://transcribe.integrateapi.ai` (see [SETUP-SERVER.md](SETUP-SERVER.md)
  if you need to deploy your own).
- You have a WisprAlt **integration** API key. Mint one at
  `https://transcribe.integrateapi.ai/admin/keys/new` (admin only), or ask
  your operator to mint one for you (see
  [ADMIN.md → Integration Keys](ADMIN.md#integration-keys)). Integration
  keys are scoped to `/v1/*` only — they cannot access `/me/*` or
  `/telemetry/*`. For programs, that's exactly what you want.

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

### verbose_json (with timestamps + segments)

Useful for subtitling clients (Buzz, Bazarr) that need per-segment timing.

```python
resp = client.audio.transcriptions.create(
    file=open("audio.wav", "rb"),
    model="whisper-1",
    response_format="verbose_json",
    timestamp_granularities=["word"],   # optional; adds words[] when aligned
)
print(resp.text)
for seg in resp.segments:
    print(seg.start, seg.end, seg.text)
```

Full body shape documented in [OPENAI-COMPAT.md → verbose_json](OPENAI-COMPAT.md#response-formats).
`language` is always `"english"` (full lowercase word), `transient: false`
is always emitted on each segment, and `tokens` is always `[]` (Parakeet
has no Whisper-BPE IDs).

### SRT (SubRip) export

```bash
curl https://transcribe.integrateapi.ai/v1/audio/transcriptions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F file=@audio.wav \
  -F response_format=srt \
  -o audio.srt
```

Returns `application/x-subrip` with comma decimal separators. Drop straight
into VLC, Premiere, or any subtitle workflow.

### VTT (WebVTT) export

```bash
curl https://transcribe.integrateapi.ai/v1/audio/transcriptions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F file=@audio.wav \
  -F response_format=vtt \
  -o audio.vtt
```

Returns `text/vtt` with the required `WEBVTT` magic line and period decimal
separators.

### Swift (URLSession)

Use `multipart/form-data` with field `file` pointing at your audio data. See
`client/WisprAlt/Server/DictationAPI.swift` in this repo for a reference
implementation.

---

## Supported file formats

WisprAlt decodes uploads through two paths — libsndfile first, then ffmpeg as
fallback. ffmpeg sniffs by content (magic bytes), not by extension.

| Format | Decoder |
|---|---|
| wav, flac, ogg (Vorbis/Opus), aiff, au, caf | libsndfile (`soundfile`) |
| mp3, m4a, mp4, webm, aac, mpeg, mpga | ffmpeg |

If your file is something neither decoder handles, you'll get a **400**
with `code: "unsupported_file_type"`. Pre-convert to wav/mp3 locally.

---

## Supported parameters

| Parameter         | Required | Notes                                                              |
| ----------------- | -------- | ------------------------------------------------------------------ |
| `file`            | yes      | Audio bytes. Any format `ffmpeg`/`libsndfile` can decode.          |
| `model`           | no       | One of the 5 listed model IDs (see `GET /v1/models`). Routed to Parakeet TDT 0.6B v2 internally. `gpt-4o-transcribe-diarize` is rejected (404). |
| `response_format` | no       | One of `json` (default), `text`, `verbose_json`, `srt`, `vtt`.    |
| `timestamp_granularities[]` | no | `word` or `segment`. Requires `response_format=verbose_json`.   |
| `language`        | no       | Accepted, ignored (Parakeet is English-only).                      |
| `prompt`          | no       | Accepted, ignored.                                                 |
| `temperature`     | no       | 0.0–1.0. Range-validated, then ignored.                            |
| `stream`          | no       | Must be `false`. `true` returns 400 `streaming_unsupported`.       |
| `user`            | no       | Accepted, debug-logged, ignored.                                   |

---

## Limits

- **Max audio size: 25 MB.** Matches OpenAI's documented cap. Returns 413 on
  overflow.
- **Max duration: 15 minutes** (after decode). Returns 400
  `code: "audio_too_long"` over the cap.
- **Sync only.** This endpoint blocks until transcription completes (typically
  <500ms wall-clock for short clips).
- **For longer audio (meetings, calls)**, use the native async
  `/transcribe/meeting` endpoint. See [API.md](API.md).
- **Per-token rate limit: 60 requests per 60-second window.** Independent of
  the per-IP bucket on `/transcribe/dictate`. Honor `Retry-After` on 429.

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
80) words. Meaning is preserved — no rephrasing, no summarization. **We
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

- **401 invalid_api_key**: token wrong, expired, or revoked. Mint a fresh one
  at `https://transcribe.integrateapi.ai/admin/keys/new` (or ask your admin).
- **413 file_too_large**: clip exceeds 25 MB. Chunk the audio client-side or
  use the native async API.
- **400 audio_too_long**: clip exceeds 15 minutes. Use `/transcribe/meeting`
  for longer audio.
- **400 unsupported_file_type**: ffmpeg can't decode the file. Pre-convert
  to wav/mp3 locally.
- **422 invalid_response_format**: the value you sent isn't a recognized
  format string. Use one of `json`, `text`, `verbose_json`, `srt`, `vtt`.
- **429**: per-token rate limit hit. Honor the `Retry-After` header.
- **5xx errors**: the server may be loading models. Retry after 30s.

For server-side debugging see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Differences from upstream OpenAI

- All 5 response formats supported (`json`, `text`, `verbose_json`, `srt`, `vtt`).
- We always route to Parakeet TDT 0.6B v2 (English, sub-200ms inference). The
  `model` field is accepted for compatibility against the 5 listed model IDs.
- No diarization on `/v1/*` (use the native `/transcribe/meeting`).
- No translation (Parakeet is English-only).
- No SSE streaming (`stream=true` → 400 `streaming_unsupported`). Matches
  real OpenAI's whisper-1 behavior.

For diarization + multi-speaker meetings, use the native `/transcribe/meeting`
async API, which does mlx-whisper + Pyannote with full speaker attribution.

---

## See also

- [OPENAI-COMPAT.md](OPENAI-COMPAT.md) — full /v1 surface reference (canonical)
- [API.md](API.md) — full WisprAlt-native API reference (async meeting endpoint, `/me`, admin endpoints)
- [SETUP-SERVER.md](SETUP-SERVER.md) — deploy your own WisprAlt server
- [SETUP-CLIENT.md](SETUP-CLIENT.md) — install the macOS client
- [ADMIN.md](ADMIN.md) — minting tokens and managing users (incl. [Integration Keys](ADMIN.md#integration-keys))
