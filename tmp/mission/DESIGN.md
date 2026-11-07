# Target design v7 (Part 1, 2026-11-07, after review rounds 1-6)

Owner rule: clean, concise, future-proof, reusable; no abstraction for show.
Review reports:
- r1: /var/folders/mc/6xd4rvtd7d7gwmq5_lp37vjh0000gn/T//codex-review.tF2k3K/report-final.md
- r2: /var/folders/mc/6xd4rvtd7d7gwmq5_lp37vjh0000gn/T//codex-review.AM5XaP/report-final.md
- r3: /var/folders/mc/6xd4rvtd7d7gwmq5_lp37vjh0000gn/T//codex-review.EwuKzI/report-final.md
- r4: /var/folders/mc/6xd4rvtd7d7gwmq5_lp37vjh0000gn/T//codex-review.lpv2Ae/report-final.md
- r5: /var/folders/mc/6xd4rvtd7d7gwmq5_lp37vjh0000gn/T//codex-review.OvgtJi/report-final.md

Context: single user, the old host was wiped, so there is no live install to stay compatible with. Simplify accordingly.

**What history survives:** the old host's SQLite (all dictation and file-job text) was on the wiped mini, and no copy exists. Supabase holds metadata only (users, usage_events, fallback_events). The client has no fallback-queue folder on this Mac. So dictation text cannot be recovered. The import (§5) brings back accounts, usage statistics and any transcript files the owner still has, and says so plainly.

## 1. Client

### 1.1 Mode
- `Settings.mode: online | local`. Default **online**, including on upgrade. There is no probe of the saved URL: the owner's saved URL points at the wiped host, and probing a reused hostname could route audio elsewhere. Local is chosen only explicitly or through a `wispralt://join` link.
- Menu bar: an "Online (OpenAI)" / "Local server" picker and a status line (mode, key present or "key missing", host reachable).
- Online mode hides server-only UI: `MeAPI.get` at launch (AppDelegate.swift:110), DisplayNameSheet, `/me` links, the server health check.
- A mode change applies to the next dictation. Each dictation and retry entry records the mode it was captured with. An unreachable Local host is a hard failure, shown as a toast. There is no cross-provider fallback.

### 1.2 Audio pipeline
- `DictationRecorder` keeps capturing at the device's native rate and channel count (DictationRecorder.swift:117-123, 417-432).
- New: the recorder **spools the full recording to disk** as it captures (`spool/<dictationId>.caf`, native PCM). It is the single owner of the full recording, and memory stays bounded.
  - At `start()`, before any audio, it atomically writes the sidecar `spool/<dictationId>.json`: mode, Local host URL, start time. `dictationId` (lowercase UUIDv4) **is** the dedup id.
  - The spool is deleted once the dictation succeeds or its retry entry has been written.
  - On launch, any orphan spool becomes a retry entry using its sidecar's mode and host, so audio is never silently dropped. An orphan without a sidecar is offered only for manual retry and is never sent automatically.
- New `MonoResampler` (WisprAltCore, pure Swift): averages channels to mono, then resamples (16 kHz for file/Local, the Realtime rate for live). It never uses `AVAudioConverter` for downmix, because that sums channels and caused the peak-3.97 bug.
  - Unit-tested with a 48 kHz stereo 1 kHz sine fixture.
  - The test asserts output RMS within ±1 dB of the input channel, a dominant frequency of 1 kHz ±1 %, the correct duration, and peak ≤ 1.0. Silence cannot pass.
- New `AACEncoder` (app target, AVAssetReader → AVAssetWriter):
  - Converts the spool or any file to m4a, 16 kHz mono, 32 kbps (600 s ≈ 2.4 MB), reading through `MonoResampler`.
  - Used for every OpenAI upload. AudioExtractor (passthrough) stays only for Local file uploads, where the server transcodes.
  - Part 3 runs an assumption test that AVAssetWriter accepts these settings and that `gpt-transcribe` accepts the resulting m4a.

