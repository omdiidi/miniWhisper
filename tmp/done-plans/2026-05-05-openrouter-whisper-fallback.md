# OpenRouter Whisper Fallback for Mac-Mini-Offline

## Goal

Add a Cloudflare-Worker-hosted cloud fallback that handles **dictation** when
the Mac mini origin (`transcribe.integrateapi.ai`) is unreachable, using
OpenRouter's `openai/whisper-large-v3-turbo`. Meeting uploads degrade to a
"server temporarily offline — meeting will upload when it's back. Recording
is saved locally." toast (no cloud transcription — diarization is the
defining feature there). Detection is conservative enough that bad-wifi
slow uploads, transient origin 5xx, or auth failures NEVER trigger fallback.

## Summary

- **Worker** at `fallback.integrateapi.ai` proxies dictation to OpenRouter
  (`/chat/completions` with `input_audio` block), validates the existing
  employee bearer token via a `SECURITY DEFINER` Postgres RPC (the Worker
  never reads `wispralt.users` directly), enforces per-user rate limits +
  org-wide monthly budget in a single Durable Object call, emits usage rows.
- **Swift client** dictation switches to `URLSessionUploadTask` with a
  delegate so we get `didSendBodyData` for the byte-progress signal (brief
  constraint #2). Offline-signature classifier: Cloudflare 502/522/523/524
  with `CF-Ray` and **no** `X-Request-Id`, OR
  `URLError.cannotConnectToHost / dnsLookupFailed / cannotFindHost /
  networkConnectionLost`, OR `URLError.timedOut` AND ≥5s since last
  byte-ACK AND first-attempt elapsed > 30s.
- **Meetings** stay local-only on offline; meeting upload's catch path runs
  the same shared classifier independently (it does NOT go through
  `ServerClient.execute`), surfaces a toast, queues the WAV.
- **Supabase**: `wispralt.fallback_events` table mirroring `usage_events`;
  monthly cost via SECURITY DEFINER RPC; **no** direct table grants to the
  Worker role.
- **Auth**: dedicated Postgres role `wispralt_fallback_worker` granted ONLY
  `EXECUTE` on two RPCs — `lookup_user_by_token_hash`,
  `fallback_micro_usd_this_month` — and `INSERT(..) ON
  wispralt.fallback_events`. Granted to PostgREST's `authenticator` role
  so JWT role-switching works. JWT signed with project JWT secret via a
  one-shot `scripts/mint-fallback-jwt.mjs` script (HS256, 1-year exp,
  rotated quarterly).
- No mini-side code changes apart from a separate `dev_faults.py` route
  module that is imported only when `WISPRALT_DEV_FAULTS=1`.

## Intent / Why

- Mac mini is a single point of failure; outages today = 100% transcription
  loss for every employee.
- Dictation is the high-frequency surface and needs cloud parity even at
  degraded latency.
- Meetings are async and rare; degraded cloud (no diarization) would be
  worse than queue-and-retry.
- Cost ceiling: OpenRouter at $0.04/audio-hour, ~5K dictations/day @ ~10s
  avg ≈ ~14 audio-hours/day worst case ≈ **$0.56/day org-wide max**.
  Negligible.

## Source Artifacts

- Brief: `tmp/briefs/2026-05-04-openrouter-whisper-fallback.md`
- Research dossier: `researcher` agent, 2026-05-04.
- Reviewer pass 1: 2 parallel `plan-reviewer` agents, 2026-05-05.
- Reviewer pass 2: 2 parallel `plan-reviewer` agents, 2026-05-05.

## What

### User-visible behavior

- Mini reachable → dictation behavior unchanged.
- Mini unreachable + dictation → after one retry confirmation, client
  uploads to Worker; user sees text injected normally (realistic p50
  ~3–6s, p95 ~8–10s).
- Mini unreachable + meeting attempt → toast: *"Server temporarily offline
  — meeting will upload when it's back. Recording is saved locally."*
- Rate-limit hit → toast: *"Cloud fallback rate limit hit — try again in
  N minutes."*
- Monthly budget exhausted → Worker returns 429 with synthetic
  `Retry-After` = seconds-until-month-end → same toast surface.
- Worker also down → toast: *"Transcription temporarily unavailable."*
  (debounced, 1 per 10 min).

### Success Criteria

- [ ] `launchctl stop co.wispralt.server` on prod-mini → next FN dictation
      lands via Worker, log shows `inject ... source=fallback`.
- [ ] Auth failure (401 from origin with `X-Request-Id`) → does NOT
      trigger fallback. Verified by revoking a test token.
- [ ] Transient origin 503 with `X-Request-Id` → does NOT trigger
      fallback. Verified via the dev-only `dev_faults` router (registered
      only when `WISPRALT_DEV_FAULTS=1` AND hostname is non-prod).
- [ ] Bad-wifi slow upload (50KB/s sustained throughput, last byte-ACK
      ≤ 5s ago) → does NOT trigger fallback. Verified via Apple Network
      Link Conditioner.
- [ ] Mini unreachable (host blocked via `/etc/hosts` →
      `0.0.0.0 transcribe.integrateapi.ai`) → DOES trigger fallback after
      30s health window + 5s confirm.
- [ ] Cloudflare 522 from origin (sleeping mini) → DOES trigger fallback.
- [ ] `URLError.networkConnectionLost` mid-upload → DOES trigger
      fallback after retry confirmation.
- [ ] Hour-cap: with `WISPRALT_FALLBACK_HOUR_CAP=2` (env-driven), 3rd
      dictation in same hour returns 429 with `Retry-After`.
- [ ] Day-cap: with `WISPRALT_FALLBACK_DAY_CAP=3`, 4th dictation in
      same day returns 429.
- [ ] Service-role key absent: `grep -rn -E "(SUPABASE_SERVICE_ROLE|service_role)" workers/`
      returns nothing; `wrangler secret list` shows ONLY
      `OPENROUTER_API_KEY`, `SUPABASE_FALLBACK_JWT`, `SUPABASE_ANON_KEY`,
      `OPS_ALERT_WEBHOOK`.
- [ ] OpenRouter empirical probe committed at
      `workers/fallback/probes/openrouter-shape.json` (with `id`,
      `created`, `usage` scrubbed).
- [ ] Meeting upload during offline → toast fires, WAV present in
      pending dir; auto-uploads via all 4 drain triggers (dictation
      success, app foreground, 120s timer, manual menu).
- [ ] Both-down test: kill Worker via `wrangler tail` + block mini via
      `/etc/hosts` → "Transcription temporarily unavailable" toast
      within 30s, debounced.
- [ ] `wispralt.fallback_events` row written for every Worker hit
      (success + 401 + 429 + 502 + budget-exhausted).

## Verified Repo Truths

### Data / State

- Fact: Token validation hashes the bearer with SHA-256 hex and looks it
  up in `wispralt.users.token_hash` with `revoked_at IS NULL`.
  Evidence: `server/src/wispralt_server/users/store.py:58-75`
  Implication: Worker re-implements sha256, but accesses the table only
  via SECURITY DEFINER RPC.

- Fact: `wispralt.users.token_hash` is `TEXT NOT NULL UNIQUE`.
  Evidence: `server/migrations/2026-04-27-v1-wispralt-schema.sql:31-39`
  Implication: RPC parameter is `text`; lookup is unique.

- Fact: `wispralt.schema_version.version` is `INTEGER PRIMARY KEY`.
  Evidence: `server/migrations/2026-04-27-v1-wispralt-schema.sql:20-24`
  Implication: PK gives `ON CONFLICT (version) DO NOTHING` a target;
  `INSERT ... ON CONFLICT DO NOTHING` is well-formed.

- Fact: Existing migration files are
  `2026-04-27-v1-wispralt-schema.sql` and
  `2026-04-27-v2-display-name.sql`. Next free version is **3**.
  Evidence: `ls server/migrations/`
  Implication: New file is `2026-05-05-v3-fallback-events.sql`. Hard-code
  `version=3`.

- Fact: `usage_events` schema is `id BIGSERIAL, user_id INT FK, ts
  TIMESTAMPTZ, kind TEXT, status INT, chars INT, duration_ms REAL,
  bytes_in INT, bytes_out INT, error_class TEXT, request_id TEXT`.
  Evidence: `server/migrations/2026-04-27-v1-wispralt-schema.sql:52-67`
  Implication: `fallback_events` mirrors this exactly + 3 columns
  (`provider`, `provider_request_id`, `cost_micro_usd`).

### Entry Points / Integrations

- Fact: Dictation route is `POST /transcribe/dictate`,
  `multipart/form-data`, field `file`, optional header `X-Smart-Format`.
  Evidence: `server/src/wispralt_server/routes/dictate.py:33-114`
  Implication: Worker accepts identical multipart shape.

- Fact: 200 dictation response is exactly
  `{text: str, model_id: str, duration_ms: float, smart_formatted: bool}`.
  Evidence: `server/src/wispralt_server/routes/dictate.py:149-156`
  Implication: Worker emits identical fields. Swift's `TranscribeResponse`
  decoder at `client/WisprAlt/Server/DictationAPI.swift:7-18` reused
  unchanged.

- Fact: FastAPI error envelope is `{"detail": "..."}`.
  Evidence: `server/src/wispralt_server/auth.py:152, 183, 199`
  Implication: Worker emits identical shape on 4xx/5xx.

- Fact: Dictation call site IS already opting into retry:
  `let (data, _) = try await ServerClient.shared.execute(request,
  retryOnReset: true)`.
  Evidence: `client/WisprAlt/Server/DictationAPI.swift:54`
  Implication: Fallback gate plugs in at this call site without
  changing other callers.

- Fact: `ServerClient.retryableURLErrorCodes` includes
  `networkConnectionLost`.
  Evidence: `client/WisprAlt/Server/ServerClient.swift:106`
  Implication: classifier can include the same error without contradicting
  existing retry policy.

- Fact: Meeting route is `POST /transcribe/meeting`.
  Evidence: `client/WisprAlt/Server/MeetingAPI.swift:79`
  Implication: Meeting offline-detection keys off this URL.

### Execution / Async Flow

- Fact: `MeetingAPI.submit(_ wavURL: URL, progress: @escaping (Double) ->
  Void) async throws -> JobID` is static; builds its OWN URLSession with
  `UploadSessionDelegate`.
  Evidence: `client/WisprAlt/Server/MeetingAPI.swift:72, 141-165`
  Implication: Offline-signature classifier MUST be a shared helper
  callable from both `MeetingAPI.submit` and the dictation path.

- Fact: No retry/queue exists for meeting uploads.
  Evidence: `client/WisprAlt/Server/MeetingAPI.swift:61-64`
  Search Evidence: `grep -rn "pending-uploads\|retry"
  client/WisprAlt/Server/` → only TODO comment.
  Implication: Build the `PendingUploadsQueue` from scratch.

- Fact: Toast helper is `MenuBarController.showToast(_:)` wrapping
  `AppNotifications.notify`.
  Evidence: `client/WisprAlt/App/MenuBarController.swift:639-642`
  Implication: Reused for offline messages.

### Frontend / UI

- Fact: `Settings.shared.serverURL` is UserDefaults-backed; the bearer
  token is Keychain-backed (`co.wispralt`).
  Evidence: `client/WisprAlt/Storage/Settings.swift:25, 59-63`,
  `client/WisprAlt/Util/KeychainHelper.swift`
  Implication: New `Settings.shared.fallbackURL` follows UserDefaults
  pattern; reuses Keychain bearer.

### External / Operational

- Fact: No `wrangler.toml`, no `workers/` directory exists.
  Search Evidence: `find . -name "wrangler.toml" -o -name "workers" -type d`
  → no results.
  Implication: Worker is greenfield. Place under `workers/fallback/`.

## Locked Decisions

- **Provider: OpenRouter only.**
- **Audio API path: `/api/v1/chat/completions` with `input_audio` content
  block.** Response shape pinned via empirical probe before any
  production wiring.
- **Fallback proxy: Cloudflare Worker on the Paid plan ($5/mo, 30s CPU).**
  Required, not optional.
- **Rate limit + budget: single combined Durable Object.**
  - Two DO classes: `RateLimit` (per-user, keyed by `u:<id>`) and
    `BudgetCache` (global singleton, keyed by `global:budget`).
  - **One worker → DO call** per dictation: `POST /check-and-debit` on the
    user's `RateLimit` DO does (1) read current hour/day buckets, (2) ask
    `BudgetCache` for current month spend, (3) decide all-or-nothing
    debit. If denied, no counter incremented. If allowed, hour/day++ AND
    budget reservation recorded; reservation is reconciled with
    `cost_micro_usd` in a follow-up `POST /reconcile` after OpenRouter
    completes.
  - Reservation = upfront fixed estimate (~30s of audio = 333 µ$).
    Reconcile adjusts to actual.
- **Per-user limits**: `WISPRALT_FALLBACK_HOUR_CAP=100`,
  `WISPRALT_FALLBACK_DAY_CAP=200`, `WISPRALT_FALLBACK_MAX_BYTES=10485760`
  (10 MB). Env-driven so testing can lower them.
- **Org-wide monthly cap**: `WHISPER_BUDGET_USD_PER_MONTH=50` (default).
  Global `BudgetCache` DO caches month-to-date spend with **5s TTL** and
  **fail-closed** semantics: ≥3 consecutive RPC failures within 5min →
  cap engages, alert webhook fires.
- **Budget-exhausted UX**: 429 with synthetic `Retry-After` =
  seconds-until-month-end.
- **Meeting fallback: toast + local queue.** No cloud transcription.
- **Pending-uploads atomicity**: write `<uuid>.wav.tmp`, fsync, rename,
  `Darwin.fsync(dirFd)` on parent directory. Source WAV must be fsynced
  by the recording pipeline before `enqueue` is called (recording
  pipeline updated to do this).
- **Drain triggers** (any one fires drain): (a) successful dictation,
  (b) `NSApplication.didBecomeActiveNotification`, (c) periodic 120s
  `Timer`, (d) manual menubar action "Retry pending uploads".
- **Drain coordinator: `actor PendingUploadsDrainCoordinator`** owns
  `inFlight: Bool` and exposes `func drain() async` so concurrent
  callers await the same task; no double-drain.
- **Toast debounce**: process-local `[ToastKind: Date]` on
  `MenuBarController` actor; 10-min cooldown per kind.
- **Auth in Worker**: dedicated role `wispralt_fallback_worker` granted
  ONLY `EXECUTE` on `wispralt.lookup_user_by_token_hash(text)` and
  `wispralt.fallback_micro_usd_this_month()`, plus `INSERT` on
  `wispralt.fallback_events`. Role granted to PostgREST's `authenticator`
  role so JWT role-switching works. **No direct grants on
  `wispralt.users`.**
- **Worker → PostgREST headers**: `Accept-Profile: wispralt` (read),
  `Content-Profile: wispralt` (write); `Authorization: Bearer <role-JWT>`;
  `apikey: <SUPABASE_ANON_KEY>` (the project anon key is required by
  Supabase as the gateway-level apikey, separate from the role JWT).
- **No token cache.** Every Worker dictation hits Supabase fresh.
- **OpenRouter call**: wrapped in
  `AbortSignal.timeout(60_000)` per attempt; backoff 200/800/3200 ms,
  3 attempts max.
- **Fallback detection signature** (ALL must hold):
  1. First attempt failure is one of:
     - `URLError.cannotConnectToHost`, `dnsLookupFailed`, `cannotFindHost`,
       `networkConnectionLost`,
     - `URLError.timedOut` AND `(now - lastByteSentAt) > 5s` AND
       first-attempt elapsed > 30s,
     - HTTP 502/522/523/524 with `CF-Ray` present AND `X-Request-Id`
       absent.
  2. One retry against origin within 5s. Same failure class confirms.
  3. Failure is NEVER 401, 403, 413, 422, 429, or 5xx-with-`X-Request-Id`.
- **Dictation transport switches to `URLSessionUploadTask` with
  `URLSessionTaskDelegate`** so `didSendBodyData` is observable.
  `lastByteSentAt` is captured per-task.
- **Mini changes minimized**: `dev_faults.py` is a separate file
  `include_router`'d only when `WISPRALT_DEV_FAULTS=1` AND
  `socket.gethostname() != "<prod-mini-hostname>"` (default
  prod-mini-hostname = `omidsmacmini.local`). Dictate.py untouched.

## Known Mismatches / Assumptions

- Mismatch: Brief said OpenRouter has a "dedicated transcription
  endpoint". Reality: chat-completions only. Plan uses verified path.
- Mismatch: Brief said `wispralt.api_tokens`. Reality: `wispralt.users`.
  Plan uses real table.
- Mismatch: Brief said "read-only service-role key" — does not exist.
  Plan uses dedicated narrow role + RPCs instead.
- Assumption: OpenRouter response shape is
  `choices[0].message.content` as string OR multimodal array. Probe
  verifies before wiring; defensive parser handles both.
- Assumption: OpenRouter Whisper auto-detects English. Optional
  `WHISPER_PROMPT_HINT` env knob (default unset) prepends a text content
  block "Transcribe in English." if accuracy slips.
- Assumption: PostgREST gateway accepts the role JWT in `Authorization`
  AND the project anon key in `apikey`. Verified empirically in Task 4
  (Worker integration test against real Supabase, not a raw PostgREST
  simulator).

## Critical Codebase Anchors

- Anchor: `server/src/wispralt_server/routes/dictate.py:33-156`
  Reuse: response shape `{text, model_id, duration_ms, smart_formatted}`.
  Worker `model_id` = literal `"openai/whisper-large-v3-turbo"`;
  `smart_formatted` always `false`.

- Anchor: `client/WisprAlt/Server/ServerClient.swift:102, 113-189`
  Reuse: `retryableURLErrorCodes` set; `mapHTTPError` decoder for
  `{detail}` envelope. Insert shared `isOfflineSignature` helper here.

- Anchor: `client/WisprAlt/Server/DictationAPI.swift:54`
  Reuse: existing `execute(req, retryOnReset: true)` call path. Replace
  with new transport (`URLSessionUploadTask`) but keep the same response
  decoder.

- Anchor: `client/WisprAlt/Server/MeetingAPI.swift:72, 141-165`
  Reuse: `UploadSessionDelegate` pattern (and copy it for dictation).
  Catch path adds offline-signature classification.

- Anchor: `server/src/wispralt_server/users/store.py:58-75`
  Reuse: sha256 hex hashing.

## All Needed Context

### Documentation & References

- Repo: `server/src/wispralt_server/routes/dictate.py` —
  request/response contract.
- Repo: `server/migrations/2026-04-27-v1-wispralt-schema.sql:52-67` —
  `usage_events` shape to mirror.
- Repo: `client/WisprAlt/Server/ServerClient.swift:150-189` — error
  decoder; matches FastAPI envelope.
- Repo: `client/WisprAlt/Server/MeetingAPI.swift:141-180` — UploadSession
  + delegate pattern (template for dictation transport).

- External: https://openrouter.ai/docs/guides/overview/multimodal/audio —
  audio uses `/chat/completions` with `input_audio`.
- External: https://openrouter.ai/openai/whisper-large-v3-turbo —
  pricing $0.04/audio-hour.
- External: https://developers.cloudflare.com/durable-objects/api/state/ —
  `state.blockConcurrencyWhile` is the modern serialization primitive.
- External: https://developers.cloudflare.com/durable-objects/api/alarms/
  — alarm-driven GC.
- External: https://developers.cloudflare.com/workers/configuration/secrets/
  — `wrangler secret put`.
- External: https://developers.cloudflare.com/workers/runtime-apis/fetch/#abort-a-request
  — `AbortSignal.timeout(ms)` for OpenRouter call.
- External: https://postgrest.org/en/stable/references/auth.html —
  `Accept-Profile` / `Content-Profile`; role JWT must be `GRANT`'d to
  `authenticator`.
- External: https://supabase.com/docs/guides/api/api-keys — anon key vs
  role JWT.

### Files Being Changed

```
workers/                                          ← NEW
└── fallback/                                     ← NEW
    ├── wrangler.toml                             ← NEW
    ├── package.json                              ← NEW
    ├── tsconfig.json                             ← NEW
    ├── README.md                                 ← NEW
    ├── probes/
    │   └── openrouter-shape.json                 ← NEW (scrubbed)
    ├── scripts/
    │   └── mint-fallback-jwt.mjs                 ← NEW (HS256 JWT minter)
    └── src/
        ├── index.ts                              ← NEW (Worker: GET /health, POST /transcribe/dictate)
        ├── supabase.ts                           ← NEW (sha256 + RPC: lookupUser, sumMicroUsd, writeEvent)
        ├── openrouter.ts                         ← NEW (chat-completions audio + defensive parser + AbortSignal)
        ├── rateLimitDO.ts                        ← NEW (per-user RateLimit DO, alarm GC)
        ├── budgetDO.ts                           ← NEW (global BudgetCache DO, 5s TTL, fail-closed)
        └── types.ts                              ← NEW

server/migrations/
└── 2026-05-05-v3-fallback-events.sql             ← NEW

server/src/wispralt_server/routes/
└── dev_faults.py                                 ← NEW (only included when WISPRALT_DEV_FAULTS=1 + non-prod hostname)

server/src/wispralt_server/
└── main.py                                       ← MODIFIED (conditional include of dev_faults router)

client/WisprAlt/
├── Storage/
│   ├── Settings.swift                            ← MODIFIED (add fallbackURL UserDefaults-backed)
│   └── PendingUploadsQueue.swift                 ← NEW (FS queue, atomic writes, drain coordinator actor)
├── Server/
│   ├── ServerClient.swift                        ← MODIFIED (RequestAttempt struct, isOfflineSignature, classifier)
│   ├── DictationAPI.swift                        ← MODIFIED (URLSessionUploadTask transport + delegate; fallback retry)
│   └── MeetingAPI.swift                          ← MODIFIED (offline classification in catch path)
├── Capture/
│   └── MeetingRecorder.swift                     ← MODIFIED (fsync source WAV before handing URL off)
├── App/
│   └── MenuBarController.swift                   ← MODIFIED (toast debounce, drain triggers, "Retry pending" menu, (N pending) suffix)

docs/
├── OVERVIEW.md                                   ← MODIFIED (file-to-doc map adds 8 worker files + queue + classifier → FALLBACK.md)
├── ARCHITECTURE.md                               ← MODIFIED (Worker + DO + dedicated DB role)
└── FALLBACK.md                                   ← NEW (deploy, secrets, rotation, kill switch, monitor, threat model)

CLAUDE.md                                         ← MODIFIED (mention fallback subdomain + dev-faults flag)
```

### Known Gotchas & Library Quirks

- **OpenRouter audio path is `/chat/completions` with `input_audio`** —
  not a dedicated transcription endpoint. Transcript is at
  `choices[0].message.content` — probe pins exact shape.
- **OpenRouter pricing $0.04/audio-hour ≈ 11.11 µ$/sec.**
- **Cloudflare 522** is the most common code when origin tunnel can't
  connect to a sleeping mini. Plan handles 502/522/523/524.
- **`URLSession.data(for:)` has no body-progress hooks.** Dictation
  switches to `URLSessionUploadTask` + `URLSessionTaskDelegate` to get
  `didSendBodyData` for the byte-progress signal.
- **`MeetingAPI.submit` builds its own session** — classifier must be a
  shared helper, not embedded in `ServerClient.execute`.
- **PostgREST role JWT requires `GRANT wispralt_fallback_worker TO
  authenticator;`** in the migration. Without this, role-switching
  silently falls through to `anon`.
- **PostgREST gateway requires both** `apikey: <anon-key>` AND
  `Authorization: Bearer <role-JWT>` on every call.
- **PostgREST RPC scalar return shape**: parser is defensive — accepts
  bare number OR `{"<fn_name>": value}` OR `[{"<fn_name>": value}]`.
- **DO storage has NO `expirationTtl`.** Use `state.storage.setAlarm`.
- **`state.blockConcurrencyWhile` returns its callback's value** since
  2023. Pin `compatibility_date >= "2023-09-25"` (we'll use today).
- **Workers free tier 10ms CPU is too tight.** Paid plan required.
- **Chunked-base64 of 10 MB is ~200–500 ms CPU.** Within Paid 30s budget.
- **OpenRouter `fetch` await does NOT consume CPU time** (suspended
  fetches aren't billed against CPU). Subrequest watchdog DOES apply —
  use `AbortSignal.timeout(60_000)`.
- **Cloudflare anon key**: `apikey` header value is the project anon
  key, NOT the role JWT. Sending the JWT in `apikey` may be rejected.
- **macOS `FileHandle.synchronize()` does NOT fsync the parent
  directory.** Call `Darwin.fsync(open(dir, O_RDONLY))` after rename.
- **Captive-portal HTTP 200 with HTML body** → JSON decode fails →
  `ServerError.decoding`, not fallback. Documented behavior; user has
  no internet for OpenRouter either.
- **macOS Happy Eyeballs** can add 2–3s of v6→v4 fallback delay; 30s
  health window accommodates it.
- **`?fault=503` ONLY exists when `dev_faults` router is mounted**;
  prod has zero new branches in `dictate.py`.

## Reconciliation Notes

- Pass 1 fixes: DO API (`blockConcurrencyWhile` + alarms), removed
  `expirationTtl`, switched to `URLSessionUploadTask` for byte-progress,
  shared classifier (meetings bypass `execute`), Workers Paid plan
  required, replaced service-role with dedicated role + JWT, dropped
  budget table (sum from events), added empirical probe, removed
  PostgREST `expirationTtl` and used `Accept-Profile`.
- Pass 2 fixes: SECURITY DEFINER RPCs replace direct table grants,
  `GRANT TO authenticator`, real JWT signing script, single
  `/check-and-debit` DO endpoint, AbortController on OpenRouter,
  defensive RPC scalar parser, fail-closed budget cache, global budget
  DO, hard-coded v3 migration with `ON CONFLICT (version) DO NOTHING`,
  added 522 + `networkConnectionLost` to classifier, real `grep -E
  service_role` instead of meaningless `eyJ` grep, env-driven caps for
  testability, hosts-file test instead of pfctl, scrub probe before
  commit, separate `dev_faults.py` router, drain coordinator actor,
  process-local toast debounce, no break-on-failure in retryAll, fsync
  source WAV in recording pipeline, RequestAttempt struct, anon-key in
  apikey header.
- Brief constraint #2 (byte-progress) restored via `URLSessionUploadTask`
  + delegate.
- Brief toast copy restored verbatim.
- Intentionally dropped: separate budget table, KV rate-limit pattern,
  60s token cache, sidecar JSON in queue.

## Delta Design

### Data / State Changes

Existing: `wispralt.users`, `wispralt.usage_events`,
`wispralt.schema_version`.

Change:
- `wispralt.fallback_events` (mirror of usage_events + provider columns).
- SECURITY DEFINER function
  `wispralt.lookup_user_by_token_hash(p_hash text) RETURNS TABLE(id int,
  role text)`. Owned by `postgres` (default); EXECUTE granted only to
  `wispralt_fallback_worker`.
- SECURITY DEFINER function
  `wispralt.fallback_micro_usd_this_month() RETURNS bigint`.
- Role `wispralt_fallback_worker` (NOLOGIN, NOINHERIT). Granted EXECUTE
  on the two RPCs and INSERT on `fallback_events`. Granted to
  `authenticator` so PostgREST can switch to it.
- Schema version row inserted: `INSERT INTO wispralt.schema_version
  (version, notes) VALUES (3, 'Fallback events + worker role') ON
  CONFLICT (version) DO NOTHING`.

Why: SECURITY DEFINER hides the user table; only the function's
typed projection escapes. Role granted to `authenticator` is the
PostgREST contract.

Risks: Role-switching misconfig manifests as silent `anon` fallthrough →
likely 401 from PostgREST; integration test in Task 4 catches this.

### Entry Point / Integration Flow

Existing: `POST https://transcribe.integrateapi.ai/transcribe/{dictate,meeting}`.

Change: New `https://fallback.integrateapi.ai/{health,/transcribe/dictate}`.

Risks: DNS race — Task 8 hard-precedes client release; client
fast-fails when `Settings.fallbackURL` is empty.

### Execution / Control Flow

Dictation:
```
client → POST origin (URLSessionUploadTask + delegate captures lastByteSentAt)
  → success → inject
  → failure (URLError or HTTPURLResponse) → ServerClient.shared.isOfflineSignature(attempt)
    → if NOT offline-signature → existing error handling
    → if offline-signature → retry origin once (5s spacing, same transport)
      → confirm offline → switch URL to Settings.shared.fallbackURL
        → execute one Worker call (URLSessionUploadTask, no delegate gate)
          → success → inject
          → 429 → rate-limit toast (debounced)
          → 5xx / failure → "transcription unavailable" toast (debounced)
```

Meeting:
```
MeetingAPI.submit catches error → build RequestAttempt → 
  ServerClient.shared.isOfflineSignature(attempt) →
    true → PendingUploadsQueue.shared.enqueue(wav) + meetingOffline toast
    false → rethrow
```

### User-Facing / Operator-Facing Surface

- 4 toasts (debounced 1/kind/10min): meeting-offline (verbatim brief
  copy), rate-limit, budget-exhausted, both-down.
- Menubar suffix `(N pending)` when ≥1 pending; menu item "Retry
  pending uploads" visible same.

### External / Operational Surface

- Worker: Cloudflare Logpush + `wispralt.fallback_events`.
- Webhook alerts (best-effort via `ctx.waitUntil`): budget-exhausted
  (1/month), Worker 5xx > 5/hour, Supabase RPC failures > 10/hour.
- FALLBACK.md runbooks: deploy, key rotation (90 days; JWT exp = 1
  year, mint new + push secret), kill switch (3 levels:
  `WHISPER_BUDGET_USD_PER_MONTH=0`, DNS-flip to 503-stub,
  `wrangler delete`), monitor.

## Implementation Blueprint

### Architecture Overview

```
Client (Swift, URLSessionUploadTask + delegate)
  POST origin → if classifier→offline → retry → confirm → switch to fallback URL
  → POST fallback.integrateapi.ai/transcribe/dictate
     │
     Worker (workers/fallback/src/index.ts)
       1. parse multipart, hash bearer (sha256 hex)
       2. PostgREST RPC call wispralt.lookup_user_by_token_hash($hash)
              headers: Accept-Profile: wispralt,
                       apikey: <ANON_KEY>,
                       Authorization: Bearer <FALLBACK_JWT>
       3. RateLimit DO POST /check-and-debit
              → reads hour/day, queries BudgetCache DO for spend,
              → atomically allows or denies (single round-trip from index.ts)
              → on allow: hour/day++, budget reservation += est_cost
              → on deny: returns {ok:false, retryAfterSec, reason}
       4. base64-encode WAV (chunked) → POST OpenRouter
              with AbortSignal.timeout(60_000) + backoff
       5. defensiveParseContent(choices[0].message.content)
       6. ctx.waitUntil:
              - INSERT fallback_events (status, bytes, dur, cost, gen_id)
              - RateLimit DO POST /reconcile (actual_cost - reservation)
       7. return { text, model_id, duration_ms, smart_formatted: false }
```

### Key Pseudocode

#### Migration (`server/migrations/2026-05-05-v3-fallback-events.sql`)

```sql
BEGIN;

CREATE TABLE IF NOT EXISTS wispralt.fallback_events (
  id BIGSERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES wispralt.users(id) ON DELETE RESTRICT,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind TEXT NOT NULL,
  status INTEGER NOT NULL,
  bytes_in INTEGER,
  duration_ms REAL,
  error_class TEXT,
  request_id TEXT,
  provider TEXT NOT NULL,
  provider_request_id TEXT,
  cost_micro_usd BIGINT
);
CREATE INDEX fallback_events_idx_user_ts ON wispralt.fallback_events (user_id, ts DESC);
CREATE INDEX fallback_events_idx_ts      ON wispralt.fallback_events (ts DESC);

CREATE OR REPLACE FUNCTION wispralt.lookup_user_by_token_hash(p_hash text)
RETURNS TABLE(id integer, role text)
LANGUAGE sql SECURITY DEFINER STABLE
SET search_path = wispralt, public
AS $$
  SELECT u.id, u.role
    FROM wispralt.users u
   WHERE u.token_hash = p_hash AND u.revoked_at IS NULL
   LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION wispralt.fallback_micro_usd_this_month()
RETURNS bigint
LANGUAGE sql SECURITY DEFINER STABLE
SET search_path = wispralt, public
AS $$
  SELECT coalesce(sum(cost_micro_usd), 0)::bigint
    FROM wispralt.fallback_events
   WHERE status = 200
     AND ts >= date_trunc('month', now() AT TIME ZONE 'UTC');
$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wispralt_fallback_worker') THEN
    CREATE ROLE wispralt_fallback_worker NOLOGIN NOINHERIT;
  END IF;
END$$;

REVOKE ALL ON wispralt.users FROM wispralt_fallback_worker;
REVOKE ALL ON wispralt.fallback_events FROM wispralt_fallback_worker;

GRANT USAGE ON SCHEMA wispralt TO wispralt_fallback_worker;
GRANT INSERT (user_id, kind, status, bytes_in, duration_ms, error_class,
              request_id, provider, provider_request_id, cost_micro_usd)
   ON wispralt.fallback_events TO wispralt_fallback_worker;
GRANT EXECUTE ON FUNCTION wispralt.lookup_user_by_token_hash(text)
   TO wispralt_fallback_worker;
GRANT EXECUTE ON FUNCTION wispralt.fallback_micro_usd_this_month()
   TO wispralt_fallback_worker;

-- Required for PostgREST role-switching via JWT.
GRANT wispralt_fallback_worker TO authenticator;

INSERT INTO wispralt.schema_version (version, notes)
VALUES (3, 'Fallback events + worker role')
ON CONFLICT (version) DO NOTHING;

COMMIT;
```

#### JWT minter (`workers/fallback/scripts/mint-fallback-jwt.mjs`)

```javascript
// Run once per rotation. Reads SUPABASE_JWT_SECRET from env.
// Output: a JWT to be set via `wrangler secret put SUPABASE_FALLBACK_JWT`.
import { createHmac } from "node:crypto";

const secret = process.env.SUPABASE_JWT_SECRET;
if (!secret) { console.error("SUPABASE_JWT_SECRET unset"); process.exit(1); }

const now = Math.floor(Date.now() / 1000);
const header = { alg: "HS256", typ: "JWT" };
const payload = {
  role: "wispralt_fallback_worker",
  iss: "supabase",
  iat: now,
  exp: now + 365 * 24 * 3600, // 1 year; rotate quarterly per FALLBACK.md
};
const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const unsigned = `${b64(header)}.${b64(payload)}`;
const sig = createHmac("sha256", secret).update(unsigned).digest("base64url");
console.log(`${unsigned}.${sig}`);
```

#### Worker entry (`workers/fallback/src/index.ts`)

```typescript
import { RateLimit } from "./rateLimitDO";
import { BudgetCache } from "./budgetDO";
import * as supabase from "./supabase";
import * as openrouter from "./openrouter";
export { RateLimit, BudgetCache };

const FALLBACK_MODEL = "openai/whisper-large-v3-turbo";

interface Env {
  RATE_LIMIT: DurableObjectNamespace;
  BUDGET_CACHE: DurableObjectNamespace;
  OPENROUTER_API_KEY: string;
  SUPABASE_FALLBACK_JWT: string;
  SUPABASE_ANON_KEY: string;
  SUPABASE_URL: string;
  WHISPER_BUDGET_USD_PER_MONTH: string;
  WISPRALT_FALLBACK_HOUR_CAP: string;
  WISPRALT_FALLBACK_DAY_CAP: string;
  WISPRALT_FALLBACK_MAX_BYTES: string;
  OPS_ALERT_WEBHOOK?: string;
}

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(req.url);
    if (req.method === "GET" && url.pathname === "/health") {
      return new Response("ok", { status: 200 });
    }
    if (req.method !== "POST" || url.pathname !== "/transcribe/dictate") {
      return jsonError(404, "not_found");
    }

    const bearer = req.headers.get("Authorization")?.replace(/^Bearer\s+/i, "");
    if (!bearer) return jsonError(401, "Missing bearer token");
    const tokenHash = await sha256Hex(bearer);
    const user = await supabase.lookupUser(env, tokenHash);
    if (!user) return jsonError(401, "Invalid bearer token");

    const ct = req.headers.get("Content-Type") || "";
    if (!ct.startsWith("multipart/form-data")) return jsonError(415, "Expected multipart/form-data");
    const form = await req.formData();
    const file = form.get("file");
    const maxBytes = parseInt(env.WISPRALT_FALLBACK_MAX_BYTES);
    if (!(file instanceof File)) return jsonError(415, "Expected field 'file' of type WAV");
    if (file.size > maxBytes) return jsonError(413, `Upload too large (${maxBytes} bytes max)`);

    // SINGLE round-trip: rate-limit + budget check + reservation in one call.
    const rlId = env.RATE_LIMIT.idFromName(`u:${user.id}`);
    const rlStub = env.RATE_LIMIT.get(rlId);
    const audioSeconds = estimateWavSeconds(file.size);
    const reservedMicro = Math.ceil(Math.min(audioSeconds, 30) * (40_000 / 3600));
    const decisionResp = await rlStub.fetch("https://do/check-and-debit", {
      method: "POST",
      body: JSON.stringify({
        hour: parseInt(env.WISPRALT_FALLBACK_HOUR_CAP),
        day: parseInt(env.WISPRALT_FALLBACK_DAY_CAP),
        capUsd: parseFloat(env.WHISPER_BUDGET_USD_PER_MONTH),
        reservedMicro,
        budgetDoName: "global:budget",
      }),
    });
    const decision = await decisionResp.json<{ ok: boolean; retryAfterSec?: number; reason?: string }>();
    if (!decision.ok) {
      ctx.waitUntil(supabase.writeEvent(env, {
        user_id: user.id, kind: "dictate", status: 429,
        bytes_in: file.size, error_class: decision.reason,
        provider: "openrouter",
      }));
      return jsonError(429, decision.reason ?? "Rate limit hit", { "Retry-After": String(decision.retryAfterSec ?? 60) });
    }

    const buf = await file.arrayBuffer();
    const b64 = chunkedBase64(buf);
    const t0 = Date.now();
    const orResp = await openrouter.transcribe(env, b64, "wav");
    const durationMs = Date.now() - t0;

    if (!orResp.ok) {
      ctx.waitUntil(supabase.writeEvent(env, {
        user_id: user.id, kind: "dictate", status: orResp.status,
        bytes_in: file.size, duration_ms: durationMs,
        provider: "openrouter", error_class: orResp.errorClass,
      }));
      // Reconcile: refund reservation since we didn't spend.
      ctx.waitUntil(rlStub.fetch("https://do/reconcile", {
        method: "POST",
        body: JSON.stringify({ deltaMicro: -reservedMicro }),
      }));
      return jsonError(502, "Upstream provider failed");
    }

    const text = openrouter.defensiveParseContent(orResp.json);
    const actualMicro = Math.ceil(audioSeconds * (40_000 / 3600));

    ctx.waitUntil(supabase.writeEvent(env, {
      user_id: user.id, kind: "dictate", status: 200,
      bytes_in: file.size, duration_ms: durationMs,
      provider: "openrouter", cost_micro_usd: actualMicro,
      provider_request_id: orResp.generationId,
    }));
    ctx.waitUntil(rlStub.fetch("https://do/reconcile", {
      method: "POST",
      body: JSON.stringify({ deltaMicro: actualMicro - reservedMicro }),
    }));

    return jsonOk({
      text,
      model_id: FALLBACK_MODEL,
      duration_ms: durationMs,
      smart_formatted: false,
    });
  },
};

// helpers (sha256Hex, jsonError, jsonOk, chunkedBase64, estimateWavSeconds)
// in src/util.ts (omitted from pseudocode).
```

#### Per-user `RateLimit` DO (`rateLimitDO.ts`)

```typescript
export class RateLimit implements DurableObject {
  constructor(private state: DurableObjectState, private env: Env) {
    state.blockConcurrencyWhile(async () => {
      // If storage has data but no alarm scheduled, schedule one.
      const items = await state.storage.list({ limit: 1 });
      if (items.size > 0 && !(await state.storage.getAlarm())) {
        await state.storage.setAlarm(Date.now() + 86400_000);
      }
    });
  }

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname === "/check-and-debit") return this.checkAndDebit(await req.json());
    if (url.pathname === "/reconcile")       return this.reconcile(await req.json());
    return new Response("not_found", { status: 404 });
  }

  private async checkAndDebit(req: { hour: number; day: number; capUsd: number; reservedMicro: number; budgetDoName: string }): Promise<Response> {
    return this.state.blockConcurrencyWhile(async () => {
      const now = Math.floor(Date.now() / 1000);
      const hourBucket = Math.floor(now / 3600);
      const dayBucket = Math.floor(now / 86400);
      const hKey = `h:${hourBucket}`;
      const dKey = `d:${dayBucket}`;
      const [hCount, dCount] = await Promise.all([
        this.state.storage.get<number>(hKey).then(v => v ?? 0),
        this.state.storage.get<number>(dKey).then(v => v ?? 0),
      ]);
      if (hCount >= req.hour) return Response.json({ ok: false, retryAfterSec: 3600 - (now % 3600), reason: "hour_cap" });
      if (dCount >= req.day)  return Response.json({ ok: false, retryAfterSec: 86400 - (now % 86400), reason: "day_cap" });

      // Budget check via global BudgetCache DO.
      const budgetId = this.env.BUDGET_CACHE.idFromName(req.budgetDoName);
      const budgetStub = this.env.BUDGET_CACHE.get(budgetId);
      const budgetResp = await budgetStub.fetch("https://do/check?cap_usd=" + req.capUsd + "&reserve_micro=" + req.reservedMicro);
      const budget = await budgetResp.json<{ ok: boolean; secsToMonthEnd?: number; reason?: string }>();
      if (!budget.ok) return Response.json({ ok: false, retryAfterSec: budget.secsToMonthEnd ?? 60, reason: budget.reason ?? "budget" });

      // All-or-nothing debit.
      await Promise.all([
        this.state.storage.put(hKey, hCount + 1),
        this.state.storage.put(dKey, dCount + 1),
      ]);
      if (!(await this.state.storage.getAlarm())) {
        await this.state.storage.setAlarm(Date.now() + 86400_000);
      }
      return Response.json({ ok: true });
    });
  }

  private async reconcile(req: { deltaMicro: number }): Promise<Response> {
    // Forward to budget DO so its cached spend stays roughly correct.
    const budgetId = this.env.BUDGET_CACHE.idFromName("global:budget");
    const budgetStub = this.env.BUDGET_CACHE.get(budgetId);
    await budgetStub.fetch("https://do/reconcile", {
      method: "POST",
      body: JSON.stringify({ deltaMicro: req.deltaMicro }),
    });
    return Response.json({ ok: true });
  }

  async alarm(): Promise<void> {
    const now = Math.floor(Date.now() / 1000);
    const cutoffHour = Math.floor(now / 3600) - 48;
    const cutoffDay = Math.floor(now / 86400) - 2;
    const all = await this.state.storage.list();
    const toDelete: string[] = [];
    for (const k of all.keys()) {
      if (k.startsWith("h:") && parseInt(k.slice(2)) < cutoffHour) toDelete.push(k);
      if (k.startsWith("d:") && parseInt(k.slice(2)) < cutoffDay) toDelete.push(k);
    }
    if (toDelete.length) await this.state.storage.delete(toDelete);
    await this.state.storage.setAlarm(Date.now() + 86400_000);
  }
}
```

#### Global `BudgetCache` DO (`budgetDO.ts`)

```typescript
export class BudgetCache implements DurableObject {
  private failureCount = 0;
  private failureWindowStart = 0;

  constructor(private state: DurableObjectState, private env: Env) {}

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname === "/check") return this.check(url.searchParams);
    if (url.pathname === "/reconcile") return this.reconcile(await req.json());
    return new Response("not_found", { status: 404 });
  }

  private async check(params: URLSearchParams): Promise<Response> {
    const capUsd = parseFloat(params.get("cap_usd") ?? "50");
    const reserveMicro = parseInt(params.get("reserve_micro") ?? "0");
    return this.state.blockConcurrencyWhile(async () => {
      const cached = await this.state.storage.get<{ centsThisMonth: number; ts: number; reservedMicro: number }>("budget");
      const now = Date.now();
      const fresh = cached && (now - cached.ts) < 5_000;
      let centsThisMonth: number;
      let reservedMicro = cached?.reservedMicro ?? 0;
      if (fresh) centsThisMonth = cached!.centsThisMonth;
      else {
        try {
          const microUsd = await import("./supabase").then(m => m.sumMonthlyMicroUsd(this.env));
          centsThisMonth = Math.floor(microUsd / 10_000);
          this.failureCount = 0;
        } catch {
          if (now - this.failureWindowStart > 5 * 60_000) { this.failureWindowStart = now; this.failureCount = 0; }
          this.failureCount++;
          if (this.failureCount >= 3) {
            // Fail-closed.
            const monthEnd = monthEndUTC(now);
            return Response.json({ ok: false, secsToMonthEnd: Math.ceil((monthEnd - now)/1000), reason: "budget_fail_closed" });
          }
          centsThisMonth = cached?.centsThisMonth ?? 0; // fall back to last good
        }
        await this.state.storage.put("budget", { centsThisMonth, ts: now, reservedMicro });
      }
      const totalMicroIfReserved = (centsThisMonth * 10_000) + reservedMicro + reserveMicro;
      const capMicro = Math.floor(capUsd * 1_000_000);
      if (totalMicroIfReserved >= capMicro) {
        const monthEnd = monthEndUTC(now);
        return Response.json({ ok: false, secsToMonthEnd: Math.ceil((monthEnd - now)/1000), reason: "budget_exhausted" });
      }
      // Reserve.
      await this.state.storage.put("budget", { centsThisMonth, ts: cached?.ts ?? now, reservedMicro: reservedMicro + reserveMicro });
      return Response.json({ ok: true });
    });
  }

  private async reconcile(req: { deltaMicro: number }): Promise<Response> {
    return this.state.blockConcurrencyWhile(async () => {
      const cached = await this.state.storage.get<{ centsThisMonth: number; ts: number; reservedMicro: number }>("budget") ?? { centsThisMonth: 0, ts: Date.now(), reservedMicro: 0 };
      // Net effect: actual spend gets folded into cents on next refresh; for now,
      // adjust reserved (downward when actual < reserved, upward when actual > reserved).
      cached.reservedMicro = Math.max(0, cached.reservedMicro + req.deltaMicro);
      await this.state.storage.put("budget", cached);
      return Response.json({ ok: true });
    });
  }
}

function monthEndUTC(now: number): number {
  const d = new Date(now); d.setUTCMonth(d.getUTCMonth() + 1, 1); d.setUTCHours(0,0,0,0);
  return d.getTime();
}
```

#### Supabase REST helper (`supabase.ts`)

```typescript
export async function sha256Hex(s: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
}

const READ_HEADERS = (env: Env) => ({
  "Accept-Profile": "wispralt",
  "Authorization": `Bearer ${env.SUPABASE_FALLBACK_JWT}`,
  "apikey": env.SUPABASE_ANON_KEY,
});
const WRITE_HEADERS = (env: Env) => ({
  ...READ_HEADERS(env),
  "Content-Type": "application/json",
  "Content-Profile": "wispralt",
  "Prefer": "return=minimal",
});

export async function lookupUser(env: Env, tokenHash: string): Promise<{ id: number; role: string } | null> {
  const url = `${env.SUPABASE_URL}/rest/v1/rpc/lookup_user_by_token_hash`;
  const r = await fetch(url, {
    method: "POST",
    headers: { ...WRITE_HEADERS(env) },
    body: JSON.stringify({ p_hash: tokenHash }),
  });
  if (!r.ok) return null;
  const data = await r.json<any>();
  // RPC TABLE return → array of rows.
  if (Array.isArray(data) && data[0]) return { id: data[0].id, role: data[0].role };
  return null;
}

export async function writeEvent(env: Env, ev: FallbackEventRow): Promise<void> {
  await fetch(`${env.SUPABASE_URL}/rest/v1/fallback_events`, {
    method: "POST",
    headers: WRITE_HEADERS(env),
    body: JSON.stringify(ev),
  });
}

export async function sumMonthlyMicroUsd(env: Env): Promise<number> {
  const url = `${env.SUPABASE_URL}/rest/v1/rpc/fallback_micro_usd_this_month`;
  const r = await fetch(url, { method: "POST", headers: WRITE_HEADERS(env), body: "{}" });
  if (!r.ok) throw new Error(`rpc_${r.status}`);
  const text = await r.text();
  // Defensive parse: bare number, {fn:value}, or [{fn:value}].
  const tryParse = (raw: string): number => {
    const trimmed = raw.trim();
    if (/^-?\d+$/.test(trimmed)) return parseInt(trimmed);
    try {
      const j = JSON.parse(trimmed);
      if (typeof j === "number") return j;
      if (Array.isArray(j) && typeof j[0]?.fallback_micro_usd_this_month === "number") return j[0].fallback_micro_usd_this_month;
      if (typeof j?.fallback_micro_usd_this_month === "number") return j.fallback_micro_usd_this_month;
    } catch {}
    return NaN;
  };
  const v = tryParse(text);
  if (Number.isNaN(v)) throw new Error("rpc_parse_failed");
  return v;
}
```

#### OpenRouter (`openrouter.ts`)

```typescript
export async function transcribe(env: Env, b64: string, format: string) {
  const body = JSON.stringify({
    model: "openai/whisper-large-v3-turbo",
    messages: [{ role: "user", content: [{ type: "input_audio", input_audio: { data: b64, format } }] }],
  });
  const delays = [0, 200, 800];
  for (const d of delays) {
    if (d) await sleep(d);
    let r: Response;
    try {
      r = await fetch("https://openrouter.ai/api/v1/chat/completions", {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.OPENROUTER_API_KEY}`,
          "Content-Type": "application/json",
        },
        body,
        signal: AbortSignal.timeout(60_000),
      });
    } catch (e) {
      if (d === delays[delays.length - 1]) return { ok: false, status: 504, json: null, generationId: undefined, errorClass: "abort_or_network" };
      continue;
    }
    if (r.status === 429 || r.status >= 500) {
      if (d === delays[delays.length - 1]) {
        const j = await r.json().catch(() => null);
        return { ok: false, status: r.status, json: j, generationId: undefined, errorClass: `http_${r.status}` };
      }
      continue;
    }
    const j = await r.json<any>();
    return {
      ok: r.ok,
      status: r.status,
      json: j,
      generationId: r.headers.get("X-Generation-Id") || r.headers.get("OpenRouter-Generation-Id") || j?.id,
      errorClass: r.ok ? undefined : `http_${r.status}`,
    };
  }
  return { ok: false, status: 504, json: null, generationId: undefined, errorClass: "exhausted" };
}

