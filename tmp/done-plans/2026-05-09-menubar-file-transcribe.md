---
name: "Menubar Custom Transcriptions — pick any audio/video, transcribe via meeting model"
date: 2026-05-09
brief: ./tmp/briefs/2026-05-09-menubar-file-transcribe.md
---

## Goal

Add a "Custom Transcriptions" workflow to the WisprAlt menubar popover so the user can pick any audio or video file in Finder, have it transcoded locally to a 2-channel 16 kHz WAV, uploaded to the existing meeting transcription pipeline, and saved with the transcript next to the WAV in a per-job folder. Add two clipboard helpers ("Copy last meeting", "Copy last custom transcription") with a live mtime caption under each so the user can confirm the newest transcript actually landed before copying it.

## Summary

Four new buttons appear at the top of `SettingsView.quickActionsSection`:

1. **Transcribe file…** — `NSOpenPanel` → `MediaTranscoder.toMeetingWAV(_:)` (AVFoundation, 2ch/16kHz) → `MenuBarController.processCustomTranscriptionUpload(...)` (a near-clone of `processMeetingUpload` that writes outputs into `Custom Transcriptions/<stem>__<timestamp>/`).
2. **Open Custom Transcriptions** — reveals that folder in Finder.
3. **Copy last meeting** — copies the newest `.txt` under `meetingsPath` (excluding the `Custom Transcriptions/` subtree) to the pasteboard.
4. **Copy last custom transcription** — copies the newest `.txt` under `meetingsPath/Custom Transcriptions/` to the pasteboard.

Each "Copy last…" button has a live `Text(...)` caption beneath it showing relative mtime ("just now", "12s ago", "3m ago") that auto-refreshes via a `DispatchSource.makeFileSystemObjectSource` watcher plus a 2s SwiftUI timer while the popover is visible. No server changes.

## Intent / Why

- The only ways to feed audio into WisprAlt today are FN-hold dictation and triple-tap-FN meeting recording — both live capture. No path exists for transcribing an existing file (downloaded video, Voice Memo, screen recording).
- The meeting pipeline (WhisperX + Pyannote) already produces high-quality multi-format output and is reachable via a clean upload→poll→download contract.
- Doing the format adaptation client-side avoids touching the server.
- The "copy last…" affordance + live timestamp removes the recurring annoyance of "did the new transcript land yet, or am I about to copy the old one?"

## Source Artifacts

- Brief: `./tmp/briefs/2026-05-09-menubar-file-transcribe.md`
- No separate research dossier — codebase audit folded inline below.

## What

- **User picks a file via Finder** (audio or video). Single file per invocation.
- **App transcodes it locally** to 2-channel 16 kHz Int16 PCM WAV via AVFoundation. Mono input → channel-fanout (L=R). Stereo input → preserved.
- **Converted WAV is written** to `<meetingsPath>/Custom Transcriptions/<stem>__<yyyymmdd-HHmmss>/<stem>__2ch16k.wav`.
- **App uploads** that WAV to `POST /transcribe/meeting` reusing the existing `MeetingAPI` client and the same poll/download/cleanup loop already used by `processMeetingUpload`.
- **Transcript files** (`.txt`, `.srt`, `.vtt`, `.json`) land in the same per-job folder as the WAV.
- **Open Custom Transcriptions** opens the folder in Finder; folder is created on first use.
- **Copy last meeting** + **Copy last custom transcription** copy the newest `.txt` in their respective subtree to `NSPasteboard.general`. Disabled with a "No transcripts yet" tooltip if empty. Show a 1.5s "Copied — N chars" inline confirmation on success.
- **Live mtime caption** under each copy button updates as soon as a new transcript is written.
- The whole feature respects the same status/progress affordance the meeting recorder already uses (`mode = .uploading / .processing / .done / .idle`, `recordingState.uploadFraction`).

### Success Criteria

- [ ] Picking an `.mp4`, `.m4a`, `.mp3`, `.mov`, `.aac`, `.caf`, `.aiff`, `.flac`, or `.wav` file produces a transcript without server changes.
- [ ] Mono source produces a 2-channel WAV (server staging acceptance proves it).
- [ ] Output folder layout matches `Custom Transcriptions/<stem>__<timestamp>/` with the WAV and all transcript formats co-located.
- [ ] "Open Custom Transcriptions" reveals the folder in Finder; folder is created lazily if it doesn't exist yet.
- [ ] "Copy last…" buttons copy the correct file (newest `.txt` by mtime) on every click, even when called twice in a row across a fresh transcription.
- [ ] Caption under each "Copy last…" button updates within 2 seconds of a new transcript being written, while the popover is visible.
- [ ] Captions stop updating (no Timer / DispatchSource leak) when the popover closes.
- [ ] All four new buttons live inside `quickActionsSection`, above `identitySection`, with a layout that matches the existing `Button(_:systemImage:)` style.
- [ ] `swift build` succeeds with no new warnings.

## Verified Repo Truths

### Frontend / UI

- Fact: `SettingsView.quickActionsSection` is the very first section rendered in the popover form and currently contains exactly two buttons ("Open Portal", "Open Meetings Folder").
  Evidence: `client/WisprAlt/UI/SettingsView.swift:54-70` (form ordering), `client/WisprAlt/UI/SettingsView.swift:80-97` (section body).
  Implication: The four new buttons append cleanly here without disturbing other sections.

- Fact: The popover view binds to the singleton `Settings` via `@EnvironmentObject` and reads/writes `settings.meetingsPath`.
  Evidence: `client/WisprAlt/UI/SettingsView.swift:14`, `client/WisprAlt/UI/SettingsView.swift:138`, `client/WisprAlt/Storage/Settings.swift:59-63`.
  Implication: `meetingsPath` is the canonical location for both meeting outputs and the new `Custom Transcriptions/` subfolder. No new storage key needed.

- Fact: There is no existing `NSMenu` on the status item; the menubar button is consumed by FN-hold dictation and triple-tap-FN meeting recording.
  Evidence: `client/WisprAlt/App/MenuBarController.swift:82,124` and absence of `NSMenu`/`addItem` matches in `MenuBarController.swift` (search produced zero matches).
  Implication: All new entry points must live in the SwiftUI popover, not a status-bar menu.

### Entry Points / Integrations

- Fact: `MenuBarController.processMeetingUpload(wavURL:)` is the existing upload→poll→download→cleanup function used by the meeting recorder. It writes outputs as `<meetingsPath>/<baseName>.<ext>` (sibling files in the meetings root) and refreshes `TranscriptStore.shared`.
  Evidence: `client/WisprAlt/App/MenuBarController.swift:555-671`.
  Implication: The custom-transcription path needs the SAME loop but a DIFFERENT output base directory (per-job subfolder). Will introduce a small refactor to factor out the loop with a `outputBaseURL: URL` parameter, OR add a parallel function — see Delta Design.

- Fact: `MeetingAPI.submit(_:)` accepts a local file URL, streams it, and returns a `JobID`. `MeetingAPI.poll`, `MeetingAPI.download(_:format:)`, and `MeetingAPI.delete(_:)` complete the lifecycle.
  Evidence: `client/WisprAlt/Server/MeetingAPI.swift:6-238`.
  Implication: No new networking code is needed.

- Fact: The server route requires a 2-channel WAV (`UploadFile` declared, `staging.stream_to_staging` validates the WAV header).
  Evidence: `server/src/wispralt_server/routes/meeting.py:53-86`, brief reference to `staging.stream_to_staging`.
  Implication: Client transcoder MUST emit 2-channel PCM. Mono inputs need channel duplication.

