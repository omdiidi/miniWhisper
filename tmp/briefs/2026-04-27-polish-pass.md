# Brief: Polish pass — mic selector, human meeting filenames, cleaner REC indicator, API-key copy button

## Why

Four user-reported polish items on top of the be720a1 / 5ed4ec7 baseline:

1. **Mic selector in the menubar popover** — user wants to pick which input
   device WisprAlt records from (AirPods, built-in, USB mic). Currently the
   client always uses the macOS default input. There's mid-recording
   abort-on-device-change logic but no user-facing "I want this mic"
   control.
2. **Meeting filename format** — current format
   `2026-04-27_1543-0700_meeting.wav` is ISO-style. The user wants
   human-readable, e.g. `Mon Apr 27 2:06-4:50pm.wav` so meetings are
   recognizable in Finder at a glance.
3. **REC indicator polish** — the be720a1 dot+REC combo renders the bold
   "REC" text and the solid red dot with awkward baseline alignment in some
   menubar densities. Should also reflect recording state in the new mic
   selector once that lands.
4. **Copy API key button** — in the Advanced section next to Export/Import,
   add a one-click "Copy API Key" button that puts the current Keychain
   value on the clipboard. Saves the user from having to export-to-file
   when pasting the key into a one-shot context.

## Context

Files in scope:

- `client/WisprAlt/App/MenuBarController.swift`:
  - `updateIcon()` (~L249-300) — current REC drawing path: custom red dot
    via NSBezierPath + bold red " REC" attributed title. The vertical
    alignment between the dot image and the title text is what reads as
    "weirdly positioned".
  - `startMeetingRecording()` (~L344-360) — generates the meeting filename
    using `DateFormatter` with `dateFormat = "yyyy-MM-dd_HHmmZZZZZ"` and
    appends `_meeting.wav`. We have START time at filename gen but NOT
    end time (recording is still going).
  - `processMeetingUpload()` (~L389+) — runs after stop. We know END time
    here. The function already renames files indirectly via outputs paths,
    so this is the natural place to compute the end-time-aware filename.

- `client/WisprAlt/Capture/`:
  - `MeetingRecorder.swift` — uses default system input. Has an
    `AudioDeviceListener` that watches for default-input-device changes
    and aborts mid-recording. No "use a specific device" code path yet.
  - `DictationRecorder.swift` — same default-input behavior.
  - `AudioDeviceListener.swift` — CoreAudio HAL listener; could be
    extended to enumerate available input devices via
    `AudioObjectGetPropertyData` with `kAudioHardwarePropertyDevices`.

- `client/WisprAlt/UI/SettingsView.swift` — the Quick Actions section is
  the natural home for the mic selector (top, before Connection). Add a
  SwiftUI `Picker` reading from a fresh `MicEnumerator` helper.

- `client/WisprAlt/App/Settings.swift` (or similar) — add a stored
  property `preferredInputDeviceUID: String?` (nil = system default).
  Bound to the picker's `selection`.

- Server side: NO changes needed for any of these three items. Rename
  is purely client-side; the server only sees uploads.

## Decisions

### 1. Mic selector — SEPARATE macOS menubar dropdown

- **Placement**: a SECOND `NSStatusItem` ("MicMenuBarController") in
  the macOS menubar, positioned right beside the existing WisprAlt
  status item. Click → shows a native `NSMenu` dropdown with all
  available input devices. Current selection has a checkmark. Click a
  device to switch. NO popover — pure NSMenu, native macOS pattern.
- **Reasoning**: User explicitly wants this AT THE TOP of the menu
  bar, not inside the popover. A separate status item is the standard
  macOS idiom for "frequently changed audio I/O" (matches how
  third-party apps like Wave Link / Loopback expose mic selection).
- **Icon**: SF Symbol `mic` template-tinted, matching the existing
  status item's monochrome styling.
