# Brief: OpenRouter Whisper fallback when Mac mini is offline

## Why

The Mac mini at `transcribe.integrateapi.ai` is a single point of failure. When it
goes offline (power, network, crash, reboot, scheduled maintenance), every
employee's dictation and meeting transcription dies with no recovery path. We
want a cloud fallback so dictation keeps working when the mini is genuinely
unreachable — without sacrificing the local-first speed in the normal case.

## Context

- Client is Swift macOS-15+. Dictation talks to `POST /transcribe/dictate`,
  meetings upload via `POST /meetings/upload` (see
  `client/WisprAlt/Server/{DictationAPI,MeetingAPI,ServerClient}.swift`).
- Server is FastAPI on the mini. Routes:
  `server/src/wispralt_server/routes/{dictate,meeting,v1_transcriptions}.py`.
  Models are resident at startup; meetings load lazily.
- Auth: per-employee opaque tokens stored in Supabase `wispralt` schema
  (project ref `lmaffmygjrfgkwrapfax`). Tokens are validated by the server's
  auth.py against Supabase.
- Tunnel: Cloudflare. `transcribe.integrateapi.ai` → mini origin via cloudflared.
  When the mini is down, Cloudflare returns 502/523/524 with a Cloudflare-flavored
  body — distinct from origin 5xx (origin returns FastAPI JSON with `X-Request-Id`).
- OpenRouter provides `openai/whisper-large-v3-turbo` via a dedicated
  transcription endpoint (NOT chat completions). Base64 audio in, JSON out.
  ~$0.04/hr of audio. Confirmed via openrouter.ai page fetch.
- Dictation audio: short clips (<60s typical), 16kHz mono WAV, single channel.
  Smart-format optional via Mercury (now 600ms timeout).
- Meeting audio: long-form (10–60min), dual-channel WAV, requires Pyannote
  diarization on the mini. **Fallback whisper-large-v3-turbo cannot diarize.**

## Decisions

- **Fallback proxy lives on a Cloudflare Worker** at a sibling subdomain
  (e.g. `fallback.integrateapi.ai`). Cheap (Workers free tier covers this volume
  comfortably), no new infra to babysit, same Cloudflare account, doesn't
  reduce the local-first efficiency in the normal path. — Cost-negligible per
  user constraint #1.
- **Provider: OpenRouter only.** Single integration, one key to rotate, one
  bill. — Per user constraint #3.
- **Detection (client-side, conservative)** — fall back ONLY when ALL hold:
  1. Two attempts to mini failed, second attempt confirms.
  2. Failure mode is one of: TCP/TLS connect refused, DNS resolves but
     connection times out (>= 8s without bytes), Cloudflare 502/523/524 with
     CF-Ray header AND no `X-Request-Id` header (origin-unreachable signature).
  3. Failure happens BEFORE successful upload completes (long upload on bad
     wifi is NOT a fallback trigger — only "can't reach origin at all").
  4. Total elapsed time on first attempt did not exceed `mini_health_window`
     (default 30s) — slow uploads on bad wifi count as "still talking to mini",
     not "mini offline".
  - Explicitly NEVER trip on: 401/403 (auth), 503 with origin body (mini up,
    pool dead — let the existing watcher recover), 5xx with `X-Request-Id`
    header (origin response), Mercury timeout, smart-format degraded warning.
  - Long messages on bad wifi: rely on the upload-progress signal — if any
    bytes have been ACKed by Cloudflare in the last 5s, keep waiting. Only
    fall back when the connection is dead, not when it's slow. — User
    constraint #2.
- **Dictation: full fallback.** Client uploads to `fallback.integrateapi.ai`
  with same employee token. Worker validates token against Supabase, base64s
  the WAV, calls OpenRouter, returns text in the existing dictation response
  shape. Smart-format is skipped on fallback path (Mercury isn't reachable
  either; degrade gracefully).
- **Meetings: fallback with degradation OR user-facing notice — pick simpler.**
  Whisper-large-v3-turbo via OpenRouter returns text only, no diarization.
  Decision: when mini offline + meeting upload attempted, **show the user a
  toast: "Server temporarily offline — meeting will upload when it's back.
  Recording is saved locally."** Queue the recording locally. Don't burn
  OpenRouter budget on long-form audio with no speaker labels (would degrade
  the meeting product worse than just delaying). — Per user constraint #4
  ("if it's too much, just pop something up").
- **Rate limits per employee** (enforced in Worker via Workers KV counter):
  - 200 fallback dictations per day
  - **100 fallback dictations per hour** (doubled from initial 50)
  - 25 MB max upload size
  - Hard org-wide monthly cap on OpenRouter spend (Worker refuses past
    threshold, returns 429 with retry hint).
  - 429 surfaces as a polite client toast: "Cloud fallback rate limit hit —
    try again in N minutes."
- **Auth in Worker:** Worker hits Supabase REST (`wispralt.api_tokens`) with a
  read-only service-role key to validate the bearer token. Same token the
  client already sends to the mini. No new credential on the client side.
- **Observability:** Worker logs every fallback request to Supabase
  `wispralt.fallback_events` (timestamp, user_id, bytes_in, openrouter_ms,
  outcome). Lets us audit how often fallback fires and watch for false
  positives.
- **Failure of the failure:** if Worker itself is unreachable OR OpenRouter is
  down, client shows a final "Transcription unavailable — try again shortly"
  notice. No third tier.

## Rejected Alternatives

- **Server-side fallback (mini proxies to OpenRouter when its own pool is
  bad)** — useless for the actual failure case. If the mini is offline, the
  client never reaches it to negotiate.
- **Vercel/Render fallback service** — yet another box; Worker covers it for
  free.
- **Client-direct to OpenRouter with embedded key** — can't rate-limit, can't
  revoke, key in client binary is a leak waiting to happen.
- **Groq instead of OpenRouter** — user explicitly wants OpenRouter only.
- **Meeting fallback with degraded (no-diarization) transcript** — worse UX
  than queueing locally + retry. Burns budget on a degraded product.
- **Aggressive fallback triggers (any 5xx)** — too easy to trip on transient
  origin issues that the existing pool watcher already handles in seconds.
- **Pre-issued fallback JWT** — extra complexity vs. just hitting Supabase
  from the Worker.

## Direction

Build a Cloudflare Worker at `fallback.integrateapi.ai` that proxies dictation
requests to OpenRouter's `whisper-large-v3-turbo` transcription endpoint. The
Swift client detects mini-offline via a strict, narrowly-defined signature
(connect-refused / Cloudflare 502/523/524 with no `X-Request-Id`, two
consecutive confirmations, never on slow upload) and only then hands the WAV
off to the Worker. Per-employee rate limits (200/day, 100/hour, 25 MB) live
in Workers KV; auth validates against the existing Supabase token table.
Meetings get a "server offline, recording saved locally, will upload when
back" toast instead of a degraded cloud transcript — diarization is the
defining feature there and Whisper-only would make it worse, not better.
