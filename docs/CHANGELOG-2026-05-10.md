# Changelog — 2026-05-10 — MLX-Whisper swap, mode discriminator, observability

> Single-document handoff. An agent picking up the codebase cold should be able to read this + `docs/ARCHITECTURE.md` and operate the system safely. The plan that drove this change is at `tmp/done-plans/2026-05-10-mlx-swap-mode-discriminator-observability.md`.

## What changed in one paragraph

WhisperX (CPU CTranslate2 + CrisperWhisper) was swapped for mlx-whisper (Apple Silicon GPU, `mlx-community/whisper-large-v3-turbo`). The pipeline gained an explicit `request_mode` form field on `/transcribe/file` (`file` or `meeting`) that replaces the old "mono = no diarization, stereo = diarize" channel-count heuristic — file mode skips diarization for speed, meeting mode runs pyannote regardless of channel count (fixes yesterday's "all Speaker 1" bug on mono in-person recordings). The job runner grew per-phase structured logs, an advisory watchdog, SQLite checkpoints (phase + chunk index + cancel_requested), and two new admin endpoints (`/admin/active` and `/admin/server-log/<id>`). The Swift client gained a `ProgressInfo` poll-response field, a per-phase progress UI with chunk counts, a Cancel button, a "View server log" sheet, a "Previous job finishing" banner, and `activeJobID` UserDefaults persistence so a relaunched app resumes polling.

## Measured outcome on prod Mac mini M4

| Metric | Before (WhisperX-CPU) | After (mlx-whisper-turbo) |
|---|---|---|
| 105-min mode=file end-to-end | ~70 min wall | **6.4 min wall (16.5× realtime)** |
| 105-min standalone transcribe spike | n/a | 12.3 min (8.57× realtime) |
| 6-min synthetic mode=file end-to-end | n/a (would be ~4 min) | 55 s (5.99× realtime) |
| Peak RSS during 105-min file mode | n/a | 2550 MB |

The matrix runner (`scripts/run-matrix-local.sh`) records the full per-row table; see `tmp/matrix-results.md`.

## The dependency-pin gotcha

`pyannote-3.1` still calls `hf_hub_download(use_auth_token=...)` internally. That kwarg was removed in `huggingface_hub` 1.13.0. The mini's existing install was on 1.12.0, which works. Installing `mlx-whisper==0.4.2` pulls a newer `huggingface_hub` by default which breaks pyannote.

**Constraint pinned in `server/pyproject.toml`:**
```toml
"huggingface_hub>=1.12.0,<1.13",
```

If pyannote ever bumps to use `token=` instead of `use_auth_token=`, the upper bound can be relaxed. Don't touch this without testing pyannote afterwards.

## Server-side file map (new and modified)

```
server/src/wispralt_server/
├── meeting/
│   ├── mlx_whisper_loader.py        ← NEW (replaces whisperx_loader.py — deleted in Phase 10)
│   │                                   one-shot transcribe with tqdm.auto monkey-patch
│   │                                   for progress_cb(phase, idx, total). Wall-clock
│   │                                   fallback emitter starts after 60 s of tqdm silence.
│   │                                   word_timestamps gated on meeting mode only (memory
│   │                                   leak on long files when True).
│   ├── pipeline.py                  ← MODIFIED (call sites swap _wx_mod → _mlx_mod;
│   │                                   request_mode threaded; new progress_cb / cancel_cb /
│   │                                   phase_setter_cb kwargs; mono-warning gated on
│   │                                   request_mode=meeting; assign_speakers_segments
│   │                                   replaces whisperx.assign_word_speakers.)
│   ├── merge.py                     ← MODIFIED (assign_speakers_segments helper; word-aware
│   │                                   split when words present, largest-overlap when not.)
│   ├── output.py                    ← UNCHANGED (already tolerated absent/empty words[].)
│   ├── diarize.py                   ← UNCHANGED structurally.
│   └── whisperx_loader.py           ← KEPT through Phase 8 (rollback insurance).
│                                       Deleted in Phase 10 by `chore: remove whisperx after green matrix`.
├── jobs/
│   ├── runner.py                    ← MODIFIED (ProcessingMode enum: FILE/MEETING;
│   │                                   PHASE_BUDGETS dict with scalar + callable mix;
│   │                                   PHASE_LABELS dict for UI; submit_or_429 +
│   │                                   submit_source_or_429 + _run_pipeline_inner +
│   │                                   reenqueue_pending all thread request_mode;
│   │                                   _phase() helper wraps ffprobe + ffmpeg_decode in
│   │                                   asyncio.wait_for; _phase_watchdog runs as separate
│   │                                   task — marks failed but CANNOT release semaphore;
│   │                                   _on_progress writes phase + chunk to store.)
│   └── store.py                     ← MODIFIED (7 new columns: request_mode, phase,
│                                       phase_started_at, chunk_index, total_chunks,
│                                       cancel_requested, audio_duration_s. Idempotent
│                                       ALTERs. _row_to_job switched to dict-based to
│                                       eliminate positional-coupling footgun. Helpers
│                                       update_phase / update_chunk / set_cancel_requested
│                                       / check_cancel_requested / update_audio_duration /
│                                       list_pending_ids_with_mode.)
├── ops/
│   └── staging.py                   ← MODIFIED (StagingCancelled exception;
│                                       ffprobe_duration helper; transcode_to_canonical_wav
│                                       switched from subprocess.run to Popen with 500 ms
│                                       cancel-flag poll; SIGTERMs ffmpeg on cancel; .partial
│                                       cleanup in finally.)
└── routes/
    ├── transcribe_file.py           ← MODIFIED (mode: ProcessingMode = Form(ProcessingMode.FILE)
    │                                   FastAPI Enum form param — auto-validates; disk gate
    │                                   507 + Retry-After 300 when free < 2× content_length;
    │                                   RAM gate 503 + Retry-After 60 when free < 4 GiB.)
    ├── meeting.py                   ← MODIFIED (legacy POST persists request_mode=MEETING
    │                                   internally; poll_meeting response includes
    │                                   progress block when status=running.)
    └── admin.py                     ← MODIFIED (GET /admin/active returns rich per-job
                                        projection: phase, phase_label, phase_elapsed_s,
                                        chunk_index, total_chunks, wav_path, current_rss_mb,
                                        cancel_requested. GET /admin/server-log/{job_id}
                                        returns 100-line bracket of server.log around
                                        the job's first + last appearance.)

server/scripts/
├── benchmark-mlx-whisper.py         ← NEW (Phase 0 spike script — ffmpeg / transcribe /
│                                      pyannote timed separately; psutil RSS sampler;
│                                      handles 24-bit + 32-bit + 16-bit WAVs.)
└── prefetch-mlx-whisper.sh          ← NEW (huggingface_hub.snapshot_download via python -c.
                                       Pin WHISPER_REVISION env var for prod. Validates
                                       model.safetensors > 800 MB to catch partials.)

scripts/
└── deploy-server.sh                 ← NEW (versioned deploy script.
                                       Backup → rsync src + pyproject → pip install -e .
                                       in the existing venv → launchctl kickstart →
                                       healthz poll with `|| echo "000"` to survive set -e
                                       during uvicorn rebind window. Exits non-zero on real
                                       failure only (rsync/pip/timeout).)

docs/
├── ARCHITECTURE.md                  ← MODIFIED (3-mode table, MLX section, observability,
│                                       honest limitations.)
├── DEPLOYMENT-NOTES.md              ← MODIFIED (hf_hub pin rationale, deploy script
│                                       contract, prefetch recovery.)
├── OVERVIEW.md                      ← MODIFIED (file→doc map updated.)
├── TESTING.md                       ← NEW (12-run matrix, parameterized timings, cancel
│                                       test scenario, SRT/VTT regression pin.)
└── CHANGELOG-2026-05-10.md          ← NEW (this file.)
```

## Client-side file map

```
client/WisprAlt/
├── App/MenuBarController.swift      ← MODIFIED (RecordingState extended INLINE at line 21
│                                       — NOT a separate State/ file. New @Published
│                                       fields: phase, phaseLabel, chunkIndex, totalChunks,
│                                       phaseElapsedS, audioDurationS, activeJobID,
│                                       serverFinishingJobID. activeJobIDDefaultsKey for
│                                       UserDefaults persistence. activeJobTask handle.
│                                       cancelActiveTranscription(), resumeInFlightJobIfNeeded(),
│                                       resumePollingForJob() methods. Poll loop has
│                                       pollWithBackoff with 1/2/4s exponential retry on
│                                       transient URLError. processMeetingUpload passes
│                                       mode:"meeting", processCustomTranscriptionUpload
│                                       passes mode:"file".)
├── Server/MeetingAPI.swift          ← MODIFIED (ProgressInfo struct: phase, phaseLabel,
│                                       phaseStartedAt, chunkIndex, totalChunks,
│                                       phaseElapsedS, audioDurationS. PollResponse
│                                       extended with progress: ProgressInfo? and
│                                       serverFinishing: Bool?. JobStatus.running shape
│                                       changed to .running(ProgressInfo?, Bool). submitFile
│                                       gained mode: String parameter. New cancel(_:) and
│                                       fetchServerLog(_:) methods.)
├── Storage/PendingUploadsQueue.swift ← MODIFIED (.m4a replay passes mode: "meeting"
│                                       explicitly.)
├── UI/SettingsView.swift            ← MODIFIED (QuickActionsSection embeds the in-flight
│                                       status row + Cancel button + "View server log"
│                                       button when a job is active. "Previous job
│                                       finishing" banner when serverFinishingJobID is set —
│                                       blocks the "Transcribe file…" action with a
│                                       tooltip.)
├── UI/RecordingIndicatorView.swift  ← MODIFIED (@EnvironmentObject var recordingState:
│                                       RecordingState; processing row shows phaseLabelDisplay
│                                       with "— chunk N/M" suffix when phase=="transcribe"
│                                       and total > 0. Friendly phase label fallback map
│                                       in RecordingState.phaseLabelDisplay.)
└── App/AppDelegate.swift            ← MODIFIED (resumeInFlightJobIfNeeded() called after
                                        MenuBarController init so a relaunched app picks
                                        up a UserDefaults-persisted activeJobID.)
```

## Deploy procedure (next time)

1. Make changes locally. Run `python3 -c "import ast; ast.parse(open(f).read())"` on each modified .py to catch syntax errors. Run `swift build` in `client/`.
2. Build the tarball:
   ```
   cd /Users/omidzahrai/Desktop/CODEBASES/TOOLS/wisprflowALT
   D=/tmp/wf-deploy-staging && rm -rf $D && mkdir -p $D/server $D/scripts
   rsync -a server/src server/scripts $D/server/
   cp server/pyproject.toml $D/server/
   cp scripts/deploy-server.sh $D/scripts/
   find $D -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
   tar -czf /tmp/wf-deploy.tar.gz -C $D .
   base64 -i /tmp/wf-deploy.tar.gz -o /tmp/wf-deploy.tar.gz.b64
   ```
3. Upload as gist: `gh gist create /tmp/wf-deploy.tar.gz.b64` (note the gist ID).
4. On the mini via `/macmini paste`: clone the gist, base64-decode with `base64 -D -i ... -o ...` (BSD syntax, not GNU `-d`), tar -xzf, then `bash scripts/deploy-server.sh /tmp/wf-deploy-src`.

## The silent-ch2 fix (mono meeting recordings)

The plan's "one-line fix" was `force_single = (request_mode == ProcessingMode.FILE)`. That was necessary but not sufficient. The first deployed version produced this output on a mono meeting recording:

```
mode: remote
[You]   line of text
[Other] line of text   ← duplicated!
```

Root cause: `ffmpeg -ac 2` on a mono source **duplicates** ch0 into ch1 (it does not zero-pad). Pipeline's `is_silent_robust(ch2)` then returns False → remote branch fires → both channels transcribed → 2× duplicated output with `You`/`Other` labels instead of `Speaker N`.

Fix in `ops/staging.py`: `transcode_to_canonical_wav` gained a `pad_mono_to_stereo_silent` kwarg. When True (set by `runner._run_source` when request_mode=MEETING + source channel_count=1), ffmpeg is invoked with `-af "pan=stereo|c0=c0|c1=0*c0"` which keeps ch0 and zeros ch1. Pipeline's `is_silent_robust(ch2)` then returns True → in-person branch → pyannote on ch1 → `Speaker 1`/`Speaker 2`/... labels.

Verification (post-fix, 5-min slice of the Sammamish recording):
```
mode: in_person
speakers: ['Speaker 1', 'Speaker 2']
segments: 91  (was 182 — duplicated — before)
```

## Operational gotchas (every one of these tripped us)

1. **CRD shift-strip** — typing pipes, quotes, capitals, or special chars into the Mac mini Terminal via chrome-devtools is unreliable. All commands go through `/macmini paste` (gist-cloned bash files). Clone command itself uses only `[a-z0-9 /.;:_-]`.
2. **BSD vs GNU base64** — the mini is macOS. `base64 -D -i input -o output` (BSD), NOT `base64 -d input > output` (GNU). The deploy script uses the BSD form.
3. **`gh gist create --filename`** — silently ignored when fed a process-substitution `<(...)`. The substitution's name (e.g. `/dev/fd/11`) becomes the gist filename. Always feed a real file path.
4. **Deploy `set -e` polling** — `curl --max-time 3 ... || echo "000"` is load-bearing. Without `|| echo "000"`, curl's exit 7 trips `set -e` and the deploy script bails during the brief window when uvicorn is rebinding to port 8000 after kickstart. This was a known bug from the prior session.
5. **Mini venv has no `pip` binary** — `~/wispralt/server/.venv/bin/python` is a symlink that activates the venv via `pyvenv.cfg`, but the pip binary was missing. Bootstrap with `python -m ensurepip --upgrade` before `pip install`.
6. **Two distinct API keys** — `~/wispralt/server/.env`'s `WISPRALT_API_KEY` is for *internal* use; the user-facing API key is in the macOS Keychain (`security find-generic-password -s "co.wispralt" -w`) and is Supabase-backed. `/transcribe/file` validates the user-facing key. Don't conflate them.
7. **Watchdog cannot release the semaphore** — the runner's `async with self._semaphore:` block is awaiting the executor thread. The watchdog can call `set_failed` to mark the row in SQLite but the semaphore stays held until the executor returns naturally. The client UI surfaces this as a "Previous transcription still finishing on server" banner so a 429 on resubmit isn't mysterious.
8. **mlx-whisper memory leak with `word_timestamps=True`** — ~10 MB per 30-s chunk on long files. We use word_timestamps only in meeting mode (where the `assign_speakers_segments` helper needs them); file mode disables it.
9. **TCC dialogs on the mini** can pop up during /macmini paste runs (Terminal asking for Documents access). Either click Allow via Finder, or rerun the command after granting access. Doesn't block the gist clone itself.

## Test matrix (latest run)

See `tmp/matrix-results.md` for the full table. The matrix runs locally (`scripts/run-matrix-local.sh`) submitting via the public Cloudflare endpoint with the Keychain key. Each row has a name, audio duration, mode, wall clock, computed realtime ratio, segments, speakers detected, and final status.

Pass criteria (from `docs/TESTING.md`):
- 30s rows: wall < 30/R + LOAD_COST. Cold-start absorbs ~60 s of model load on the first row.
- 105m mode=meeting (#11): expect ≥2 speakers detected on the multi-person Sammamish recording. This validates yesterday's "all Speaker 1" bug fix.
- All rows: outputs (json/srt/vtt/txt) non-empty. SRT/VTT use segment-level timestamps; word-level only in `words[]` of JSON for meeting mode.

## Roll-back plan

WhisperX is kept in `pyproject.toml` and on disk through Phase 8. If matrix surfaces an unacceptable regression:

1. `git revert <phase-1-commit>` (reverts pipeline.py + merge.py changes; whisperx_loader.py never deleted yet)
2. `pip install -e .` in the venv (already has whisperx and ctranslate2)
3. Kickstart launchd

Phase 10 is the explicit-approval gate that removes WhisperX. Once that lands, rollback is `git revert <phase-10-commit>` + `pip install -e .`.

## Things deferred

- **Resume-from-checkpoint** — `recover_orphans` logs the last-known phase but does NOT resume from there. A future plan can wire `_run_pipeline_inner` to skip already-completed phases.
- **CrisperWhisper-on-MLX** — `[UH]` / `[UM]` filler-marker output is gone with the WhisperX swap. nyrahealth/CrisperWhisper has no MLX-format conversion on HF; conversion is feasible via `mlx-examples/whisper/convert.py` but not trivial. Defer until a user complains.
- **Per-segment word timestamps in client UI** — `words[]` is in the JSON output for meeting mode but no UI surface consumes it today. Future "click word to seek" feature lives here.
- **SSE long-poll for progress** — currently 5 s polling. Acceptable for jobs that are minutes long. Future v0.2 can add `/transcribe/meeting/{id}/stream`.
- **Pyannote upgrade to drop `use_auth_token` constraint** — bumps the `huggingface_hub<1.13` ceiling away.

## How a context-less agent picks this up

1. Read `docs/ARCHITECTURE.md` for the system overview + the three modes table.
2. Read this file (`CHANGELOG-2026-05-10.md`) for what changed and why.
3. Read `docs/DEPLOYMENT-NOTES.md` for the deploy procedure + gotchas.
4. Read `docs/TESTING.md` for the matrix + cancel test scenario.
5. Read `tmp/done-plans/2026-05-10-mlx-swap-mode-discriminator-observability.md` if the agent wants to understand the design decisions (rejected alternatives, where reasoning clashed, etc.).
6. Read `CLAUDE.local.md` for the latest session handoff (always overwritten by `/pre-compact`).

Common operational commands:

```bash
# Run the matrix end-to-end (from the MacBook):
./scripts/run-matrix-local.sh
# Skip specific rows:
SKIP_ROWS="10,12" ./scripts/run-matrix-local.sh

# Deploy server changes to the mini:
# (see "Deploy procedure" above)

# Check server health from anywhere:
API_KEY=$(security find-generic-password -s "co.wispralt" -w)
curl -H "Authorization: Bearer $API_KEY" https://transcribe.integrateapi.ai/admin/active
curl -H "Authorization: Bearer $API_KEY" https://transcribe.integrateapi.ai/metrics

# Pull the server log for a specific job:
curl -H "Authorization: Bearer $API_KEY" "https://transcribe.integrateapi.ai/admin/server-log/<job-id>"
```