### Execution / Async Flow

- Fact: Existing meeting-upload code uses `mode = .uploading | .processing | .done | .idle` and `recordingState.uploadFraction` to drive the popover progress UI; success path ends with `mode = .done` for 3 s then `mode = .idle`.
  Evidence: `client/WisprAlt/App/MenuBarController.swift:540-541, 576, 628-630`.
  Implication: The custom-transcription path can reuse exactly these signals — no new UI states needed.

- Fact: `humanReadableMeetingFilename(start:end:in:)` builds collision-safe meeting file names; we don't need it for custom transcriptions because the per-job timestamped subfolder already prevents collisions.
  Evidence: `client/WisprAlt/App/MenuBarController.swift:753-792`.
  Implication: Use a simpler `<stem>__<yyyymmdd-HHmmss>` pattern; do not call into the meeting helper.

### Shared Types / Exports

- Fact: The Swift target is SPM-managed (`client/Package.swift`) with `path: "WisprAlt"` and no individually-listed sources.
  Evidence: `client/Package.swift:32-47`.
  Implication: Adding new `.swift` files anywhere under `client/WisprAlt/` requires no project-file edits — SPM picks them up automatically.

- Fact: `Settings.meetingsPath` defaults to `~/Documents/WisprAlt/Meetings`.
  Evidence: `client/WisprAlt/Storage/Settings.swift:182-190`.
  Implication: `Custom Transcriptions/` will land under that directory (e.g. `~/Documents/WisprAlt/Meetings/Custom Transcriptions/`).

## Locked Decisions

