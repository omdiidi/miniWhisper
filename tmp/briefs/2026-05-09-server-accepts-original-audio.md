# Brief: Server accepts original audio/video — kill client-side WAV transcoding everywhere

## Why

The current pipeline forces every audio path through 2-channel 16 kHz Int16 PCM WAV. This is a leftover from the live meeting recorder, where capturing raw dual-channel PCM made sense. Applied uniformly, it's making the product worse on three observed dimensions:

1. **Upload bloat.** A 90-minute m4a (~50 MB) becomes a 387 MB WAV. At a typical home uplink (~15 KB/s observed), upload time goes from ~1 hour to ~7 hours. For custom transcriptions of long-form audio, this kills the feature.
2. **Audible quality loss in stored audio.** WAV at 16 kHz mono-fanned-out-to-stereo (mono signal duplicated into both channels) is what we hand back to the user as the "original" alongside their transcript. They listen to it and notice — high frequencies above 8 kHz are gone, stereo image is gone. The m4a they started with sounded better. (Whisper's *transcription quality* is unaffected because it runs at 16 kHz mono internally regardless.)
3. **Meeting playback glitching.** User reports their voice on the captured `*.wav` from meeting recording sounds chopped/cutting-out on playback. Likely a separate underrun/sync bug in `MeetingRecorder` / `AlignedRingBuffer` / the dual-channel capture path — but it's another data point that the raw-WAV path is fragile and the artifacts land in the user's archive.

The fix flips the responsibility: **server takes whatever the client has, ffmpeg handles decode/resample, the stored audio is the user's actual recording**.

## Context

- **Current contract:** `POST /transcribe/meeting` requires a 2-channel WAV. `staging.stream_to_staging` validates the WAV header.
  Evidence: `server/src/wispralt_server/routes/meeting.py:53-86`.
- **Whisper / Pyannote internals already use ffmpeg.** Both libraries decode arbitrary audio containers internally. The 2-channel WAV requirement is a *staging-layer* constraint, not a *model* constraint.
- **Custom transcriptions** (just-shipped feature) currently go: pick file → AVAssetReader → 2ch/16kHz/Int16 WAV → upload. The transcoder is at `client/WisprAlt/Audio/MediaTranscoder.swift`.
- **Meeting recorder** captures into `MeetingRecorder` (mic) + screen capture (system audio) → dual-channel WAV. Suspected glitch source: `client/WisprAlt/Capture/MeetingRecorder.swift`, `client/WisprAlt/Capture/AlignedRingBuffer.swift`.
- **Server stack on prod-mini:** FastAPI + Python; ffmpeg is already a transitive dep of Whisper/Pyannote so adding an explicit ffmpeg call is zero new install burden.
- **Brief precedent:** the original menubar custom-transcription brief (`./tmp/briefs/2026-05-09-menubar-file-transcribe.md`) considered server-side ffmpeg and *rejected* it to ship faster. That trade-off has now flipped — observed cost (hours of upload + degraded archive audio) > saved server-change effort.

## Decisions

- **New server endpoint: `POST /transcribe/file`.** Accepts any audio/video container ffmpeg can decode (m4a, mp3, mp4, mov, wav, aac, flac, opus, ogg, webm, …). Streams upload bytes to a temp file, invokes ffmpeg to produce 16 kHz mono PCM (Whisper's native input format), pipes that into the existing pipeline. Returns the same `{job_id, status}` response as `/transcribe/meeting` so the client polling/download code is identical.
- **Old `/transcribe/meeting` endpoint stays for now.** Keep backwards compatibility while we migrate. We can deprecate after both client paths have been moved over and verified.
- **Custom transcriptions migrate first.** Drop `MediaTranscoder` entirely on the client. `transcribePickedFile()` uploads the picked file as-is to `/transcribe/file`. Per-job folder stores the original file (e.g. `meeting.m4a`) instead of the bloated WAV.
- **Meeting recordings migrate next.** `MeetingRecorder` keeps capturing live audio (it has to — there's no source file). But instead of writing dual-channel Float32 WAV to disk, it writes a compressed AAC (m4a) file via `AVAssetWriter`. That single m4a is what's uploaded and what's kept in the user's meetings folder. Roughly 8–10× smaller than the current WAV. As a side effect this likely fixes the user-reported voice-cutting glitch — `AVAssetWriter`-managed AAC encode handles its own framing/queuing instead of relying on the hand-rolled `AlignedRingBuffer` PCM path.
- **Investigate the meeting glitch in parallel.** Before tearing out the AlignedRingBuffer path entirely, capture one fresh repro and document the failure mode (frame drops? sync skew? AVAudioEngine config-change race?). The diagnosis informs whether the AAC migration is itself the fix or whether there's a deeper issue that would also affect the new path. File: `client/WisprAlt/Capture/MeetingRecorder.swift`, `client/WisprAlt/Capture/AlignedRingBuffer.swift`.
- **Keep stereo intent for meetings.** Mic and system audio are still distinct logical channels; pyannote's diarization benefits from channel separation. AAC supports 2-channel encoding cheaply (~64–96 kbps total). For custom transcriptions, channel count = whatever the source has.
- **No client-side resampling.** Server's ffmpeg does it. Client just uploads bytes.

## Rejected Alternatives

- **Opus instead of AAC for meetings.** Smaller still (~3 KB/s vs ~10 KB/s), better speech quality at low bitrates. Real options. Going with AAC because `AVAssetWriter` supports it natively without third-party libs (Opus on macOS would need libopus or Apple's not-quite-supported AVAudioFile path). Future optimization.
- **Keep the WAV path, just compress on upload.** Could gzip the WAV on the wire — but PCM doesn't compress well (~30% reduction at best), and we still pay disk-space + audio-quality cost on the client side.
- **Server-side resample only, keep WAV requirement.** Doesn't fix the upload-bloat or stored-audio-quality problems for custom transcriptions.
- **Drop `/transcribe/meeting` immediately.** Risk of breaking in-flight builds / older clients. Cheap to keep both endpoints during migration.

## Where Reasoning Clashed

The meeting-recording glitch could either be (a) a bug in the existing PCM capture path that the AAC migration accidentally fixes, or (b) a deeper capture-engine bug that will follow us into the new path. Reasonable case for both:

- **Migrate first, diagnose later** — if the new AAC path doesn't glitch, we've shipped the fix and the original bug becomes moot.
- **Diagnose first, migrate second** — if the bug is at the AVAudioEngine / Tap level, AAC won't help, and we'll have spent the migration effort without solving the user complaint.

Plan: spend ~30 min on a single repro session BEFORE the migration. Capture one log + one short glitchy WAV. If the symptom is obviously frame-drop / underrun in the ring buffer, migrate-first is the right call. If it looks like raw input audio is wrong (TCC issue, device sample-rate mismatch), fix the underlying issue first and the migration becomes pure cleanup.

## One Thing to Do First

Loosen the server: add `POST /transcribe/file` accepting any audio/video, ffmpeg-decoding to 16 kHz mono PCM in `staging`, then handing to the existing meeting pipeline. Test end-to-end with a 50 MB m4a from the client by curl. This is the unblock — every client change downstream becomes mechanical once the server accepts arbitrary containers.

## Direction

Stop transcoding on the client. Server's ffmpeg eats whatever container the user has — m4a, mp3, mp4, mov, raw WAV, anything. Custom transcriptions upload the picked file as-is (massive bandwidth + quality win). Meeting recordings switch from raw PCM WAV capture to AAC m4a via `AVAssetWriter` (8–10× smaller archive, likely fixes the voice-glitching bug as a side effect). Old `/transcribe/meeting` endpoint stays during migration; deprecate once both client paths are on `/transcribe/file`.