- **Menu structure**:
  ```
  Input Mic
  ─────────────────────
  System Default (current: MacBook Pro Microphone)
  ─────────────────────
  ✓ MacBook Pro Microphone
    AirPods Pro
    USB Audio Interface
  ─────────────────────
  Open Sound Settings…
  ```
- **API**: AVFoundation `AVCaptureDevice.DiscoverySession` with
  device types `[.builtInMicrophone, .external, .microphone]` and
  media type `.audio`. Returns `[AVCaptureDevice]` with `.uniqueID`
  (stable across reboots) and `.localizedName`. Refresh menu items
  on every menu open via `NSMenuDelegate.menuWillOpen`.
- **Persistence**: New `Settings.shared.preferredInputDeviceUID:
  String?` (nil = "System default"). Stored in UserDefaults.
- **Apply to recorders**:
  - `DictationRecorder` (AVAudioEngine): set the input device on
    `engine.inputNode.audioUnit` via
    `AudioUnitSetProperty(kAudioOutputUnitProperty_CurrentDevice)`.
    Translate AVCaptureDevice.uniqueID → AudioDeviceID via
    `kAudioHardwarePropertyTranslateUIDToDevice`.
  - `MeetingRecorder` (SCStream): SCStream uses the system default
    mic and has no per-stream device API. Decision: **temporarily
    override** `kAudioHardwarePropertyDefaultInputDevice` to the
    user's choice before SCStream starts; restore on stop. Acceptable
    because: (a) only happens during a meeting recording, (b) user
    explicitly chose the device, (c) other apps using audio during
    the meeting will also use the chosen device which matches
    intent.
- **Live-switch behavior**: changing the menu mid-recording updates
  the saved preference for the next session but does NOT swap the
  current recording's input (avoids audible glitches). Show a
  toast "Mic switched to AirPods. Active recording is unchanged."
- **Reflect recording state**: when `mode == .meetingRecording` or
  `.dictating`, the mic status item's icon is rendered with a
  red tint (template off, contentTintColor=systemRed) so a glance
  shows recording state. When idle, the icon is back to template
  monochrome.

### 2. Human-readable meeting filenames

- **Target format**: `EEE MMM d h:mma-h:mma.wav`, lowercased
  am/pm. Examples:
  - `Mon Apr 27 2:06pm-4:50pm.wav`
  - `Tue Mar 5 9:00am-10:30am.wav`
- **Reasoning**: Matches user request "Mon Apr 27 2:06-4:50pm" while
  including am/pm on both ends to disambiguate cross-noon meetings.
- **When the rename happens**: The recording file is initially written
  to a temp path (or with the existing ISO-style name) at start. After
  `stopMeetingRecording()` returns the wavURL, BEFORE
  `processMeetingUpload()` runs, rename the WAV to the human-readable
  name. The renamed wav becomes the source for upload + the
  `baseName` used to derive transcript outputs.
- **Filesystem-safe sanitization**: Colons in `2:06pm` are filesystem-
  problematic on some FS layers and inside zip archives — REPLACE with
  `.` (`2.06pm-4.50pm.wav`). Explicit decision: clarity over time
  punctuation purity. The Finder will still read the colons fine on
  HFS+/APFS but downstream tools (rsync to Linux, zip distribution,
  Windows visitors) won't.
  - **Updated decision**: use period not colon →
    `Mon Apr 27 2.06pm-4.50pm.wav`. Compact and unambiguous.
- **Collision handling**: Two meetings ending at the same minute (rare
  but possible) → append `(2)`, `(3)`, etc. Use `FileManager.default`
  + a check loop.
- **Backwards compat**: Old `_meeting.wav` files in the meetings dir
  stay untouched. New format only applies to new recordings. The
  client lists meetings by filesystem mtime, not by parsing the
  filename, so old files still display fine.
- **Sidecar files** (.json, .srt, .vtt, .txt): same baseName as the
  wav, written by `processMeetingUpload`. Already follows the wav's
  basename, so renaming the wav before processing is sufficient.