export function defensiveParseContent(json: any): string {
  const c = json?.choices?.[0]?.message?.content;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) {
    const text = c.find((b: any) => b?.type === "text")?.text;
    if (typeof text === "string") return text;
  }
  return "";
}

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));
```

#### Swift `RequestAttempt` + classifier (`ServerClient.swift`)

```swift
struct RequestAttempt {
    let startedAt: Date
    let finishedAt: Date
    let lastByteSentAt: Date?      // populated by URLSessionTaskDelegate.didSendBodyData
    let result: Result<HTTPURLResponse, Error>
    var elapsedSec: TimeInterval { finishedAt.timeIntervalSince(startedAt) }
}

extension ServerClient {
    static let miniHealthWindowSec: TimeInterval = 30
    static let byteACKMaxAgeSec: TimeInterval = 5

    func isOfflineSignature(_ a: RequestAttempt) -> Bool {
        switch a.result {
        case .failure(let err):
            if let urlErr = err as? URLError {
                switch urlErr.code {
                case .cannotConnectToHost, .dnsLookupFailed, .cannotFindHost, .networkConnectionLost:
                    return true
                case .timedOut:
                    let bytesQuiet = (a.lastByteSentAt.map { a.finishedAt.timeIntervalSince($0) } ?? .infinity) > Self.byteACKMaxAgeSec
                    return bytesQuiet && a.elapsedSec > Self.miniHealthWindowSec
                default: return false
                }
            }
            return false
        case .success(let resp):
            let cfLayer = resp.value(forHTTPHeaderField: "CF-Ray") != nil
                       && resp.value(forHTTPHeaderField: "X-Request-Id") == nil
            return cfLayer && [502, 522, 523, 524].contains(resp.statusCode)
        }
    }
}
```

#### Swift `DictationAPI` switching to UploadTask (`DictationAPI.swift`)

```swift
final class DictationAPI: NSObject, URLSessionTaskDelegate {
    static let shared = DictationAPI()
    private var lastByteSentAt = [Int: Date]() // taskIdentifier → time

