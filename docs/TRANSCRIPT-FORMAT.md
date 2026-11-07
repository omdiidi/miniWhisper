---
title: Transcript Format
---

# Transcript Format

## Locked JSON schema

The following schema is the canonical contract between server output and client storage. Every server transcript is a file job: one mono stream transcribed by mlx-whisper, labelled `"Speaker 1"`. The `speaker_raw` field in `speakers` is the stable raw label (always `"mic"` for file jobs); it is used as the lookup key and is never overwritten. The `display_name` field is the user-visible name and is the only field modified by client-side speaker rename.

```jsonc
{
  "job_id": "string (uuid4)",
  "mode": "single",
  "created_at": "ISO 8601 string",
  "duration_s": "number",
  "language": "en",
  "model": {
    "transcription": "mlx-community/whisper-large-v3-turbo"
  },
  "segments": [
    {
      "start": 0.0,                  // seconds
      "end": 3.42,
      "channel": null,               // always null for file jobs (one mono stream)
      "speaker": "Speaker 1",        // canonical label after any client renames
      "speaker_raw": "mic",          // raw label, never overwritten
      "text": "string",
      "words": [],                   // empty: file jobs run without word timestamps
      "overlap": false
    }
  ],
  "speakers": {                      // keyed by speaker_raw (v3 schema)
    "mic": { "display_name": "Speaker 1", "channel": null }
  }
}
```

**Important notes:**

- `model` lists the ASR model only. Transcripts written before the meeting pipeline was removed also carried `diarization` and `denoise` keys, and `mode` values `remote` / `in_person`; readers must tolerate those on old files. File audio is no longer passed through the (pass-through) denoise stub.
- Stereo uploads are averaged to one mono stream before transcription.
- `warnings` (optional top-level array) may appear on transcripts written by older servers; current servers do not emit it.
- `speaker_raw` is preserved even after rename so the client can map raw labels back to user names.
- `speakers` is keyed by `speaker_raw` (not display name). This is a v3 change from the v1/v2 schema where `speakers` was keyed by display name.
- `speaker` in each segment is the denormalized current display name; it is rewritten on every rename to keep the file self-contained for human readers.
- Rename collision check: `renameSpeaker(raw:to:)` throws `.speakerNameConflict` if another speaker already has `display_name == newName`.

## Filename convention

Files are saved under `~/Documents/WisprAlt/Meetings/` with the pattern:

```
YYYY-MM-DD_HHMM<±HHMM>_<title>.{json,srt,vtt,txt}
```

The UTC offset is included to eliminate DST collision (e.g. `2026-04-24_1543-0700_meeting`).

## SRT format

Speaker labels use the `Speaker: text` convention on a single line:

```
1
00:00:00,000 --> 00:00:03,420
You: Let me share my screen.

2
00:00:03,800 --> 00:00:06,100
Other: Sure, go ahead.
```

## VTT format

Speaker labels use `<v Speaker>text</v>` WebVTT voice tags so playback tools render the speaker label natively:

```
WEBVTT

00:00:00.000 --> 00:00:03.420
<v You>Let me share my screen.</v>

00:00:03.800 --> 00:00:06.100
<v Other>Sure, go ahead.</v>
```

## TXT format

Plain text with bracketed speaker prefix:

```
[You] Let me share my screen.
[Other] Sure, go ahead.
```

## Client-side speaker rename contract

Speaker rename happens entirely on the client. There is no server `PATCH` endpoint. The client:

1. Loads the local `.json` file.
2. Calls `renameSpeaker(raw:to:)` which updates `display_name` in `speakers` and rewrites `speaker` in every matching segment.
3. Writes all four formats (`.json`, `.srt`, `.vtt`, `.txt`) atomically: each file is written to a `.{uuid}.tmp` file in the same directory first, then `replaceItemAt` replaces the original. A `.transcriptWriteInProgress` sentinel file is created before the first replace and deleted after the last; on app launch, orphan sentinels trigger a revert (delete partial outputs, keep originals).

This is fully offline-capable and requires no network connectivity.
