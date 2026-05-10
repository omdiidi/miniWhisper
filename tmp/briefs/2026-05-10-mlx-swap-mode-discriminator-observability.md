# Brief: MLX-Whisper swap + explicit mode discriminator + observability beef-up

## Why

The 2026-05-09 Custom Transcriptions smoke test surfaced three coupled problems that should be fixed in one coordinated change:

1. **Slow.** A 105-min mono m4a took ~70 min of server processing — ~1.5× realtime. WhisperX runs on CPU because its CTranslate2 / faster-whisper backend has no Apple Silicon GPU support. Pyannote already uses MPS. The transcribe step is the bottleneck. Acceptable as a one-off; not acceptable as steady state.

2. **Wrong speaker labeling.** The 105-min transcript was a multi-person meeting (Voice Memos / phone recording of an in-person business conversation) but every one of 1,734 segments was labeled `Speaker 1`. Root cause: the pipeline treats mono audio as "single-speaker dictation" and skips pyannote entirely. That assumption was correct for the FN-hold dictation path it was originally written for, and wrong for the file-upload and in-person-meeting paths that were grafted on later.

3. **Opaque mid-run.** Nothing in the server log told us what phase the job was in, how far along it was, or how long each step took. We watched RSS oscillate for an hour to infer chunk boundaries. The client got `pending` / `running` / `done` and nothing in between.