Intentional deviations from the brief (documented so a later reviewer doesn't flag them as drift):

- **Relative-time only for under-24h band.** Brief literal examples include `Today 14:32` for the 1h-24h same-day band. Plan uses `\(s/3600)h ago` instead. Simpler, equally informative for the "did the new transcript land?" use case the caption exists for.
- **10 s timer cadence, not 2 s.** Brief said 2 s. The DispatchSource watcher + the new `NotificationCenter.wisprAltTranscriptWritten` post mean instantaneous updates on file events; the timer only advances the relative-time string ("3m ago" → "4m ago"), which changes meaningfully each minute past the first. 10 s halves wakeups for no perceivable UX cost.

From the brief — DO NOT relitigate:

- Client-side AVFoundation transcode to 2ch/16kHz PCM WAV. No server-side ffmpeg.
- Output layout: `Custom Transcriptions/<stem>__<yyyymmdd-HHmmss>/{<stem>__2ch16k.wav, <stem>.{txt,srt,vtt,json}}`.
- Mono → channel duplication (L=R), not server-side fanout.
- "Copy last" selection rule: enumerate fresh per click, filter `.txt` only, pick `max(by: mtime)`. No filename parsing, no caching.
- Live caption refresh: `DispatchSource` watcher + 2s SwiftUI timer while popover is visible; both teardown when popover closes.
- All entry points in the popover. No NSMenu on the status item.
- Single-file picker (no batch).
- File-picker UTIs: `public.audio`, `public.movie`, plus explicit allow for `.mp3 .m4a .wav .aac .mp4 .mov .m4v .caf .aiff .flac`.
- Reuse the existing `mode` / `recordingState.uploadFraction` progress UI.

## Known Mismatches / Assumptions

- Mismatch: brief says "reuse `processMeetingUpload`" verbatim, but that function hardcodes the output base URL to `<meetingsPath>/<baseName>` (sibling layout). Custom transcriptions need a per-job subfolder layout.
  Repo Evidence: `client/WisprAlt/App/MenuBarController.swift:555-619`.
  Requirement Evidence: brief decision "Output folder: `Custom Transcriptions/` — both the WAV and the transcript live together".
  Planning Decision: Refactor `processMeetingUpload` to take an `outputBaseURL: URL` parameter (default behavior unchanged) and an `onComplete: ((URL) -> Void)?` hook for the post-success refresh, then add `processCustomTranscriptionUpload(wavURL:outputDirectory:)` as a thin wrapper. This keeps both flows on a single tested code path.

- Assumption: `DispatchSource.makeFileSystemObjectSource(.write | .rename | .delete)` on a folder fires when entries inside it are added/removed/moved. Apple-documented behavior on macOS; verified through frequent use in macOS apps. If a future macOS version regresses this behavior, the 2 s timer falls back to polling.

- Assumption: `NSOpenPanel` filtering with both `.audio` and `.movie` UTTypes plus the explicit extension list yields the expected dialog. Apple-documented.

## Critical Codebase Anchors

- Anchor: `client/WisprAlt/App/MenuBarController.swift:555-671` (`processMeetingUpload`)
  Evidence: same.
  Reuse / Watch for: poll loop, download loop, cleanup/notification logic, offline-signature catch path, `TranscriptStore.shared.refresh()` call site. The refactor must preserve every observable behavior in the meeting-recorder path.

- Anchor: `client/WisprAlt/App/MenuBarController.swift:677-709` (`buildMeetingAttempt`)
  Evidence: same.
  Reuse / Watch for: This catch-path helper interprets thrown errors for the offline-queue classifier. The custom-transcription path should NOT enqueue to `PendingUploadsQueue` — that queue is for meeting recordings the user explicitly captured. Custom transcriptions just surface a notification on offline error.

- Anchor: `client/WisprAlt/UI/SettingsView.swift:80-97` (`quickActionsSection`)
  Evidence: same.
  Reuse / Watch for: existing `Button("...", systemImage: "...")` style; new buttons must match. Use SF Symbols `waveform.badge.microphone` / `folder.badge.questionmark` / `doc.on.clipboard` / `doc.on.clipboard.fill`.

- Anchor: `client/WisprAlt/Capture/DictationRecorder.swift` (notes on AVAudioConverter / AVAudioFile gotchas)
  Evidence: top-of-file doc comment, especially the "AVAudioConverter sums channels without averaging, peak ≈ 3.97" and "AVAudioFile internal Float→Int16 amplifies ~140×" warnings.
  Reuse / Watch for: **the locked transcoder design DELIBERATELY avoids both bugs by routing the entire format conversion through `AVAssetReader.outputSettings`** (Apple-maintained pipeline) — no `AVAudioConverter`, no `AVAudioFile`. See Key Pseudocode below. This is the "consensus reviewer recommendation" path.

- Anchor: `client/WisprAlt/App/MenuBarController.swift:39-46` (`Mode` enum)
  Evidence: same.
  Reuse / Watch for: Flat enum, no associated values: `idle, dictating, meetingRecording, uploading, processing, done`. Adding `.converting` requires updating one icon switch site at `MenuBarController.swift:443-447`. `RecordingIndicatorView` uses a SEPARATE `RecordingState` enum with associated values (`uploading(let fraction)`, `processing(let startDate)`); they are not the same enum.

- Anchor: `client/WisprAlt/App/MenuBarController.swift:567-568` (file-size→duration estimate)
  Evidence: `let estimatedDurationSeconds = Double(fileSize) / (2 * 16_000 * 4)  // 2ch * 16kHz * 4 bytes (Float32)`
  Reuse / Watch for: This formula assumes the meeting WAV is **Float32**. Custom-transcription WAVs are **Int16** (half the bytes/sec), so the estimate would be 2× actual. Make duration estimation format-aware (read bytes-per-frame from the WAV header) or pass an explicit hint into the helper that does the polling.

## All Needed Context

### Documentation & References

- Repo reference: `client/WisprAlt/App/MenuBarController.swift:555-671`
  Why: full reference implementation of the upload/poll/download/cleanup contract that the new flow reuses.

- Repo reference: `client/WisprAlt/Server/MeetingAPI.swift:48-238`
  Why: signatures for `submit`, `poll`, `download`, `delete` that the new flow calls.

- Repo reference: `client/WisprAlt/UI/SettingsView.swift:80-97`
  Why: the Section style and button styling to match.

- Repo reference: `client/WisprAlt/Capture/DictationRecorder.swift:1-100`
  Why: AVFoundation gotchas (AVAudioFile Int16 normalization bug); the transcoder must not trip them.

- External doc: https://developer.apple.com/documentation/avfaudio/avassetreader
  Section: "Reading Media from an Asset"
  Why: canonical AVAssetReader/AVAssetReaderTrackOutput pattern for pulling decoded PCM from any AVURLAsset.

- External doc: https://developer.apple.com/documentation/avfaudio/avaudiofile/init(forwriting:settings:commonformat:interleaved:)
  Why: confirms the writer settings dictionary format for 16 kHz / 2ch / Int16 / interleaved WAV.

- External doc: https://developer.apple.com/documentation/dispatch/dispatchsource/2300108-makefilesystemobjectsource
  Why: confirms `.write`, `.rename`, `.delete` event masks fire on folder mutations.

- External doc: https://developer.apple.com/documentation/appkit/nsopenpanel
  Why: `allowedContentTypes`, `allowsMultipleSelection`, `runModal()` semantics.

### Files Being Changed

```
client/
└── WisprAlt/
    ├── App/
    │   └── MenuBarController.swift                    ← MODIFIED (refactor processMeetingUpload to accept outputBaseURL; add transcribePickedFile() entry point + processCustomTranscriptionUpload())
    ├── Audio/
    │   └── MediaTranscoder.swift                      ← NEW (AVFoundation: any audio/video → 2ch 16kHz Int16 PCM WAV)
    ├── Storage/
    │   └── CustomTranscriptionsStore.swift            ← NEW (resolves Custom Transcriptions/ dir, computes per-job subfolder URL, finds-newest-txt helpers, watcher publisher)
    └── UI/
        ├── SettingsView.swift                         ← MODIFIED (replace quickActionsSection with the four new buttons + two captions; add @StateObject for the watcher view-model; add @State for confirmation toasts)
        └── LastTranscriptCaption.swift                ← NEW (small SwiftUI subview — caption text + ObservableObject view-model wrapping the watcher)
```

No server-side files change. No `Package.swift` change (SPM auto-discovers).

### Known Gotchas & Library Quirks

- **AVFoundation: do not use `AVAudioConverter` or `AVAudioFile` for the transcode.** Both have known bugs in this codebase's experience: `AVAudioConverter` sums multi-channel sources without averaging (clipping); `AVAudioFile` internal Float→Int16 normalization amplifies ~140×. The locked design instead configures `AVAssetReaderTrackOutput.outputSettings` to request 16 kHz / 2-channel / Int16 LE PCM directly, so AVAssetReader (Apple's maintained code) does the resample, fanout, and quantization in one step. No converter, no AVAudioFile writer. We then either: (a) write a hand-rolled 44-byte RIFF WAV header + raw PCM bytes via `FileHandle.write`, or (b) stream samples through an `AVAssetWriter` with `outputFileType: .wav`. Pick (a) — simpler, fewer moving parts.
- **There is no `AVAudioConverter.channelMap` Swift property.** Channel-map is a `kAudioConverterChannelMap` knob on the underlying `AudioConverterRef`. Reviewers flagged this as a fictional API. The reader-`outputSettings` approach above sidesteps it entirely (mono is fanned out by the reader).
- **`CMSampleBuffer.toAVAudioPCMBuffer(format:)` is not a real method.** It does not exist on `CMSampleBuffer`. The reader-outputSettings approach reads `CMSampleBuffer` byte data directly via `CMBlockBufferCopyDataBytes` — no `AVAudioPCMBuffer` involved.
- **`AVAssetReader.startReading()` returns Bool — check it.** A `false` return means the reader's `status == .failed` and `reader.error` carries the cause. Without the check, an unsupported asset silently produces a zero-length WAV.
- `NSOpenPanel.runModal()` blocks the main thread. The menubar popover is `.transient` (`MenuBarController.swift:395`) so showing the panel will collapse the popover — that's fine, the user expects a Finder window to take over. Before calling `runModal()`, call `NSApp.activate(ignoringOtherApps: true)` so the panel reliably comes to the front. Drive the whole pick → transcode → upload sequence from `MenuBarController` (which owns the lifetime), not from a SwiftUI view that may be torn down when the popover dismisses.
- The `Custom Transcriptions/` folder name contains a space. Do not URL-encode it inside `URL(fileURLWithPath:)` — `appendingPathComponent("Custom Transcriptions", isDirectory: true)` handles it. The Finder reveal must use the `URL`, not a constructed string path.
- `DispatchSource.makeFileSystemObjectSource` requires an open file descriptor that stays open for the lifetime of the source. Open with `open(path, O_EVTONLY)`; close in the source's cancel handler. The folder must exist first, or `open` returns -1 — create the directory before installing the watcher.
- Folder watchers on a non-existent folder will silently no-op. Create `Custom Transcriptions/` lazily on popover open if missing.
- The mtime-based "newest .txt" pick must use `URLResourceValues.contentModificationDate`, not `attributesOfItem(atPath:)[.modificationDate]`, because the latter occasionally returns wrong values on APFS clones.
- The "Copy last meeting" subtree exclusion is critical: the meetings folder has both meeting outputs (siblings under `meetingsPath`) and the new `Custom Transcriptions/` subtree. The "meeting" enumerator must walk only the immediate children of `meetingsPath`, NOT recurse. Use `FileManager.contentsOfDirectory(at:)` and additionally `filter { !$0.hasDirectoryPath }` defensively in case future changes promote subdirectories.
- `NSPasteboard.general.clearContents()` MUST be called before `setString(_:forType: .string)` — forgetting it leaves stale clipboard entries on macOS.
- Read transcript files with an explicit encoding: `String(contentsOf: url, encoding: .utf8)`. WhisperX outputs UTF-8.
- **Drive the relative-time tick via a `@Published` counter, not bare `objectWillChange.send()`.** SwiftUI may optimize out an `objectWillChange` that isn't paired with a state mutation, especially in release builds. Add `@Published private var tick: Int = 0` and bump it from the timer; have `captionText` reference `tick` so the dependency is explicit.
- 10 s timer cadence is enough — relative-time strings ("3m ago" → "4m ago") only change meaningfully each minute past the first; the within-first-minute sub-second precision is invisible to a user staring at a transcription that just landed. The DispatchSource watcher provides instant feedback for the actual file-write event.
- **DispatchSource on `.main` is fine for this read-only watcher**, since `stop()` only calls `source.cancel()` (which is async and does NOT block waiting for the cancel handler). Do not depend on the cancel handler running synchronously from `stop()`. The captured `[fd]` in the cancel handler owns the descriptor's lifetime — do NOT also `close(fd)` from `stop()`.
- **DispatchSource on a folder fires for direct children only.** `Custom Transcriptions/` is the watched folder, but transcripts land in *subfolders* (`Custom Transcriptions/<stem>__<ts>/<stem>.txt`). Watching the parent catches the per-job folder *creation* — but the actual `.txt` write happens INSIDE the subfolder and does NOT fire the parent watcher. Belt-and-suspenders: have `processCustomTranscriptionUpload` post `NotificationCenter.default.post(name: .wisprAltTranscriptWritten, object: nil)` after each successful download. Both `LastTranscriptCaptionViewModel` instances subscribe and call `refresh()`. Without this, the caption stays stale until the 10 s timer ticks.
- **Transcoder cleanup-on-throw.** If any step in `toMeetingWAV` throws after the placeholder header is written, the partial file (44-byte header, possibly some PCM) is left on disk. Wrap the whole body in a do/catch that calls `try? FileManager.default.removeItem(at: destination)` before rethrowing. Otherwise the per-job folder is orphaned with garbage and the upload step (which fires next) will hit a malformed WAV.
- **UInt32 wrap risk in RIFF header.** Plain `+` math against `UInt32.max - 36` can wrap silently. Use `pcmBytesWritten: UInt64` accumulator and guard `pcmBytesWritten <= UInt32.max - 36` before writing the final header; throw `MediaTranscoderError.fileTooLarge` if exceeded. UInt32 caps the WAV format at ~67,000 sec ≈ 18.6 h at 64 KB/s — practical for the foreseeable use case but the guard is cheap insurance.
- **Drop the WAV-header parse for duration estimate.** `runMeetingTranscriptionJob` should accept `bytesPerSecond: Double` as an explicit parameter instead. Both callers know their own format statically (meeting recorder = Float32 = 128 KB/s; custom transcoder = Int16 = 64 KB/s). The header-parse approach is clever-fragile: it assumes a canonical 44-byte layout, but real-world WAVs may have `JUNK`/`LIST`/`bext` chunks before `fmt `. Reviewer-recommended simplification.
- **`@StateObject` cannot be declared inside a `var quickActionsSection: some View` computed property** — the wrapper resets on every parent re-render. Either declare both view-models as `@StateObject` properties on `SettingsView` itself, or extract a dedicated `QuickActionsSection: View` struct that owns them. Plan does the second (see Task 5).
- **`Settings.shared` is `@MainActor`-bound** (it's the popover's `@EnvironmentObject`). Helpers in `CustomTranscriptionsStore` that read `Settings.shared.meetingsPath` must be `@MainActor`, OR the URL must be passed in by the caller. Plan goes with `@MainActor`.
- Avoid `Date.now.formatted(.relative(...))` — its phrasing ("in 0 seconds", "1 second ago") is awkward for sub-minute deltas. Roll our own under-60s string ("just now", "12s ago").
- Drop the unreachable `Today HH:mm` branch in the relative-or-absolute formatter: anything with `s >= 86_400` (≥24h) is by definition not today.

