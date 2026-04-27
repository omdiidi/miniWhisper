# Brief: OpenAI-compat API + Smart Formatting + Display Names + App Icon

## Why

Four bundled improvements that make WisprAlt easier to integrate, more polished to use, and more identifiable as a product:

1. **OpenAI-compat shim** so any project (current or future) can plug WisprAlt in as the transcription provider just by setting two env vars — no custom client code.
2. **Smart formatting** so dictation output reads cleanly (punctuation, casing, paragraph breaks) without changing words.
3. **Display names** so employees can identify themselves and the admin sees who's who.
4. **App icon swap** to the new dark-mic-and-chat-bubble brand mark, cleanly rendered in all the places macOS shows it.

## Context

### Server transcription surface (today)
- `POST /transcribe/dictate` — multipart `file`, Bearer auth via `require_api_key`. Response `{text, model_id, duration_ms}` (`server/src/wispralt_server/routes/dictate.py:116`). 80% of OpenAI's `/v1/audio/transcriptions` shape.
- `POST /transcribe/meeting` — async multipart, returns `{job_id, status}`.
- `GET /transcribe/meeting/{id}` — poll for completion, returns formats list + signed URLs.
- Auth dependency: Bearer token, validated against `wispralt.users.token_hash` via 60s LRU `TokenCache`.
- `/admin/me` exists but is HTML-only (no JSON `/me`).

### Client dictation path
- `DictationAPI.swift:46` returns `decoded.text` raw to the caller.
- `MenuBarController.swift:615` calls `TextInjector.inject(text)`.
- `TextInjector.inject()` (`client/WisprAlt/Inject/TextInjector.swift:20`) tries `AccessibilityInjector.tryInsert()` then falls back to `ClipboardInjector.injectViaCmdV()`.
- **Zero post-processing exists today.**

