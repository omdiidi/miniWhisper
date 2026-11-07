# Brief: Direct-to-OpenAI transcription + architecture cleanup

## Mission decisions (2026-11-07, SUPERSEDE the sections below and the spec where they conflict)
Companion spec (verbatim owner requirements): `tmp/briefs/2026-11-07-modernization-spec.md`.
- **Two transcription modes in the client menu: Online (default) and Local.** Online = client → OpenAI
  directly (model picker + prices, live models fully supported, key in macOS Keychain, single user).
  Local = the self-hosted server from the spec (MLX Parakeet/Whisper, CLI, portal, local DB), set up
  separately via `wispralt setup`, switchable from the menu. Everything should converge into one clean design.
- **Meetings (triple-tap FN recorder) removed everywhere; uploaded file transcription is KEPT** (spec §4).
- **OpenRouter audio fallback removed; Mercury 2 smart formatting via OpenRouter KEPT and fixed** (spec §5–6).
- **`server/` is NOT deleted** — it becomes the Local mode host, modernized per the spec (supersedes the
  "Where Reasoning Clashed" note below).
- **Secrets: no 1Password.** Owner had the 1Password CLI uninstalled (brew cask `1password-cli` removed
  2026-11-07; `/load-creds` no longer works here). Keys live in the macOS Keychain only (client:
  `co.wispralt.openai`; OpenRouter for Mercury likewise Keychain or host `.env` mode 0600). The OpenAI key is
  added by the owner via `security add-generic-password -U -s co.wispralt.openai -a default -w` in Terminal.
  If it is missing at test time, mark real-OpenAI tests BLOCKED, never fake them.
- **Real-spend budget:** up to ~$5 on OpenAI test calls across all picker models (answered "figure it out"
  — conservative default; short clips only).
- **Local E2E on this Mac (arm64, 16 GB, ~37 GB free):** allowed to download models and run server + CLI
  + team/portal flows for real. **Delete all of it afterwards** (models, venvs, test DBs, LaunchAgents).
- **Supabase: read-only.** Export history for the local-DB import and verify counts. NEVER modify or delete
  Supabase data — owner wants to keep it.
- **Guardrails:** dedicated branch, local commits only, no push/release/deploy.
- **Quality bar from owner:** keep the repo clean, future-proof, reusable; do not bloat; concise.
- **Execution:** fully autonomous, owner unavailable to iterate. Research via Codex; subagents on Sonnet.

## Why
The WisprAlt Mac mini (the self-hosted transcription server) was factory reset. Rather than rebuild it,
the user wants the Mac client to transcribe via OpenAI's API directly, defaulting to `gpt-transcribe`
($0.0045/min), with a model picker in Advanced settings showing every OpenAI STT model and its price.
Overarching goal stated by the user: "overall seek to clean up the architecture" — make it simple.

## Context
- Today: client → mini server (`/transcribe/dictate` + streaming chunk endpoints in
  `client/WisprAlt/Server/DictationStreamSession.swift`) → on offline, falls back to OpenRouter
  `openai/gpt-4o-audio-preview` (`client/WisprAlt/Server/DictationAPI.swift:39`, `callOpenRouter` ~L209).
- OpenRouter key lives in Keychain service `co.wispralt.openrouter`
  (`client/WisprAlt/Storage/KeychainHelper.swift:40`, L111–165) — this is the pattern to copy for an
  OpenAI key (e.g. `co.wispralt.openai`).
- Advanced settings: `client/WisprAlt/UI/SettingsView.swift` (`showAdvanced`, L94–120; server URL + API key
  fields + export/import). Settings model: `client/WisprAlt/Storage/Settings.swift`.
- Meetings: `client/WisprAlt/App/MenuBarController+Meeting.swift`, `PendingUploadsQueue`,
  `TranscriptDetailView.swift` / rename-speakers sheet, `docs/TRANSCRIPT-FORMAT.md`, server `meeting/`,
  `scripts/smoke-meeting.sh`. All to go.
- `DictationFallbackQueue` syncs cloud dictations to the server for history/telemetry — loses its purpose.
- Server folder (`server/`: users, usage, admin portal, insights, smart_format, jobs, meeting) + scripts
  (`setup-server.sh`, `setup-cloudflared.sh`, `deploy-server.sh`, `download-models.sh`, …) and slash
  commands (`/setup-server`, `/update-models`, `/test-connection`, `/verify-autostart`) all assume the mini.
