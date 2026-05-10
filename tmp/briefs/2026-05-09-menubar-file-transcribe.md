# Brief: Menu-bar "Transcribe file…" — pick any audio/video, run through meeting model

## Why
Today the only ways to feed audio into WisprAlt are FN-hold dictation (live) and triple-tap-FN dual-channel meeting recording (live). There is no way to transcribe an existing file (a downloaded video, a Voice Memo, an interview recording, a screen recording's audio). Adding a single button in the menubar UI that opens Finder, picks any audio or video, and runs it through the high-quality meeting pipeline (WhisperX + Pyannote) closes that gap with no new server work.

## Context
- **Settings UI host.** The menubar's popover is `client/WisprAlt/UI/SettingsView.swift`. `body` (line 54) renders a `Form` whose first section is `quickActionsSection` (line 80). This is "the very top" the user referred to.
- **Server pipeline already fits.** "Good model" = meeting pipeline (WhisperX + Pyannote, lazy-loaded; first job pays load cost, subsequent jobs are fast). Endpoint: `POST /transcribe/meeting` → returns job_id → poll `GET /transcribe/meeting/{id}` → download `txt|srt|vtt|json` (`server/src/wispralt_server/routes/meeting.py:53,100,139`).
- **Server contract.** Requires a **2-channel WAV**. `staging.stream_to_staging` validates the WAV header.
- **Client API client already exists.** `client/WisprAlt/Server/MeetingAPI.swift` has `submit / poll / download / delete`. No new networking code needed — only the file-picker + transcode + a thin command that drives the existing flow.
- **Existing reuse target.** `MenuBarController.processMeetingUpload(wavURL:)` already does the upload→poll→download dance for the meeting recorder. The new flow joins this same function once it has produced a valid 2-channel WAV.
- **Settings store.** `client/WisprAlt/Storage/Settings.swift` holds the meetings folder; transcripts can land there alongside meeting outputs.
- **No NSMenu on the status item.** The status-item button is FN-hold + triple-tap-FN. No menu changes needed — entry point is in the popover, not the menu.

## Decisions

- **Entry point: top of the popover (`quickActionsSection`).** A single full-width "Transcribe file…" button at the top of `SettingsView`, above identity/mic/hotkey sections. Reasoning: user explicitly said "at the very top" of the menubar UI where mic + hotkey timing live. The popover is that surface; the status-item button keeps its current dictation/meeting muscle memory.

- **Client-side transcode to 2-channel 16 kHz WAV via AVFoundation.** Use `AVAssetReader` + `AVAssetWriter` (or `AVAssetExportSession` to m4a then a PCM pass) to:
  - Extract the audio track from any audio/video file the picker yields.
  - Resample to 16 kHz.
  - Output **2-channel** PCM (mono → duplicate L=R; stereo → keep). The server requires 2ch; duplicating mono into both channels is the cheapest way to satisfy the contract without a server change.
  - Reasoning: zero backend risk, keeps the meeting endpoint clean, ships fastest.

- **Output folder: `Custom Transcriptions/` — both the WAV and the transcript live together.** Inside the user's meetings folder (or `~/Documents/WisprAlt/` as fallback) we create a `Custom Transcriptions/` subfolder. Each picked file produces a per-job subfolder containing both the converted WAV and the final transcript:
  ```
  Custom Transcriptions/
    <original-stem>__<yyyymmdd-HHmmss>/
      <original-stem>__2ch16k.wav
      <original-stem>.txt   (and .srt/.vtt/.json as available)
  ```
  Reasoning: user wants the WAV and the transcript co-located in a clearly-named folder, and `Custom Transcriptions` (their final naming choice) keeps it short and obvious. The per-job timestamped subfolder prevents collisions when re-transcribing the same source file.

- **"Open Custom Transcriptions" button on the popover.** Sits in `quickActionsSection` next to (or under) the "Transcribe file…" button. Calls `NSWorkspace.shared.open(...)` on the `Custom Transcriptions/` folder so the user can browse outputs in Finder without hunting for them.

- **Two "copy last…" buttons in the popover.** Also in `quickActionsSection`:
  - **"Copy last meeting"** — finds the newest `.txt` under `settings.meetingsPath` **excluding** the `Custom Transcriptions/` subtree, reads it, writes to `NSPasteboard.general`.
  - **"Copy last custom transcription"** — finds the newest `.txt` under `settings.meetingsPath/Custom Transcriptions/`, reads it, writes to `NSPasteboard.general`.
  - Selection rule: most recent by file modification time (mtime). Reasoning: simpler and more correct than parsing timestamps from filenames — mtime reflects when the transcript was actually written, even if the user re-runs an old recording.
  - Disabled state: if no transcript exists yet, the button is disabled with a subtle "No transcripts yet" tooltip rather than throwing an error.
  - Confirmation: brief inline confirmation ("Copied — 1,243 chars") for ~1.5s so the user knows it worked. No system notification needed.
  - **Live "last updated" timestamp under each button.** A small secondary-text caption under each "Copy last…" button shows the mtime of the newest transcript it would copy. Format: relative ("just now", "12s ago", "3m ago", "1h ago") for anything under 24h, otherwise absolute (`Today 14:32`, `Yesterday 09:15`, `May 7 18:04`). Empty state: "No transcripts yet" (button disabled).
  - **Purpose: copy-the-right-file confidence.** Transcription takes seconds; the user needs to see the timestamp flip before clicking copy, so they know the new transcript actually landed and they're not copying the previous one.
  - **Refresh strategy.** (a) Recompute on popover-open. (b) `DispatchSource.makeFileSystemObjectSource` watching each folder for `.write/.rename/.delete` while the popover is visible — fires the recompute. (c) Belt-and-suspenders: a 2s SwiftUI `Timer` while the popover is visible to advance the relative-time label ("3s ago" → "5s ago") without needing a filesystem event. Stop the watcher and timer when the popover closes.
  - **Make sure the "last" lookup is correct.** User flagged this explicitly. Implementation must enumerate the folder fresh on each click (no caching), filter to `.txt` files only, and pick `max(by: mtime)`. No reliance on filename ordering or remembered state — the file system is the source of truth.

- **NSOpenPanel filter scope.** Allow the common audio/video UTIs: `public.audio`, `public.movie`, plus explicit `.mp3 .m4a .wav .aac .mp4 .mov .m4v .caf .aiff .flac`. Single-file pick (no batch in v1).

- **Reuse `processMeetingUpload`.** After transcode, hand the WAV URL to the existing function — it already handles upload, poll, download, error UI, transcript landing in the meetings folder. No new error paths.

- **Progress UX.** Reuse the same toast/progress affordance the meeting recorder uses today. Two phases: "Converting…" then "Transcribing…" (the second phase is whatever the meeting upload already shows).

## Rejected Alternatives

- **Server-side ffmpeg transcoding** — broadest UX (drop anything, server figures it out), but requires loosening the meeting endpoint contract, adding ffmpeg as a server dep, new staging codepaths, more failure modes. Skipped because client AVFoundation already does the job for free.
- **WAV-only strict picker** — too restrictive; the whole point is "drag in any video".
- **NSMenu on the status item** — would change today's button behavior (FN-hold is button-driven). The popover already exists for this kind of action.
- **Standalone `scripts/transcribe-file.sh` curl helper** — initial reading of the brief but the user clarified they meant the *converted file's filename*, not a CLI script. Not building.
- **Mono-as-mono upload** with server-side channel-fanout — would need a server change for arguably no win; client duplication is one line of buffer math.
- **Batch / drag-and-drop multi-file** — out of scope for v1; single file via Finder is the ask.

## Where Reasoning Clashed
None. The three real questions (format handling, UX surface, "script file" meaning) all collapsed cleanly once the user pointed at the popover's top section and clarified the filename remark.

## One Thing to Do First
In `client/WisprAlt/UI/SettingsView.swift`, modify `quickActionsSection` (line 80) to render a "Transcribe file…" button as the first row, wired to a new command on `MenuBarController` that (a) presents `NSOpenPanel`, (b) calls a new `MediaTranscoder.toMeetingWAV(_:)` helper, (c) hands the resulting URL to `processMeetingUpload`. Stub the transcoder to return the input URL unchanged on first iteration so the wiring can be tested end-to-end with an already-conformant WAV before AVFoundation work begins.

## Direction
Add four buttons at the top of the menubar popover (`SettingsView.quickActionsSection`): **"Transcribe file…"**, **"Open Custom Transcriptions"**, **"Copy last meeting"**, **"Copy last custom transcription"**. Picking a file transcodes it locally to a 2-channel 16 kHz WAV via AVFoundation, drops it into a per-job timestamped subfolder under `Custom Transcriptions/` (inside the meetings folder), then feeds the WAV through the existing `processMeetingUpload` flow so the meeting model (WhisperX + Pyannote) transcribes it — and the transcript is written into the same subfolder next to the WAV. The two "copy last…" buttons each enumerate their respective folder live and copy the newest `.txt` to the clipboard. No server changes.
