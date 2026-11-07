# Owner spec (verbatim, pasted 2026-11-07) — miniWhisper / WisprAlt Complete Modernization

> Read alongside `2026-11-07-openai-direct-transcription-cleanup.md`, whose "Mission decisions"
> section OVERRIDES this spec where they conflict (notably: Online/OpenAI mode is added and is the default).

Repository: https://github.com/omdiidi/miniWhisper

## Objective
Turn this project from a personal tool built around the owner's Mac mini and infrastructure into a polished,
reliable, self-hosted, open-source transcription platform anyone can install, configure, operate, and maintain.
Do not rebuild from scratch: understand, preserve what works, simplify, remove obsolete functionality, make it portable.
Goal: hand the repo to a coding agent on a new computer and it installs/configures everything with minimal human involvement.
Implementation project: audit first, plan, execute, rigorously test.

## 1. Understand the existing architecture
Map: Swift macOS client; Python/FastAPI server; every model (purpose, deps, install, memory); server setup, client
install, CLI scripts, background services, updates; auth/API keys/users/roles; Supabase/Postgres + local SQLite;
transcript storage/history/import/export/retention; admin portal, analytics, AI insights; Cloudflare Tunnel;
OpenRouter integrations (transcription fallback vs Mercury cleanup vs analytics); tests, CI, config, deploy scripts.
Key files: `scripts/setup-server.sh`, `install.sh`, `scripts/doctor.sh`, Swift client, `jobs/store.py`,
`meeting/pipeline.py`, `insights/cron.py`, `routes/admin_ui.py`, `routes/admin_data.py`.
Verify from source and tests, not docs. Identify dead code, duplication, hardcoded personal config, fragile deps,
security concerns, confusing abstractions, install/maintenance complications. Document findings, then implement.

## 2. One unified CLI (primary install/maintenance interface)
`wispralt setup | status | doctor | start | stop | restart | users add | keys create | import | backup | update | uninstall`
(refine names). `wispralt setup` is interactive; first question: Host / Client / Both. Host: model runtime, deps,
local DB, networking, background services, admin portal. Client: install desktop app, collect server address + auth,
configure. Both: combine without redundancy. Also noninteractive setup with flags and securely supplied config.
Existing scripts may remain as internal implementation details.

## 3. Automatic fresh-computer installation (very important)
Clone + one bootstrap command. Handle: platform/hardware detection, macOS checks, Python/venv, Homebrew + system
packages, dependency versions, FFmpeg, model download + validation, local DB init + migrations, secure config +
credential generation, server install/start, background services/restart, client acquisition/install/config,
connection verification, open web portal. Idempotent, resumable, safe to rerun. Never overwrite data, regenerate
working credentials, or destroy a working install on update. Human only for macOS permissions, credentials,
gated-model licenses, optional external networking. Remove personal domain/machine/username/Supabase/server-URL
assumptions. Keep Apple Silicon MLX path; structure inference backend so other platforms can be added; detect
unsupported hardware and explain. Native macOS client stays; third-party apps connect via HTTP API.

## 4. Simplify transcription
Verify: dictation = Parakeet via MLX; uploaded files = MLX Whisper Large V3 Turbo; meeting diarization = Pyannote.
Preserve FN hold-to-dictate (streaming optimizations, low latency, text injection, mic selection, recovery).
Preserve uploaded file transcription (select audio/video, progress, TXT/SRT/VTT/JSON, large files, chunked
uploads, cancellation, recoverable processing). Decide whether Whisper stays the file model without sacrificing
quality/long-file reliability. Remove the triple-tap-FN meeting recorder (workflow, capture infra, hotkey, UI,
server endpoints exclusively serving it) WITHOUT breaking file transcription that shares code. Remove Pyannote /
HF gated setup if no longer needed. Preserve the OpenAI-compatible transcription API + integration API keys.

## 5. Completely remove cloud transcription fallback
Remove direct OpenRouter audio transcription, backup audio model + routing, client-side fallback key config,
cloud fallback queues + telemetry ingestion, related config/docs/UI/DB logic. Preserve historical records from it.
Keep ordinary network retries, safe local failure handling, local transcription API. Distinguish from other
OpenRouter integrations that stay.

## 6. Keep and fix Mercury 2 smart formatting (OpenRouter)
Improve punctuation, capitalization, paragraphs, fillers, repetitions without changing meaning. Investigate
timeouts, request formatting, response parsing, word thresholds, silent failures. Optional; works with local
transcription; clear indication whether applied; reliable fallback to raw; no latency for short dictations; no
misleading success; disclose that text goes to OpenRouter. Separate from the deleted audio fallback.