### 1.3 Dictation provider seam
- Lives in `WisprAltCore` (testable):
  ```
  protocol DictationProvider {               // NEW instance per dictation, created at start() from the captured mode
    var inputSampleRate: Int? { get }         // rate append() expects; nil = provider ignores live frames
    func start()                              // non-blocking; may open a socket in the background
    func append(_ frames: MonoFrames)
    func finish(spool: URL) async throws -> DictationResult
    func cancel()
  }
  struct DictationResult { text; modelUsed; formatting: applied|skipped(reason)|failed(reason)|off }
  ```
- `LocalServerProvider`:
  - The VAD chunk stream to `/transcribe/dictate/stream` (16 kHz mono after resampling), with a whole-file `/transcribe/dictate` fallback.
  - Every request carries the dictation's dedup id (the `dictationId`). It is sent on both the stream and whole-file routes.
  - For a dedup id it has already seen, the server returns the stored text and writes no new `dictations` **or** `usage_events` row. This is built in Part 4 with a test.
  - The server stores the raw transcript, which is canonical for analytics. The client history stores the text actually delivered (formatted, if Mercury applied).
- `OpenAIFileProvider` ignores frames. `finish` runs AACEncoder on the spool and makes one POST to `/v1/audio/transcriptions`.
- `OpenAIRealtimeProvider`:
  - `start()` opens the WebSocket in the background. Frames that arrive first are queued in order.
  - A single serial sender sends base64 PCM16 `input_audio_buffer.append`, with turn detection off.
  - `finish` drains the queue, sends `commit`, and waits at most 15 s for the transcription `completed` event. It then closes the socket on success, error or cancel.
  - Recovery: if the socket never opened (short press), or it errors or times out, the same audio goes through `OpenAIFileProvider` with `gpt-transcribe`. This is same-vendor recovery, not the removed cross-provider ladder. The result records `modelUsed`.
  - Build it in two steps:
    - First, commit-on-release only.
    - Then, only if the Part 3 pre-flight shows a per-commit limit under 600 s, commit at VAD pauses. Each completion is mapped to its committed `item_id`, and the results are joined in commit order.
  - The pre-flight covers both live models and includes one 2-minute clip.
- Concurrency and delivery:
  - The next FN press may start while a previous dictation is still transcribing. Today's behaviour is kept, because `dictationStop` sets the mode to idle at once (+Dictation.swift:99). A new recorder session starts once the previous spool is closed (bounded wait of 2 s).
  - Results are delivered **strictly in start order**, bounded only by each provider's own timeout. There is no head deadline. A head that fails becomes a retry entry and never injects late.
  - Injection goes ahead only if the target captured at start still matches: the focused element when both captures return one, otherwise the app's process id (element identity is unreliable in Electron and web apps). If not, and for every manual retry, the text goes to history and the clipboard with a toast. A Part 3 assumption test confirms the check.
- Errors:
  - Transient (transport failure, 5xx, 429 rate limit with backoff): one automatic retry. A 429 with `insufficient_quota` is permanent.
  - Permanent (401/403/400/`model_not_found`): no retry. `model_not_found` resets the picker to the default of that kind (`gpt-transcribe` or `gpt-live-transcribe`) and shows a toast.
  - Final failure: a retry entry `pending-retry/<id>/` is written. It holds:
    - the audio, in the captured mode's format: m4a for Online, 16 kHz mono WAV for Local (which `/transcribe/dictate`'s soundfile decoder reads);
    - metadata: dedup id, mode, Local host URL, model actually used, timestamp.
  - Retry entries have **no cap** (≈2.4 MB per 10-minute Online m4a entry; ≈19 MB per 10-minute Local WAV entry). They are removed only after a successful retry or an explicit "Discard". The menu shows "Retry N pending".
  - A retry uses its recorded mode and **always a file model**: the picked model if it is a file model, otherwise `gpt-transcribe`. A Local retry goes **only to the current host** (the device key is per current host). If the recorded host differs, the Retry item says so and offers "Send to current host" or "Discard".