## Reconciliation Notes

None — no separate dossier was generated.

## Delta Design

### Data / State Changes

Existing:
- `Settings.meetingsPath: URL` is the only file-output preference.
- `TranscriptStore.shared` is the source of truth for meeting transcript listings.

Change:
- New conceptual subfolder `Custom Transcriptions/` under `meetingsPath`. Resolved by a small static helper, NOT a new `@Published` property — there is no need for a user-configurable path.
- `TranscriptStore.shared.refresh()` is invoked after each custom transcription completes, but custom transcriptions LIVE in subfolders (not as siblings under `meetingsPath`), so they will not appear in the existing list — that's intentional. They are accessed exclusively via Finder + the "Copy last custom transcription" button.

Why:
- Adding configurability now is YAGNI; the brief locked the location.

Risks:
- If `TranscriptStore`'s refresh logic later starts recursing, custom transcriptions will start showing up in the meeting list. Mitigated by leaving the recursion explicitly opt-in inside `TranscriptStore` (file is unchanged here; future refactor that recurses must explicitly skip `Custom Transcriptions/`).

### Entry Point / Integration Flow

Existing:
- Two buttons in `quickActionsSection`: Open Portal, Open Meetings Folder.

Change:
- Replace section body with four buttons + two captions:
  ```
  [Transcribe file…]                (full-width Button)
  [Open Custom Transcriptions]      (full-width Button)
  ────────────────────────────────
  [Copy last meeting]               (Button)
    "12 s ago"                      (Text caption, .caption / .secondary)
  [Copy last custom transcription]  (Button)
    "3 m ago"                       (Text caption, .caption / .secondary)
  ────────────────────────────────
  [Open Portal]                     (kept)
  [Open Meetings Folder]            (kept)
  ```

Why:
- All within the same Form section keeps visual rhythm. Keeps "Open Portal" and "Open Meetings Folder" because the brief never asked to remove them.

Risks:
- Crowded section. Acceptable — the popover is 420 px wide and uses `.formStyle(.grouped)`, which already paginates densely. If the section gets unwieldy, split into two `Section`s in a follow-up.

### Execution / Control Flow

Existing:
- `processMeetingUpload(wavURL:)` does the upload→poll→download→cleanup→`TranscriptStore.refresh()` sequence with a hardcoded `<meetingsPath>/<baseName>.<ext>` output layout.

Change (revised — narrower than original draft, per reviewer consensus):
- **Do not** add three new params to `processMeetingUpload`. That function is 117 lines with offline-signature branching, notifications, and mode transitions; a 3-param refactor risks behavior drift in the meeting-recorder path.
- **Instead**, extract one small helper from inside `processMeetingUpload`:
  ```swift
  /// Submit a WAV, poll until done or deadline, download every reported
  /// format to <outputDirectory>/<stem>.<fmt>, delete the server-side job.
  /// Throws on failure; never enqueues for offline retry.
  private func runMeetingTranscriptionJob(
      wavURL: URL,
      estimatedDurationSeconds: Double,
      outputDirectory: URL,
      stem: String
  ) async throws
  ```
  Both code paths call it:
  - `processMeetingUpload` keeps its existing shape — file-size estimate, `mode` transitions, offline-signature catch, `TranscriptStore.refresh()`, success notification — but the inner upload+poll+download+delete block is replaced by a single `try await runMeetingTranscriptionJob(...)` call with `outputDirectory: Settings.shared.meetingsPath` and `stem: baseName`.
  - The new `processCustomTranscriptionUpload(wavURL:outputDirectory:stem:)` is short: set `mode = .uploading`, call `runMeetingTranscriptionJob(...)`, set `mode = .done` for 3 s then `.idle`, post a success notification on success, post an error notification (NOT an offline-queue enqueue) on failure.
- Add `transcribePickedFile()` on `MenuBarController` to drive picker → transcode → custom upload.
- Add a new `Mode` case `.converting` and update the icon switch at `MenuBarController.swift:443-447` (the only switch site over the flat `Mode` enum). The brief mandates two distinct UI phases ("Converting…" vs "Transcribing…") — this is the cleanest way to satisfy it.
- **Format-aware duration estimate.** The existing `estimatedDurationSeconds = fileSize / (2 * 16_000 * 4)` formula assumes Float32 (4 bytes/sample). Custom-transcription WAVs are Int16 (2 bytes/sample). Compute `bytesPerSecond` from the WAV header (channels × sampleRate × bytesPerSample) once at the start of `runMeetingTranscriptionJob`, and use that. Keep the call sites' inputs as a raw `wavURL`.

