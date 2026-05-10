# Meeting voice-cutting glitch — diagnosis

**Date:** 2026-05-09 20:14
**Method:** /usr/bin/log stream --predicate 'subsystem == "co.wispralt"' captured during a triple-tap-FN test recording. Live log piped to /tmp/wispralt-glitch.log; resulting WAV `~/Documents/WisprAlt/Meetings/Sat May 9 8.14pm-8.14pm.wav` (9.3s, 2ch / 16000 Hz / Float32, 1.19 MB).

## What the log shows

- `MeetingRecorder: capture started` + `Meeting recording started` at 20:14:43
- `MeetingRecorder: capture stopped` + `Meeting WAV renamed` + uploaded + `Meeting transcription complete` by 20:15:04
- **Zero `AlignedRingBuffer` events** — no `pad`, no `stall`, no `drift`, no `gap` warnings.
- **Zero SCStream / AVAudioConverter / capture errors.**
- The `Error` lines in the log are unrelated: "Dictation start ignored — meeting recording is active." (FN-tap during recording, expected protection.)

## Diagnosis

**Outcome (b) per plan**: AlignedRingBuffer's pad-with-silence path is NOT firing during the symptom window. The instrumentation we have doesn't surface the failure mode the user reports.

This means the AAC migration alone is **NOT guaranteed to fix the cutting-out**:
- If the symptom is in the AVAudioFile.write Float→Int16 path (a known WisprAlt bug per `DictationRecorder.swift`'s comment) → AAC migration WOULD fix it (different downstream code).
- If the symptom is upstream (SCStream's mic delivery, the CMSampleBufferConverter from 48k mono → 16k mono, or the sample-position interleaving) → AAC migration via AVAssetWriter would inherit the same upstream bug.

## Recommendation for Task 9

The plan defaults to **keep AlignedRingBuffer, change only the downstream sink** (AVAudioFile.write → AVAssetWriterInput.append). That decision now has even more support: since we don't know which layer causes the glitch, the smallest-blast-radius change is the right call. AAC encoding via AVAssetWriter has its own framing logic, so even if the bug is upstream, the AAC encoder may smooth artifacts that the raw-PCM-write path lets through.

If user-reported cutting persists in the new AAC m4a captures after Task 11 lands, escalate: add detailed sample-counter logging to MeetingRecorder's SCStream callbacks + a CMSampleBufferConverter timing log. That's a separate investigation arc.

## What was NOT confirmed in this session

- Whether the user's symptom actually occurred during this 9.3s recording. Need user to play back `Sat May 9 8.14pm-8.14pm.wav` and report. If it sounds clean, the symptom may need a longer/more complex recording to repro.

## Update: user listened to the test recording

The 9.3s test WAV (`Sat May 9 8.14pm-8.14pm.wav`) **sounded clean** on playback — no cutting symptom audible. Combined with the zero-AlignedRingBuffer-events log, this is now closer to **outcome (c)**: not reproducible in a controlled short test.

Implication: the cutting symptom likely correlates with longer recordings, sustained capture, or a state we haven't reproduced yet (network-bound queue, post-config-change state, a specific input device, etc.). The recorder rewrite will still happen because the brief mandates the WAV→m4a switch for size/quality reasons; the glitch is a parallel concern that needs longer-form repro to investigate properly. If the new AAC m4a recordings still exhibit cutting after Task 11 ships, escalate to a longer-form capture session with SCStream-callback timing logs.

## Round 2 outcome: cutting fixed, quality intentional

After Task 9 + Task 10 shipped, user reported the cutting symptom AGAIN on the new `.m4a` recordings. Root cause turned out to be an Apple back-pressure violation in `MeetingRecorder.appendSample(_:)` — the new code called `input.append(sample)` without first checking `input.isReadyForMoreMediaData`. Per Apple docs, calling `append()` while not ready silently drops samples — exactly the cut-out symptom. `expectsMediaDataInRealTime = true` advises but does NOT guarantee. Fixed by spin-waiting until ready (1 ms ticks, 100 ms warning log, 2 s hard ceiling) before each append.

After the back-pressure fix: cutting gone. ✓

User also flagged that audio quality sounds less crisp than Voice Memos. Diagnosed: we encode at 16 kHz (everything above 8 kHz is gone) because AlignedRingBuffer's upstream resamples to 16 kHz and the server's Whisper input is 16 kHz mono regardless. Voice Memos uses 44.1-48 kHz. **Decision: ship at 16 kHz.** Optimized for transcription, accepts lower listening quality. Revisit if user changes their mind — ~30 min change to bump everything to 48 kHz / 128 kbps stereo AAC (~3× file size, still ~3× smaller than the old WAV).