- The old `DictationFallbackQueue` folder is left untouched. It is absent on the owner's Mac, and the app never deletes it. The new code stops reading it.
- `isMeetingActive` gates, handled in **Part 2** together with the meeting removal so every commit compiles:
  - The dictation-start gate at +Dictation.swift:67 is deleted.
  - SparkleController.swift:66 and UpdateChecker.swift:127 become "defer while a dictation is recording or transcribing".

### 1.4 Model catalog
- `OpenAIModels.swift` (WisprAltCore) is a static table.
  - Each entry has: id, kind (file|live), $/min, status and retire date, capability flags (prompt, keywords, languages, stream, response formats, max duration/commit), and `verifiedOn`.
  - Prices are re-checked against OpenAI's pricing page in Part 3.
- The picker shows each model with its price in small secondary text and a "retires Feb 26, 2027" tag. Default `gpt-transcribe`. The diarize model is omitted.
- Request builders send only the fields a model's flags allow.
- File transcription uses the picked model if it is a file model, otherwise `gpt-transcribe`.

### 1.5 File transcription jobs
- Online:
  - AACEncoder produces the audio. It is split by duration (catalog maximum, conservative default 20 min) at the lowest-energy point within ±10 s of each boundary.
  - Each chunk gets the previous chunk's last ~200 characters as `prompt`.
- Both modes share one job folder holding `manifest.json`: mode, source file path, server job id (Local), output dir and stem, chunk states. For Online it also keeps the encoded `audio.m4a` (≈14 MB per hour) and `chunk-N.txt`, so unfinished chunks can resume without the source.
- The client transcript folder (`Settings.meetingsPath`) and its files are never deleted. It may hold the owner's only surviving text history. Part 2 keeps or migrates that UserDefaults key.
  - A relaunch resumes: Online skips finished chunks; Local resumes polling and downloads the results. This fixes audit #11 (+Transcription.swift:211).
  - Final outputs are written atomically (tmp + rename) only after everything has succeeded. Cancel deletes the folder.
- **Stated limitation:** Online produces TXT and JSON only, because `gpt-transcribe` returns no timestamps and `whisper-1` retires 2027-02-26. SRT/VTT stay in Local mode. The UI says so.

### 1.6 Secrets, logging, transport
- The OpenAI key lives in Keychain `co.wispralt.openai` (account `default`).
  - It is read once, at launch or when set, then cached in memory. A denied read shows "key missing".
  - Enter it through a SecureField, or with `security add-generic-password -U -s co.wispralt.openai -a default -w`. The docs warn about a one-time access prompt per signed build.
  - It never appears in CLI args, URLs or logs.
- Client logs are metadata only. `Logger` stops marking messages public (Logger.swift:13-26). The text logging at DictationAPI.swift:188-190 and +Dictation.swift:152 is removed. Local key export is removed (KeychainHelper.swift:175-196).
- Local URLs:
  - `https://` works anywhere; `http://` works to loopback.
  - Plain `http://` to any other host needs an explicit per-host toggle that warns the key and audio are visible on the network. The Part 3 ATS test decides which host forms it can cover (IP / `.local` / single-label). ATS cannot be changed at runtime.
  - The existing Settings URL + SecureField key entry stays, for joining with an existing token.
  - The docs recommend `tailscale serve` HTTPS or the optional tunnel.
  - Enforced in Settings validation and in ServerClient.
  - Info.plist gets `NSAllowsLocalNetworking`. The exact ATS keys are confirmed by a Part 3 assumption test.

### 1.7 `wispralt://` join link
- `CFBundleURLTypes` and the handler are added in Part 3. Part 3 tests the handler against a stub redeem endpoint; the real end-to-end test comes in Part 4/8.
- The only form is `wispralt://join?host=<url>&invite=<id>`.
  - A confirmation sheet names the host.
  - On approval, the app redeems the invite (`POST /invites/redeem`) under the §1.6 transport rules and gets a device key, stored in Keychain `co.wispralt`.
- Invites are single-use and expire after 10 minutes. Nothing else can be configured through a URL.