## 7. Fully local database
Prefer one local DB on the host (likely SQLite). No Supabase project needed for new users. Store users/auth, API
keys/roles, usage events/stats, dictation + file-transcription history, jobs/state, AI insights, config metadata.
Clean migrations, indexes, backups. Minimize sensitive retention; configurable, transparent retention; migration
must never silently destroy history. Backup/restore + move to another computer. Safe migration from Supabase +
current SQLite: back up first, verify, only then consider removing obsolete infra.

## 8. Modernize the web portal
Modern, clean, responsive; polished developer-dashboard feel; restrained style; avoid heavy frontend framework if
existing stack suffices. Secure first-time admin setup; join existing instance by token or admin invite; no
public signup or arbitrary admin creation. Easy API key management; users vs integration keys; create users,
invite devices, issue/revoke/rotate keys, inspect usage; show keys once; consider onboarding links.
System monitoring: host/server status, uptime/restarts, connected clients (active vs recently used), loaded models
+ readiness, CPU/memory, disk, jobs/queues, latency + error rates, DB health, network, versions + updates.

## 9. Rebuild Data & Insights
Audit the full analytics flow; fix broken calcs, missing data, stale insights, misleading info, unreliable cron.
Useful insights: what was worked on, recurring projects/topics, decisions w/ supporting transcripts, action items,
blockers, patterns over time, team activity, searchable history, real usage/perf metrics. No generic AI fluff;
separate measured stats from AI interpretation; link insights to source records; personal + admin team views
with access control. Basic stats always local; AI analysis via explicitly configured provider with privacy
disclosure, cost controls, progress, failure handling, rerun/backfill. Critique existing prompts/data structures.

## 10. Import existing history
Sources: local SQLite, Supabase records, transcript output files, other legitimate storage. Formats TXT/JSON/SRT/VTT.
Preserve timestamps, ownership, text, source, metadata. Handle duplicates, malformed files, missing metadata,
interruptions. Dry-run preview. Idempotent + resumable. Imported records appear in portal + analytics. Be
transparent about records lost to prior retention; never invent history.

## 11. Clean up the repo for public open-source release
Remove personal assumptions, outdated instructions, obsolete scripts, unused deps, redundant abstractions,
abandoned experiments. Coherent structure: CLI, server, native client, models, DB, portal. No abstraction for
show. Consolidate docs. README: what it does, architecture, supported OS/hardware, install, host/client/both,
connect another computer, portal, users/keys, update/backup/migrate/troubleshoot, agent install instructions.
Accurate `AGENTS.md`. No secrets/transcripts/personal config. Licensing, attribution, packaging, signing, updates.
Never bypass macOS security / strip quarantine as a distribution substitute.

## 12. Test everything end to end
1 clean install; 2 host-only; 3 client-only; 4 both; 5 new admin + existing-token auth; 6 user/key create/auth/
rotate/revoke; 7 real FN dictation; 8 streaming + regular paths; 9 real uploaded-file transcription with local
model; 10 short/long/large files incl. failures + recovery; 11 Mercury success + safe failure; 12 restart/crash
recovery/background launch; 13 DB migrations preserve records; 14 history import + dedup; 15 accurate personal +
team analytics; 16 portal auth/authz/monitoring/usability; 17 OpenAI-compatible API; 18 updates/backup/restore/
repeat installs; 19 meeting recording removed; 20 OpenRouter audio removed without breaking other OpenRouter use.
Run all Python/Swift/CLI/UI/integration tests; expand CI incl. macOS. Real Apple Silicon + real weights for ML
tests, distinguished from mocks. Compiling ≠ verified. Record actual results, failures, blocked tests.

## 13. Execution rules
Phases: 1 Audit (architecture map + prioritized findings) → 2 Design (target architecture, CLI flows, storage,
migration, removal boundaries) → 3 Core implementation → 4 Portal + insights → 5 Verification → 6 Release prep.
Continue through implementation. Preserve functioning installs. No destructive changes to real data/credentials
without backup + verified recovery. Dedicated branch. No push/release/deploy without explicit approval. Explain
tradeoffs on compatibility/privacy/behavior decisions.

## Final deliverables
Clean codebase; unified tested CLI; repeatable host + client setup; modern portal; local-first storage with tested
migrations; useful analytics; history import + backup; preserved dictation + file transcription; meeting recording
and cloud audio fallback removed; verified Mercury cleanup; accurate docs; real test results; before/after
architecture explanation + limitations.

**Success criterion:** clean compatible Mac + AI coding agent + this public repo → installed, configured, host or
client chosen, permissions/credentials done, fully functioning, no source edits, no reliance on the owner's
personal infrastructure.
