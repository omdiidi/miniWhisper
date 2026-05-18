# Cloud Fallback — OpenRouter Whisper

When the Mac mini at `transcribe.integrateapi.ai` is unreachable, the Swift
client routes **dictation** directly to OpenRouter's
`openai/whisper-large-v3-turbo` and injects the result. Meetings degrade to
a local "save & retry" queue (Whisper alone can't replace the mini's
speaker diarization).

## Architecture

```
Client (Swift)  ──POST──►  transcribe.integrateapi.ai  (Mac mini, FastAPI)
       │
       │   on offline-signature (Cloudflare 502/522/523/524 with no
       │   X-Request-Id, OR connect-refused / dnsLookupFailed /
       │   networkConnectionLost, OR timedOut > 30 s) and after one
       │   retry confirms, the client falls back to:
       ▼
   https://openrouter.ai/api/v1/chat/completions
     (input_audio content block, base64 WAV inline,
      OpenRouter API key from co.wispralt.openrouter Keychain item)
```

The classifier lives at `client/WisprAlt/Server/ServerClient.swift::isOfflineSignature`.
It NEVER trips on origin 5xx (FastAPI middleware sets `X-Request-Id`),
401/403/413/422, rate-limit responses, or bad-wifi slow uploads.

## Setup (per machine)