    func transcribe(_ wav: URL, smartFormat: Bool) async throws -> TranscribeResponse {
        let body = try multipartBody(wav: wav)
        // Try origin
        let origin = try await attempt(url: ServerClient.shared.baseURL.appendingPathComponent("/transcribe/dictate"), body: body, smartFormat: smartFormat)
        if let r = origin.success { return r }
        let attempt1 = origin.attempt!
        if !ServerClient.shared.isOfflineSignature(attempt1) { throw origin.error! }

        // Retry origin once after ~5s spacing
        try await Task.sleep(nanoseconds: 5_000_000_000)
        let retry = try await attempt(url: ServerClient.shared.baseURL.appendingPathComponent("/transcribe/dictate"), body: body, smartFormat: smartFormat)
        if let r = retry.success { return r }
        let attempt2 = retry.attempt!
        if !ServerClient.shared.isOfflineSignature(attempt2) { throw retry.error! }

        // Confirmed offline → fallback URL
        let fb = Settings.shared.fallbackURL
        guard !fb.isEmpty, let fbBase = URL(string: fb) else { throw retry.error! }
        let fallback = try await attempt(url: fbBase.appendingPathComponent("/transcribe/dictate"), body: body, smartFormat: smartFormat)
        if let r = fallback.success { os_log("inject source=fallback http=200", log: .inject, type: .info); return r }
        throw fallback.error!
    }