### 4. Copy API key button

- **Placement**: in the existing `apiKeyExportImportSection` HStack
  inside `SettingsView.swift`. Add a third button: **Copy API Key**
  alongside Export and Import.
- **Behavior**: read the current value from Keychain via
  `KeychainHelper.getAPIKey()`. If non-nil, write to general pasteboard
  with `NSPasteboard.general.clearContents()` +
  `NSPasteboard.general.setString(key, forType: .string)`. Show a
  transient "Copied!" caption below the buttons that fades after ~2 s.
- **Auto-clear after 60 s**: after copy, schedule a Task that, after
  60 s of sleep, checks if the pasteboard still contains our key —
  if so, clear it. Avoids leaving the secret on the system clipboard
  indefinitely. (Pattern from password managers.)
- **Empty-state guard**: if `getAPIKey()` returns nil (no key set),
  the button is disabled with help text "Paste an API key first".
- **Logging**: never log the key value. Log only "API key copied to
  clipboard" + "auto-cleared from clipboard" at info level.

### 3. REC indicator polish

- **Root cause confirmed by screenshot**: in the user's menubar the
  bold "REC" attributed title is being rendered **vertically** — R on
  top of E with C cut off — next to the red dot. The menubar height
  doesn't accommodate the horizontal word, so macOS character-wraps
  the title. The dot+title pair is unreadable.
- **Fix**: render the entire indicator (red dot + "REC" text) as a
  **single composite NSImage**, drawn together with shared baseline
  control via NSAttributedString + NSBezierPath in the same drawRect.
  Set the composite as `button.image`, clear `attributedTitle`, set
  `imagePosition = .imageOnly`. This guarantees pixel-perfect
  alignment and lets us tune the dot-to-text spacing in code.
- **Composite drawing**:
  - Image size: ~36×14pt (auto-sized based on text width).
  - Dot: 8pt diameter, vertically centered on the text baseline, 2pt
    leading padding.
  - Text: SF Pro Bold 11pt, red, baseline-aligned to the dot center.
  - 3pt gap between dot and "R".
- **Reflect recording state in the mic selector** (per task 1
  decision): the picker is disabled and shows "Recording — cannot
  change mic" as its label while `mode == .meetingRecording` or
  `.dictating`. No additional mirror needed in the menubar icon
  itself; the existing red dot is the canonical indicator.

## Rejected Alternatives

- **CoreAudio HAL enumeration for the mic selector** — works but
  produces low-level `AudioDeviceID` numbers and requires extra
  plumbing for `kAudioObjectPropertyName`/`kAudioDevicePropertyStreams`.
  AVFoundation is cleaner and gives stable UIDs for free.
- **ISO-only meeting filenames with a separate display-name sidecar** —
  more complex (two names per meeting), and Finder would still show
  the ISO. Renaming the file is simpler.
- **Keeping `_meeting.wav` suffix in the human-readable name** —
  redundant in a folder that only contains meetings. Drop it.
- **Mid-recording mic switch without abort** — would require swapping
  AVCaptureSession inputs without dropping audio buffers. Hard to do
  reliably across device types (built-in vs USB vs Bluetooth). Not
  worth the engineering cost when the existing abort-and-restart
  path already works.
- **Animated pulsing dot** for the REC indicator — drew this earlier;
  rejected as distracting in the menubar context. A solid red dot is
  the macOS-native idiom (matches QuickTime, screen recording).

## Direction

Three independent, parallel-shippable changes — all client-only, no
server work needed. Estimated total: ~250 LOC of Swift + ~30 LOC of
SwiftUI + a 60-line new file `MicEnumerator.swift`. Order them as
(1) MicEnumerator + Settings field, (2) SettingsView Picker integration
+ recorder wiring, (3) Filename rename in
`stopMeetingRecording`/`processMeetingUpload`, (4) REC composite
NSImage in `updateIcon`. Build, sign, install, and verify each step
end-to-end on the macbook before committing.
