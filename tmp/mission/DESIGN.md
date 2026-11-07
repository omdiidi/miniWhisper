# Target design v3 (Part 1, 2026-11-07, after review rounds 1-2)

Owner rule: clean, concise, future-proof, reusable; no abstraction for show.
Review reports:
- r1: /var/folders/mc/6xd4rvtd7d7gwmq5_lp37vjh0000gn/T//codex-review.tF2k3K/report-final.md
- r2: /var/folders/mc/6xd4rvtd7d7gwmq5_lp37vjh0000gn/T//codex-review.AM5XaP/report-final.md

Context: single user, the old host was wiped, so there is no live install to stay compatible with. Simplify accordingly.

## 1. Client

### 1.1 Mode
- `Settings.mode: online | local`. Default **online**.
- One-time migration on first launch of the new build: if a saved `serverURL` answers `GET /healthz` within 3 s, choose **local**. Otherwise choose **online** and show a notice ("previous server unreachable — using Online"). The decision is persisted and never re-derived. (The owner's saved URL `https://transcribe.integrateapi.ai` points at the wiped host, so this lands on Online.)
- Menu bar: an "Online (OpenAI)" / "Local server" picker and a status line (mode, key present or "key missing", host reachable).
- Online mode hides server-only UI: `MeAPI.get` at launch (AppDelegate.swift:110), DisplayNameSheet, `/me` links, the server health check.
- A mode change applies to the next dictation. Each dictation and retry entry records the mode and model it was captured with. A Local host that is unreachable is a hard failure, shown as a toast. There is no cross-provider fallback.

### 1.2 Audio pipeline (corrects v2: the recorder is NOT canonical)
- `DictationRecorder` keeps capturing at the device's native rate and channel count (DictationRecorder.swift:117-123, 417-432).
- New: the recorder **spools the full recording to disk** as it captures (`spool/<dictationId>.caf`, native PCM). It is the single owner of the full recording, and memory stays bounded (commit 84a7d8b).
- New `MonoResampler` (WisprAltCore, pure Swift): averages channels to mono, then resamples (16 kHz for file/Local, the Realtime rate for live). It never uses `AVAudioConverter` for downmix (it sums channels and caused the peak-3.97 bug). Unit-tested with a 48 kHz stereo fixture, asserting peak ≤ 1.0 and the correct duration.
- New `AACEncoder` (app target, AVAssetReader → AVAssetWriter): spool or any file → m4a, 16 kHz mono, 32 kbps (600 s ≈ 2.4 MB). It reads via `MonoResampler` output. It replaces AudioExtractor's passthrough for every OpenAI upload. AudioExtractor stays only for Local file uploads, where the server transcodes.

### 1.3 Dictation provider seam
- Lives in `WisprAltCore` (testable):
  ```
  protocol DictationProvider {               // NEW instance per dictation, created at start() from the captured mode
    func start()                              // non-blocking; may open a socket in the background
    func append(_ frames: MonoFrames)         // already resampled to the provider's declared rate
    func finish(spool: URL) async throws -> DictationResult
    func cancel()
  }
  struct DictationResult { text; formatting: applied|skipped(reason)|failed(reason)|off }
  ```
- `LocalServerProvider`:
  - Today's VAD chunk stream (`/transcribe/dictate/stream`, 16 kHz mono after resample, since the server already accepts it), with a whole-file `/transcribe/dictate` fallback.
  - Every request carries the dictation's `X-Client-Dedup-Id`. The id is generated at `start()` and persisted with any retry entry, so fallback, auto-retry and manual retry never double-count.
- `OpenAIFileProvider` ignores frames. `finish` runs AACEncoder(spool) and makes one POST to `/v1/audio/transcriptions`.
- `OpenAIRealtimeProvider`:
  - `start()` opens the WebSocket in the background. Frames arriving before it is open are queued in order.
  - A single serial sender sends base64 PCM16 `input_audio_buffer.append`, with turn detection off.
  - `finish` drains the queue, sends `commit`, and awaits the transcription `completed` event for at most 15 s, then closes the socket on success, error or cancel.
  - If the socket never opened (short press), or it errors or times out, the dictation goes through `OpenAIFileProvider` with `gpt-transcribe`. This is same-vendor recovery of the same audio, not the removed cross-provider ladder.
  - Event names, the rate and the per-commit audio limit are set by the Part 3 pre-flight against **both** live models, including one 2-minute clip. If the commit limit is below 600 s, the provider commits at VAD pauses and joins the results.
- Concurrency: the next FN press starts a new recorder session as soon as the previous spool is closed (bounded wait of 2 s). Transcriptions run concurrently, but **results are delivered in start order**. Each result is injected into the app that was frontmost when its own dictation started.
- Errors:
  - Transient (transport failure, 5xx, 429 with backoff) → one automatic retry.
  - Permanent (401/403/400/`model_not_found`) → no retry. `model_not_found` resets the model to `gpt-transcribe` and shows a toast.
  - Final failure → the m4a and its metadata (dedup id, mode, model, timestamp) go to `pending-retry/`, up to 20 entries. Never auto-deleted. Removed only after a successful retry or an explicit "Discard". The menu shows "Retry N pending".
- The old `DictationFallbackQueue` data on upgrade: queued items already have text, so they move into DictationHistory and the old directory is deleted. Its `failed/` name is not reused.

### 1.4 Model catalog
- `OpenAIModels.swift` (WisprAltCore) is a static table. Each entry has: id, kind (file|live), $/min, status + retire date, capability flags (prompt, keywords, languages, stream, response formats, max duration/commit), `verifiedOn`. Prices are re-checked against OpenAI's pricing page in Part 3.
- The picker shows each model with its price in small secondary text and a "retires Feb 26, 2027" tag. Default `gpt-transcribe`. The diarize model is omitted. Request builders send only the fields a model's flags allow.

### 1.5 Online file transcription
- AACEncoder produces the audio. It is split by duration (catalog max, conservative default 20 min) at the lowest-energy point within ±10 s of each boundary, and each chunk gets the previous chunk's last ~200 characters as `prompt`.
- A job folder holds `manifest.json` and `chunk-N.txt`. A relaunch resumes and skips finished chunks. Final TXT/JSON are written atomically, only once every chunk has succeeded. Cancel deletes the folder.
- **Stated limitation:** Online produces TXT and JSON only. `gpt-transcribe` returns no timestamps, and `whisper-1` retires 2027-02-26. SRT/VTT remain in Local mode. The UI says so.

### 1.6 Secrets, logging, transport
- The OpenAI key lives in Keychain `co.wispralt.openai` (account `default`), read once at launch or when set, then held in memory. A denied read shows "key missing". It is entered through a SecureField or `security add-generic-password -U -s co.wispralt.openai -a default -w` (docs warn about a one-time access prompt per signed build). It never goes into CLI args, URLs or logs.
- Client logging is metadata only (lengths, durations, status, model). `Logger` stops marking messages public (Logger.swift:13-26). Remove the text logging at DictationAPI.swift:188-190 and +Dictation.swift:152. Local key export is removed (KeychainHelper.swift:175-196).
- Local URLs: `https://` anywhere, and `http://` to loopback. Plain `http://` to any other host requires an explicit per-host toggle ("Allow unencrypted connection to <host> — your key and audio are visible on this network"). Docs recommend HTTPS via Tailscale (`tailscale serve`) or the optional tunnel. Enforced in Settings validation and ServerClient. `NSAllowsLocalNetworking` goes in Info.plist, and the exact ATS keys are confirmed by a Part 3 assumption test.

### 1.7 `wispralt://` join link
- `CFBundleURLTypes` and the handler are added in Part 3. The only supported form is `wispralt://join?host=<url>&invite=<id>`.
- The app shows a confirmation sheet naming the host. On approval it redeems the invite over the transport rules in §1.6 (`POST /invites/redeem`) and receives a device key, which it stores in Keychain `co.wispralt`.
- Invites are single-use with a 10-minute TTL, so the id is useless once redeemed or expired. Nothing else is configurable via URL.

### 1.8 Client history
- `DictationHistory` holds the last 20 dictations (text, time, mode) in Application Support with mode 0600. It is protected at rest by FileVault, not by an iOS-style data-protection class.
- Settings: keep for 7 days (default) / 30 days / off, plus "Clear history". "Copy last dictation" reads from it.
- In Local mode the server `dictations` table is the system of record for analytics. The client history is a convenience cache.

### 1.9 Mercury smart formatting (pending owner decision pd:1, assumption applied)
- **One implementation, in the client** (`WisprAltCore/Mercury`), used in both modes. The server copy is removed in Part 4.
- The server's 34 safety vectors are ported to `mercury_vectors.json` and run in XCTest.
- Policy:
  - opt-in toggle, with "Sends transcript text to OpenRouter" shown beside it;
  - key in Keychain `co.wispralt.mercury`;
  - skips dictations under 20 words;
  - 2.5 s budget;
  - keeps the 70-110 % word-ratio guard;
  - on any failure, returns the raw text.
- The result status (applied / skipped: reason / failed: reason) shows in the caption.
- The Part 3 benchmark measures latency and the applied rate over 20 real dictations and tunes the thresholds.
- Sparkle and the update checker: the `isMeetingActive` gates (SparkleController.swift:66, UpdateChecker.swift:127, +Dictation.swift:67) become "defer while a dictation is recording or transcribing".

## 2. Local host (server)

### 2.1 Database
- One SQLite file per host: `<data_dir>/wispralt.db`, WAL mode, `busy_timeout=5000`.
- Repository layer: a short-lived connection per call or per thread. Every write is `BEGIN IMMEDIATE`.
- JobStore state transitions stay single-transaction (the semantics of today's lock at store.py:122). Write errors are typed and never swallowed, with no drop-on-failure usage writer. Tests cover a concurrent usage write during a long job transaction.
- Tables:
  - `schema_migrations`
  - `users`
  - `api_keys` (user_id FK, sha256 `token_hash` UNIQUE, kind, role)
  - `invites`
  - `usage_events`
  - `dictations`
  - `jobs`
  - `weekly_insights` (non-null `scope_key`)
  - `import_runs`
- `source` and `source_id` exist only on `dictations`, `jobs`, `usage_events` and `weekly_insights`, the tables that receive imports.
- Paths are stored relative to `data_dir`.
- **Retention:** default 0 (keep forever) for dictations, jobs, usage and insights.
  - Purging is opt-in per table: `wispralt retention set dictations 90`, with a mandatory `--preview` count first.
  - Imported rows (`source != 'live'`) are never purged unless `--include-imported` is passed.
  - The current policy and how many rows each policy would purge are shown in `status` and the portal.
- **Backup:** `wispralt backup` sets a maintenance flag (the job worker and sweeps pause), then:
  - snapshots the database with `VACUUM INTO`;
  - tars the job outputs and staged inputs (`wav_path`);
  - writes a manifest (counts, hashes, relative paths);
  - clears the flag.
- **Restore:**
  1. stop services;
  2. take an automatic pre-restore backup;
  3. extract to a temp dir and run `PRAGMA integrity_check` and the manifest check;
  4. swap `data_dir` atomically and remove stale `-wal`/`-shm` files;
  5. restart, and roll back to the pre-restore backup on any failure.
- `update` backs up before any migration. A `setup` rerun never rotates working keys.

### 2.2 Runtime
- Package: `wispralt` (renamed from `wispralt_server` in one mechanical commit at the start of Part 2, together with `meeting/` → `files/`, so there is one sweep).
  - The base install has the CLI, API, DB and portal.
  - The `host` extra adds `parakeet-mlx` and `mlx-whisper` as lazy imports.
  - A CI smoke test imports the base without MLX.
- Modules: `dictation/` and `files/`, plain modules with no backend switch. `doctor` explains unsupported hardware.
- Routes: `/transcribe/file` (+chunked) stays. Poll, download and delete move from `routes/meeting.py` to `/transcribe/file/{id}`, with owner-or-admin checks.
  - No `/transcribe/meeting` alias: there is no live install, and the docs (API.md, INTEGRATION-GUIDE.md, OPENAI-COMPAT.md) are updated in the same part.
  - `/v1` returns `model_not_found` for unknown models.
  - The `uid<0` break-glass key is removed: it was a Postgres-outage escape hatch and is moot with SQLite.
- Security: no key printing (admin.py:128), CSRF on every cookie mutation, chunk-index verification, metadata-only logs.

## 3. CLI + install
- `wispralt` (argparse):
  - `setup [--role host|client|both] [--non-interactive ...]`
  - `status`, `doctor`, `start|stop|restart`
  - `users`, `keys`, `invite` (prints a `wispralt://join` link and a QR)
  - `import [--dry-run]`, `backup`, `restore`, `retention`, `update`
  - `uninstall [--purge-data]`
  - `tunnel` (optional)
- No secret ever passes in argv. The OpenAI key is entered in the app.
- `install-state.json` records what `setup` created, separating `artifacts` (plists, venv, model dirs) from `data` (`data_dir`). Plain `uninstall` removes only artifacts. `--purge-data` first runs `backup` (unless `--no-backup`), then deletes the data.
- launchd: user agents in `gui/$UID`, rendered from templates. `restart` re-bootstraps when a plist has changed. Old `co.wispralt.server` and cloudflared plists are detected and removed (they are artifacts of the wiped install). `doctor` warns that user agents run only while the user is logged in.
- `install.sh`: Homebrew, ffmpeg and uv, then `uv tool install <this clone>[host]`, then `wispralt setup`. No quarantine stripping. A signed and notarized DMG ships when credentials exist; otherwise the build is local with documented first-launch approval. Install from a tagged release is a later option.

## 4. Portal + insights
- FastAPI + Jinja + local HTMX/Alpine, redesigned.
- First-admin token shown once in the terminal. Invites are one-time with a 10-minute TTL. Keys are shown once.
- Monitoring:
  - host, uptime/restarts, versions/updates;
  - CPU, memory, disk;
  - models ready, job queue;
  - latency and error rates;
  - DB health, network;
  - clients: active (a request in the last 2 min) vs last seen.
- Data tab: measured stats are computed locally and always shown. AI insights are optional (explicit provider, cost cap, privacy note), with each claim linked to its source ids, plus rerun and backfill.

## 5. Import (Part 7)
- **Sources:**
  - Supabase: a read-only export of users, token hashes, usage, and `fallback_events` (imported as legacy).
  - The previous SQLite file (`dictations`, `jobs`, `weekly_insights`).
  - Transcript files (TXT/JSON/SRT/VTT).
- **Supabase access:** read-only SQL via the Supabase MCP `execute_sql`, SELECT-only, or a connection string the owner stores in Keychain `co.wispralt.supabase-import`. Every session sets `default_transaction_read_only=on`. If the project is paused, stop and ask the owner, because resuming it changes state.
- **Identity mapping:**
  - Each Supabase `users` row becomes a `users` row (same id) plus one `api_keys` row (its `token_hash`).
  - The SQLite `api_key_id` column (which holds `users.id`, store.py:330,394) maps to `user_id`.
  - Unmatched ids go to a `legacy-unknown` user and are listed in the report.
  - Imports never collide with keys issued before the import: matching is by `token_hash`.
- **Dedup across all sources:** sha256 of the normalized text plus the `created_at` minute when known (text-only for undated files). No mtime.
- **Dry run:** per-source counts (new, duplicate, malformed, unmatched owner), with no writes.
- **Resume:** each run is recorded in `import_runs` with a checkpoint. The report states counts, the earliest record, and gaps left by earlier retention. Nothing is fabricated.

## 6. Inventory tables (required in part plans)
- Part 2: a KEEP / DELETE / RENAME table covering:
  - client `Server/`, `Storage/`, `Capture/`, `Hotkeys/`, `Update/`, `UI/`;
  - server `meeting/`, `routes/`, `jobs/`;
  - the SQLite-touched server files `db.py`, `auth.py`, `users/`, `usage/`, `admin/`, `insights/`, `ops/`, `migrations/`, `routes/telemetry.py`, `routes/dev_faults.py`, CI and tests (the deletions belonging to Part 4 are marked as such).
- Part 5: a RETIRE / KEEP / FOLD-INTO-CLI table covering `scripts/*.sh`, `.claude/commands/*`, CLAUDE.md conventions, and the docs/OVERVIEW map.

## 7. Sequencing and tests
1. **Part 2:** a mechanical rename commit (package + `files/`), then the meeting/fallback removals. File transcription tests stay green at every commit.
2. **Part 3:** real-API pre-flight first (gpt-transcribe; both live models, plus one 2-min clip; ATS; Keychain prompt). Then the audio pipeline, the provider seam, catalog, picker, join link, Mercury and history.
3. **Part 4:** SQLite repositories, `import_rows`, invites, retention, backup/restore core, security fixes.
4. **Part 5:** CLI + install.
5. **Part 6:** portal + insights.
6. **Part 7:** import.
7. **Part 8:** release, E2E, cleanup.

**Spend cap:**
- `@real-openai` tests use clips of 10 s or less, at most 3 calls per model, plus one 2-min live clip.
- A running cost tally is printed. The suite aborts above $1 cumulative, with the mission total at or under ~$5.
- With no key, the tests report BLOCKED.

macOS CI arrives in Part 8. Every spec §12 scenario maps to a named test or manual step in the Part 8 report. Part 8 cleanup is `wispralt uninstall --purge-data --no-backup` on the test install, followed by a check that no LaunchAgents, models, venvs or DBs remain.