    private func attempt(url: URL, body: MultipartBody, smartFormat: Bool) async throws -> AttemptResult { /* ... */ }

    // URLSessionTaskDelegate
    func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
                    totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
        lastByteSentAt[task.taskIdentifier] = Date()
    }
}
```

#### Swift `PendingUploadsQueue` + drain coordinator (`PendingUploadsQueue.swift`)

```swift
final class PendingUploadsQueue {
    static let shared = PendingUploadsQueue()
    private let dir: URL
    private let coordinator = PendingUploadsDrainCoordinator()

    init() {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        self.dir = base.appendingPathComponent("co.wispralt/pending-uploads", isDirectory: true)
    }

    func enqueue(wav source: URL) throws {
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let attrs = try FileManager.default.attributesOfFileSystem(forPath: dir.path)
        let free = (attrs[.systemFreeSize] as? Int64) ?? 0
        guard free > 2_000_000_000 else { throw QueueError.diskFull }

        // Source must already be fsynced by recording pipeline.
        let id = UUID().uuidString
        let dest = dir.appendingPathComponent("\(id).wav")
        let tmp = dir.appendingPathComponent("\(id).wav.tmp")
        try FileManager.default.copyItem(at: source, to: tmp)
        let fh = try FileHandle(forUpdating: tmp); try fh.synchronize(); try fh.close()
        try FileManager.default.moveItem(at: tmp, to: dest)
        let dirFd = open(dir.path, O_RDONLY); if dirFd >= 0 { fsync(dirFd); close(dirFd) }
    }