### 1.8 Client history
- `DictationHistory` keeps the last 20 dictations (text, time, mode) in Application Support, file mode 0600, protected at rest by FileVault.
- Settings: keep for 7 days (default) / 30 days / off, plus "Clear history". "Copy last dictation" reads from it.
- In Local mode the server `dictations` table is the system of record for analytics.
- **Stated limitation:** Online dictations never reach a host, so Data & Insights covers Local usage only.

### 1.9 Mercury smart formatting (pending owner decision pd:1, assumption applied)
- One implementation, in the client (`WisprAltCore/Mercury`), used in both modes. Server Mercury is ignored from Part 3 onward and removed in Part 4. `X-Smart-Format` is documented as removed for Local HTTP callers. The host needs no OpenRouter key.
- The server's 34 safety vectors are ported to `mercury_vectors.json` (XCTest).
- Policy:
  - opt-in toggle, with "Sends transcript text to OpenRouter" shown beside it;
  - key in Keychain under the existing `co.wispralt.openrouter` service (no migration);
  - skips dictations under 20 words;
  - 2.5 s budget;
  - 70-110 % word-ratio guard;
  - any failure returns the raw text;
  - the result status shows in the caption.
- The Part 3 benchmark (20 real dictations) tunes the thresholds.

## 2. Local host (server)

### 2.1 Database
- One SQLite file per host: `<data_dir>/wispralt.db`, WAL mode, `busy_timeout=5000`.
  - Short-lived connection per call or per thread. Writes use `BEGIN IMMEDIATE`.
  - JobStore transitions run in a single transaction.
  - Write errors are typed and never swallowed; there is no drop-on-failure usage writer.
  - No transaction spans model inference. Tests cover a concurrent usage write while a job is running.
- Tables:
  - `schema_migrations`
  - `settings` (config metadata)
  - `users` (+`source`, `source_id`, `disabled_at`)
  - `api_keys` (user_id FK, sha256 `token_hash` UNIQUE, kind, role, `revoked_at`)
  - `invites`
  - `usage_events`
  - `dictations`
  - `jobs`
  - `weekly_insights` (non-null `scope_key`)
  - `import_runs`
- `source` and `source_id` columns exist only on tables that receive imports. Paths are stored relative to `data_dir`.
- **Retention:** one global setting, default 0 (keep forever). This is a stated tradeoff: the old default was 90 days, and the owner asked never to destroy history silently.
  - Set it with `wispralt retention set <days>`, which first shows a mandatory preview count. Imported rows are never purged.
  - `status` and the portal show the setting and what it would purge.
- **Backups:**
  - Backups live outside `data_dir` (default `~/WisprAlt-Backups/`). `restore` and `uninstall --purge-data` refuse a location inside `data_dir`.
  - A backup is a plain directory, so restore extracts nothing; restore still resolves every path, refuses symlinks, and verifies each stays inside the backup root (a backup may come from another machine).
  - Backups are **offline**: stop services → `VACUUM INTO` snapshot plus a copy of job outputs and staged inputs → manifest (counts, hashes, relative paths) → start services. Nothing can change mid-backup, and no maintenance flag is needed.
  - `restore`: stop services → move the current `data_dir` aside unchanged as `data_dir.pre-restore-<ts>` (with `-wal`/`-shm`; never `VACUUM INTO`, so a corrupt live DB never blocks restore) → validate the copy (`PRAGMA integrity_check` and the manifest) → swap `data_dir` atomically and remove stale `-wal`/`-shm` files → restart. Any failure swaps the moved-aside `data_dir` back.
  - `update` backs up before migrating. A `setup` rerun never rotates working keys.

### 2.2 Runtime
- Package `wispralt`: the rename from `wispralt_server`, together with `meeting/` → `files/`, is one mechanical commit at the start of Part 2.
  - Until Part 4, the renamed server still uses its Postgres-backed `db.py`/`users/`/`usage/`, and tests stay green with the existing mocks.
  - The base install holds the CLI, API, DB and portal. The `host` extra adds `parakeet-mlx` and `mlx-whisper` (lazy imports).
  - An import smoke test without MLX runs as a local pytest from Part 4 and in CI from Part 8.
