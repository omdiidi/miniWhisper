---
name: "Server accepts original audio — kill client-side WAV transcoding everywhere"
date: 2026-05-09
brief: ./tmp/briefs/2026-05-09-server-accepts-original-audio.md
---

## Goal

Replace the WAV-only upload contract with a "send whatever you have" contract. The server takes any audio/video container ffmpeg can decode, runs ffmpeg internally to produce 16 kHz PCM, and feeds it to the existing Whisper/Pyannote pipeline. The client stops transcoding for both custom transcriptions (uploads source file as-is) and meeting recordings (captures to AAC m4a directly via AVAssetWriter instead of raw WAV via AlignedRingBuffer). Net effects: 8–10× smaller uploads, no audio-quality loss in stored archives, and likely fixes the user-reported "voice cutting out" glitch in meeting playbacks because the rewrite removes the AlignedRingBuffer pad-with-silence path.

## Summary

Five-phase migration:

1. **Server**: add `POST /transcribe/file` endpoint that accepts arbitrary containers; add `staging.transcode_to_canonical_wav()` that ffmpegs the upload to a 16 kHz mono or stereo WAV in staging; extend the meeting pipeline to accept either mono (custom) or stereo (meeting) inputs; verify ffmpeg is installed (already is — `setup-server.sh:78-83`).
2. **Diagnose meeting glitch** (30 min, before the recorder rewrite): repro the voice-cutting symptom on the current build, capture log + WAV, classify root cause (AlignedRingBuffer pad-with-silence vs AVAudioEngine config-change vs upstream PCM corruption). Outcome decides whether the recorder rewrite IS the fix or whether a deeper issue must be addressed first.
3. **Client custom transcriptions**: drop `MediaTranscoder` entirely, switch `transcribePickedFile` → `processCustomTranscriptionUpload` to upload the picked file as-is via a new `MeetingAPI.submitFile()` call to `/transcribe/file`. Per-job folder stores the original file (e.g. `meeting.m4a`).
4. **Client meeting recorder**: replace the `AVAudioEngine` mic-tap + `AlignedRingBuffer` + `AVAudioFile` PCM path with `AVAssetWriter` writing AAC m4a (2-channel: mic on L, system on R). Upload m4a to `/transcribe/file`.
5. **Deprecate** `/transcribe/meeting` server-side after the new client is rolled out to all employees.

## Intent / Why

- The current 90-min Custom Transcription test produced a 387 MB WAV that Cloudflare Tunnel choked on (two failures: timeout + "cannot parse response"). The same source file as m4a is ~50 MB and fits within the proxy's comfort zone.
- The stored audio file in the user's archive sounds *worse* than the m4a they started with — we throw away high frequencies (>8 kHz) and stereo image during the WAV transcode. Whisper's transcript quality is unaffected (it runs at 16 kHz mono internally regardless), but the user-visible artifact is real.
- The user reports "voice cutting out" on meeting playback — symptoms are consistent with `AlignedRingBuffer.padMissing(uptoSampleEnd:)` (`AlignedRingBuffer.swift`) inserting silence into one channel when the other has drifted past the gap tolerance. AAC capture via `AVAssetWriter` handles its own framing/queuing instead of a hand-rolled aligner.
- Multi-user, prod system. Old clients in the field still expect `/transcribe/meeting` to work. The new endpoint is additive; deprecation only happens after all clients update.

## Source Artifacts

- Brief: `./tmp/briefs/2026-05-09-server-accepts-original-audio.md`
- Earlier (now-completed) plan: `./tmp/done-plans/2026-05-09-menubar-file-transcribe.md` — the just-shipped Custom Transcriptions feature this plan replaces the transcoder path of.

## What

- **New server endpoint** `POST /transcribe/file` that:
  - Accepts any audio/video container (m4a, mp3, mp4, mov, wav, aac, flac, opus, ogg, webm, m4v, caf, aiff).
  - Reads a query/form param `mode` ∈ `{single, stereo}`. `single` = mono pipeline (custom transcriptions); `stereo` = 2-channel pipeline (meetings — mic on L, system on R).
  - Streams the upload to a temp file in the existing staging dir.
  - Calls `ffmpeg` to decode into a 16 kHz canonical WAV (mono or stereo, matching `mode`).
  - Hands the canonical WAV to the existing meeting pipeline (which already does Whisper + Pyannote on 16 kHz audio).
  - Returns the same `{job_id, status}` response shape as `/transcribe/meeting` so client polling code reuses unchanged.
- **Pipeline extension**: `meeting/pipeline.py` accepts a `force_single_channel: bool` flag. Single-channel mode runs Whisper + diarization on one channel (no L/R split). Stereo mode is the existing path.
- **Staging extension**: new `staging.transcode_to_canonical_wav(source_path, *, target_channels) -> Path`. Uses `subprocess.run(["ffmpeg", "-y", "-i", source, "-ac", str(target_channels), "-ar", "16000", "-acodec", "pcm_s16le", canonical_wav_path], ...)`. Logs ffmpeg stderr on failure.
- **Custom-transcription client** uploads source file as-is. `MediaTranscoder` deleted. Per-job folder layout becomes `Custom Transcriptions/<stem>__<ts>/{<original-filename>, <stem>.{txt,srt,vtt,json}}`.
- **Meeting recorder client** rewritten around `AVAssetWriter`:
  - `AVCaptureSession` for mic input → `AVAssetWriterInput` (audio).
  - `SCStream` system-audio buffers → second `AVAssetWriterInput` (or, simpler, a single 2-channel input where mic and system are interleaved at append time).
  - File extension changes from `.wav` to `.m4a` in the meetings folder.
  - `AlignedRingBuffer` and the AVAudioFile path are deleted.
- **Old endpoint** `/transcribe/meeting` stays during migration. Deletion is a separate task (Phase 5) after all employees update their clients.

### Success Criteria

- [ ] `curl -F "file=@meeting.m4a" -F "mode=single" .../transcribe/file` returns a job_id; polling that job_id eventually returns `done` with `txt/srt/vtt/json` outputs.
- [ ] Same with `mode=stereo` and a 2-channel meeting m4a.
- [ ] Same with `.mp4`, `.mov`, `.mp3`, `.wav`, `.flac` inputs (one each).
- [ ] Custom Transcriptions: a 90-min m4a uploads in <30 min on the user's home uplink and produces a transcript. The per-job folder contains the original m4a (not a 387 MB WAV).
- [ ] Meeting recorder produces an m4a (not a WAV) in `~/Documents/WisprAlt/Meetings/`. The m4a is roughly 8–10× smaller than the equivalent old WAV (90s recording: ~1.2 MB m4a vs ~10 MB WAV).
- [ ] Voice-cutting symptom is either gone OR diagnosed as a deeper issue requiring a separate fix.
- [ ] Old clients (those still on the just-shipped build) continue to upload to `/transcribe/meeting` successfully — no breakage during migration.
- [ ] After all employees update: `/transcribe/meeting` route is removed; integration smoke test confirms no client paths still reference it.

## Verified Repo Truths

### Data / State

- Fact: ffmpeg is already installed via `brew install ffmpeg` as part of `setup-server.sh`.
  Evidence: `scripts/setup-server.sh:78-83` (`if brew list ffmpeg ...else brew install ffmpeg`).
  Implication: No deploy-time install step needed on prod-mini. Runtime check `command -v ffmpeg` is still sensible defense-in-depth.

- Fact: `staging.stream_to_staging` validates a 2-channel WAV header today.
  Evidence: `server/src/wispralt_server/ops/staging.py:35` (function definition), `:170` (`validate_wav_completeness`).
  Implication: New `transcode_to_canonical_wav` lives in the same module so the new endpoint can compose `stream_to_staging` (without WAV validation) → `transcode_to_canonical_wav`.

- Fact: `meeting/pipeline._load_channels` uses `soundfile.read(..., always_2d=True)` and assumes 2-channel WAV.
  Evidence: `server/src/wispralt_server/meeting/pipeline.py:215`, `:232`.
  Implication: Single-channel mode needs an alternate code path inside the pipeline. Cleanest: branch on `force_single_channel` early; the single-channel branch reads as `always_2d=True` then collapses to one channel for the Whisper pass and runs diarization on that one stream.

- Fact: `MAX_UPLOAD_BYTES = 2_147_483_648` (2 GB) server-side.
  Evidence: `server/src/wispralt_server/config.py:52`, `server/.env.example:12`.
  Implication: Server-side limit is generous; the upload-failure root cause is Cloudflare Tunnel / proxy intermediates, not server config. The fix is reducing payload size (m4a vs WAV), not raising server limits.

### Entry Points / Integrations