    func drain() async { await coordinator.drain { await self._drainOnce() } }

    private func _drainOnce() async {
        let items = pending()
        var attempts = readAttemptCounts()
        for wav in items {
            let id = wav.deletingPathExtension().lastPathComponent
            if (attempts[id] ?? 0) >= 5 {
                try? FileManager.default.createDirectory(at: dir.appendingPathComponent("failed"), withIntermediateDirectories: true)
                try? FileManager.default.moveItem(at: wav, to: dir.appendingPathComponent("failed/\(id).wav"))
                continue
            }
            do {
                _ = try await MeetingAPI.submit(wav, progress: { _ in })
                try? FileManager.default.removeItem(at: wav)
                attempts.removeValue(forKey: id)
            } catch {
                attempts[id, default: 0] += 1
                // Continue rather than break — next item may still succeed.
            }
        }
        writeAttemptCounts(attempts)
    }

    func pending() -> [URL] { /* enum dir, .wav only, sorted by mtime asc */ }
    private func readAttemptCounts() -> [String: Int] { /* read attempts.json */ }
    private func writeAttemptCounts(_ d: [String: Int]) { /* atomic write attempts.json */ }
    enum QueueError: Error { case diskFull }
}

actor PendingUploadsDrainCoordinator {
    private var inFlight: Task<Void, Never>?
    func drain(_ work: @escaping () async -> Void) async {
        if let t = inFlight { await t.value; return }
        let t = Task { await work() }
        inFlight = t
        await t.value
        inFlight = nil
    }
}
```

### Tasks (in implementation order)

1. **Migration** — write/apply `2026-05-05-v3-fallback-events.sql`.
   DoD: `wispralt.fallback_events` exists; both RPCs callable; role + grants present; `schema_version` row 3 inserted.
2. **Worker scaffold** — `workers/fallback/` builds; `wrangler dev` serves `/health`. DoD: `curl localhost:8787/health` → 200 "ok".
3. **DOs + supabase.ts + openrouter.ts** — implement RateLimit, BudgetCache, Supabase RPC helper (with defensive scalar parser), OpenRouter call (with AbortSignal). DoD: unit-style probes via wrangler dev.
4. **JWT minter + secrets** — `scripts/mint-fallback-jwt.mjs`; `wrangler secret put` for OPENROUTER_API_KEY, SUPABASE_FALLBACK_JWT, SUPABASE_ANON_KEY, OPS_ALERT_WEBHOOK. DoD: `wrangler secret list` shows exactly 4 secrets.
5. **Empirical OpenRouter probe** — call OpenRouter with a 1s test WAV, save response (scrubbed `id`/`created`/`usage`) to `probes/openrouter-shape.json`. Pin parser. DoD: probe committed.
6. **Worker entry full flow** — `index.ts` end-to-end. DoD: local curl with real bearer + WAV returns documented JSON.
7. **Dev faults router (mini)** — `routes/dev_faults.py` with `?fault=503` endpoint; conditional `include_router` in `main.py`. DoD: with flag set + non-prod hostname, `curl /transcribe/dictate?fault=503` returns 503 with `X-Request-Id`.
8. **DNS + deploy** — `fallback.integrateapi.ai` → orange-cloud → Worker route; `wrangler deploy`; verify `curl https://fallback.integrateapi.ai/health` → 200.
9. **Settings.fallbackURL** — UserDefaults-backed; default `https://fallback.integrateapi.ai`; empty-string fast-fail.
10. **ServerClient: RequestAttempt + isOfflineSignature** — shared classifier callable from both call sites.
11. **DictationAPI: switch to URLSessionUploadTask + delegate** — `lastByteSentAt` per task; new `transcribe` flow with origin retry + fallback switch.
12. **PendingUploadsQueue + DrainCoordinator** — atomic write, dir-fsync, attempts.json, no-break drain, dead-letter at 5 attempts.
13. **MeetingRecorder fsync source** — recording pipeline calls `fsync` on the WAV before yielding URL to `MenuBarController.processMeetingUpload`.
14. **MeetingAPI: offline classification in catch** — RequestAttempt build + isOfflineSignature check; on offline → enqueue + toast.
15. **MenuBarController** — debounce dict, 4 drain triggers, `(N pending)` suffix, "Retry pending uploads" menu item.
16. **Documentation** — `docs/FALLBACK.md`, OVERVIEW map updates (8 worker files + queue + classifier → FALLBACK.md), ARCHITECTURE.md, CLAUDE.md.
17. **End-to-end verification** — all 13 success criteria.

