# Target design v2 (Part 1, 2026-11-07, revised after review round 1)

Owner rule: clean, concise, future-proof, reusable; no abstraction for show.
Review round 1 report: /var/folders/mc/6xd4rvtd7d7gwmq5_lp37vjh0000gn/T//codex-review.tF2k3K/report-final.md (B1-B18 referenced below).

## 1. Client: two modes

### 1.1 Mode
- `Settings.mode: online | local`. Default **online** on a fresh install. On upgrade, an existing `serverURL` and Keychain key mean **local** (one-time migration).
- Menu bar: "Online (OpenAI)" / "Local server" picker, plus a status line showing the active mode and its reachability (key present / host healthy).
- In Online mode, server-only UI is hidden: `MeAPI.get` at launch (AppDelegate.swift:110), DisplayNameSheet, `/me` links, server health check, server "copy last dictation". (B17)
- Local mode unreachable = a hard failure with a clear toast. There is no cross-provider fallback (owner removed it).

### 1.2 The dictation seam (B1, B4, B15)
- The branch point is `DictationAPI.transcribe`, which today picks between the stream session and a whole-file upload (DictationAPI.swift:68-90). The new contract lives in `WisprAltCore` (testable):
  ```
  protocol DictationProvider {            // one instance PER dictation, created at start()
    var wantsLiveAudio: Bool { get }      // true: receive frames as captured
    func begin() async throws              // open socket / session before audio (Realtime, Local stream)
    func append(frames: PCMFrames)         // 16 kHz mono Float32 frames (recorder already canonicalizes)
    func finish(fullRecording: AudioFile) async throws -> DictationResult
    func cancel()
  }
  struct DictationResult { text: String; formatting: applied|skipped(reason)|failed(reason)|none }
  ```