Each machine that wants the fallback needs an OpenRouter API key in its
Keychain. Run this once per machine (the same key works for every employee
— OpenRouter dashboard's monthly spend cap is the kill switch):

```bash
read -rs -p 'OpenRouter key: ' K && \
  security add-generic-password -U -s co.wispralt.openrouter -a default -w "$K" && \
  unset K && echo && \
  echo "Key stored in Keychain at co.wispralt.openrouter."
```

If no key is set, the client simply errors out when the mini is offline
instead of falling back — the rest of WisprAlt continues working
normally.

To remove:

```bash
security delete-generic-password -s co.wispralt.openrouter -a default
```

## Setting an org-wide spend cap (recommended)

The fallback path doesn't enforce per-employee rate limits — it relies on
OpenRouter's own **Monthly spend limit** in the dashboard. Set this once
under https://openrouter.ai/settings/credits → "Monthly limit". A value
like $25/mo is comfortable headroom over the realistic load (≈$0.56/day
worst-case across 10 employees at the $0.04/audio-hour rate).

If the cap is hit, OpenRouter starts returning 402/429 — the client will
surface a generic dictation error toast.

## Observability

Cloud fallback events do NOT write to `wispralt.fallback_events` in this
direct-from-client design. The Supabase table + RPCs that earlier plans
added are harmless leftovers (no inserts ever happen because no Worker
holds the role JWT). Telemetry comes from two places:

- **OpenRouter dashboard** (per-key spend, request count by model).
- **Client OSLog**: filter by `subsystem == "co.wispralt"` and look for
  `[fallback]` lines. The success line is
  `inject source=fallback http=200 chars=N ms=N`. Failure paths log a
  `[fallback]` warning or error with the HTTP status from OpenRouter.

If observability matters more, the Swift client could be extended to POST
to a Supabase RPC after each fallback — but that requires either a
shared anon key in the binary (acceptable trade-off, negligible blast
radius) or a per-employee JWT (out of scope for this minimal version).

## Telemetry sync

Plan A adds a disk-backed Swift queue
(`client/WisprAlt/Storage/DictationFallbackQueue.swift`) that records every
OpenRouter-served dictation and replays it to the origin's
`POST /telemetry/cloud-dictation` endpoint once the mini is reachable
again. The dictation lands in the same `dictations` table as
origin-served ones (`source='cloud_fallback'`), shows up in
`/me/history`, and feeds the future weekly-insights cron — closing the
"Known gap: cloud-fallback dictations" hole that earlier phases left
open.

Each queue file lives at
`~/Library/Application Support/co.wispralt/cloud-fallback-queue/<client_dedup_id>.json`
where `<client_dedup_id>` is a fresh UUIDv4 minted at enqueue time.
Drain triggers fire on `NSApplication.didBecomeActiveNotification` and
immediately after any successful online dictation; concurrent drains are
coalesced into one in-flight task. Each POST carries up to 200 items.
Idempotency is enforced server-side by a partial unique index on
`dictations.client_dedup_id` plus `ON CONFLICT DO NOTHING`, so a batch
that is retried after a partial 5xx lands the missing rows exactly once.

Items that fail 5 drain attempts — or sit in the queue more than 7 days
— move to a `failed/` sibling directory so they don't retry forever.
See [ARCHITECTURE.md → Cloud-fallback telemetry sync](ARCHITECTURE.md#cloud-fallback-telemetry-sync)
for the full data lineage diagram and status policy.

**Privacy note.** The `client_dedup_id` is a randomly-generated UUIDv4
with no embedded user / device / time signal — it exists solely so the
server can dedup retries. The server records exactly what the
[`POST /telemetry/cloud-dictation`](API.md#post-telemetrycloud-dictation)
request body carries (text, UTC timestamp, word count, client app
version, dedup id) and nothing else; no IP, no User-Agent fingerprint,
no per-batch attestation. The same 90-day retention sweep that zeroes
origin-served dictations applies to cloud-fallback rows — they are not
treated as a distinct class for retention purposes.

## Threat model

- **OpenRouter key compromise**: bounded by the monthly spend cap set
  in the OpenRouter dashboard. Worst case = one month's cap = $25 (or
  whatever you set). Rotate by re-running the `security add-generic-password`
  command on each employee's Mac with a fresh key.
- **Single key shared across employees**: there's no per-user rate
  limiting or revocation. If you need that, you're back in Worker
  territory — but for a 10-person internal tool where the fallback is
  the 1% case, this is fine.
- **Stolen laptop**: the Keychain entry is locked behind macOS user
  authentication. An attacker with the laptop unlocked can extract the
  key (so can `security find-generic-password -s co.wispralt.openrouter -w`)
  — same threat surface as any Keychain-stored credential.

## Removing the fallback

Delete the Keychain item on each machine (see Setup → "To remove"). The
next dictation that hits the offline-signature classifier will throw the
existing 503 error toast instead of falling back. No code changes
required.

## Repo touch points

- `client/WisprAlt/Server/DictationAPI.swift` — origin → retry →
  OpenRouter direct.
- `client/WisprAlt/Server/ServerClient.swift::isOfflineSignature` —
  classifier; shared with meeting offline detection.
- `client/WisprAlt/Storage/KeychainHelper.swift` — `getOpenRouterAPIKey()`
  / `setOpenRouterAPIKey()`.
- `client/WisprAlt/Storage/PendingUploadsQueue.swift` — meeting-only,
  unrelated to OpenRouter; queues WAVs for retry when the mini is back.
- `client/WisprAlt/Storage/DictationFallbackQueue.swift` — Plan A
  cloud-fallback telemetry queue: disk-backed, idempotent via UUIDv4
  `client_dedup_id`, drains to `POST /telemetry/cloud-dictation` on
  app foreground + successful online dictation. See "Telemetry sync"
  above.
- `server/src/wispralt_server/routes/telemetry.py` — Plan A ingest
  endpoint (`POST /telemetry/cloud-dictation`). Bearer-only, rate-limited
  to 10 batches/min/IP, batches of up to 200 dictations each.
- `client/WisprAlt/App/MenuBarController.swift` — meeting-offline toast,
  drain triggers (dictation success / app foreground / 120s timer),
  fallback-unavailable toast. Plan A adds two cloud-fallback-queue drain
  triggers: `didBecomeActive` observer + post-successful-online-dictation
  hook.
- `server/src/wispralt_server/routes/dev_faults.py` — dev-only
  `?fault=503` injection so we can verify the classifier doesn't trip on
  origin 5xx-with-X-Request-Id. Mounted only when `WISPRALT_DEV_FAULTS=1`
  AND host is non-prod.
- `server/migrations/2026-05-05-v3-fallback-events.sql` — applied to
  Supabase but UNUSED by this design. Harmless leftover; safe to leave
  in place. Drop later if you want with a v4 migration.