### Integration Points

- Schema source of truth: `server/migrations/`.
- New entry point: `workers/fallback/src/index.ts`.
- Existing client extension points: `DictationAPI.transcribe`,
  `MeetingAPI.submit`, `ServerClient.isOfflineSignature`,
  `MenuBarController` toasts/menu.
- Validation: `Bearer` + sha256 + RPC `lookup_user_by_token_hash`.
- External: Cloudflare Worker (Paid), DNS, Wrangler secrets, Supabase
  RPCs, dedicated worker role + JWT, optional `OPS_ALERT_WEBHOOK`.

## Validation

```bash
# Worker
cd workers/fallback && npm run typecheck && npm run dev
curl localhost:8787/health   # → 200 "ok"

# Client
cd client && xcodebuild -scheme WisprAlt -configuration Debug build | xcpretty

# Migration
psql "$SUPABASE_DATABASE_URL" -f server/migrations/2026-05-05-v3-fallback-events.sql

# E2E (local)
launchctl stop co.wispralt.server          # prod-mini OR /etc/hosts override on dev
# Hold FN, dictate
log stream --predicate 'subsystem == "co.wispralt"' | grep "source=fallback"

# Secret hygiene
grep -rn -E "(SUPABASE_SERVICE_ROLE|service_role)" workers/   # → no matches
wrangler secret list   # → exactly 4 entries
```

