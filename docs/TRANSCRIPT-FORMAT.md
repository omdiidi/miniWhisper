---
title: Transcript Format
---

# Transcript Format

## Locked JSON schema

The following schema is the canonical contract between server output and client storage. Every server transcript is a file job: one mono stream transcribed by mlx-whisper, labelled `"Speaker 1"`. The `speaker_raw` field in `speakers` is the stable raw label (always `"mic"` for file jobs); it is used as the lookup key and is never overwritten. The `display_name` field is the user-visible name. (Client-side speaker rename was removed with the meeting recorder; the fields stay so old and new files share one schema.)

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
      "speaker": "Speaker 1",        // display label
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
- `speakers` is keyed by `speaker_raw` (not display name). This is a v3 change from the v1/v2 schema where `speakers` was keyed by display name.

## Filename convention

The client saves each file job into its own folder under the output folder chosen in Settings (default `~/Documents/WisprAlt/Transcripts/`; a Mac that already has `~/Documents/WisprAlt/Meetings/` keeps using it):

```
<output folder>/Custom Transcriptions/<source stem>__<yyyymmdd-HHmmss>/
    <source file, copied as-is>
    <source stem>.{json,srt,vtt,txt}
```

A second-resolution collision appends `-2`, `-3`, … to the folder name. Meeting transcripts saved by older versions stay in that folder untouched; the app no longer lists or edits them.

## SRT, VTT and TXT formats

Every current transcript is a single-speaker file job (`"mode": "single"`), so the text formats carry **no speaker prefix**:

```
1
00:00:00,000 --> 00:00:03,420
Let me share my screen.
```

```
WEBVTT

00:00:00.000 --> 00:00:03.420
Let me share my screen.
```

TXT is one segment per line, plain text.

Multi-speaker transcripts written by older servers used `Speaker: text` (SRT), `<v Speaker>text</v>` voice tags (VTT) and `[Speaker] text` (TXT). The server's writers still render those prefixes for any non-`single` transcript, but nothing produces one today.