### User schema
- `wispralt.users` columns: `id`, `label` (text NOT NULL — currently used as the user's display name in admin UI), `token_hash`, `role` ('admin'|'employee'), `created_at`, `last_seen_at`, `revoked_at`, `notes` (`server/migrations/2026-04-27-v1-wispralt-schema.sql:31-40`).
- No `display_name` column. `label` is currently the admin-set handle.
- Admin user-list template shows `u.label` as a clickable link.

### Icon assets
- No `Assets.xcassets/AppIcon.appiconset/` in source tree.
- `Info.plist` has no `CFBundleIconFile` / `CFBundleIcons`.
- App is `LSUIElement=true` (menubar-only, no Dock icon) — but AppIcon still appears in Finder Get Info, About panel, Spotlight, Cmd+Tab, notifications.
- Menubar status item uses SF Symbols (`mic`, `waveform`, etc.) — fully decoupled from AppIcon. Icon swap won't affect the menubar.
- Source PNG: `/Users/omidzahrai/.pane/images/43793091-b8c4-4d81-9906-efc05b7914f6_3_1777325113499_yazq1hk.png` — dark mic + chat-bubble, has squircle pre-baked in.

### Prior-handoff items in scope (sweep up during this build)
- 3 uncommitted icon WIP files: `client/WisprAlt/Info.plist`, `scripts/build-client-local.sh`, `client/WisprAlt/Resources/` — must inspect first; may already contain partial AppIcon scaffolding.
- Multi-sentence dictation latency baseline measurement — instrumentation already shipped in PR #4 (`8809135`); want a clean before/after to prove smart-formatting toggle has the expected latency cost.
- `CHANGELOG.md` doesn't exist — start it with this PR.
- Mac mini local git on stale branch `fix/dictate-audio-decode-leak-plus-readyz-open` — `git checkout main && git pull --ff-only` during deploy step.

### Out of scope (separate concerns)
- Background URLSession resumption for meeting uploads (TODO G3 in `MeetingAPI.swift:61`).
- Sparkle EdDSA `SUPublicEDKey` placeholder.
- README mention of Apple Development cert prerequisite.
- The user's `~/.claude-dotfiles/commands/transcribe` skill — irrelevant to this work.

## Decisions

### Topic 1 — OpenAI-compat drop-in

- **Add `POST /v1/audio/transcriptions`** as a thin shim over the existing `/transcribe/dictate` model path. Multipart `file` field, optional `model`, `language`, `prompt`, `temperature`, `response_format` (json|text|srt|vtt|verbose_json). Bearer auth = same WisprAlt token.
- **Sync, dictate-only.** Cap clip size at ~30s (or settings-configurable max). On overflow, return 413 with a body that points to the native `/transcribe/meeting` async flow.
- **Response envelope per `response_format`:** `json` → `{text}`, `text` → raw text, `srt`/`vtt`/`verbose_json` → standard OpenAI shapes. (For now, `srt`/`vtt`/`verbose_json` may not be fully supported because Parakeet doesn't emit per-segment timestamps in the dictate path — return 422 with "format not supported on /v1 sync endpoint" if requested. Document this limitation.)
- **Raw output only.** `/v1` never runs smart formatting — external consumers always get the model's verbatim text.
- **Documentation:** new `docs/INTEGRATION-GUIDE.md` is the hand-off artifact. Shows the 3-step setup using **`OPENAI_API_KEY` env var name** (NOT `WISPRALT_API_KEY`) — keep client convention to maximize drop-in compat. Doc explicitly notes "the value is your WisprAlt token; the env var name follows OpenAI conventions for compatibility." Snippets for Python (openai SDK), Node (@openai/node), curl, Swift (URLSession multipart). Reasoning: third-party libraries, plugins, and SDKs auto-read `OPENAI_API_KEY`. Renaming would break drop-in setup.

### Topic 2 — Smart formatting (server-side, admin-funded LLM)

- **Single client toggle** in Settings: "Smart formatting: on/off". **Default OFF** — user opts in.
- **Implementation:** server-side post-processor that calls **OpenRouter `inception/mercury`** (regular Mercury, not Mercury Coder — prose use case). Tightly-constrained prompt: "Add punctuation, casing, paragraph breaks. Do NOT add, remove, or change any words. Return only the cleaned text, nothing else."
- **Hard 250ms timeout.** On timeout/error/missing OpenRouter key, fall through to raw text silently. No user-visible failure.
- **Dictation-only.** Never applied to `/transcribe/meeting` (meetings already produce structured WhisperX output with timestamps; cleanup adds nothing and risks changing speaker-attributed text).
- **Authenticated client only.** `/v1/audio/transcriptions` never calls Mercury. External integrations always get raw output.
- **Server config:** `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` (default `inception/mercury`) in `server/.env` (mode 0600). Admin pays via personal OpenRouter account.
- **Client signaling:** `X-Smart-Format: true` header on `/transcribe/dictate` requests when toggle is on. Server only applies cleanup when (a) header is true AND (b) `OPENROUTER_API_KEY` is configured server-side.
- **Smart-formatting availability:** any authenticated client user (admin and employees) can enable it. The phrase "only for my employees" means "for clients using the WisprAlt app, NOT for /v1 OpenAI-compat consumers." Admin (the customer themselves) is also a client user and can use it.
- **Latency cost:** ~150-250ms added round-trip when ON (network + Mercury inference). Zero added when OFF. Measure before/after with PR #4's existing instrumentation.

### Topic 3 — Display names (Path B)

- **New column** `display_name TEXT NULL` on `wispralt.users` via migration `2026-04-27-v2-display-name.sql`.
- **`label` stays as admin-set stable handle** (e.g., "ops-laptop-7"). Only admin can change it.
- **`display_name` is employee-set.** 1-40 chars, trimmed, NULL-able (employee can clear). Employee owns their own value.
- **New JSON endpoints:**
  - `GET /me` — returns `{label, display_name, role, created_at, last_seen_at}`. Auth: Bearer.
  - `PATCH /me` — body `{display_name: str | null}` (1-40 chars or null). Auth: Bearer. Updates only the caller's own row.
- **Admin user list:** show `display_name (label)` if both set, fall back to `label` if `display_name` is null.
- **First-launch UX:** after API key validates and `GET /me` returns `display_name = null`, show a one-shot SwiftUI sheet "What should we call you?" — text field + Save. Skippable; if skipped, sheet shows again on next launch until set.
- **Settings popover:** add a "Your name" row in the identity section (top of popover). Inline-editable, save on commit.
- **Existing rows:** migration leaves `display_name` NULL for everyone. Both you (admin) and existing employees get the first-launch prompt on next client update.

### Topic 4 — App icon (full-bleed square, programmatically prepared)

- **Inspect 3 uncommitted icon WIP files first.** They may already have `AppIcon.appiconset/Contents.json` or partial sizes I can build on.
- **Strip the baked-in squircle:** programmatically extract the inner full-bleed square content from the source PNG so macOS can apply its native rounded-corner mask cleanly. Tool: `sips` + a Python/Swift script using CoreGraphics, or just `sips -p` to crop. If extracting cleanly produces a content-bleed problem (logo touches the edges), fall back to shipping the source as-is and accept the slight double-rounding.
- **Generate 7 sizes** via `sips`: 16, 32, 64, 128, 256, 512, 1024 px (each at @1x and @2x where appropriate per `Contents.json` template).
- **Drop into** `client/WisprAlt/Resources/Assets.xcassets/AppIcon.appiconset/`. Reference from `Info.plist` via `CFBundleIconName=AppIcon` (modern asset-catalog approach).
- **Verify rendering** in: Finder Get Info, About panel (NSApp.orderFrontStandardAboutPanel), Spotlight result, Cmd+Tab (briefly visible during transitions despite `LSUIElement`), notifications.
- **Menubar unaffected** — confirm `MenuBarController.swift:255+` still uses SF Symbols only.

## Rejected Alternatives

- **`/v1` syncing all clip sizes (no cap)** — would hold HTTP connections open for minutes on long audio. Native async `/transcribe/meeting` exists for that. Capping at ~30s keeps the contract predictable.
- **Renaming `OPENAI_API_KEY` to `WISPRALT_API_KEY` in integration docs** — would break auto-pickup by third-party SDKs and tools. Documented note that "the value is your WisprAlt token, env var name follows OpenAI convention" is enough.
- **Rules-based smart formatting (no LLM)** — would work but produce mechanical-feeling output. Mercury via OpenRouter is fast enough (~200ms) and produces qualitatively better punctuation/paragraph breaks. User accepted the small latency cost.
- **Wispr Flow's "Auto-edit" style word-substitution cleanup** — explicitly rejected; user wants no word changes, just formatting.
- **Smart formatting on `/transcribe/meeting`** — meetings have speaker-attributed segments already; LLM rewrite risks corrupting attribution. Skip.
- **Smart formatting available on `/v1`** — external API consumers expect raw transcription; injecting LLM cleanup violates principle of least surprise. Skip.
- **Mercury Coder over Mercury** — Coder is tuned for code, not prose. Use regular Mercury.
- **Reusing `label` as the employee-set name (Path A)** — loses admin's stable identifier. Path B (separate column) is cleaner.
- **Required `display_name` (no NULL)** — annoying for employees who don't want to engage. Allow NULL with first-launch nudge.
- **Default smart formatting ON** — users should opt-in once they trust it; ON-by-default invites complaints when it does anything they didn't expect.
- **Shipping icon with double-rounded squircle as primary path** — looks slightly off; programmatic strip is cheap to try first.

## Direction

Bundle all four into a single coordinated PR over ~5 commits:

1. **Server**: migration for `display_name`, new JSON `/me` endpoints, `/v1/audio/transcriptions` shim, OpenRouter integration for smart formatting (gated by header + env), updated admin templates.
2. **Client**: smart formatting toggle in Settings (default OFF) sending `X-Smart-Format` header, display-name first-launch sheet + Settings row, calls to `GET/PATCH /me`.
3. **Icon**: extract full-bleed square from source, generate 7 sizes, drop into Assets.xcassets, wire `CFBundleIconName`.
4. **Docs**: new `docs/INTEGRATION-GUIDE.md` (hand-off artifact for external projects), update `OVERVIEW.md` / `ARCHITECTURE.md` / `API.md` / `ADMIN.md` / `SETUP-CLIENT.md` / `SETUP-SERVER.md`. Start `CHANGELOG.md`.
5. **Verification**: latency baseline measurement (multi-sentence dictation, before/after smart-formatting toggle), end-to-end test of OpenAI-compat with `openai` Python SDK pointing at our base URL, first-launch display-name flow on a fresh keychain, icon rendering in Finder/About/Spotlight, mini stale-branch cleanup during deploy.

After plan is written: run plan-reviewer **3 times back-to-back**, fold fixes between each pass, then `/implement`. Build + sign + install client and server. Push to GitHub once verified. Then `/pre-compact`.