### Manual Checks

- Block mini via `/etc/hosts` (`0.0.0.0 transcribe.integrateapi.ai`):
  fallback fires after 30s window + 5s confirm.
- 522 simulation: stop cloudflared on prod-mini → tunnel returns 522.
- `URLError.networkConnectionLost`: kill prod-mini mid-upload.
- Slow upload (Network Link Conditioner 50KB/s): no fallback, last byte
  ACK keeps refreshing.
- Hour-cap test with `WISPRALT_FALLBACK_HOUR_CAP=2` → 3rd request 429.
- Day-cap test with `WISPRALT_FALLBACK_DAY_CAP=3` → 4th 429.
- Both-down: kill Worker (`wrangler tail` Ctrl-C deploy with stub
  returning 500) + block mini → "Transcription temporarily unavailable"
  toast within 30s, debounced.

## Open Questions

- None blocking. Defaults: budget=$50/mo, hour=100, day=200, max=10MB,
  webhook empty.

## Final Validation Checklist

- [ ] Worker `npm run typecheck` clean
- [ ] Worker `wrangler deploy --dry-run` clean
- [ ] Xcode build clean
- [ ] Migration applied; schema_version row 3 present; both RPCs
      callable as `wispralt_fallback_worker`
- [ ] All 13 Success Criteria ticked
- [ ] `grep -rn -E "(SUPABASE_SERVICE_ROLE|service_role)" workers/`
      returns nothing