- Docs file→doc map: `docs/OVERVIEW.md`. CLAUDE.md "Key conventions" describe the mini/OpenRouter world
  and will need rewriting.

### OpenAI STT models (verified 2026-11-06 via developers.openai.com docs + costgoat/cartesia/digitalapplied)
| Model | Kind | $/min | Status |
|---|---|---|---|
| `gpt-transcribe` | file, `POST /v1/audio/transcriptions` | 0.0045 | current, recommended — **default** |
| `gpt-live-transcribe` | live (Realtime API) | 0.017 | current, recommended for live |
| `gpt-realtime-whisper` | live (Realtime API) | 0.017 | current, not recommended for new builds |
| `gpt-4o-transcribe` | file | ~0.006 (token-billed) | deprecated, removed 2027-02-26 |
| `gpt-4o-mini-transcribe` (+ `-2025-12-15`) | file | ~0.003 (token-billed) | deprecated, removed 2027-02-26 |
| `gpt-4o-transcribe-diarize` | file | ~0.006 | deprecated 2027-02-26 — omit (meetings gone) |
| `whisper-1` | file | 0.006 | deprecated, removed 2027-02-26 |
`gpt-transcribe` facts: 25 MB/file, formats mp3/mp4/m4a/wav/webm, `stream=true` gives text deltas,
supports `prompt`, `keywords` (no `<`,`>`,CR,LF), `languages` (do not also send `language`); no
timestamps, no diarization. Re-verify prices/IDs against OpenAI's pricing page during /plan — per-minute
prices above came from third-party pages.

## Decisions
- **Client calls OpenAI directly; no server in the dictation path.** Fastest to ship; reuses the existing
  Keychain-key pattern.
- **Single user (just the owner), one key, one Mac.** No team key distribution needed.
- **Key stored only in macOS Keychain**, sent only to `api.openai.com` as a Bearer header, never logged,
  never in Settings/UserDefaults, never in a gist/chat. Key entered via a SecureField in Advanced settings
  (or `security add-generic-password -s co.wispralt.openai -a default -w` run by the user). The user should
  NOT paste the key into the Claude chat. Recommend a project-scoped key + monthly cap in the OpenAI dashboard.
- **Model picker in Advanced** listing all models above (minus diarize), price in small text beside each,
  deprecated ones tagged "retiring Feb 2027". Default `gpt-transcribe`.
- **Live models fully supported** (`gpt-live-transcribe`, `gpt-realtime-whisper`) via the Realtime API
  transcription session over WebSocket — user explicitly chose full support over greyed-out.
- **Remove the meetings feature everywhere** (client UI, recording, queue, transcript viewer, docs,
  server meeting code paths that the client cleanup touches, smoke scripts) — user: "we don't need the
  meetings feature, ensure it's clean everywhere".
- **Remove the OpenRouter fallback** — one provider, one key; on OpenAI failure show the existing error toast.
- **Simplify architecture overall**: drop client code that only exists to talk to the mini (server URL/API
  key fields, `ServerClient` offline ladder, streaming-chunk session, fallback sync queue, readiness checks).

## Rejected Alternatives
- Rebuild the mini as a key-holding relay — slower; only needed for multi-user, and it's single-user.
- Mic-vs-system channel split or diarize model for meetings — moot, meetings removed.
- Greying out / hiding live models — user wants them working.
- Keeping OpenRouter as backup — second key, more code, against the simplify goal.

## Where Reasoning Clashed
- **What to do with `server/`** — user said "wait on this". Deleting it fits the cleanup goal (it's in git
  history), but it also removes the admin portal / usage stats / insights the user built. Unresolved: /plan
  must NOT delete `server/` or server scripts; ask the user again at plan time. Client-side code that talks to
  the server is still removed (implied by "direct" + "clean up"), so `server/` would sit unused meanwhile.

## One Thing to Do First
Add the OpenAI Keychain entry + a single `gpt-transcribe` call in `DictationAPI` replacing the server/OpenRouter
ladder, and prove one real dictation round-trips — everything else (picker, live models, deletions) builds on that.

## Direction
Turn WisprAlt into a single-user Mac app that sends dictation audio straight to OpenAI with a Keychain-held key,
selectable model (file + live) with prices in Advanced settings. Strip meetings, OpenRouter, and the mini-facing
client plumbing; decide the fate of `server/` separately.