- Modules: `dictation/` (Parakeet) and `files/` (mono **MLX Whisper large-v3-turbo**).
  - Whisper stays as the file model: it is the only model here with segment timestamps for SRT/VTT, and it is proven on long files.
  - Inference seam: `dictation/` and `files/` each keep all MLX code in a single backend module behind one `transcribe(...)` function. Adding a new platform means one new module plus a `doctor` check. There is no registry and no switch today. `doctor` explains unsupported hardware.
  - `setup` pins `HF_HOME` to a WisprAlt-owned model directory recorded in `install-state.json`.
- Removed with meetings:
  - pyannote, torch, torchaudio, matplotlib;
  - the `HF_TOKEN` setting (config.py:32) and its setup prompt;
  - the `mode=meeting` parameter and the `request_mode` meeting default (store.py:313, pipeline.py:390);
  - diarize/silence/merge/deepfilter, and the torch shims.
- Routes:
  - `/transcribe/file` (plus chunked) stays.
  - Poll, download and delete move from `routes/meeting.py` to `/transcribe/file/{id}`, with owner-or-admin checks.
  - There is no `/transcribe/meeting` alias. API.md, INTEGRATION-GUIDE.md and OPENAI-COMPAT.md are updated in the same part.
  - `/v1` returns `model_not_found` for unknown models.
  - The `uid<0` break-glass key is removed.
- Security: no key printing (admin.py:128), CSRF on every cookie mutation, chunk-index verification, metadata-only logs.

## 3. CLI + install
- `wispralt` (argparse) commands:
  - `setup [--role host|client|both] [--non-interactive ...]`
  - `status`, `doctor`, `start|stop|restart`
  - `users`, `keys`, `invite` (prints a `wispralt://join` link)
  - `import [--dry-run]`, `backup`, `restore`, `retention`, `update`
  - `uninstall [--purge-data]`
  - `tunnel` (optional)
- No secret ever passes in argv. The OpenAI key is entered in the app.
- `install-state.json` records what `setup` created, split into `artifacts` (plists, venv, model dirs) and `data` (`data_dir`).
  - Plain `uninstall` removes only the artifacts.
  - `--purge-data` first runs `backup` (unless `--no-backup`) and then deletes the data.
- launchd: user agents in `gui/$UID`, rendered from templates.
  - `restart` re-bootstraps when a plist has changed.
  - Old `co.wispralt.server` and cloudflared plists are detected and removed.
  - `doctor` warns that user agents run only while you are logged in.
- `install.sh`:
  - Steps: Homebrew, ffmpeg and uv → `uv tool install <clone>/server` (base only) → `wispralt setup`.
  - `setup` adds the `[host]` extra only for the host or both role, after `doctor` confirms Apple Silicon.
  - It never strips the quarantine attribute.
  - `setup --role client|both` installs the app:
    - it uses the signed DMG when one is available;
    - otherwise it builds from the clone (`scripts/build-client-local.sh`);
    - it installs the app to `/Applications`, opens it, and hands over a join link via `open` when a host is known;
    - it verifies with `wispralt status --client`.
  - Building locally needs full Xcode and an Apple Development signing identity. `doctor` checks for both and reports what is missing (owner prerequisite). There are no published releases yet, by mission rule.
  - Signed and notarized DMG when credentials exist; otherwise a local build with documented first-launch approval.

## 4. Portal + insights
- FastAPI + Jinja + local HTMX/Alpine, redesigned.
  - The first-admin token is shown once, in the terminal.
  - Invites are one-time with a 10-minute TTL. Keys are shown once.
- Access: every user has a personal view of their own data. The team view is admin-only. Both are enforced by `user_id`/role in the repository queries, not only in templates, and tested, including source-id links.
- Monitoring:
  - host, uptime/restarts, versions/updates;
  - CPU, memory, disk;
  - models ready, job queue;
  - latency and error rates;
  - DB health, network;
  - clients: active (a request in the last 2 min) vs last seen.