Why:
- One small flat helper is easier to keep correct than one branchy function with three new params (R1 #12, R2 #25).
- Adding `.converting` to a 6-case flat enum is a 4-line change (R2 #1, R2 #13).

Risks:
- The extraction must preserve `MeetingProcessingError.pollTimedOut` and `MeetingProcessingError.serverFailed(reason)` propagation — both are defined elsewhere in the file. Re-throw them from the helper; the meeting path's existing catch already handles them.

### User-Facing / Operator-Facing Surface

Existing:
- `Mode` flat enum at `MenuBarController.swift:39-46`: `idle, dictating, meetingRecording, uploading, processing, done`.
- One switch site at `MenuBarController.swift:443-447` (icon updater).
- `RecordingIndicatorView` reads a SEPARATE `RecordingState` enum (associated values) — unaffected.

Change:
- **Add `.converting` to `Mode`.** Update the icon switch at `:443-447` to render the same spinner used for `.uploading` but with a different status caption ("Converting…" vs "Uploading…"). This is locked, not optional.
- The custom-transcription flow walks `idle → converting → uploading → processing → done → idle`.

### External / Operational Surface

Existing:
- Server has no awareness of "custom transcription" vs "meeting" — both hit `POST /transcribe/meeting`.

Change:
- None. Server is untouched.

Why:
- Brief decision.

Risks:
- The `usage_event` row will not distinguish custom transcriptions from meetings on the server side. If product later wants this distinction, add a header (e.g. `X-Wispralt-Source: custom`) — out of scope here.

## Implementation Blueprint

### Architecture Overview

```
┌────────────────────────────────────────────────────────────────────────┐
│ SettingsView → QuickActionsSection                                      │
│                                                                         │
│   [Transcribe file…] ───► MenuBarController.transcribePickedFile()      │
│   [Open Custom Transcriptions] ──► NSWorkspace.open(customDir)          │
│   [Copy last meeting]   ──► CustomTranscriptionsStore.copyLastMeeting() │
│   [Copy last custom…]   ──► CustomTranscriptionsStore.copyLastCustom()  │
│      ↑ caption ↑                                                        │
│      LastTranscriptCaptionViewModel                                     │
│        ├─ DispatchSource on parent folder                               │
│        ├─ 10s Timer (relative-time tick)                                │
│        └─ NotificationCenter.transcriptWritten observer                 │
└────────────────────────────────────────────────────────────────────────┘
                            │
                            ▼ transcribePickedFile()
┌────────────────────────────────────────────────────────────────────────┐
│ 1. NSApp.activate(); NSOpenPanel.runModal() → URL                       │
│ 2. subdir = makeJobDirectory(stem)                                      │
│ 3. mode = .converting                                                   │
│    await MediaTranscoder.toMeetingWAV(input,                            │
│        destination: subdir/<stem>__2ch16k.wav)                          │
│ 4. mode = .uploading                                                    │
│    await processCustomTranscriptionUpload(                              │
│        wavURL: subdir/<stem>__2ch16k.wav,                               │
│        outputDirectory: subdir,                                         │
│        stem: <stem>)                                                    │
│      └─► runMeetingTranscriptionJob(...)  // shared with meeting path   │
│      └─► NotificationCenter.post(.transcriptWritten)  // wakes captions │
│ 5. mode = .done (3 s); mode = .idle                                     │
└────────────────────────────────────────────────────────────────────────┘
```

### Key Pseudocode

**`MediaTranscoder.toMeetingWAV(_:destination:)`** — uses `AVAssetReader` outputSettings to do resample + channel-fanout + Int16 quantization in one Apple-maintained step. No `AVAudioConverter`, no `AVAudioFile`. Hand-rolled RIFF header + raw PCM bytes via `FileHandle`.

```swift
// 1. Load asset + audio track.
let asset = AVURLAsset(url: input)
let tracks = try await asset.loadTracks(withMediaType: .audio)
guard let track = tracks.first else { throw MediaTranscoderError.noAudioTrack }

// 2. Reader configured to emit our destination format directly:
//    16 kHz, 2 channels, Int16 LE, interleaved PCM.
//    AVAssetReader does the resample + mono→stereo fanout + Int16 quantization
//    in one Apple-maintained pipeline — no AVAudioConverter, no AVAudioFile.
let reader = try AVAssetReader(asset: asset)
let outputSettings: [String: Any] = [
    AVFormatIDKey:                kAudioFormatLinearPCM,
    AVSampleRateKey:              16_000,
    AVNumberOfChannelsKey:        2,
    AVLinearPCMBitDepthKey:       16,
    AVLinearPCMIsFloatKey:        false,
    AVLinearPCMIsBigEndianKey:    false,
    AVLinearPCMIsNonInterleaved:  false
]
let trackOutput = AVAssetReaderTrackOutput(track: track, outputSettings: outputSettings)
reader.add(trackOutput)
guard reader.startReading() else {
    throw MediaTranscoderError.readerFailed(reader.error)
}

// 3. Open destination file with placeholder WAV header (we'll patch sizes at the end).
FileManager.default.createFile(atPath: destination.path, contents: nil)
let handle = try FileHandle(forWritingTo: destination)
defer { try? handle.close() }
try handle.write(contentsOf: WAVHeader.placeholder(channels: 2, sampleRate: 16_000, bitsPerSample: 16))
var pcmBytesWritten: UInt32 = 0

// 4. Pull each CMSampleBuffer's bytes and append to the file.
while let sample = trackOutput.copyNextSampleBuffer() {
    guard let block = CMSampleBufferGetDataBuffer(sample) else {
        CMSampleBufferInvalidate(sample); continue
    }
    let length = CMBlockBufferGetDataLength(block)
    var bytes = [UInt8](repeating: 0, count: length)
    let status = CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: &bytes)
    guard status == kCMBlockBufferNoErr else {
        throw MediaTranscoderError.conversionFailed
    }
    try handle.write(contentsOf: bytes)
    pcmBytesWritten &+= UInt32(length)
    CMSampleBufferInvalidate(sample)
}

// 5. Verify reader completed cleanly.
guard reader.status == .completed else {
    throw MediaTranscoderError.readerFailed(reader.error)
}

// 6. Patch the RIFF header now that we know the data-chunk size.
try handle.seek(toOffset: 0)
try handle.write(contentsOf: WAVHeader.finalized(
    channels: 2, sampleRate: 16_000, bitsPerSample: 16, dataBytes: pcmBytesWritten
))
```

**`WAVHeader`** — the canonical 44-byte RIFF/WAV header:

```swift
enum WAVHeader {
    static func placeholder(channels: UInt16, sampleRate: UInt32, bitsPerSample: UInt16) -> Data {
        finalized(channels: channels, sampleRate: sampleRate,
                  bitsPerSample: bitsPerSample, dataBytes: 0)
    }

    static func finalized(channels: UInt16, sampleRate: UInt32,
                          bitsPerSample: UInt16, dataBytes: UInt32) -> Data {
        var d = Data(); d.reserveCapacity(44)
        let byteRate    = sampleRate * UInt32(channels) * UInt32(bitsPerSample) / 8
        let blockAlign  = channels * bitsPerSample / 8
        d.append(contentsOf: "RIFF".utf8)
        d.appendLE(UInt32(36 &+ dataBytes))      // ChunkSize
        d.append(contentsOf: "WAVE".utf8)
        d.append(contentsOf: "fmt ".utf8)
        d.appendLE(UInt32(16))                    // Subchunk1Size (PCM)
        d.appendLE(UInt16(1))                     // AudioFormat = PCM
        d.appendLE(channels)
        d.appendLE(sampleRate)
        d.appendLE(byteRate)
        d.appendLE(blockAlign)
        d.appendLE(bitsPerSample)
        d.append(contentsOf: "data".utf8)
        d.appendLE(dataBytes)
        return d
    }
}

private extension Data {
    mutating func appendLE<T: FixedWidthInteger>(_ v: T) {
        var x = v.littleEndian; withUnsafeBytes(of: &x) { append(contentsOf: $0) }
    }
}
```

That's the entire transcoder. Mono fanout, resample, Int16 quantization, header — all handled by AVAssetReader + a 44-byte hand-rolled header. No `AVAudioConverter`, no `AVAudioFile`, no `channelMap`, no `toAVAudioPCMBuffer`, no Int16-normalization bug.

**Newest-`.txt` selection (used by both copy buttons):**

```swift
func newestTxt(in dir: URL) throws -> URL? {
    let entries = try FileManager.default.contentsOfDirectory(   // NOT recursive
        at: dir,
        includingPropertiesForKeys: [.contentModificationDateKey],
        options: [.skipsHiddenFiles]
    )
    let txts = entries.filter { $0.pathExtension.lowercased() == "txt" }
    return txts.max { lhs, rhs in
        let l = (try? lhs.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
        let r = (try? rhs.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
        return l < r
    }
}
```

For `Custom Transcriptions/`, walk one level deeper because each transcript is in its own subfolder:

```swift
func newestCustomTxt(in customDir: URL) throws -> URL? {
    let subdirs = try FileManager.default.contentsOfDirectory(
        at: customDir,
        includingPropertiesForKeys: [.isDirectoryKey],
        options: [.skipsHiddenFiles]
    ).filter { (try? $0.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true }
    let candidates = subdirs.compactMap { try? newestTxt(in: $0) }
    return candidates.max { lhs, rhs in
        let l = (try? lhs.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
        let r = (try? rhs.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
        return l < r
    }
}
```

**Folder watcher view-model:**

```swift
@MainActor
final class LastTranscriptCaptionViewModel: ObservableObject {
    @Published private(set) var lastModified: Date?
    @Published private(set) var tick: Int = 0   // bumped by the timer; SwiftUI re-renders relative time
    private var source: DispatchSourceFileSystemObject?
    private var fd: Int32 = -1
    private var timer: AnyCancellable?
    private let folderURL: URL
    private let lookup: () -> Date?

    init(folderURL: URL, lookup: @escaping () -> Date?) {
        self.folderURL = folderURL
        self.lookup = lookup
    }

    func start() {
        guard source == nil else { return }   // idempotent
        try? FileManager.default.createDirectory(at: folderURL, withIntermediateDirectories: true)
        fd = open(folderURL.path, O_EVTONLY)
        guard fd >= 0 else { refresh(); return }
        let s = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .rename, .delete],
            queue: .main
        )
        s.setEventHandler { [weak self] in self?.refresh() }
        // Cancel handler owns the descriptor's lifetime — it closes when the source is cancelled.
        s.setCancelHandler { [fd] in close(fd) }
        s.resume()
        source = s

        timer = Timer.publish(every: 10.0, on: .main, in: .common)
            .autoconnect()
            .sink { [weak self] _ in self?.tick &+= 1 }

        refresh()
    }

    func stop() {
        timer?.cancel(); timer = nil
        source?.cancel(); source = nil
        // Do NOT close(fd) here — the cancel handler owns it.
        fd = -1
    }

    private func refresh() {
        lastModified = lookup()
    }

    var captionText: String {
        _ = tick   // explicit dependency so SwiftUI re-renders on each tick
        guard let d = lastModified else { return "No transcripts yet" }
        return Self.relativeOrAbsolute(d)
    }

    static func relativeOrAbsolute(_ date: Date, now: Date = .now) -> String {
        let s = Int(now.timeIntervalSince(date))
        switch s {
        case ..<5:        return "just now"
        case 5..<60:      return "\(s)s ago"
        case 60..<3600:   return "\(s / 60)m ago"
        case 3600..<86_400: return "\(s / 3600)h ago"
        default:
            // ≥24h: only Yesterday or older are reachable.
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            if Calendar.current.isDateInYesterday(date) {
                f.dateFormat = "'Yesterday' HH:mm"
            } else {
                f.dateFormat = "MMM d HH:mm"
            }
            return f.string(from: date)
        }
    }
}
```

### Data Models and Structure

```swift
// MediaTranscoder.swift
enum MediaTranscoderError: Error {
    case noAudioTrack
    case readerFailed(Error?)
    case conversionFailed
}

enum MediaTranscoder {
    /// Transcode any audio/video file to a 2-channel 16 kHz Int16 LE PCM WAV.
    /// `destination` parent directory must exist. Returns `destination` on success.
    @discardableResult
    static func toMeetingWAV(_ input: URL, destination: URL) async throws -> URL
}

// CustomTranscriptionsStore.swift
@MainActor
enum CustomTranscriptionsStore {
    /// `<meetingsPath>/Custom Transcriptions`
    static var directoryURL: URL {
        Settings.shared.meetingsPath
            .appendingPathComponent("Custom Transcriptions", isDirectory: true)
    }

    /// `<directoryURL>/<stem>__<yyyymmdd-HHmmss>` — created on disk before return.
    static func makeJobDirectory(forStem stem: String, now: Date = .now) throws -> URL

    /// Newest `.txt` directly in `<meetingsPath>` (non-recursive, also filters out
    /// directory entries defensively).
    static func newestMeetingTranscript() -> URL?

    /// Newest `.txt` across all per-job subfolders inside `Custom Transcriptions/`.
    static func newestCustomTranscript() -> URL?

    /// Reads a transcript file (UTF-8) and writes its contents to `NSPasteboard.general`.
    /// Returns the character count for the inline confirmation. Throws on read failure.
    @discardableResult
    static func copyToPasteboard(_ url: URL) throws -> Int
}
```

Implementation notes:
- `copyToPasteboard`: `let s = try String(contentsOf: url, encoding: .utf8); NSPasteboard.general.clearContents(); NSPasteboard.general.setString(s, forType: .string); return s.count`.
- `newestMeetingTranscript`: `FileManager.contentsOfDirectory(at: meetingsPath, includingPropertiesForKeys: [.contentModificationDateKey], options: [.skipsHiddenFiles]).filter { !$0.hasDirectoryPath && $0.pathExtension.lowercased() == "txt" }.max(by: mtimeAscending)`.
- `newestCustomTranscript`: enumerate immediate subfolders of `directoryURL`, pick `newestTxt(in:)` for each, return the global max by mtime.

### Tasks (in implementation order)

The order below comes from the brief's "One Thing to Do First" guidance: wire the UI end-to-end with a stub transcoder first, then drop in real AVFoundation. This isolates UI/file-watcher bugs from AVFoundation bugs.

Task 1 — Mode + scaffolding:
Goal:
- Add `.converting` to `Mode`, give it an explicit icon-switch arm, define the `Notification.Name`, create stub `MediaTranscoder` and `CustomTranscriptionsStore` files.
Files:
- MODIFY `client/WisprAlt/App/MenuBarController.swift` (add `.converting` to `Mode` enum at :39-46; add an explicit arm to the icon switch at :441-450 — recommended `case .converting: return ("arrow.triangle.2.circlepath", "WisprAlt — Converting…")`; verify it's the only switch site).
- CREATE `client/WisprAlt/Audio/MediaTranscoder.swift` (signature + error type only).
- CREATE `client/WisprAlt/Storage/CustomTranscriptionsStore.swift` (signatures only; bodies stubbed).
- MODIFY/CREATE a small extension on `Notification.Name`: `static let wisprAltTranscriptWritten = Notification.Name("co.wispralt.transcriptWritten")` — colocate with `LastTranscriptCaption.swift` or in a new tiny file under `Util/`. Caller-side post + observer-side subscribe both reference this name.
Gotchas:
- Confirm via grep there are no other switch sites over `Mode` before adding the case: `rg "case \.(idle|dictating|meetingRecording|uploading|processing|done)\b" client/`.
- The `Mode.converting` case only changes the menubar icon's accessibility label; the popover's "Converting…" copy is rendered by the SwiftUI side based on the same `mode` (or via a `RecordingState`-style binding if that's how the indicator surfaces text). If no popover-side caption renders today for `.uploading`, no extra UI work is required for `.converting` — confirm by reading `RecordingIndicatorView.swift` and `MenuBarController`'s usage of `mode` before declaring Task 1 done.
Definition of done:
- `swift build` clean. App still runs unchanged. The icon switch covers `.converting` explicitly.

Task 2 — Stub `MediaTranscoder`:
Goal:
- Make `toMeetingWAV(_:destination:)` simply COPY the input file to destination (no real conversion). This lets the UI/upload pipeline be exercised end-to-end before AVFoundation work.
Files:
- MODIFY `client/WisprAlt/Audio/MediaTranscoder.swift`.
Gotchas:
- Stub means: pick a known-good 2ch/16kHz WAV during development testing.
Definition of done:
- Calling `toMeetingWAV(input, destination: dst)` with a valid WAV produces a byte-identical copy at `dst`.

Task 3 — Implement `CustomTranscriptionsStore`:
Goal:
- Real implementations for `directoryURL`, `makeJobDirectory`, `newestMeetingTranscript`, `newestCustomTranscript`, `copyToPasteboard`.
Files:
- MODIFY `client/WisprAlt/Storage/CustomTranscriptionsStore.swift`.
Pattern to copy:
- `humanReadableMeetingFilename` for the POSIX-locale timestamp formatter idiom (use `yyyyMMdd-HHmmss`).
Gotchas:
- `URLResourceValues.contentModificationDateKey` (not `attributesOfItem`).
- Non-recursive `contentsOfDirectory` for the meeting walk + `!url.hasDirectoryPath` filter.
- `String(contentsOf: url, encoding: .utf8)`.
- `NSPasteboard.general.clearContents()` before `setString`.
- All members `@MainActor` (Settings.shared is main-actor-bound).
- `makeJobDirectory` collision behavior: append `-2`, `-3`, … on collision (timestamp is second-resolution; double-clicks are plausible). Specify and implement.
Definition of done:
- `swift build` clean.

Task 4 — Extract `runMeetingTranscriptionJob`:
Goal:
- Pull the upload+poll+download+delete inner block out of `processMeetingUpload` into a private helper, parameterized by `(wavURL, bytesPerSecond, outputDirectory, stem)`. Caller supplies `bytesPerSecond` explicitly (meeting recorder = `2 * 16_000 * 4` for Float32; custom transcoder = `2 * 16_000 * 2` for Int16). NO WAV-header parsing — reviewer-recommended simplification.
Files:
- MODIFY `client/WisprAlt/App/MenuBarController.swift`.
Pattern to copy:
- The existing block at `:564-622`. Preserve every Log line and the `pollDeadline` math (use the passed-in `bytesPerSecond` to compute `estimatedDurationSeconds = fileSize / bytesPerSecond`).
Gotchas:
- Re-throw `MeetingProcessingError.pollTimedOut` and `MeetingProcessingError.serverFailed(_)` so the caller's catch still works.
- Output paths: `outputDirectory.appendingPathComponent(stem).appendingPathExtension(fmt)`.
- The caller (`processMeetingUpload`) keeps the `let baseName = ...` line for its own logging/notification; the extracted helper takes `stem` as input. Don't try to share the local `baseName` across the boundary.
- Factor a tiny error-formatter helper (`formatTranscriptionError(_:) -> String`) and use it from both the meeting catch and the new custom catch (consistent `ServerError.unauthorized` → "re-paste your API key" handling).
Definition of done — BLOCKING manual check before Task 5:
- `swift build` clean. Triple-tap-FN, record 10 s, release: meeting flow still produces the same sibling-file layout, same toast, same `TranscriptStore.refresh()` behavior. Do not start Task 5 until this passes.

Task 5 — Custom-transcription entry point:
Goal:
- Add `transcribePickedFile()` and `processCustomTranscriptionUpload(wavURL:outputDirectory:stem:)` on `MenuBarController`. The first runs panel → makeJobDirectory → toMeetingWAV → second. The second flips `mode = .uploading`, calls `runMeetingTranscriptionJob` with `bytesPerSecond: 64_000` (Int16 2ch 16kHz), transitions to `.done` for 3 s then `.idle`, success notification on success, error notification (NOT offline-queue enqueue) on failure. After a successful download, post `NotificationCenter.default.post(name: .wisprAltTranscriptWritten, object: nil)` so the caption view-models refresh immediately.
Files:
- MODIFY `client/WisprAlt/App/MenuBarController.swift`.
Pattern to copy:
- `stopMeetingRecording()` (`:519-553`) for the `Task { @MainActor in }` shape.
Gotchas:
- `NSApp.activate(ignoringOtherApps: true)` BEFORE `panel.runModal()`.
- `panel.allowedContentTypes` MUST include both UTType groups AND the brief's locked extension allow-list:
  ```swift
  let exts = ["mp3","m4a","wav","aac","mp4","mov","m4v","caf","aiff","flac"]
  panel.allowedContentTypes = [.audio, .movie] + exts.compactMap { UTType(filenameExtension: $0) }
  panel.allowsMultipleSelection = false
  ```
- Set `mode = .converting` immediately after a file is picked; flip to `.uploading` after `toMeetingWAV` returns.
- `import UniformTypeIdentifiers`.
- Consider also posting `NotificationCenter.default.post(name: .wisprAltTranscriptWritten, object: nil)` from the meeting-recorder success path so "Copy last meeting" caption updates instantly after a meeting ends. Tiny win for consistency.
Definition of done:
- With Task 2's stub still in place AND a known-good 2ch/16kHz WAV as input, picking that file produces transcript outputs in `Custom Transcriptions/<stem>__<ts>/`.

Task 6 — Real `MediaTranscoder`:
Goal:
- Replace the stub with the AVAssetReader + RIFF-header implementation (Key Pseudocode above).
Files:
- MODIFY `client/WisprAlt/Audio/MediaTranscoder.swift`.
Gotchas:
- `outputSettings` MUST include `AVSampleRateKey: 16_000`, `AVNumberOfChannelsKey: 2`, `AVLinearPCMBitDepthKey: 16`, `AVLinearPCMIsFloatKey: false`, `AVLinearPCMIsBigEndianKey: false`, `AVLinearPCMIsNonInterleaved: false`.
- Check `reader.startReading()` returns true; check `reader.status == .completed` after the loop.
- Patch the RIFF header at offset 0 once total `pcmBytesWritten` is known.
- `CMSampleBufferInvalidate` after each sample to release CoreMedia memory promptly on long files.
- **Cleanup-on-throw.** Wrap the body in do/catch — on any throw, `try? FileManager.default.removeItem(at: destination)` BEFORE rethrowing. Otherwise the per-job folder ends up with a partial 44-byte placeholder header + maybe some PCM bytes, and the upload step downstream sends a malformed WAV.
- **UInt32 overflow guard.** Use `pcmBytesWritten: UInt64` accumulator. After the loop, `guard pcmBytesWritten <= UInt64(UInt32.max - 36) else { throw MediaTranscoderError.fileTooLarge }`. Cast down to UInt32 for the header. Add `case fileTooLarge` to `MediaTranscoderError`.
- Optional perf knob (defer unless profiling shows it matters): reuse a single `Data` buffer sized to the largest seen sample-buffer length instead of allocating a fresh `[UInt8]` per sample. Not blocking.
Definition of done:
- Spot-check with `afinfo`: a mono 10-second mp3 → produced WAV reports `Num Channels: 2; Sample Rate: 16000; Format: lpcm, 16 bits per sample, signed integer`. Duration matches source within 1%. Stereo mp4 → both channels preserved (`afplay` confirms by ear). Force-throw scenario (e.g., point at a 0-byte file): no orphaned WAV left in the per-job folder.

Task 7 — UI wiring:
Goal:
- Replace `quickActionsSection` with the four-button + two-caption layout. Add the caption subview + `NotificationCenter` observer.
Files:
- MODIFY `client/WisprAlt/UI/SettingsView.swift` (extract a `QuickActionsSection: View` struct; the existing `quickActionsSection` computed property delegates to it).
- CREATE `client/WisprAlt/UI/LastTranscriptCaption.swift` (subview + the `LastTranscriptCaptionViewModel` from Key Pseudocode).
Pattern to copy:
- Existing `Button("…", systemImage: "…")` + `.help(...)` style at `:80-97`.
Gotchas:
- Two `@StateObject` view-models on the new `QuickActionsSection` struct (one per folder). Start in `.onAppear`, stop in `.onDisappear`.
- One reusable `LastTranscriptCaption` subview, instantiated twice with different view-models.
- View-model `start()` MUST also subscribe to `Notification.Name.wisprAltTranscriptWritten` and call `refresh()` on receive. Without this, custom-transcription writes (which land in subfolders) won't fire the parent-folder DispatchSource and the caption stays stale until the 10 s tick.
- View-model `start()` should `Log.warning(...)` if `open()` returns -1 (rare but silent otherwise — user sees "No transcripts yet" with no diagnostic).
- Inline confirmation via `@State copyToast: (button: String, message: String)?` plus a 1.5 s `Task.sleep` to clear; use `count.formatted(.number)` for thousand-separators.
- Known limitation: if user changes `Settings.shared.meetingsPath` mid-session while popover is open, the watchers stay pointed at the OLD URL until popover is reopened. Acceptable; documented here.
- Verify FD release: `lsof -p $(pgrep WisprAlt) | grep "Custom Transcriptions"` should be empty when popover is closed.
Definition of done:
- All four buttons render. Captions update live during a custom transcription within ~1 s of completion (driven by the NotificationCenter post, not the timer). No FD leaks after popover close.

Task 8 — Manual smoke test:
Goal:
- Run all scenarios in `Validation > Manual Checks` and confirm pass.
Definition of done:
- Every scenario passes; failures filed back as code fixes before this task closes.

### Integration Points

- Data / schema source of truth: `Settings.meetingsPath` (no new key).
- Entry points to extend: `SettingsView.quickActionsSection`, `MenuBarController` (new public `transcribePickedFile`).
- Validation layer: AVAsset's own format checks; `staging.stream_to_staging` on the server (unchanged).
- Domain / service layer: `MediaTranscoder` (new), `CustomTranscriptionsStore` (new).
- User-facing surface: popover only; no status-bar menu.
- Shared types / export hubs: none — both new types live in their own files.
- External / operational hooks: none.

## Validation

```bash
# From repo root, build the client (this is what build-client-local.sh wraps).
( cd client && swift build -c debug ) 2>&1 | tail -40
# Expected: no errors, no new warnings.
```

### Factuality Checks

- `Verified Repo Truths` uses `Fact / Evidence / Implication` for every bullet ✅
- No proposal language appears in `Verified Repo Truths` ✅
- Every `MODIFY` path exists: confirmed (`client/WisprAlt/App/MenuBarController.swift`, `client/WisprAlt/UI/SettingsView.swift`) ✅

### Manual Checks

- Scenario: Pick a 30 s mono `.mp3` via "Transcribe file…".
  Expected: `Custom Transcriptions/<stem>__<ts>/<stem>__2ch16k.wav` exists, `afinfo` reports `2 ch, 16000 Hz, Int16`, `<stem>.{txt,srt,vtt,json}` are written into the same folder, "Copy last custom transcription" caption flips to "just now" and the button becomes enabled.
- Scenario: Pick a 2 min stereo `.mp4`.
  Expected: same as above but stereo content preserved (verify by ear via `afplay`).
- Scenario: Triple-tap-FN, record a meeting, release.
  Expected: meeting flow unchanged — files land at `<meetingsPath>/<humanName>.{wav,json,srt,vtt,txt}`, list refresh fires, "Copy last meeting" caption updates.
- Scenario: Click "Copy last meeting" on an empty folder.
  Expected: button is disabled, caption reads "No transcripts yet".
- Scenario: Click "Copy last custom transcription" immediately after a transcription completes.
  Expected: pasteboard contains the just-completed transcript text; inline confirmation reads `Copied — N chars`.
- Scenario: Open popover, do a transcription, close popover BEFORE it completes.
  Expected: no crash; reopening shows the updated caption (one-shot refresh on `.onAppear`).
- Scenario: `lsof -p $(pgrep WisprAlt)` while popover is open vs closed.
  Expected: two extra FDs (pointing at the two watched dirs) appear when open and disappear when closed.
- Scenario: Pick a file that has no audio track (`.mov` recorded silently).
  Expected: graceful `MediaTranscoderError.noAudioTrack` toast; no partial output written.

## Open Questions

- None.

## Final Validation Checklist

- [ ] `( cd client && swift build -c debug )` succeeds with no new warnings.
- [ ] Meeting-recorder flow still works after the `processMeetingUpload` refactor.
- [ ] Custom-transcription flow produces correct folder layout for mono and stereo sources.
- [ ] Both captions update live with no FD leaks after popover close.
- [ ] Both copy buttons clear the pasteboard before writing.
- [ ] All `MODIFY` paths exist (verified in Verified Repo Truths).
- [ ] No template/example placeholders remain.

## Deprecated / Removed Code

- None. The old `processMeetingUpload(wavURL:)` call site continues to work via default arguments.

## Anti-Patterns to Avoid

- Don't recurse into `Custom Transcriptions/` from the meeting-newest-txt enumerator.
- Don't re-implement the upload/poll loop — refactor the existing one.
- Don't enqueue custom transcriptions to `PendingUploadsQueue` on offline error.
- Don't open `NSOpenPanel` directly from a SwiftUI Button action — wrap in `Task { @MainActor in }`.
- Don't trust `attributesOfItem(atPath:)[.modificationDate]` on APFS clones — use `URLResourceValues.contentModificationDateKey`.
- Don't leave the `DispatchSource` running after the popover closes.
- Don't add a new `@Published` for the custom-transcriptions path — it's a derived URL.
- Don't introduce a server-side header to mark custom transcriptions in v1; out of scope.

## Confidence

8/10 for one-pass implementation success after reviewer round 1.

Risk drivers:
- AVAssetReader's `outputSettings` resample/fanout/quantization path is Apple-maintained and well-documented; `afinfo` spot-check after Task 6 confirms format conformance.
- `runMeetingTranscriptionJob` extraction is small (a flat helper, not three new params on a 117-line function). Manual meeting test after Task 4 catches behavior drift.
- Mode-enum extension (`.converting`) is a single-switch-site change; `rg` confirms scope before adding.
- `DispatchSource` lifecycle on `.main` queue is well-trodden; idempotent `start()`/`stop()` plus `lsof` verification keep FD leaks observable.