The user explicitly wants:
- File transcribe to skip diarization (per their call — accuracy of chunk-by-chunk speaker tracking on long uploads is suspect, and they don't need it for file uploads).
- Meeting transcribe to keep diarization AND work for mono in-person captures, not only stereo Zoom-style captures.
- Speed to be "reasonably quick" — explicitly targeting the file-transcribe path getting much faster.
- Robustness for files of any size including 2-hour meetings.
- Better observability so problems are visible while they're happening, not three days later.

## Context

### Today's actual run (evidence)

| Metric | Value | Source |
|---|---|---|
| Audio | 105.7 min mono AAC 48 kHz (6341.5 s) | `ffprobe` on `Sammamish Endodontics.m4a` |
| Wall clock | ~70 min from job submit to JSON write | folder mtime 22:24:29 → JSON mtime ~23:36 |
| Throughput | ~1.5× realtime | derived |
| Segments produced | 1,734 | `Sammamish Endodontics.json` `len(segments)` |
| Speakers detected | 1 (all `Speaker 1`) | same JSON |
| Outputs written | json / srt / vtt / txt | folder listing |
| Server RSS pattern | oscillating 3 GB ↔ 8.5 GB throughout, peak 6.85 GB during pyannote-style burst (but no pyannote actually ran — see below) | /metrics polling at 10–20 s |
| Server logs during run | only `GET /transcribe/meeting/<id> 200` poll responses + `GET /metrics 200` from the monitor | mini Terminal tail |

The RSS oscillation pattern (~50–60 cycles observed) maps to WhisperX processing ~30 s mel-feature windows. The 6.85 GB peak earlier in the run looked like pyannote loading; on reflection, that was probably WhisperX's own first-pass model warmup since pyannote was *not* invoked (mono → force_single_channel branch).

### Codebase touchpoints

- `server/src/wispralt_server/meeting/whisperx_loader.py:23` — `_DEVICE = "cpu"`. Single line that locks transcription to CPU. Backend is faster-whisper / CTranslate2; CrisperWhisper variant in use (gives `[UH]` `[UM]` filler markers, visible in today's transcript).
- `server/src/wispralt_server/meeting/whisperx_loader.py:51` — `whisperx.load_align_model()`. wav2vec2 alignment pass for word-level timestamps. Word timestamps DO appear in `json` (`words` array per segment) but SRT/VTT only use segment-level. Not user-visible today.
- `server/src/wispralt_server/meeting/pipeline.py` — single-channel branch with hardcoded `label_all(display_name="Speaker 1", channel=None, raw_speakers=["mic"])`. This is the "mono = dictation, skip pyannote" shortcut.
- `server/src/wispralt_server/meeting/diarize.py:62-69` — pyannote already auto-routes to MPS if available, else CPU. Works on mono and stereo audio.
- `server/src/wispralt_server/jobs/runner.py` + `jobs/store.py` — SQLite-backed job store. Phases recorded today: `pending` / `running` / `failed` / `done`. No per-phase progress field.
- `server/src/wispralt_server/main.py` — `TRACKED_ROUTES` + `_KIND_MAP` for usage telemetry. `/transcribe/file` is registered. Job status surfaced via `GET /transcribe/meeting/<id>`.
- `client/WisprAlt/App/MenuBarController.swift:633-700` — `runFileTranscriptionJob`, the polling loop. Today's fix (2026-05-09) made the deadline duration-aware: `max(600s, min(6h, dur×3+300s))`. Without it, today's run would have died at 10 min.
- `client/WisprAlt/Server/MeetingAPI.swift` — `.submitFile()` POSTs to `/transcribe/file`; `.poll()` hits `/transcribe/meeting/<id>` (lifecycle endpoints are shared with the legacy meeting route).

### The three pipeline shapes that actually exist

| Path | Channel | Speakers | Today's behavior |
|---|---|---|---|
| Dictation (FN-hold) | mono mic | 1 | Parakeet MLX, sub-200 ms, no diarization. Fine. |
| Meeting (Zoom-style live) | stereo (mic L / system R) | many | WhisperX CPU + pyannote MPS. Correct. |
| Meeting (in-person live) | mono mic capturing room | many | Today: would fall into the `force_single_channel` branch and label everyone `Speaker 1`. BROKEN. |
| File transcribe — mono upload | mono | many | Today: same broken behavior as in-person meeting. |
| File transcribe — stereo upload | stereo | many | WhisperX + pyannote. Slow but correct. |

The bug: channel count is being used as a mode discriminator, but mode and channel count are independent dimensions. Mode is "what kind of audio is this?" (dictation vs meeting vs file). Channel count is "how many channels does the audio have?" (mono vs stereo). The pipeline conflates them.

### Where the user is pinning the trade-offs

- File transcribe: **drop diarization entirely.** "I'm sure it's pretty accurate, but if we're chunking it up and all that, it might lose touch of who is speaker one and speaker two which might mess up the context. I don't know if we actually need it really." Decision: skip diarization on the file path. Speed > speaker labels. Easy to add back later if a user requests it.
- Meeting: **keep diarization, but make it work for mono too.** In-person meetings are mono. Today they'd be broken.
- Speed: file transcribe should target ~3-5× realtime (so a 2-hour file completes in 25-30 min). Today's 1.5× is "ridiculous."
- Robustness: any size file, including 2-hour meetings, must complete or fail loudly. No silent stuck states.

## Decisions

### Architecture

- **Replace channel-count-as-mode-discriminator with an explicit `mode` form field on `/transcribe/file`.** Values: `"file"` (no diarization, speed-first) and `"meeting"` (diarization on, channel-count-aware). Default to `"file"` when the field is omitted (today's behavior was meeting-shaped, so this is a deliberate flip toward speed for uploads). Reasoning: the channel-count shortcut was always a proxy for the real intent; making intent explicit is one form field and eliminates a whole class of footguns.

- **Live meeting capture stays as-is for channel handling.** The `MeetingRecorder` already produces stereo m4a when system audio is present. For in-person meetings (no system audio), the recorder should still produce a single-channel m4a and the server should run diarization on it. Implementation note: `MeetingRecorder` already handles this via `AlignedRingBuffer` (zero-pads the system channel when absent) — verify this produces a valid mono-equivalent path through the server.

### Model swap

- **Swap WhisperX (CPU) → `mlx-whisper` (Apple Silicon GPU).** Same large-v3 weights, ~3-5× realtime expected on M4. Text accuracy is unchanged (it's literally the same model). What we lose: word-level wav2vec2 alignment (segment-level timestamps only) and CrisperWhisper's `[UH]`/`[UM]` markers (CrisperWhisper isn't in `mlx-community/whisper-large-v3-mlx`; reconverting CrisperWhisper weights to MLX is possible later if filler markers turn out to matter).

- **Spike before commit.** Before tearing out WhisperX, run a standalone `mlx-whisper` script against today's `Sammamish Endodontics.m4a` on the prod-mini and measure wall clock. If it's <25 min, the swap is worth it. If it's still 40+ min, the bottleneck is elsewhere (probably ffmpeg decode or audio I/O) and we need to look there instead.

- **Diarization stays on pyannote-MPS for the meeting path.** It's already on the GPU and already works on mono.

### Observability

- **Per-phase structured logging** with timings. Required phase markers: `ffprobe`, `ffmpeg_decode`, `transcribe_load`, `transcribe`, `diarize_load`, `diarize`, `merge`, `output_write`. Each phase logs `phase_start` and `phase_done duration_ms=…`. Single grep gets you the full timeline of a job.

- **New `progress` field on the job-status response.** Shape: `{"phase": "transcribe", "chunk": 47, "total_chunks": 211, "started_at": ..., "phase_started_at": ...}`. Client polls `/transcribe/meeting/<id>` and surfaces phase + chunk progress in the menubar UI ("Transcribing 47/211"). For mlx-whisper this requires hooking into its segment-emission callback; standard mlx-whisper does emit segments incrementally, so we can write progress to the SQLite job row each segment.

- **SQLite job checkpoints.** Each phase boundary writes a `phase` + `chunk` cursor into the `jobs` row. On server restart, `runner.recover_orphans` (already exists) can either resume the job from the last checkpoint or fail it loudly with the phase it died in. Either is better than today's silent disappearance.

- **A new lightweight endpoint `GET /admin/active`** returning a list of in-flight jobs with their phase, chunk progress, RSS, and idle time. Used both by the client UI and by debugging tools (replaces having to launchctl-tail server logs).

- **Client-side log surfacing.** The menubar UI currently shows `Uploading` / `Processing` / `Done`. Add a phase + progress line under that ("Transcribing 47/211") and a "View server log" link that fetches the last N lines of `~/Library/Logs/WisprAlt/server.log` for the active job ID. For employees in the field, this is the difference between "it's broken" and "it's chunk 180 of 211, almost done."

### Robustness

- **Hard timeouts per phase** that fail the job with a useful error instead of running forever. Default budgets: ffprobe 30 s, ffmpeg_decode 5 min, transcribe (duration × 4), diarize (duration × 1.5), output_write 30 s. Per-phase ceilings let us catch hangs that wouldn't trigger a job-level deadline.

- **Cancel button in the menubar UI** wired to a new `DELETE /transcribe/meeting/<id>` (route already exists, but the client doesn't currently surface it for file jobs). Lets the user kill a runaway job without restarting the server.

- **Server-side resource gate.** Before accepting a `/transcribe/file` submission, check (a) free RAM > 4 GB, (b) free disk > 2 × upload size, (c) no other meeting/file job running (existing semaphore). Return 507 / 429 / 503 with `Retry-After` if not. Today the runner has this for the at-most-one-job invariant but RAM/disk gates are weaker.

- **Test matrix** before merging: 30-second clip, 5-min, 30-min, 2-hour. All four sizes for both mono and stereo. All four sizes for both `mode=file` and `mode=meeting`. 16 runs total. Documented in the plan.

### Implementation logistics

- **Mac mini desktop access during implementation.** The user has CRD + `/macmini paste` set up. Plan will explicitly use the mini as a dev target: spike script runs there, build + redeploy through gist transport, smoke tests run there. No more "build on MacBook, hope it deploys cleanly." Each phase of the plan ends in a verified state on the prod mini before moving on.

- **Backward-compatible rollout.** New `mode` field on `/transcribe/file` defaults to `"file"` when absent. Existing clients (none in the field yet for the file endpoint) keep working. Meeting endpoint untouched.

## Rejected Alternatives

- **Keep WhisperX, just enable its CUDA backend on the Mac mini.** Mac mini has no CUDA. WhisperX has no Metal/MPS backend in its current release.

- **whisper.cpp via Python bindings.** Faster than WhisperX-CPU but slower than `mlx-whisper` on M-series. Adds a CGo/CFFI dependency. Mixed precision support is rougher.

- **Smaller Whisper model (medium or small) instead of large-v3.** Faster, but text quality drops noticeably on accents and quiet speakers. Wrong knob to turn for transcription quality.

- **Diarization on by default for file transcribe.** User explicitly rejected — chunked-audio diarization "might lose touch" of who's who, and they don't need speaker labels on uploaded files. Mode field defaults to `"file"` (no diarization).

- **Word-level alignment as a separate optional pass on file uploads.** Doable but no current consumer (SRT/VTT/TXT are segment-level). Defer until a feature actually needs it.

- **CrisperWhisper-to-MLX conversion as part of this plan.** Adds days of model-conversion work for `[UH]`/`[UM]` markers that no current UX surface depends on. Defer to a follow-up if the user misses them.

- **Server Sent Events (SSE) for progress instead of polling.** SSE is cleaner but adds an FastAPI/Uvicorn wrinkle and a second connection per job. Polling at 5 s is already what the client does for status; adding a `progress` field is one extra key in the response. Lower complexity.

- **Real-time transcription (stream as you upload).** A different product feature. Defer.

## Where Reasoning Clashed

- **Drop diarization on file uploads vs make it optional.** Both reasonable. User's call: drop it for now, simpler is better, can re-add as a checkbox later if real users ask for it. Counterargument worth flagging: if you ever upload a recording of a meeting you weren't at, you'll wish you had speaker labels. We're betting that case is rare enough to live with for now.

- **Replace WhisperX vs keep both stacks.** Could keep WhisperX as a fallback for cases where mlx-whisper fails or where word-level alignment is needed. Adds maintenance burden and double the models in RAM. Cleaner to fully cut over; if mlx-whisper has gaps, we discover and fix them. Reversible if needed.

- **Per-phase timeouts vs single job-level deadline.** Per-phase is more diagnostic (you know which phase hung) but more configuration surface. Job-level is simpler. Going with per-phase because the observability story is the whole point of this plan.

## One Thing to Do First

Run a standalone `mlx-whisper` benchmark on the prod-mini against today's actual file (`Sammamish Endodontics.m4a`, 105.7 min mono). One Python script, one model load, one transcribe call, log wall clock. Two outcomes:

- **If < 25 min:** the swap is justified, ship the full plan as written.
- **If 25-40 min:** the swap helps but the bottleneck is partly elsewhere (ffmpeg decode? I/O?). Plan grows a "profile and fix the second bottleneck" phase before the swap ships.
- **If > 40 min:** something is wrong with how mlx-whisper is configured. Investigate before committing to the swap direction.

Either way the spike is ~20 min of work and tells us whether the rest of the plan is worth the investment.

## Direction

One coordinated change: swap WhisperX → mlx-whisper for speed, make mode an explicit form field on `/transcribe/file` (default `"file"` = no diarization), keep the meeting path's diarization and make it work for mono in-person captures, and add per-phase structured logging + SQLite checkpoints + a `progress` field on the job-status response. Implement and verify directly on the prod-mini using CRD / `/macmini paste`. Sixteen-run test matrix (4 sizes × 2 channel counts × 2 modes) before merging. Estimated effort: a benchmark spike + 1-1.5 days of focused work + one deploy session.