- Data tab:
  - Measured stats are computed locally and always shown.
  - AI insights are optional: explicit provider, cost cap, privacy note.
  - Each claim links to its source ids. Rerun and backfill are supported.

## 5. Import (Part 7)
- **Sources:**
  - Supabase, via a read-only export: users (identity, display name, role, `revoked_at`), `usage_events`, and `fallback_events` (imported as legacy usage).
  - Transcript files the owner points at: the client's transcript folder (`Settings.meetingsPath`, TranscriptStore.swift:22) and any TXT/JSON/SRT/VTT.
  - An old SQLite file only if one is found. None is known to exist.
- **Supabase access:** the agent runs read-only `SELECT`s, each wrapped in `BEGIN READ ONLY … COMMIT`, through the Supabase MCP.
  - It exports to a snapshot folder: one JSON file per table plus `counts.json`, holding the source `count(*)` for each table. That folder doubles as the pre-migration backup.
  - `wispralt import --from-snapshot <dir>` reads only that folder. For each table it fails closed unless the source count equals new + duplicate + malformed + unmatched.
  - If the project is paused, Part 7 marks the import BLOCKED and continues. It never unpauses the project.
- **Identity:**
  - Imported users get **new** local ids, with the old id kept in `users.source_id`. Every imported foreign key goes through an old-to-new map.
  - **No credentials are imported**. Users revoked in Supabase are imported as `disabled_at`, and devices rejoin by invite.
- **Dedup:**
  - Re-imports are idempotent on a unique `(source, source_id)`.
  - Transcript files are grouped by stem (`{stem}.json/.txt/.srt/.vtt`) into ONE record; its `source_id` is the `job_id` in the JSON when present, else the sha256 of the primary file (JSON, else TXT), scoped to the owner. Sibling formats attach to that record and never create rows.
  - There is no cross-source text dedup, because Supabase holds no text.
- **Dry run:** per-source counts (new, duplicate, malformed, unmatched owner). Nothing is written.
- **Resume:** `import_runs` checkpoints. The report states counts, the earliest record, and that no dictation text survives the wipe. Nothing is fabricated.

## 5a. Requirements assigned to part plans (from review rounds 1-5; each part plan must satisfy and test them)
- **Part 3 (client):**
  - **Injection.** When only the pid matches, inject only if the focused window title is unchanged; otherwise use the clipboard. Capture the target at FN press (deliberate change from release-time; stated in the plan).
  - **Realtime.** One overall deadline covers connect + drain + commit + completion, scaled to audio length, then falls back to the file path. Recovery runs only on transient or socket errors.
  - **Pre-flight.** One max-length (10 min) clip per model kind, under $0.25 total. The catalog gains `maxOutputTokens`; chunk length and maximum dictation length are derived from it. Flag truncation when the word count is implausibly low for the audio duration.
  - **File jobs.**
    - Cancel removes the job folder only after the server confirms `DELETE /transcribe/file/{id}`.
    - Delete the folder after the final atomic write.
    - In Online mode, SRT/VTT controls are disabled with an explanation.
  - **Server interaction.**
    - From Part 3 on, the client stops sending `smart_format`.
    - The menu shows "transcribing N" while results queue.
    - Local-provider tests run against a stub server.
- **Part 4 (server):**
  - **Dedup.** The lookup is scoped to the caller's `user_id` and runs before any stream-session lookup. Ids are lowercased. A cross-user collision returns 409. Exit test: a real client↔server dedup round-trip, including a finalize replay after the session expired.
  - **Job recovery.** On restart, requeue running jobs whose staged input still exists.
  - **Restore.** Path containment check (as §2.1).
  - **Schema.** Imported transcript files become `jobs` rows with `source`, timestamped from the JSON metadata or else the file mtime, flagged `estimated`.