- [ ] `wrangler secret list` shows exactly 4 expected entries
- [ ] OpenRouter probe JSON committed (scrubbed)
- [ ] Workers Paid plan active
- [ ] `/docs-check` clean

## Deprecated / Removed Code

- `client/WisprAlt/Server/MeetingAPI.swift:61-64` TODO comment removed
  (queue now exists).

## Anti-Patterns to Avoid

- No fallback on meeting path.
- No fallback on origin 5xx with `X-Request-Id`.
- No KV rate limiting.
- No service-role key. Worker uses dedicated narrow role + RPCs only.
- No re-encoding audio in Worker beyond chunked base64.
- No secrets in `wrangler.toml`.
- No DO `expirationTtl` (KV-only).
- No body-progress assumption on `URLSession.data(for:)`. Use UploadTask.
- No fallback signature in `ServerClient.execute` — meetings bypass it.
- No `eyJ` grep for secret hygiene — use `service_role` explicit grep.
- No fail-open on Supabase RPC error after the 3rd consecutive failure.
- No widening the offline signature "just in case."

**Confidence: 8.7/10.** Remaining unknowns: empirical OpenRouter
response shape (Task 5 probe pins it), and PostgREST gateway acceptance
of role JWT in `Authorization` + anon in `apikey` (Task 4 integration
test against real Supabase pins it).