- Fact: `MeetingAPI.submit(_:)` builds its own `URLSession` (separate from `ServerClient`'s default session) so it can wire up an `UploadSessionDelegate` for progress.
  Evidence: `client/WisprAlt/Server/MeetingAPI.swift:151-155`.
  Implication: New `MeetingAPI.submitFile(_:mode:)` follows the exact same pattern — separate URLSession, same delegate wiring. The 6-hour timeout patch we just applied is preserved in the new method.

- Fact: `MenuBarController.processCustomTranscriptionUpload` and the meeting upload path both call `runMeetingTranscriptionJob` (a private helper added in the just-completed plan).
  Evidence: `client/WisprAlt/App/MenuBarController.swift:638-705` (helper), `:574` (meeting path), `:797` (custom path).
  Implication: Both call sites can be updated to call `MeetingAPI.submitFile` instead of `MeetingAPI.submit` with a different `mode`. Polling/download/delete code reuses unchanged.

### Execution / Async Flow

- Fact: `MeetingRunner` queues jobs serially via a semaphore; `_run` calls `meeting_pipeline.transcribe_meeting` in a thread executor.
  Evidence: `server/src/wispralt_server/jobs/runner.py:140-169`.
  Implication: Concurrency model is unchanged — single meeting/custom job at a time. No new race conditions introduced. New endpoint just submits a job after the ffmpeg transcode completes.

- Fact: `MeetingRecorder` uses **`SCStream` for BOTH mic AND system** (not AVAudioEngine for mic). Mic = `SCStreamOutputType.microphone` (macOS 14+), system = `SCStreamOutputType.audio`. Both callbacks dispatch on a single private serial `ioQueue`. An `AlignedRingBuffer` (200 ms gap tolerance + 100 ms stall-detect timer) interleaves them into a 2-channel WAV via `AVAudioFile`.
  Evidence: `client/WisprAlt/Capture/MeetingRecorder.swift:3, 37-43, 78, 101-103, 195-199, 227-228, 377`; `client/WisprAlt/Capture/AlignedRingBuffer.swift:1-50`.
  Implication: The recorder rewrite replaces the `AlignedRingBuffer` + `AVAudioFile` PCM-stereo path with `AVAssetWriter` AAC encoding. **Both upstream callbacks stay on `SCStream`** — only the downstream framing/encoding changes. SCStream already delivers `CMSampleBuffer`s, which is exactly what `AVAssetWriterInput.append(_:)` consumes natively, so no AVAudioPCMBuffer round-trip is needed in the new path.

### Frontend / UI

- Fact: Custom Transcriptions popover button calls `MenuBarController.shared?.transcribePickedFile()`.
  Evidence: `client/WisprAlt/UI/SettingsView.swift` (QuickActionsSection).
  Implication: No popover-side changes needed. The full-width button behavior stays the same — the underlying upload contract changes.

### Shared Types / Exports

- Fact: Swift target uses SPM auto-discovery; no Package.swift change for added/removed `.swift` files.
  Evidence: `client/Package.swift:32-47`.
  Implication: Deleting `MediaTranscoder.swift` and rewriting `MeetingRecorder.swift` requires no project-file edits.

## Locked Decisions

From the brief — DO NOT relitigate:

- New server endpoint, NOT modify the existing `/transcribe/meeting` route. Old clients in the field need the existing route during migration.
- ffmpeg-based server-side decode (no client-side conversion).
- AAC m4a for meeting recordings (not Opus — `AVAssetWriter` supports AAC natively, no third-party libs).
- Keep stereo intent for meetings (mic L, system R). Custom transcriptions are mono.
- No client-side resampling.
- 30-min repro of voice-glitch BEFORE recorder rewrite. Diagnosis informs whether AAC migration is the fix or whether deeper issue exists.
- Both client paths (custom + meeting) migrate to `/transcribe/file`. After both ship, `/transcribe/meeting` is deprecated.
- Multi-user prod system. Migration order: server first (additive endpoint), then client paths, then server endpoint deletion.

## Known Mismatches / Assumptions

- Mismatch: brief says custom transcriptions should be mono ("channel count = whatever the source has"), but `_load_channels` currently REQUIRES 2-channel input.
  Repo Evidence: `meeting/pipeline.py:215, 232`.
  Requirement Evidence: brief decision "For custom transcriptions, channel count = whatever the source has."
  Planning Decision: Add a `force_single_channel: bool = False` parameter to `transcribe_meeting()` and branch `_load_channels` early. Single-channel mode reads the WAV's first channel only and feeds Whisper one stream; diarization runs on that one stream.

- Assumption: Cloudflare Tunnel can reliably handle ≤100 MB uploads. The user's failed 387 MB upload + working 50 MB uploads is the empirical bracket; we haven't done a controlled probe between those points. Mitigation: smaller m4a payloads sidestep the question for the realistic envelope.

- Assumption: `AVAssetWriter` configured with two `AVAssetWriterInput` audio tracks (mic and system) produces a single 2-channel m4a where Whisper/Pyannote can decode each channel independently. Apple-documented but worth verifying with a 30-second smoke recording before declaring the recorder rewrite complete.

- Assumption: ffmpeg's pcm_s16le output preserves enough fidelity for Whisper (it does — 16-bit PCM is more than Whisper needs). No quality concern.

- [NEEDS CLARIFICATION → resolved] Should the new endpoint name be `/transcribe/file` or `/transcribe/v2/meeting` or `/transcribe`? Going with `/transcribe/file` because (a) it's content-agnostic, (b) it doesn't conflict with the legacy route, (c) the deprecation path is "delete `/transcribe/meeting`" not a confusing renaming. Locking this.

## Critical Codebase Anchors

- Anchor: `server/src/wispralt_server/routes/meeting.py:53-86` (POST handler) + `:100-178` (poll/download/delete).
  Evidence: same.
  Reuse / Watch for: poll/download/delete handlers can be reused VERBATIM by the new route via shared registration. Easiest: extract a small APIRouter factory that takes a path prefix and registers the four endpoints. Or cheaper: register the new POST under `/transcribe/file` but keep the existing `/transcribe/meeting/{job_id}/*` handlers as the canonical poll/download/delete surface — both clients use those for the *job lifecycle* even though submission paths differ.

- Anchor: `server/src/wispralt_server/ops/staging.py:35-130` (stream_to_staging + cleanup + sweep_old).
  Evidence: same.
  Reuse / Watch for: `stream_to_staging` does WAV-completeness validation. New flow needs streaming WITHOUT WAV validation → into a temp file with the source's original extension → ffmpeg-transcode → canonical WAV → existing pipeline. Either factor a `stream_to_staging_raw` (no WAV check) or pass a `validate_wav: bool = True` flag.

- Anchor: `server/src/wispralt_server/meeting/pipeline.py:283-410` (transcribe_meeting + inner).
  Evidence: same.
  Reuse / Watch for: `_load_channels` is the bifurcation point. Single-channel branch needs to (a) read the canonical WAV, (b) collapse to one channel array, (c) run Whisper on that, (d) run pyannote diarization on that, (e) emit transcript with single speaker dimension. Stereo branch is unchanged.

- Anchor: `client/WisprAlt/Server/MeetingAPI.swift:48-180` (`submit` with the recently-patched 6-hour timeouts).
  Evidence: same.
  Reuse / Watch for: New `submitFile(_:mode:)` mirrors `submit` structurally — same multipart shape, same Content-MD5 header, same delegate. Only differences: target URL is `/transcribe/file`, adds `mode` form field, sends bytes from the source file directly (no conversion).

- Anchor: `client/WisprAlt/Capture/MeetingRecorder.swift` end-to-end (~600 lines).
  Evidence: same.
  Reuse / Watch for: This file is rewritten substantially. **Both** mic-capture and system-capture upstreams stay on `SCStream` (`SCStreamOutputType.microphone` + `SCStreamOutputType.audio`) — there is NO `AVAudioEngine` or `AVCaptureSession` in the current recorder, despite earlier draft text. The downstream — buffer queueing, alignment, file write — is replaced. The 100 ms stall-detect timer behavior remains relevant: AVAssetWriter owns AAC framing but not cross-stream alignment.

- Anchor: `client/WisprAlt/Capture/AlignedRingBuffer.swift` (entire file).
  Evidence: same.
  Reuse / Watch for: DELETED entirely after the recorder rewrite. The 200 ms gap tolerance / pad-with-silence behavior is the strongest single-file suspect for the voice-cutting glitch. Diagnosis (Phase 2) will confirm.

- Anchor: `scripts/setup-server.sh:78-83` (ffmpeg install via brew).
  Evidence: same.
  Reuse / Watch for: For new fresh installs of WisprAlt server, ffmpeg is already provisioned. For prod-mini, already there. Only need a runtime `command -v ffmpeg` sanity check at startup.

## All Needed Context

### Documentation & References

- External doc: https://ffmpeg.org/ffmpeg.html#Options
  Section: "-ac" (channel count), "-ar" (sample rate), "-acodec" (codec)
  Why: confirms the canonical decode invocation is `ffmpeg -y -i SRC -ac 1 -ar 16000 -acodec pcm_s16le DST.wav` (or `-ac 2` for stereo).
  Critical insight: `-y` overwrites without prompt; `-ac 1` downmixes any source to mono; `-ar 16000` resamples; `pcm_s16le` is what soundfile/Whisper expect.

- External doc: https://developer.apple.com/documentation/avfoundation/avassetwriter
  Section: "Writing Audio"
  Why: canonical pattern for AAC m4a writing in Swift. Combined with `AVAssetWriterInput(mediaType: .audio, outputSettings: ...)` for the mic + system track(s).
  Critical insight: `outputSettings` for AAC: `[AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48000, AVNumberOfChannelsKey: 2, AVEncoderBitRateKey: 96_000]`. 96 kbps is plenty for speech-quality 2-channel.

- External doc: https://developer.apple.com/documentation/screencapturekit/scstreamoutput
  Section: didOutputSampleBuffer
  Why: SCStream gives you `CMSampleBuffer`s for system audio, which can be appended directly to `AVAssetWriterInput` without conversion.

- Repo reference: `client/WisprAlt/Capture/MeetingRecorder.swift:101-415` (current recorder).
  Why: source-of-truth for upstream capture wiring (BOTH mic and system on `SCStream` — `.microphone` and `.audio` output types). Downstream (file write, alignment) is what changes.

- Repo reference: `server/src/wispralt_server/ops/staging.py:35-130`.
  Why: the existing streaming-to-staging pattern (chunked async iteration, hash, atomic rename) is the model for the new no-WAV-validation variant.

- Repo reference: `server/src/wispralt_server/meeting/pipeline.py:283-410`.
  Why: where the single-channel branch needs to be added.

### Files Being Changed

```
server/
├── pyproject.toml                                       (no change — ffmpeg is brew, not pip)
└── src/wispralt_server/
    ├── ops/
    │   └── staging.py                                   ← MODIFIED (add stream_to_staging_raw + transcode_to_canonical_wav)
    ├── meeting/
    │   └── pipeline.py                                  ← MODIFIED (add force_single_channel branch in transcribe_meeting + _load_channels)
    └── routes/
        └── transcribe_file.py                           ← NEW (POST /transcribe/file → ffmpeg-transcode → submit_or_429)

client/
├── WisprAlt/
│   ├── App/
│   │   └── MenuBarController.swift                      ← MODIFIED (custom-transcription path uploads source file via submitFile; meeting path also switches to submitFile in Phase 4)
│   ├── Audio/
│   │   └── MediaTranscoder.swift                        ← DELETED (no client-side transcoding anymore)
│   ├── Capture/
│   │   ├── MeetingRecorder.swift                        ← MODIFIED (rewritten around AVAssetWriter AAC)
│   │   └── AlignedRingBuffer.swift                      ← DELETED (AVAssetWriter owns framing)
│   ├── Server/
│   │   └── MeetingAPI.swift                             ← MODIFIED (add submitFile(_:mode:); keep submit() during migration; later deleted in Phase 5)
│   └── Storage/
│       └── PendingUploadsQueue.swift                    ← MODIFIED (queued meeting recordings now hold m4a paths; replay calls submitFile(mode: .stereo))

docs/
├── ARCHITECTURE.md                                      ← MODIFIED (server pipeline + recorder + new endpoint)
├── OVERVIEW.md                                          ← MODIFIED (file→doc map updated for new/deleted files)
└── DEPLOYMENT-NOTES.md                                  ← MODIFIED (note ffmpeg dep is load-bearing; document the migration order)
```

After Phase 5 deprecation:

```
server/src/wispralt_server/routes/meeting.py             ← DELETED
client/WisprAlt/Server/MeetingAPI.swift                  ← MODIFIED (submit() removed; only submitFile() remains)
```

### Known Gotchas & Library Quirks

- **`UploadFile` reads into a SpooledTemporaryFile** by default in FastAPI. For multi-hundred-MB uploads, this can exhaust /tmp inodes or RAM. Use `await file.read()` only for small payloads; for the new endpoint, stream chunks directly to disk (the existing `stream_to_staging` already does this — copy the pattern).
- **ffmpeg invocation hygiene:** never shell-out via `subprocess.run(cmd_string, shell=True)`. Always pass an arg list. Quote nothing; rely on Python's argv handling. Prevents path-with-spaces breakage and shell injection.
- **`-y` flag is critical** — without it, ffmpeg prompts on stderr and hangs forever waiting for stdin response.
- **ffmpeg stderr is informational by default** (codec banners, progress). Don't fail on stderr presence; only fail on non-zero exit code. Log stderr at WARNING level on success, ERROR on failure.
- **`pyannote.audio` diarization on a single channel** still works but produces a single speaker dimension (no L/R split). Confirm via a 60-second smoke test before declaring single-channel mode shippable.
- **`AVAssetWriter` requires `startWriting()` BEFORE `startSession(atSourceTime:)`** before `append(_:)`. Calling out of order silently produces a 0-byte file. Pattern in Apple docs.
- **`AVAssetWriter` `finishWriting` is async** (callback-based); wrap in `withCheckedContinuation` for await.
- **`AVAssetWriterInput.expectsMediaDataInRealTime = true`** for live-capture sources. Without it, encoder can starve.
- **Mic + system audio can have different sample rates** (mic 48k, SCStream may deliver 48k or other). `AVAssetWriter` will not auto-resample between inputs. Use `AVAudioConverter` to bring both to a common 48 kHz before append. (Note: this is the ONE place we use AVAudioConverter — but only for sample-rate normalization between two LIVE buffers, not the channel-mix path that has the documented bug.)
- **Channel layout in the m4a:** to put mic on L and system on R, configure ONE AVAssetWriterInput with `AVNumberOfChannelsKey: 2` and `AVChannelLayoutKey` set to `kAudioChannelLayoutTag_Stereo`. Interleave mic→L and system→R at append time by combining both buffers into a single stereo PCM buffer per chunk before encoding. (Two separate writer inputs would produce a multi-track file, which `soundfile.read` won't handle correctly server-side.)
- **PendingUploadsQueue** currently holds WAV paths. After Phase 4, it holds m4a paths. The queue's replay logic must call `submitFile(mode: .stereo)` not `submit()`. Existing queued WAVs (from before this migration) need a one-time replay through the legacy `submit()` path or be discarded. Discard policy: at startup, if any queued file ends in `.wav`, log + delete + clear the queue entry (employees losing one queued offline meeting is acceptable; multi-day-stale queues are rare).
- **Cloudflare Tunnel free tier caveat:** large multipart uploads have been observed to fail at 387 MB. The realistic ceiling is ~100 MB based on field experience. Plan keeps payloads under that; no further Cloudflare config needed. If a 90-min m4a (~50 MB) ever exceeds 100 MB (it won't — AAC at 96 kbps × 90 min = 65 MB), revisit.
- **`make_dirs` race in staging:** if two uploads land within microseconds, `Path.mkdir(parents=True, exist_ok=True)` is the safe call; do NOT check-then-create.
- **ffmpeg -t / -ss flags** are NOT used. We're transcoding the entire file; no trimming.
- **AVAssetWriter file extension MUST match `outputFileType`.** `.m4a` extension + `AVFileType.m4a`. Wrong combo silently produces an unplayable file.

## Reconciliation Notes

None — no separate dossier was generated.

## Delta Design

### Data / State Changes

Existing:
- Meeting outputs land at `<meetingsPath>/<humanName>.{wav,json,srt,vtt,txt}` (sibling files).
- Custom outputs land at `<meetingsPath>/Custom Transcriptions/<stem>__<ts>/{<stem>__2ch16k.wav, <stem>.{txt,...}}`.

Change:
- Meeting outputs: WAV → m4a. New layout: `<meetingsPath>/<humanName>.{m4a,json,srt,vtt,txt}`.
- Custom outputs: stored file is the ORIGINAL container, not a transcoded WAV. New layout: `<meetingsPath>/Custom Transcriptions/<stem>__<ts>/{<original-filename>, <stem>.{txt,...}}`.
- `PendingUploadsQueue` holds m4a paths. Pre-existing queued WAVs are discarded with a warning at startup.

Why:
- The whole point — preserve user-perceived audio quality and shrink uploads.

Risks:
- Existing meetings already on disk as `.wav` are NOT migrated. They remain playable (any media app handles WAV) but mix WAV+m4a in the meetings folder. User-acceptable; documented in DEPLOYMENT-NOTES.

### Entry Point / Integration Flow

Existing:
- `POST /transcribe/meeting` is the only audio-submission endpoint. WAV-only.
- `MeetingAPI.submit` is the only client submission method.

Change:
- ADD `POST /transcribe/file` server-side. Accepts any container, plus a `mode` ∈ `{single, stereo}` form field.
- ADD `MeetingAPI.submitFile(_:mode:)` client-side. Same shape as `submit` but targets `/transcribe/file` and includes the mode.
- KEEP `POST /transcribe/meeting` and `MeetingAPI.submit` during migration.
- DELETE both in Phase 5 once all clients updated.

Why:
- Migration safety. Old clients stay functional during rollout.

Risks:
- Two code paths exist for ~weeks. Mitigated by smoke-testing each endpoint after every server deploy.

### Execution / Control Flow

Existing:
- `MeetingRecorder` writes to `AlignedRingBuffer` → `AVAudioFile` (16 kHz Float32 stereo WAV).
- `MediaTranscoder` (client) AVAssetReader → 16 kHz Int16 stereo WAV → upload.
- Pipeline `_load_channels` reads 2-channel WAV via `soundfile`.

Change:
- `MeetingRecorder` writes via `AVAssetWriter` (AAC stereo m4a, 48 kHz, 96 kbps).
- `MediaTranscoder` deleted; client uploads source file as-is.
- New server step: ffmpeg transcodes upload → 16 kHz canonical WAV in staging → existing pipeline.
- Pipeline gets `force_single_channel: bool` for the custom-transcription path.

Why:
- Removes the buggy AlignedRingBuffer + Float32 WAV path on the client (likely fixes voice-cutting); eliminates the upload-bloat + audio-quality-loss paths on both client surfaces.

Risks:
- Recorder rewrite is invasive. Keep the old recorder file under git until the new one is verified — easy revert.
- ffmpeg failure modes need explicit handling: invalid container, no audio track, codec not supported. Map each to a clear 4xx with the specific reason in the response body.

### User-Facing / Operator-Facing Surface

Existing:
- Meeting filenames in `~/Documents/WisprAlt/Meetings/` end in `.wav`.
- Custom Transcriptions per-job folders contain `<stem>__2ch16k.wav`.
- Test Connection in Settings hits `/transcribe/dictate` (unchanged, separate path).

Change:
- Meeting filenames end in `.m4a`.
- Custom per-job folders contain the original file name (e.g. `meeting.m4a`, `interview.mp3`).
- No popover-side UI changes.
- Optional: tiny subtitle/help-text update on the "Open Custom Transcriptions" button explaining the original file is preserved.

Why:
- User-visible improvement (smaller files, original audio preserved). No behavior change in the UI itself.

Risks:
- Mixed WAV+m4a in meetings folder until employees record fresh meetings. Acceptable; auto-resolves over time.

### External / Operational Surface

Existing:
- `setup-server.sh` installs ffmpeg via brew.
- Cloudflare Tunnel routes all traffic.
- LaunchAgent at `co.wispralt.server` runs the FastAPI server.

Change:
- Document in DEPLOYMENT-NOTES.md that ffmpeg presence is load-bearing for the `/transcribe/file` endpoint and add a runtime `command -v ffmpeg` check at server startup that fails fast if missing.
- No Cloudflare Tunnel config changes.
- Deploy mechanism unchanged: tarball-via-gist + `launchctl kickstart` per CLAUDE.local.md.

Why:
- Keep operational footprint small.

Risks:
- If ffmpeg disappears from the prod-mini for any reason (brew uninstall accident), the new endpoint fails at runtime with an opaque error. The startup check + log makes diagnosis instant.

## Implementation Blueprint

### Architecture Overview

```
┌─────────────── Client (custom transcription) ───────────────┐
│ User picks file (Finder)                                     │
│   ↓                                                          │
│ MenuBarController.handlePickedFile(picked)                   │
│   ↓ (no transcoding!)                                        │
│ MeetingAPI.submitFile(picked, mode: .single)                 │
│   ↓ HTTPS upload                                             │
└──────────────────────────────────┬───────────────────────────┘
                                   ▼
        ┌──────────────── Server ────────────────┐
        │ POST /transcribe/file                  │
        │   ↓                                    │
        │ staging.stream_to_staging_raw()        │
        │   ↓ (file written to staging dir)      │
        │ staging.transcode_to_canonical_wav(    │
        │   src, target_channels=1)              │
        │   ↓ (16 kHz mono PCM WAV)              │
        │ MeetingRunner.submit_or_429(           │
        │   wav_path, force_single_channel=True) │
        │   ↓                                    │
        │ → returns job_id                       │
        └──────────────────┬─────────────────────┘
                           ▼
        Job runs in background: meeting_pipeline.transcribe_meeting(
            wav_path, force_single_channel=True
        ) → outputs in <staging>/<job_id>/.

┌─────────────── Client (meeting recorder) ────────────────────┐
│ Triple-tap-FN                                                │
│   ↓                                                          │
│ MeetingRecorder.start(to: someFile.m4a)                      │
│   ├── SCStream (mic via .microphone)   ─┐                    │
│   ├── SCStream (system via .audio)      ┴─→ mixer → AVAsset… │
│   │   (BOTH upstream stay SCStream-based; mixer interleaves  │
│   │    mic L + system R into stereo CMSampleBuffer per chunk)│
│   ↓                                                          │
│ User triple-taps again → stop                                │
│   ↓                                                          │
│ MeetingAPI.submitFile(someFile.m4a, mode: .stereo)           │
│   ↓ same /transcribe/file endpoint                           │
└──────────────────────────────────────────────────────────────┘
```

### Key Pseudocode

**`server/src/wispralt_server/ops/staging.py` — new function:**

```python
import shutil
import subprocess
from pathlib import Path

# Map of allowed source extensions → MIME buckets we accept.
# Anything outside this set returns 415.
_ALLOWED_EXTENSIONS = frozenset({
    ".m4a", ".mp3", ".mp4", ".mov", ".m4v", ".wav", ".aac",
    ".flac", ".opus", ".ogg", ".webm", ".caf", ".aiff",
})

async def stream_to_staging_raw(
    file: UploadFile,
    max_bytes: int,
    staging_dir: Path,
) -> Path:
    """Like stream_to_staging but does NOT validate WAV header.
    Writes the upload to <staging_dir>/<uuid><ext> with the source extension
    preserved so ffmpeg can sniff the format."""
    ext = Path(file.filename or "upload.bin").suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(415, f"Unsupported file extension: {ext}")

    # Disk pre-check, mirroring stream_to_staging (staging.py:73-75).
    # Conservative: require 1.5x max_bytes free.
    _assert_enough_disk(staging_dir, max_bytes)

    out_path = staging_dir / f"{uuid4().hex}{ext}"
    bytes_written = 0
    # NO aiofiles — repo doesn't depend on it. Mirror the existing
    # stream_to_staging pattern at staging.py:83 (sync open inside async function).
    with open(out_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):  # 1 MB chunks
            bytes_written += len(chunk)
            if bytes_written > max_bytes:
                out_path.unlink(missing_ok=True)
                raise HTTPException(413, "Upload too large")
            f.write(chunk)
    return out_path

def ffprobe_channel_count(source: Path) -> int:
    """Return the audio channel count of `source` via ffprobe.
    Raises HTTPException 422 if the file has no audio stream or is unreadable."""
    if shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe not found on PATH — installed alongside ffmpeg")
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=channels",
        "-of", "default=nw=1:nk=1",
        str(source),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    if result.returncode != 0 or not result.stdout.strip():
        stderr_tail = "\n".join(result.stderr.splitlines()[-3:])
        raise HTTPException(422, f"Could not probe audio: {stderr_tail or 'no audio stream'}")
    try:
        return int(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        raise HTTPException(422, f"Unexpected ffprobe output: {result.stdout!r}")

def transcode_to_canonical_wav(
    source: Path,
    *,
    target_channels: int,  # 1 = mono (custom), 2 = stereo (meeting)
    sample_rate: int = 16_000,
) -> Path:
    """Run ffmpeg to convert source to canonical PCM WAV.
    Writes to a .partial temp name and atomically renames on success so a
    crash mid-ffmpeg never leaves a half-transcoded WAV that orphan-recovery
    would later flag as truncated."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH — install via 'brew install ffmpeg'")
    if target_channels not in (1, 2):
        raise ValueError(f"target_channels must be 1 or 2, got {target_channels}")

    target = source.with_suffix(".wav")
    if target == source:
        target = source.with_name(f"{source.stem}_canonical.wav")
    temp_target = target.with_suffix(target.suffix + ".partial")

    cmd = [
        "ffmpeg",
        "-y", "-nostdin",
        "-i", str(source),
        "-map", "0:a:0",         # explicit: same audio track ffprobe inspected
        "-vn",
        "-ac", str(target_channels),
        "-ar", str(sample_rate),
        "-acodec", "pcm_s16le",
        "-f", "wav",
        str(temp_target),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False,
        timeout=30 * 60,  # ceiling per single transcode
    )
    if result.returncode != 0:
        temp_target.unlink(missing_ok=True)
        stderr_tail = "\n".join(result.stderr.splitlines()[-5:])
        logger.error("ffmpeg transcode failed (rc=%d): %s", result.returncode, stderr_tail)
        raise HTTPException(422, f"Audio transcode failed: {stderr_tail}")
    if not temp_target.exists() or temp_target.stat().st_size < 100:
        temp_target.unlink(missing_ok=True)
        raise HTTPException(422, "Audio transcode produced empty/no output")

    # Atomic publish.
    os.replace(temp_target, target)
    logger.info("ffmpeg transcoded %s → %s (%d bytes)", source.name, target.name, target.stat().st_size)
    # NOTE: do NOT unlink `source` here — let the caller (`_run_source`) delete it
    # AFTER it has updated the row's wav_path to point at the canonical WAV.
    # Otherwise a crash between rename and row-update orphans the canonical WAV
    # while leaving the row pointing at a deleted source.
    return target
```

**`server/src/wispralt_server/routes/transcribe_file.py` — new route:**

**Critical structural decision (per round-1 review):** ffmpeg-transcode runs INSIDE the worker, NOT inside the request handler. A 90-min source file may take many seconds to ffmpeg-transcode; doing that synchronously inside FastAPI would block the request, exceed Cloudflare Tunnel's idle timeout, and starve other clients. The route just streams to staging and returns 202 immediately. The runner does ffprobe → transcode → pipeline.

**Mode inference, not a form field (per round-1 review):** Don't take a `mode` form field. The server runs `ffprobe` on the staged source to read its actual channel count. That's the source-of-truth for `force_single_channel`. Removes a client/server contract surface and a class of "client said single, sent stereo" bugs.

```python
from fastapi import APIRouter, Depends, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from ..auth import require_api_key
from ..config import settings
from .._errors import MeetingInProgressError
from ..jobs.runner import MeetingRunner
from ..ops import staging

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/transcribe/file",
    dependencies=[Depends(require_api_key)],
)

@router.post("", summary="Submit any audio/video file for transcription")
async def submit_file(
    request: Request,
    file: UploadFile,
    content_length: int | None = Header(None, alias="Content-Length"),
) -> JSONResponse:
    if content_length is not None and content_length > settings.max_upload_bytes:
        raise HTTPException(413, "Upload too large")

    src_path = await staging.stream_to_staging_raw(
        file,
        settings.max_upload_bytes,
        settings.staging_dir,
    )

    runner: MeetingRunner = request.app.state.meeting_runner
    try:
        # Submit the SOURCE file (still in its original container) to the runner.
        # The runner's _run does the ffmpeg transcode + pipeline + cleanup.
        jid = await runner.submit_source_or_429(src_path)
    except MeetingInProgressError as exc:
        src_path.unlink(missing_ok=True)
        return JSONResponse(
            {"error": str(exc), "retry_after_s": 60},
            status_code=429,
            headers={"Retry-After": "60"},
        )

    # Usage event recording is MIDDLEWARE-driven (main.py:415-453), not per-route.
    # The route doesn't call any recorder. Just register `transcribe/file` in
    # TRACKED_ROUTES + _KIND_MAP — see Task 4a.
    return JSONResponse({"job_id": jid, "status": "pending"}, status_code=202)
```

**`server/src/wispralt_server/jobs/runner.py` — new submit_source_or_429 + extended _run:**

```python
async def submit_source_or_429(self, src_path: Path) -> str:
    """Like submit_or_429 but the input is a NOT-YET-TRANSCODED source file.
    The worker (_run_source) runs ffprobe + ffmpeg before the pipeline.
    Mirrors the EXACT lock+check sequence from submit_or_429 (runner.py:78-104)
    — uses self._submit_lock + self._semaphore.locked() + RAM check + create
    + create_task. Do NOT introduce a separate lock or _gate() helper."""
    async with self._submit_lock:
        if self._semaphore.locked():
            raise MeetingInProgressError("A meeting transcription is already in progress.")
        if psutil.virtual_memory().available < _MIN_FREE_BYTES:
            raise MeetingInProgressError("Insufficient RAM for new meeting job.")
        jid = self.store.create(str(src_path))   # signature unchanged — wav_path holds source initially
        asyncio.create_task(self._run_source(jid, src_path))
        return jid

async def _run_source(self, jid: str, src_path: Path) -> None:
    """Worker for /transcribe/file submissions:
       ffprobe → ffmpeg transcode → existing _run pipeline.
       Status uses the existing 'pending' / 'running' / 'done' / 'failed' set —
       no new 'transcoding' status (would require updating every status consumer)."""
    try:
        self.store.set_running(jid)   # real method — auto-bumps attempts

        # 1. Detect channel count of source.
        channel_count = staging.ffprobe_channel_count(src_path)
        force_single = (channel_count == 1)
        target_channels = 1 if force_single else 2

        # 2. ffmpeg-transcode to canonical WAV. Runs in executor (subprocess.run blocks).
        loop = asyncio.get_running_loop()
        wav_path = await loop.run_in_executor(
            None,
            functools.partial(staging.transcode_to_canonical_wav,
                              src_path, target_channels=target_channels),
        )

        # 3. Persist the canonical wav_path + force_single_channel onto the row.
        # Use the new helper update_after_transcode (added in store.py).
        # This is the durability boundary — once committed, src_path can be deleted.
        self.store.update_after_transcode(
            jid, wav_path=str(wav_path), force_single_channel=force_single,
        )
        # 3b. NOW it's safe to delete the source — the row points at the new wav.
        src_path.unlink(missing_ok=True)

        # 4. Run the pipeline using the existing path.
        await self._run_pipeline(jid, wav_path, force_single_channel=force_single)

    except Exception as e:
        # Simpler than the earlier draft: ffmpeg's .partial cleanup happens
        # inside transcode_to_canonical_wav on its own failure path. If we
        # threw AFTER transcode succeeded but BEFORE update_after_transcode,
        # the canonical wav is on disk but the row still points at src_path —
        # the next recover_orphans run won't find it. That's an acceptable
        # rare-edge orphan; sweeping is handled by staging.sweep_old.
        self.store.set_failed(jid, repr(e))   # real method name (NOT mark_failed)
        src_path.unlink(missing_ok=True)

# The existing _run (used by /transcribe/meeting) is renamed to _run_pipeline and
# takes force_single_channel: bool = False. Both _run_source and the legacy
# submit_or_429 path call into it.

async def _run_pipeline(
    self, jid: str, wav_path: Path,
    *, force_single_channel: bool = False,
) -> None:
    # Renamed from `_run`. Existing body, plus the new keyword-only param
    # forwarded to the pipeline. Use the EXISTING dedicated executor
    # (self._executor — runner.py:149-150), NOT the default pool, so meeting
    # + custom-file jobs share the same RAM-gated worker.
    await loop.run_in_executor(
        self._executor,
        functools.partial(
            meeting_pipeline.transcribe_meeting,
            wav_path, output_dir, jid, silence_threshold,
            force_single_channel=force_single_channel,
        ),
    )

# CRITICAL — both legacy call sites must be updated in the same diff:
#  1. routes/meeting.py:84 currently calls runner.submit_or_429(); the existing
#     submit_or_429 path now calls _run_pipeline(...) (no force_single_channel)
#     instead of _run(...). Verify routes/meeting.py is unchanged (still hits
#     submit_or_429); only the runner-internal name changes.
#  2. runner.py:136 reenqueue_pending currently calls self._run(...). Update
#     to the new ext-routing logic shown below.
```

**`server/src/wispralt_server/jobs/store.py` — schema migration:**

The existing pattern at `:84-88` only handles the `attempts` column with a try/except OperationalError. The plan adds a slightly more explicit migration step:

```python
# In _ensure_schema or equivalent:
def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "force_single_channel" not in cols:
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN force_single_channel INTEGER NOT NULL DEFAULT 0"
        )
    # NOTE: NO `kind` column. Discrimination uses the file extension of wav_path:
    #   *.wav  → legacy meeting (or post-transcode file job) → _run_pipeline
    #   *.m4a/.mp3/etc → pre-transcode file job              → _run_source
    # This avoids a redundant column that would die after Phase 13 deprecation.
```

Also update:
- `Job` dataclass at `:30-41` — append `force_single_channel: bool = False`. Positional unpack via `Job(*row)` requires ordered append.
- Explicit column list in `get()` at `:144-150` and `list_active_jobs()` at `:166-172`. NEVER use `SELECT *` here (positional drift hazard).
- `create()` at `:99-109` keeps its current `(self, wav_path: str)` signature unchanged.
- ADD `update_after_transcode(self, jid: str, *, wav_path: str, force_single_channel: bool)` — single explicit method, not a generic `update_meta`. It does `UPDATE jobs SET wav_path = ?, force_single_channel = ? WHERE id = ?`.

**`reenqueue_pending` (`runner.py:106-136`) MUST route by file extension** — currently it calls `_run(job.id, Path(job.wav_path))` blind. After this change:
```python
ext = Path(job.wav_path).suffix.lower()
if ext == ".wav":
    # Either legacy meeting OR file-job that already transcoded — both go to pipeline.
    asyncio.create_task(self._run_pipeline(
        job.id, Path(job.wav_path),
        force_single_channel=bool(job.force_single_channel),
    ))
else:
    # File-job that crashed BEFORE transcode — re-run from source.
    asyncio.create_task(self._run_source(job.id, Path(job.wav_path)))
```

**Orphan-recovery safety:** `transcode_to_canonical_wav` writes to a `<uuid>.partial` temp and `os.replace()` to the final `<uuid>.wav` so a crash mid-ffmpeg leaves no half-transcoded file. **`recover_orphans` in store.py:194+** must skip `validate_wav_completeness` when the file extension is not `.wav` (file-job pre-transcode case) — currently it'd reject m4a as truncated. Add a one-line check at the top of the recover loop.

**`server/src/wispralt_server/meeting/pipeline.py` — single-channel branch (matching the actual module split):**

The existing 2-channel block uses `_df_mod.deepfilter` (denoise) → `_wx_mod.transcribe_channel` → `_diarize_mod.diarize` → `whisperx.assign_word_speakers` → `relabel_in_person` (see `pipeline.py:371-417`). The single-channel branch mirrors the same pipeline applied to one stream. **There is NO `_wx_mod.transcribe`/`_wx_mod.diarize` — those are inventions; use the real names.** Likewise `_build_transcript` already accepts a mode parameter — no new builder needed.

```python
def transcribe_meeting(
    wav_path: Path,
    output_dir: Path,                        # required, positional (matches real signature)
    job_id: str,                             # required, positional
    silence_threshold: float,                # required, positional — real signature is float NOT float | None
    *,
    force_single_channel: bool = False,      # keyword-only at end (per round-1 review)
) -> dict:
    # ... existing setup ...
    return _transcribe_meeting_inner(
        wav_path, output_dir, job_id, silence_threshold,
        force_single_channel=force_single_channel,
    )

def _transcribe_meeting_inner(
    wav_path, output_dir, job_id, silence_threshold,
    *, force_single_channel: bool,
):
    if force_single_channel:
        mono_raw, src_sr = _load_mono(wav_path)
        mono_16k = _resample_to_16k(mono_raw, src_sr)
        # Mirror the real per-channel pipeline at :371-417 but for ONE stream.
        mono_clean = _df_mod.deepfilter(mono_16k)            # denoise (same as 2ch path)
        mono_result = _wx_mod.transcribe_channel(mono_clean) # real helper name
        # Diarization decision (per round-1 review): for single-channel custom
        # transcriptions, default to NO diarization (typical input is one speaker:
        # voice memo, dictation, interview from one mic). Reuse the existing
        # `label_all` (no leading underscore — see merge.py:56 / pipeline.py:383):
        mono_segments = label_all(
            mono_result,
            display_name="Speaker 1",
            channel=None,
            raw_speakers=["mic"],
        )
        # Reuse the REAL `_build_transcript(job_id, mode, segments, audio_16k)`
        # signature at pipeline.py:259-264. There is no segments_by_channel,
        # no output_dir, no silence_threshold parameter — those were inventions
        # of an earlier draft. File writing happens in write_outputs_atomic.
        transcript = _build_transcript(
            job_id=job_id,
            mode="single",   # new mode literal — add to the accepted set
            segments=mono_segments,
            audio_16k=mono_16k,
        )
        write_outputs_atomic(transcript, output_dir, job_id)
        return transcript
    else:
        # Existing 2-channel path, unchanged. Note: the existing mono-warning probe
        # at :352 (sf.SoundFile to read channel count) must be GATED behind
        # `not force_single_channel` so it doesn't fire confusingly on the new path.
        ch1_raw, ch2_raw, src_sr = _load_channels(wav_path)
        # ... existing body ...

def _load_mono(wav_path: Path) -> tuple[np.ndarray, int]:
    """Read a WAV (any channel count) and collapse to a single mono stream."""
    audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
    if audio.shape[1] > 1:
        audio = audio.mean(axis=1)   # average across channels
    else:
        audio = audio[:, 0]
    return audio, sr
```

**Note on `_build_transcript`:** the real function (`pipeline.py:259-264`) takes `(job_id, mode, segments, audio_16k)` — flat segment list, no `segments_by_channel`, no `output_dir`, no `silence_threshold`. File writing is the separate `write_outputs_atomic(transcript, output_dir, job_id)` call (`pipeline.py:432`). The v3 schema is segment-list-based with a separate `speakers_table`, NOT channel-keyed, so adding `"single"` to the accepted mode literal is structurally compatible. Verify the four output formatters (txt/srt/vtt/json) handle a `mode="single"` transcript with one speaker before declaring this done.

**`client/WisprAlt/Server/MeetingAPI.swift` — add submitFile (NO `mode` field — server infers via ffprobe):**

```swift
extension MeetingAPI {
    /// Upload any audio/video file as-is to /transcribe/file.
    /// Mirrors `submit(_:)` in shape: same multipart, same Content-MD5,
    /// same delegate-based progress, same 6-hour upload timeouts.
    /// The server runs ffprobe to detect channel count and chooses single/stereo
    /// pipeline accordingly — clients never declare mode.
    static func submitFile(
        _ fileURL: URL,
        progress: ((Double) -> Void)? = nil
    ) async throws -> JobID {
        // 1. Build the multipart body identically to submit(), but POST to /transcribe/file
        //    and use the ORIGINAL file URL (no transcoding — `fileURL` is the user's source).
        // 2. Set request.timeoutInterval = 6 * 60 * 60 (same patch we just applied to submit()).
        // 3. URLSessionConfiguration with timeoutIntervalForRequest = 300, ForResource = 6h.
        // 4. Wrap in UploadSessionDelegate exactly like submit() for progress + continuation.
        // 5. Decode SubmitResponse → JobID exactly like submit().
    }
}
```

**`client/WisprAlt/Capture/MeetingRecorder.swift` — AVAssetWriter rewrite (sketch):**

**IMPORTANT round-1-review caveat:** `AVAssetWriter` only handles framing/encoding. The cross-input alignment problem that `AlignedRingBuffer` solved (mic SCStream callbacks vs system SCStream callbacks landing at different wall-clock instants) STILL EXISTS in the new path — the new mixer must time-align two independent producers before passing chunks to `input.append(_:)`. Whether to keep `AlignedRingBuffer.swift` (and just change its output sink from `AVAudioFile.write` to `AVAssetWriterInput.append`) vs build a smaller mixer is a Phase-6-diagnosis decision. The default working assumption is **keep AlignedRingBuffer** for now and only swap the downstream sink — that is the minimum change consistent with eliminating the WAV-write path.

**Both upstream callbacks remain `SCStream`-based** (mic via `SCStreamOutputType.microphone`, system via `SCStreamOutputType.audio`). SCStream delivers `CMSampleBuffer`s directly, which is exactly what `AVAssetWriterInput.append(_:)` consumes — no `AVAudioPCMBuffer` round-trip needed for the system stream. Mic samples may need rebuilding into a CMSampleBuffer with a coherent timestamp clock.

```swift
final class MeetingRecorder {
    private var assetWriter: AVAssetWriter?
    private var audioInput: AVAssetWriterInput?
    private var stream: SCStream?
    // mic + system queue holders (kept ~as-is from current implementation)
    // Optional: keep AlignedRingBuffer with append(_:) sink swapped to AVAssetWriterInput.

    func start(to outputURL: URL) async throws {
        let writer = try AVAssetWriter(outputURL: outputURL, fileType: .m4a)

        // GOTCHA round-1 #10: kAudioFormatMPEG4AAC is UInt32 — must Int().
        // GOTCHA round-1 #10: AVChannelLayoutKey requires a serialized
        //   AudioChannelLayout struct, not just the tag UInt32.
        var layout = AudioChannelLayout()
        layout.mChannelLayoutTag = kAudioChannelLayoutTag_Stereo
        let layoutData = Data(bytes: &layout, count: MemoryLayout<AudioChannelLayout>.size)

        let outputSettings: [String: Any] = [
            AVFormatIDKey:         Int(kAudioFormatMPEG4AAC),
            AVSampleRateKey:       48_000,
            AVNumberOfChannelsKey: 2,
            AVEncoderBitRateKey:   96_000,
            AVChannelLayoutKey:    layoutData,
        ]
        let input = AVAssetWriterInput(mediaType: .audio, outputSettings: outputSettings)
        input.expectsMediaDataInRealTime = true
        writer.add(input)

        guard writer.startWriting() else { throw RecorderError.writerFailed(writer.error) }
        writer.startSession(atSourceTime: .zero)

        self.assetWriter = writer
        self.audioInput = input

        // SCStream wiring stays as today (`SCStreamOutputType.microphone` + `.audio`),
        // but the per-callback handler appends the stereo-interleaved CMSampleBuffer
        // to `input` instead of writing to AVAudioFile via AlignedRingBuffer.
        // Pre-existing 200ms gap-tolerance logic stays in the picture; it's just feeding
        // a different sink.
    }

    func stop() async throws -> URL {
        audioInput?.markAsFinished()
        guard let writer = assetWriter else { throw RecorderError.notStarted }
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            writer.finishWriting { cont.resume() }   // finishWriting is callback-based; await wrap
        }
        guard writer.status == .completed else {
            throw RecorderError.writerFailed(writer.error)
        }
        return writer.outputURL
    }
}
```

### Data Models and Structure

Server: existing `MeetingJob` model in the SQLite store gets an extra column:
```python
force_single_channel: bool = False
```

Client: `MeetingAPI.SubmissionMode` enum (above). No other model changes.

### Tasks (in implementation order)

Task 1 — Server: ffmpeg sanity check at startup:
Goal:
- Fail fast at server startup if ffmpeg is missing. Cheaper than diagnosing a 500 from a custom-transcription request later.
Files:
- MODIFY `server/src/wispralt_server/main.py` (add `shutil.which("ffmpeg")` check during `lifespan` startup; raise if missing).
Pattern to copy:
- The existing model-readiness checks in `main.py`.
Definition of done:
- Removing `ffmpeg` from PATH causes server start to fail with a clear log line. Re-installing fixes startup.

Task 2 — Server: add `stream_to_staging_raw` + `transcode_to_canonical_wav`:
Goal:
- Two new helpers in `staging.py` (per Key Pseudocode above). Pure functions; no route hookup yet.
Files:
- MODIFY `server/src/wispralt_server/ops/staging.py`.
Pattern to copy:
- Existing `stream_to_staging` for the chunked-write idiom.
Gotchas:
- ALWAYS pass `cmd` as a list, never a string (no `shell=True`).
- `-y -nostdin` flags critical.
- Surface ffmpeg stderr tail in the HTTPException detail so users see the actual reason (e.g. "Codec h264 not supported").
Definition of done:
- Manual: in a Python REPL, call `transcode_to_canonical_wav(Path("test.mp3"), target_channels=1)` and verify a 16 kHz mono PCM WAV is produced.

Task 3 — Server: extend `submit_or_429`, pipeline, and store for `force_single_channel`:
Sub-DoD items the implementer MUST do in the SAME commit (failing to do these breaks the legacy meeting path):
- Rename runner's `_run` → `_run_pipeline`. Update BOTH call sites in the same diff: `submit_or_429` (legacy meeting path) AND `reenqueue_pending`.
- `reenqueue_pending` route by extension: `.wav` → `_run_pipeline(force_single_channel=job.force_single_channel)`; non-`.wav` → `_run_source(...)`. `list_pending_ids` is unchanged (it returns ids only; `get()` is what unpacks the new column).
- `Job` dataclass append `force_single_channel: bool = False` LAST so `Job(*row)` positional unpack still works.
- Explicit column list in `get()` and `list_active_jobs()` SELECTs (NEVER `SELECT *`).
- Add `update_after_transcode(jid, *, wav_path, force_single_channel)` method.
- `recover_orphans` (`store.py:194+`): add `if Path(row.wav_path).suffix.lower() != ".wav": requeue.append(jid); continue` BEFORE the `validate_wav_completeness` block. Without this, orphaned `.m4a` source files crash recovery.
- `meeting/output.py` formatters (txt/srt/vtt/json): confirm they branch on `mode` only at known sites OR add `"single"` to any mode whitelist.
Goal:
- Plumb the flag from `MeetingRunner.submit_or_429` → `_run` → `meeting_pipeline.transcribe_meeting` → new single-channel branch in `_transcribe_meeting_inner`.
Files:
- MODIFY `server/src/wispralt_server/jobs/runner.py`.
- MODIFY `server/src/wispralt_server/jobs/store.py` (add `force_single_channel` column via in-place ALTER; append to `Job` dataclass; explicit-column SELECTs; new `update_after_transcode`; extension-skip in `recover_orphans`).
- MODIFY `server/src/wispralt_server/meeting/pipeline.py` (add `_load_mono` + single-channel branch in `_transcribe_meeting_inner` reusing real `label_all` + real `_build_transcript` — NO new `_build_transcript_single`).
- MODIFY `server/src/wispralt_server/meeting/output.py` (verify/extend formatters for `mode="single"`).
Pattern to copy:
- Existing `_load_channels` for the soundfile + `always_2d=True` idiom.
Gotchas:
- Whisperx `transcribe` and `diarize` calls take an audio array; `assign_word_speakers` produces a per-word speaker tag. Single-channel still gets multi-speaker output (pyannote distinguishes voices by acoustic features).
- The existing `_build_transcript` builds a per-channel structure; the single-channel variant needs a flat structure with a `speakers` list rather than `ch1`/`ch2` keys. Confirm the txt/srt/vtt/json templates handle this — extend templates if not.
Definition of done:
- A 60-second mono WAV runs end-to-end through `transcribe_meeting(..., force_single_channel=True)` and produces all four output formats. Smoke test from server-side Python only — no client involvement yet.

Task 4a — Server: register the new route in usage telemetry (code-only, no schema migration):
Goal:
- Wire `/transcribe/file` into the existing usage-events middleware so Phase 13's deprecation gate can query it. The `usage_events` table on Supabase already has a `kind` column; this is a code-only change to add the new route to the middleware's tracked set + kind mapping.
Files:
- MODIFY `server/src/wispralt_server/main.py:389` — `TRACKED_ROUTES = frozenset(["transcribe/dictate", "transcribe/meeting", "transcribe/file", "v1/audio"])`.
- MODIFY `server/src/wispralt_server/main.py:394` — `_KIND_MAP` add `"transcribe/file": "file"`.
Pattern to copy:
- The existing entries in TRACKED_ROUTES + _KIND_MAP at main.py:389-398.
Gotchas:
- NO Supabase schema migration. The `kind` column already exists; we just emit `kind='file'` for the new route's events.
- Phase 13's deprecation query becomes `SELECT count(*) FROM usage_events WHERE kind = 'meeting' AND created_at > NOW() - INTERVAL '3 days'` — using the existing column.
- This must land BEFORE the new route is deployed so the route's first request gets a proper kind label.
Definition of done:
- After deploy, a curl to `/transcribe/file` produces a `usage_events` row with `kind = 'file'`. Smoke test with one curl + one Supabase query.

Task 4 — Server: add `POST /transcribe/file` route:
Goal:
- Wire the new route per Key Pseudocode. Register it in the FastAPI app alongside the existing meeting router.
Files:
- CREATE `server/src/wispralt_server/routes/transcribe_file.py`.
- MODIFY `server/src/wispralt_server/main.py` (register the new router).
Pattern to copy:
- `routes/meeting.py:53-86` for the multipart + auth shape.
Gotchas:
- The poll/download/delete endpoints stay on `/transcribe/meeting/{job_id}/...` — both clients use those same job-lifecycle paths regardless of which submission route they used. Confirm the new client code reuses `MeetingAPI.poll`/`download`/`delete` unchanged.
- Test 415 (unsupported extension), 413 (too large), 422 (ffmpeg failed), 429 (job in progress) paths individually.
Definition of done:
- Local curl test: `curl -F "file=@test.m4a" -F "mode=single" -H "Authorization: Bearer $KEY" http://localhost:8000/transcribe/file` returns a 202 with a job_id; `GET /transcribe/meeting/$JOB_ID` eventually returns `done`.

Task 5 — Server: deploy to prod-mini + smoke test:
Goal:
- Deploy via the established tarball-via-gist + `/macmini` chrome-devtools/CRD flow (per CLAUDE.local.md), `launchctl kickstart`, smoke test against `https://transcribe.integrateapi.ai/transcribe/file`.
Files:
- (no source changes) — deploy artifact only.
Concrete deploy command sequence (so the autonomous agent doesn't improvise):
```
# 1. Build the deploy tarball locally (excludes .git, __pycache__, models, .env).
cd /Users/omidzahrai/Desktop/CODEBASES/TOOLS/wisprflowALT
tar -czf /tmp/wf-deploy.tar.gz \
    --exclude=__pycache__ --exclude=.git --exclude=node_modules \
    --exclude=client/build --exclude=client/.build \
    --exclude=server/.venv --exclude=server/staging \
    server scripts

# 2. Upload as a gist file (NOT clipboard — it's >32 KB).
gh gist create --filename wf-deploy.tar.gz.b64 -d "wf-deploy" \
    <(base64 < /tmp/wf-deploy.tar.gz)
# Capture the printed gist URL → extract gist-id (the trailing hex).

# 3. Drive prod-mini Terminal via /macmini chrome-devtools/CRD.
#    Type the following one-liner (gist-id substituted, validated as [a-f0-9]{32}):
#    cd ~/wispralt && \
#      mv -f .wf-deploy-backup-$(date +%s) /tmp/ 2>/dev/null; \
#      cp -r server scripts .wf-deploy-backup-$(date +%s) 2>/dev/null; \
#      gh gist clone <GIST_ID> /tmp/wf-deploy && \
#      base64 -d < /tmp/wf-deploy/wf-deploy.tar.gz.b64 | tar -xzf - -C ~/wispralt && \
#      command -v ffmpeg && ffmpeg -version | head -1 && \
#      launchctl kickstart -k "gui/$(id -u)/co.wispralt.server"

# 4. Wait ~10 s for model warm-up. Then probe:
curl -sS -o /dev/null -w "healthz: %{http_code}\n" https://transcribe.integrateapi.ai/healthz
# Until it returns 200. Smoke test:
curl -sS -F "file=@/tmp/test-mono-5s.m4a" -H "Authorization: Bearer $WISPRALT_API_KEY" \
    https://transcribe.integrateapi.ai/transcribe/file
# Then poll the returned job_id via /transcribe/meeting/{id} until done.
```
Gotchas:
- Verify ffmpeg presence on the prod-mini BEFORE the kickstart (the new startup check from Task 1 will hard-fail and respawn-loop the launchd job if ffmpeg is missing).
- Backup directory `.wf-deploy-backup-<epoch>` follows the existing convention; use it for rollback.
- Gist-id regex check before substitution: `[a-f0-9]{32}`.
Definition of done:
- Single-mode (mono m4a) and stereo-mode (2-channel m4a) round-trip from this dev box, both return real transcript text within 60 s. Backup dir present on prod-mini.

Task 6 — Client: switch custom-transcription path to `submitFile` (lands BEFORE recorder rewrite per brief order): [x] DONE 2026-05-09
Goal:
- Ship the upload-bloat + audio-quality wins for the user IMMEDIATELY. This is the low-risk, high-value piece. Do this BEFORE touching the meeting recorder so the user gets value even if the recorder rewrite turns into a multi-day investigation.
- `MenuBarController.handlePickedFile` no longer transcodes. It copies the picked file into the per-job folder (preserving original filename and bytes) and uploads via `MeetingAPI.submitFile`.
- `MediaTranscoder.swift` is DELETED (after grep confirms zero call sites outside MenuBarController — round-1 review #5).
- `processCustomTranscriptionUpload` calls `MeetingAPI.submitFile` instead of `submit`.
- Per-job folder layout becomes: `Custom Transcriptions/<stem>__<ts>/{<original-filename>, <stem>.{txt,srt,vtt,json}}`.
Files:
- MODIFY `client/WisprAlt/Server/MeetingAPI.swift` (add `submitFile`).
- MODIFY `client/WisprAlt/App/MenuBarController.swift` (handlePickedFile + processCustomTranscriptionUpload + the runMeetingTranscriptionJob helper now takes a "submit closure" so meeting and custom can both use it; OR: split into runFileTranscriptionJob).
- DELETE `client/WisprAlt/Audio/MediaTranscoder.swift` (after grep `rg "MediaTranscoder" client/` returns ONLY MenuBarController hits).
Gotchas:
- Bytes-per-second math in the polling-deadline calculation is now meaningless (we don't know the audio duration from container size). Use a flat 600-second deadline per job; the server processes in seconds-of-audio time, not bytes time.
- The upload `Content-MD5` calculation runs on the ORIGINAL container bytes, not a transcoded WAV. MD5 is just integrity, not format.
- Per-job folder still gets created via `CustomTranscriptionsStore.makeJobDirectory`. WAV step is removed; just `try FileManager.default.copyItem(at: picked, to: subdir.appendingPathComponent(picked.lastPathComponent))`.
Definition of done:
- Pick a 30-second m4a → per-job folder contains the m4a (byte-identical copy of source) + the four transcript files. No WAV anywhere. End-to-end completes in <60 s.

Task 7 — Client: build, install, smoke-test the custom path:
Goal:
- `./scripts/build-client-local.sh`, install to /Applications, run the custom-transcription scenarios in Validation > Manual Checks. Confirm the user's 90-min m4a now uploads + transcribes successfully (THE headline fix).
Files:
- (no source changes)
Gotchas:
- This is the gating decision-point for the user. If the custom path works, brief value is delivered; meeting recorder rewrite can take its time.
Definition of done:
- 90-min m4a → m4a in per-job folder + transcript text. Upload + transcription < 30 min wall clock on user's home uplink.

Task 8 — Diagnose meeting-recorder voice-cutting bug (BLOCKING gate before Task 9):
Goal:
- 30-min focused diagnosis BEFORE the recorder rewrite. Capture: one fresh repro WAV (15-30 sec, deliberate normal speech), the live `co.wispralt:capture` log stream during that recording, AlignedRingBuffer pad-with-silence event count.
Files:
- (no source changes — investigation only)
Pattern to follow:
- `tail` the unified log: `/usr/bin/log stream --predicate 'subsystem == "co.wispralt"' --info --debug > /tmp/wispralt-glitch.log`. Triple-tap-FN, speak normally for 20 s, triple-tap-FN. Inspect log for `AlignedRingBuffer` warnings + `MeetingRecorder` events. Listen to the produced WAV in QuickTime / `afplay`.
Outcomes (decide which):
- (a) AlignedRingBuffer pad events visible in log AND symptom matches → recorder change IS the fix; proceed to Task 10. The minimum change is to swap `AlignedRingBuffer`'s output sink from AVAudioFile.write to AVAssetWriterInput.append — KEEP AlignedRingBuffer (don't delete its file in Task 10).
- (b) No pad events but symptom present → upstream (mic SCStream callback or sample-rate mismatch) issue; the AAC framing won't help and we need a separate diagnostic loop. Stop and discuss with user before starting Task 10.
- (c) Symptom not reproducible → can't fix what we can't see. Document and move on.
Definition of done:
- One paragraph diagnosis written into a new `tmp/notes/2026-05-09-glitch-diagnosis.md`. Decision recorded: continue Task 10, OR pause for separate fix.

Task 9 — Client: rewrite `MeetingRecorder` around `AVAssetWriter`:
Goal:
- Swap the downstream sink from AVAudioFile.write (raw PCM WAV) to AVAssetWriterInput.append (AAC m4a). Keep `SCStream` as the upstream for both mic and system. Decision on AlignedRingBuffer fate per Task 9 outcome — default keep, just change its sink.
Files:
- MODIFY `client/WisprAlt/Capture/MeetingRecorder.swift`.
- POSSIBLY MODIFY `client/WisprAlt/Capture/AlignedRingBuffer.swift` (only if changing its sink type; or leave alone if the new mixer lives in MeetingRecorder).
- DO NOT delete AlignedRingBuffer.swift in this task — defer deletion to a follow-up commit after Task 10 validates the new recorder ships glitch-free for one user-confirmed meeting (per round-1 review #22).
Pattern to copy:
- Apple AVAssetWriter docs. The existing SCStream wiring at `MeetingRecorder.swift:78, 195-228` for the upstream side.
Gotchas:
- `startWriting()` BEFORE `startSession(atSourceTime:)` BEFORE `append(_:)`.
- `expectsMediaDataInRealTime = true`.
- `outputSettings`: `AVFormatIDKey: Int(kAudioFormatMPEG4AAC)`. `AVChannelLayoutKey: Data(bytes: &layout, count: ...)` where `layout: AudioChannelLayout` has `mChannelLayoutTag = kAudioChannelLayoutTag_Stereo`. (Per round-1 review #10 — both type fixes load-bearing.)
- `finishWriting` is callback-based — wrap in `withCheckedContinuation`.
- Output filename: change extension from `.wav` to `.m4a` in `humanReadableMeetingFilename` (`MenuBarController.swift:753`).
- SCStream delivers `CMSampleBuffer`s natively → no AVAudioPCMBuffer round-trip; pass directly to `input.append(_:)` after stereo interleaving.
Definition of done:
- A 60-second meeting recording produces an m4a roughly 600-800 KB. `afplay` on the m4a plays mic L + system R cleanly. No glitches (per Task 9 outcome).

Task 10 — Client: switch meeting upload path to `submitFile`:
Goal:
- `MenuBarController.processMeetingUpload` calls `MeetingAPI.submitFile` instead of `submit`.
- `PendingUploadsQueue.replay` upgraded for migration safety: at startup, ANY queued `.wav` entries are replayed via the LEGACY `submit()` path (still alive in this release — `/transcribe/meeting` server endpoint unchanged) so existing offline-recorded meetings are not lost. Only new entries (post-Task-10) are `.m4a` and replayed via `submitFile`. The legacy WAVs stay queued until either successfully uploaded or the queue is manually flushed by the user (per round-1 review #13 — multi-user safety).
Files:
- MODIFY `client/WisprAlt/App/MenuBarController.swift`.
- MODIFY `client/WisprAlt/Storage/PendingUploadsQueue.swift`.
Gotchas:
- The dual-replay logic must inspect file extension and route accordingly. `.wav` → legacy `submit()` (uses `/transcribe/meeting`). `.m4a` → `submitFile()` (uses `/transcribe/file`).
- A successful legacy WAV upload removes its queue entry AND deletes the on-disk WAV. A failed legacy WAV upload keeps the entry until the next replay.
- After Task 12 (server endpoint deletion), this dual logic can be simplified — but only after employees have rolled forward.
Definition of done:
- A meeting recording → m4a → uploaded via /transcribe/file → transcript appears in meetings folder. Old queued `.wav` (if any) successfully drains via the legacy path on next replay tick.

Implementation note (Task 10 — done):
- `processMeetingUpload` now calls the existing `runFileTranscriptionJob` (added in Task 6) instead of `runMeetingTranscriptionJob`. The old `runMeetingTranscriptionJob` helper and its `bytesPerSecond` deadline math have been deleted from `MenuBarController` since they had no remaining callers — the meeting and custom-transcription paths now share one helper.
- `PendingUploadsQueue` generalized: `enqueue` preserves the source extension (m4a or wav), `pending`/`count` enumerate both, and `drainOnce` routes by ext (`.wav` → `MeetingAPI.submit`, `.m4a` → `MeetingAPI.submitFile`). Legacy WAV entries from any pre-Task-9 client are NOT silently dropped.

Task 11 — Client: build, install, full smoke test:
Goal:
- `./scripts/build-client-local.sh`, install to /Applications, run all scenarios in Validation > Manual Checks.
Files:
- (no source changes)
Definition of done:
- All Validation > Manual Checks scenarios pass.

Task 12 — Server: deprecate `/transcribe/meeting` (after rollout):
Goal:
- After the new client is deployed to all employees AND `usage_events` confirms zero hits on `/transcribe/meeting` POST for the rollout window (recommend 3-7 days for this small user base — per round-1 review #20 — not the original 7-day blanket), delete the old route's POST handler.
Files:
- MODIFY `server/src/wispralt_server/routes/meeting.py` — DELETE the POST handler ONLY. KEEP the GET poll, GET download, DELETE handlers — both clients use those `/transcribe/meeting/{job_id}/*` paths as the canonical job-lifecycle surface regardless of which submission route created the job.
- MODIFY `client/WisprAlt/Server/MeetingAPI.swift` — remove the now-unused `submit()` method.
- DELETE `client/WisprAlt/Capture/AlignedRingBuffer.swift` IF Task 10's recorder rewrite eliminated all references to it AND user-confirmed glitch-free.
Definition of done:
- `usage_events` SQL using the existing `kind` column: `SELECT count(*) FROM usage_events WHERE kind = 'meeting' AND created_at > NOW() - INTERVAL '3 days'` returns 0. POST handler deleted. Verify `/transcribe/meeting/{job_id}/poll` etc. still work via curl.

### Integration Points

- Data / schema source of truth: `meeting_pipeline._build_transcript` (existing — extend mode literal to include `"single"`, no new builder).
- Entry points to extend: `routes/transcribe_file.py` (new); `MeetingAPI.submitFile` (new); usage_events `endpoint` column (new — Task 4a).
- Validation layer: HTTPException with explicit status codes (415 ext, 413 size, 422 ffmpeg, 429 busy).
- Domain / service layer: `MeetingRunner` (extended), `meeting_pipeline.transcribe_meeting` (extended).
- User-facing surface: meetings folder filename extension change (`.wav` → `.m4a`); custom-transcription per-job folder contents change (original file vs WAV).
- Shared types / export hubs: none.
- External / operational hooks: ffmpeg sanity check at server startup.

## Validation

```bash
# Server
cd /Users/omidzahrai/Desktop/CODEBASES/TOOLS/wisprflowALT/server
ruff check src/
pyright src/
pytest -x

# Client
( cd /Users/omidzahrai/Desktop/CODEBASES/TOOLS/wisprflowALT/client && swift build -c debug )

# End-to-end smoke (after Task 5 deploy + Task 11 build):
# 1. Custom mono — pick a 30-second m4a via "Transcribe file…" → wait → check transcript
# 2. Custom stereo — pick a 30-second mp4 → wait → check transcript
# 3. Meeting — triple-tap-FN, speak 20 s, triple-tap-FN → check m4a + transcript
# 4. Long custom — pick a 90-min m4a → upload completes within ~30 min on home uplink
# 5. Glitch test — re-record the meeting that previously had voice-cutting → playback is clean
```

### Factuality Checks

- All Verified Repo Truths use `Fact / Evidence / Implication` ✓
- Every MODIFY/DELETE path checked against actual codebase ✓
- No proposal language in Verified Repo Truths ✓

### Manual Checks

- Scenario: Pick a 90-min m4a via Transcribe file… → uploads in <30 min → transcript appears. Per-job folder contains the m4a (~50 MB), not a WAV.
- Scenario: Triple-tap-FN, record a 60-second meeting, release. Output is `~/Documents/WisprAlt/Meetings/<humanName>.m4a` (~600-800 KB), transcript files appear, `afplay` confirms mic L + system R cleanly.
- Scenario: Server is down (kill prod-mini's launchd). Triple-tap-FN, record, release → enqueued in `PendingUploadsQueue`, no client crash. Restart server. Replay fires → m4a uploads to /transcribe/file → transcript appears.
- Scenario: Pick a `.mov` file with no audio track. Server returns 422 with a clear ffmpeg-stderr-tail message; client surfaces "Audio transcode failed: <reason>" toast.
- Scenario: An old client (still on the just-shipped build that uses `/transcribe/meeting`) can still upload a WAV during migration. Once `/transcribe/meeting` is deleted in Phase 5, that old client breaks — coordinate with employees before Phase 5 land.

## Open Questions

- None blocking. The single decision deferred to a runtime check is whether single-channel diarization in pyannote produces useful speaker tags; if it doesn't, single-channel transcripts may render as one giant block of text. Acceptable for v1; future improvement.

## Final Validation Checklist

- [ ] No linting errors: `ruff check src/`, `pyright src/`
- [ ] No type errors: `swift build -c debug` (client)
- [ ] Server pytest passes
- [ ] All Manual Checks scenarios pass
- [ ] Verified Repo Truths bullets all link to evidence
- [ ] Migration order honored: server (Tasks 1–5), custom client cutover (Tasks 6–7), diagnose (Task 8), recorder rewrite (Task 9), meeting cutover (Task 10), full smoke (Task 11), server endpoint deletion (Task 12)
- [ ] Task 8 (diagnose glitch) decision documented in `tmp/notes/2026-05-09-glitch-diagnosis.md`
- [ ] No leftover WAV files in `/Applications/WisprAlt.app` resources after Task 12; PendingUploadsQueue's legacy WAVs drained via dual-replay (not silently dropped)

## Deprecated / Removed Code

- `client/WisprAlt/Audio/MediaTranscoder.swift` — entire file (no client transcoding).
- `client/WisprAlt/Capture/AlignedRingBuffer.swift` — entire file (AVAssetWriter owns framing).
- `server/src/wispralt_server/routes/meeting.py` POST handler — Phase 5, after employee rollout.
- `client/WisprAlt/Server/MeetingAPI.swift::submit()` method — Phase 5.

## Anti-Patterns to Avoid

- Don't try to ffmpeg-decode in-memory via `subprocess.PIPE` for large files — produces OOMs at 500 MB+ inputs. Always temp-file based.
- Don't re-implement AlignedRingBuffer-style sample-position tracking for the new recorder — `AVAssetWriter` does it.
- Don't use `subprocess.run(..., shell=True)`. Always arg list.
- Don't silently swallow ffmpeg stderr. Log it, surface a tail to the user via the HTTPException body.
- Don't break the migration order. Server endpoint MUST be live before client paths switch to it.
- Don't use `AVAudioConverter` for channel-mix anywhere (sums-without-averaging bug); only sample-rate alignment is OK.
- Don't add a `mode` form field to the new route — server infers via ffprobe (per round-1 review). Client sends just the file.
- Don't introduce a `kind` column in the SQLite job store — use the file extension of `wav_path` as the discriminator (per round-2 review #10).
- Don't introduce a new `transcoding` job status — reuse `running` (set via `set_running`) so all status-consuming code paths still work.
- Don't delete `AlignedRingBuffer.swift` until Task 12. Default plan is to keep it and just swap its sink in Task 9.
- Don't migrate existing `.wav` meetings on disk — leave them as-is; new recordings are `.m4a`.
- Don't drop legacy queued WAVs from `PendingUploadsQueue` on update — dual-replay so offline recordings are not lost (per round-1 review #13).
- Don't use `SELECT *` in `JobStore.get()` / `list_active_jobs()` after appending the new column — explicit column list, positional unpack drift hazard.

## Confidence

8/10 for one-pass implementation success after round-1 fixes.

Risk drivers:
- Recorder rewrite is the biggest remaining unknown — `AVAssetWriter` swap depends on Task 8 diagnosis. The plan now defaults to keeping AlignedRingBuffer (just changing its sink), which is the smallest change consistent with the goal.
- Pipeline single-channel branch reuses the existing `_build_transcript` and `_label_all` machinery — no new builder needed (per round-1 review #8). Risk: the `"single"` mode literal may surface schema edge cases in the four output formats (txt/srt/vtt/json templates).
- Cloudflare Tunnel still has SOME upper limit; AAC 96 kbps stereo for 90 min ≈ 65 MB, well under the empirical ~100 MB threshold.
- Operational coordination across employees for Phase 13 deprecation — small user base, 3-day window is realistic.
- Worker-runs-ffmpeg architecture (per round-1 review #17) means the route returns 202 fast — no Cloudflare Tunnel idle-timeout risk on the request path. ffprobe + ffmpeg run inside `_run_source` in the executor pool.