- **Part 5 (CLI):**
  - **Setup.** `setup` downloads and checksums the models, `doctor` runs a tiny warm-up transcription, then `setup` opens the portal. It shows the retention choice once.
  - **Update.** `update` stops the services, reinstalls the tool venv, and re-bootstraps launchd.
- **Part 6 (portal):** implements spec §9 in full, including searchable history (SQLite FTS5, scoped by `user_id`) with tests.
- **Part 7 (import):**
  - **Source labels.** Each Supabase table gets its own `source` label (`supabase.usage_events`, `supabase.fallback_events`, `supabase.users`). Assert duplicate = 0 on the first run.
  - **Columns.** Export every non-credential column, including `kind`, `label`, `created_at`, `last_seen_at` and `notes`. Map `kind` to the local user or key kind.
  - **Integrity.** A per-table checksum computed in Postgres (`md5(string_agg(row_to_json(t)::text, '' ORDER BY id))`) is stored in `counts.json` and verified on import. Export is paged by key.
- **Part 8:** isolated test root and end-state statement (§7).

## 6. Inventory tables (required in part plans)
- **Part 2:** KEEP / DELETE / RENAME table covering:
  - client: `App/`, `Audio/`, `Capture/`, `Hotkeys/`, `Inject/`, `Server/`, `Storage/`, `UI/`, `Update/`, `Util/`;
  - server: `meeting/`, `routes/`, `jobs/`;
  - the SQLite-touched server files: `db.py`, `auth.py`, `users/`, `usage/`, `admin/`, `insights/`, `ops/`, `migrations/`, `routes/telemetry.py`, `routes/dev_faults.py`, CI and tests. Part 4 deletions are marked as such.
- **Part 5:** RETIRE / KEEP / FOLD-INTO-CLI table covering `scripts/*.sh`, `.claude/commands/*`, the CLAUDE.md conventions and the docs/OVERVIEW map.

## 7. Sequencing and tests
1. **Part 2:** a mechanical rename commit, then the meeting/fallback removals and the `isMeetingActive` replacement. File transcription tests stay green at every commit.
2. **Part 3:** real-API pre-flight first: gpt-transcribe; both live models plus a 2-minute clip; the AAC m4a check; ATS; the Keychain prompt; the injection target check. Then the audio pipeline, provider seam, catalog, picker, join-link handler, Mercury and history.
3. **Part 4:** SQLite repositories, the `import_rows(table, rows, source)` writer, invites, retention, backup/restore core, the server dedup-returns-stored-text rule, removal of server Mercury, and security fixes.
4. **Part 5:** CLI and install.
5. **Part 6:** portal and insights.
6. **Part 7:** import.
7. **Part 8:** release, E2E and cleanup.

**Spend cap:** `@real-openai` tests use clips of 10 s or less, at most 3 calls per model, plus one 2-minute live clip.
- A running cost tally is printed. The suite aborts above $1 cumulative; the mission total stays at or under about $5.
- With no key, the tests report BLOCKED.

Every part updates the docs its changed files map to in docs/OVERVIEW.md. A basic CI workflow (`swift build`/`swift test` + pytest) is added in Part 2. Full macOS CI arrives in Part 8.

Each spec §12 scenario maps to a named test or manual step in the Part 8 report.

Part 8 end-to-end runs use an **isolated test root**: a test `data_dir`, a test backup dir, and a test client Application Support root, set via env override. Part 8 cleanup runs `wispralt uninstall --purge-data --no-backup` against that test install only. It removes only the test root plus the artifacts listed in its `install-state.json`, and never touches the owner's real backups, retry entries, `co.wispralt` or `co.wispralt.openai`. It then checks that none of the following test-created artifacts remain:
- the pinned `HF_HOME` model directory;
- the uv tool dir and `~/.local/bin/wispralt`;
- `~/Library/LaunchAgents/co.wispralt.*` created by the test;
- the test logs;
- build DerivedData;
- test-only Keychain items.

The Part 8 plan also states the end state on the owner's Mac: which app build is installed, in which mode, with which Keychain items untouched.