- Each provider owns its own transport framing:
  - `LocalServerProvider` does VAD chunking to the existing `/transcribe/dictate/stream` endpoints and falls back to the whole-file `/transcribe/dictate` on short audio or stream failure (today's behaviour, minus OpenRouter).
  - `OpenAIFileProvider` ignores frames. On `finish` it encodes the full recording to m4a 16 kHz mono (AudioExtractor) and POSTs it once. (B2: 600 s cap ≈ 10 MB AAC, well under 25 MB; a size guard splits if exceeded.)
  - `OpenAIRealtimeProvider` resamples to the session's required rate as mono PCM16 and sends base64 `input_audio_buffer.append`. Turn detection is off (`turn_detection: null`). On `finish` it sends `commit`, awaits `...input_audio_transcription.completed`, assembles deltas in order, then closes the socket on success, cancel or error. The exact event names and rate are verified by a Part 3 pre-flight against the real API. (B3)
- The recorder holds no shared session field. It hands frames to the provider instance created for this dictation. The next `start()` is gated until the previous `stop()` has finished encoding (fixes the overlap at DictationRecorder.swift:354/461; +Dictation.swift:98). (B4)
- Recovery (B4): any provider failure means one automatic retry of the whole recording via that mode's whole-file path. A Realtime failure re-sends the file through `OpenAIFileProvider` with the same model family default. On final failure the recording is kept in `failed/last.m4a` (one slot, overwritten) and the menu offers "Retry last dictation". It is deleted on success or after 24 h.
- `WisprAltCore` additions (all unit-tested without network): `DictationProvider` and `DictationResult`; the model catalog; OpenAI request builders (multipart + Realtime event JSON); the resampler; a `SecretStore` protocol with a Keychain implementation in the app and an in-memory fake for tests; and the mode-migration logic. (B15)

### 1.3 Model catalog (B14)
- `OpenAIModels.swift` (WisprAltCore) is one static table: id, kind (file|live), $/min, status + retire date, capability flags (prompt, keywords, languages, stream, response formats, max duration), and `verifiedOn: 2026-11-0x` (re-verified against OpenAI's pricing page in Part 3).
- The picker in Advanced shows each model with its price in small secondary text and a "retires Feb 26, 2027" tag. Default `gpt-transcribe`. The diarize model is omitted.
- Request builders only send fields the model's flags allow. On `model_not_found` or a retired model, the app resets to `gpt-transcribe`, shows a toast, and retries once.

### 1.4 Online file transcription (B13, contradiction)
- AudioExtractor produces m4a 16 kHz mono. It is split by duration per model (catalog `maxDuration`, a conservative default of 20 min), cut at the lowest-energy point within ±10 s of the boundary (EnergyVAD), and the previous chunk's last ~200 chars are passed as `prompt`.
- Per-chunk results persist in a job folder (`manifest.json` + `chunk-N.txt`). Resume after relaunch skips completed chunks. Final TXT/JSON are written atomically (tmp + rename) only when every chunk has succeeded. Cancel deletes the job folder.
- **Stated limitation:** Online produces TXT + JSON only. `gpt-transcribe` has no timestamps, and `whisper-1` retires 2027-02-26, so it is not offered as a subtitle path. SRT/VTT stay fully supported in Local mode. The UI says this next to the Online file option.

### 1.5 Secrets, logging, transport (B5, B7)
- The OpenAI key lives in Keychain `co.wispralt.openai`, account `default`. Set it via a SecureField in Advanced, or `security add-generic-password -U -s co.wispralt.openai -a default -w` (docs warn the first read may prompt once per signed build). It is sent only as a Bearer header to `api.openai.com`.
- Logging is metadata-only in the client: lengths, durations, status and model, never transcript text. `Logger` stops marking messages public (Logger.swift:13-26; remove text at DictationAPI.swift:188-190 and +Dictation.swift:152). (B5)
- Local key export: removed. A machine joins via an invite link or token from `wispralt` instead. (KeychainHelper.swift:175-196)
- Local URLs:
  - `https://` anywhere.
  - `http://` allowed for loopback, `.local`, RFC1918 and 100.64.0.0/10 (Tailscale), with a "traffic not encrypted on this network" note.
  - Enforced in `Settings` validation and `ServerClient`.
  - Info.plist gets `NSAllowsLocalNetworking`, plus whatever ATS exception the Part 3 assumption test proves necessary. (B7)

### 1.6 Client history + retention (B10)
- `DictationHistory` (client, both modes) keeps the last N=50 dictations' text, timestamp and mode. It is stored in Application Support with file protection. Settings: "keep history for 7 days / 30 days / off" (default 7 days) and a "Clear history" action. "Copy last dictation" reads from it.
- Online dictations do not reach a Local host's portal or analytics. That is an explicit tradeoff: Online has no server.

### 1.7 Mercury (B9)
- Mercury runs on the Local server only. Client-side Mercury is cut, so there is no duplicate implementation.
- The Local response returns `formatting: applied|skipped:<reason>|failed:<reason>`. The client shows it in the caption, and the toggle says "Sends transcript text to OpenRouter".

## 2. Local host (server)

### 2.1 Database (B6, B12, B18, B10)
- One SQLite database file per host: `wispralt.db`, in WAL mode with `busy_timeout=5000`.
  - Access goes through a small repository layer: a short-lived connection per call and per thread. The job worker uses its own connection.
  - Every write raises a typed error. No silently swallowed "database is locked".
  - Tests cover a concurrent usage-write during a long job transaction.
- Tables:
  - `schema_migrations`
  - `users`
  - `api_keys` (sha256 `token_hash` UNIQUE, kind, role)
  - `usage_events`
  - `dictations`
  - `jobs`
  - `weekly_insights` (person/team uniqueness via a non-null `scope_key`)
  - `import_runs`
  - Every row carries `source` (`live` / `import:supabase` / `import:sqlite` / `import:file`) and `source_id` for provenance. Legacy meeting and fallback rows are kept with their original type, and analytics labels them "legacy".
- Retention: `retention_days` per table in config (dictations/jobs default 90, usage 365, insights forever, 0 = keep forever). A daily purge sweep logs counts. Shown in `wispralt status` and the portal. Migration and import never apply retention to imported rows on first run.
- Cutover order: Part 4 builds the SQLite stores and the `import_rows(table, rows, source)` interface (tested). The first real host setup imports identities and history and verifies counts before any key is issued. No live install exists today (the mini was wiped), so no lockout is possible. The order is still enforced: `wispralt setup` refuses to issue keys while a pending Supabase import is configured but not verified. (B6)
- Backup: `wispralt backup` takes an SQLite online backup (backup API), plus a tar of job output files, plus a manifest with counts and hashes. `restore` validates the manifest into a temp dir and swaps atomically. (B12)

### 2.2 Runtime
- Package: one Python package `wispralt`, renamed from `wispralt_server` once, at the start of Part 4, before other moves.
  - The base install is CLI, API, DB and portal.
  - A `host` extra adds MLX (`parakeet-mlx`, `mlx-whisper`). MLX imports are lazy.
  - A CI smoke test imports the base without MLX.
- Modules: `dictation/` (Parakeet) and `files/` (Whisper turbo; formerly `meeting/` minus the diarization, silence, merge and torch shims). Plain modules, no config-selected backend switch. `doctor` explains unsupported hardware.
- Routes: `/transcribe/file` (plus chunked) and `/transcribe/file/{id}` (poll/download/delete) with owner-or-admin checks. A legacy shared key with uid<0 is treated as admin.
  - `/transcribe/meeting/{id}` is kept as a thin alias for one release, then removed. The alias and the docs (API.md, INTEGRATION-GUIDE.md, OPENAI-COMPAT.md) point to the new paths.
  - `/v1` returns `model_not_found` for unknown models.
- Security fixes:
  - no key printing (admin.py:128);
  - CSRF on all cookie mutations;
  - chunk-index verification;
  - metadata-only logs.

## 3. CLI + install (B8)
- `wispralt` (argparse) commands:
  - `setup [--role host|client|both] [--non-interactive --openai-key-stdin ...]`
  - `status`, `doctor`, `start|stop|restart`
  - `users ...`, `keys ...`, `invite`
  - `import [--dry-run]`, `backup`, `restore`, `update`, `uninstall`
  - `tunnel` (optional Cloudflare)
- A state file at `~/Library/Application Support/WisprAlt/install-state.json` records everything `setup` created (plists, model dirs, venv, DB path). `uninstall` removes exactly that manifest. `setup` is resumable from it.
- launchd: user agents in `gui/$UID`, generated from templates. `restart` re-bootstraps on plist change. Old `co.wispralt.server` and cloudflared plists are detected and migrated.
- Client role: installs or opens the app and hands over its configuration through a `wispralt://configure?...` URL. The app handler writes Settings and the Keychain, so no Python touches the Keychain. Keys come from an invite token, never pasted into the terminal.
- `install.sh` is a one-line bootstrap (Homebrew, ffmpeg, uv, then `uv tool install` from this repo's tagged release, then `wispralt setup`). The `xattr -dr com.apple.quarantine` line is removed. Distribution is a signed and notarized DMG when credentials exist. Otherwise it is documented as a local build with first-launch approval. (B8)

## 4. Portal + insights
- FastAPI + Jinja + local HTMX/Alpine, redesigned (no SPA).
  - The first-admin bootstrap token is shown once in the terminal.
  - Invites are one-time links; keys are shown once.
- Monitoring:
  - host and uptime/restarts, versions/updates;
  - CPU, memory, disk;
  - models ready, job queue;
  - latency and error rates (from `usage_events`);
  - DB health, network;
  - clients: active (request in the last 2 min) vs last seen.
- Data tab: measured stats always local. AI insights are optional (explicit provider, cost cap, privacy note), each claim links to source ids, with rerun/backfill.

## 5. Import (B11) — Part 7
- Sources:
  - Supabase via a read-only export (users, api key hashes, usage, fallback_events as legacy);
  - the current SQLite (`dictations`, `jobs`, `weekly_insights`);
  - transcript files (TXT/JSON/SRT/VTT).
- Dedup key:
  - Supabase/SQLite: `(source, source_id)`;
  - files: sha256 of normalized text + file mtime.
- Dry run prints per-source counts (new, duplicate, malformed) and writes nothing.
- Runs are recorded in `import_runs` with a checkpoint for resume. A report states counts and any gaps where retention already deleted records. Nothing is fabricated.

## 6. Inventory tables (B16)
Part 2's plan must include a file-by-file KEEP / DELETE / RENAME table for the client folders `Server/`, `Storage/`, `Capture/`, `Hotkeys/`, `Update/` (SparkleController's `isMeetingActive` guard) and `UI/`, and for server `meeting/`, `routes/`, `jobs/`. Part 5's plan must include a RETIRE / KEEP / FOLD-INTO-CLI table for `scripts/*.sh`, `.claude/commands/*`, CLAUDE.md conventions and the docs/OVERVIEW map.

## 7. Sequencing
1. Part 2 (removals) renames the shared meeting code to `files` before deleting the recorder. File transcription tests stay green at every commit.
2. Part 3 (Online) runs the real-API pre-flight first: one `gpt-transcribe` call and one Realtime session. Then the provider seam.
3. Part 4 (Local server) does the package rename, the SQLite repositories, `import_rows` and the security fixes.
4. Part 5 (CLI + install).
5. Part 6 (portal + insights).
6. Part 7 (import, backup).
7. Part 8 (release + E2E + cleanup).

Real-API tests are labelled `@real-openai` and reported BLOCKED when there is no key. macOS CI is added in Part 8. Each spec §12 scenario maps to a named test or manual step in Part 8's report.
