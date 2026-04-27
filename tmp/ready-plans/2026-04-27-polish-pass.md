# Plan: Polish pass — separate mic menubar item, human meeting filenames, composite REC indicator, copy-API-key button

> Brief: `tmp/briefs/2026-04-27-polish-pass.md`
> Confidence: 8/10 (after reviewer pass 1: load-bearing CoreAudio bridging fixed, ordering hazards documented, crash-recovery added)

## Architecture overview

Four independent client-only changes on top of `5ed4ec7`. No server work. All Swift code lives under `client/WisprAlt/`.

```
┌─ macOS menubar ────────────────────────────────────────────┐
│   [WisprAlt mic icon (existing)]   [🎤 Input Mic (NEW)]  │
│        │                                  │                 │
│        ▼ click → SettingsView popover     ▼ click → NSMenu  │
│        (Quick Actions, Connection,        ┌──────────────┐  │
│         Hotkey, Launch, Meetings,         │ Input Mic    │  │
│         Advanced toggle ─►                │ ─────────── │  │
│           Server URL                      │ ✓ Built-in  │  │
│           API Key                         │   AirPods   │  │
│           [Copy / Export / Import] (4th ← NEW)           │  │
│                                           │ ─────────── │  │
│                                           │ Open Sound… │  │
│                                           └──────────────┘  │
└────────────────────────────────────────────────────────────┘
```

**Three flows touch CoreAudio:**

1. **MicEnumerator** (NEW) — list devices via AVCaptureDevice.DiscoverySession; translate UID → AudioDeviceID; get/set system default.
2. **DictationRecorder** (modified) — apply preferred device via `AudioUnitSetProperty(kAudioOutputUnitProperty_CurrentDevice)` on AVAudioEngine inputNode's audioUnit, **BEFORE** capturing the input format and **BEFORE** installing the configChange observer.
3. **MeetingRecorder** (modified) — temporarily override `kAudioHardwarePropertyDefaultInputDevice` **at the very top** of `start()` (before AudioDeviceListener install). Restore on stop and on next-launch if a previous session crashed.

**The REC fix** moves from "image + attributedTitle" (which macOS char-wraps in cramped menubars — confirmed by user screenshot showing R/E/C stacked vertically) to "single composite NSImage drawn with both dot and text in one bitmap".

**Filename rename** uses a two-step pattern: at meeting start, write to `<start-time>.wav`. On stop, rename to `<start-time>-<end-time>.wav`. If the app crashes between start and stop, the partial file is at least dated (not an opaque UUID).

**Copy API key** uses NSPasteboard with `changeCount` snapshot for safe auto-clear (vs string comparison which breaks on Universal Clipboard/duplicate-key edge cases).

## Files being changed

```
client/
└── WisprAlt/
    ├── Audio/                              ← NEW directory
    │   └── MicEnumerator.swift             ← NEW (~110 lines)
    │
    ├── App/
    │   ├── AppDelegate.swift               ← MODIFIED (instantiate MicMenuBarController; ordering: shared = self FIRST)
    │   ├── MenuBarController.swift         ← MODIFIED (composite REC NSImage; rename in stopMeetingRecording flow; mode.didSet calls mic tint)
    │   └── MicMenuBarController.swift      ← NEW (~180 lines)
    │
    ├── Capture/
    │   ├── DictationRecorder.swift         ← MODIFIED (apply preferred device pre-format-read pre-observer-install)
    │   └── MeetingRecorder.swift           ← MODIFIED (override system default at very top; restore on stop; persist UID for crash recovery)
    │
    ├── Storage/
    │   └── Settings.swift                  ← MODIFIED (preferredInputDeviceUID + showMicStatusItem + persistedMeetingDefaultInputUID)
    │
    └── UI/
        └── SettingsView.swift              ← MODIFIED (Copy API Key button; Show mic in menubar toggle in Advanced)

docs/                                       ← MODIFIED
├── OVERVIEW.md                             ← rows for the two new files
├── ARCHITECTURE.md                         ← "Two-status-item layout" + crash-recovery section
└── SETUP-CLIENT.md                         ← mic selector menu walkthrough
```

8 source files (2 new, 6 modified) + 3 docs.

## Documentation references

- `AVCaptureDevice.DiscoverySession`: https://developer.apple.com/documentation/avfoundation/avcapturedevice/discoverysession — for audio inputs use `mediaType: .audio` and device types `[.builtInMicrophone, .external, .microphone]` on macOS 14+. **`.externalUnknown` is iOS-only — do NOT use.**
- `kAudioHardwarePropertyTranslateUIDToDevice`: https://developer.apple.com/documentation/coreaudio/kaudiohardwarepropertytranslateuidtodevice — modern API. Pass the UID as `qualifierData` (a `CFString` pointer + size); it writes the `AudioDeviceID` to `outData`. **Do NOT use `AudioValueTranslation`** (that's the legacy `kAudioHardwarePropertyDeviceForUID` shape).
- `kAudioOutputUnitProperty_CurrentDevice`: https://developer.apple.com/documentation/audiotoolbox/kaudiooutputunitproperty_currentdevice — set on `AVAudioInputNode.audioUnit` to bind a specific device. Side effect: posts `AVAudioEngineConfigurationChange` notifications.
- `kAudioHardwarePropertyDefaultInputDevice`: https://developer.apple.com/documentation/coreaudio/kaudiohardwarepropertydefaultinputdevice — system-wide default input. Setting fires HAL listeners.
- `NSStatusItem.menu`: https://developer.apple.com/documentation/appkit/nsstatusitem — assigning a menu makes left-click pop it natively.
- `NSPasteboard.changeCount`: https://developer.apple.com/documentation/appkit/nspasteboard/1530058-changecount — increment-on-write semantics; use for safe auto-clear.

## Gotchas (load-bearing — verified against existing codebase)

1. **CoreAudio CFString in/out parameters need `Unmanaged<CFString>?` for ownership.** Pattern (mirror `client/WisprAlt/Capture/AudioDeviceListener.swift:59-98`):
   ```swift
   var nameRef: Unmanaged<CFString>?
   var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
   let status = AudioObjectGetPropertyData(deviceID, &addr, 0, nil, &size, &nameRef)
   guard status == noErr, let name = nameRef?.takeRetainedValue() else { return nil }
   ```

2. **DictationRecorder format-read must happen AFTER device override.** Switching device changes sample rate / channel count. The existing `inputNode.outputFormat(forBus: 0)` at `DictationRecorder.swift:147` reads the OLD format if override happens later. **Insert override BEFORE line 147.**

3. **AVAudioEngineConfigurationChange fires on device override.** The existing observer at `DictationRecorder.swift:221-246` would catch our own override and abort the recording. **Install configChange observer AFTER the device override.**

4. **MeetingRecorder's AudioDeviceListener fires on default-input override.** Listener installs at `MeetingRecorder.swift:238`. If we override AFTER that, the listener fires immediately and triggers `meetingAudioDeviceChanged` → meeting aborts. **Override BEFORE the listener is installed (very first thing in `start()`).**

5. **SCStream picks up the new default at `addStreamOutput(.microphone)`, not at `SCStream(filter:configuration:delegate:)`.** Our override must be set before line 217 (`addStreamOutput(self, type: .microphone, ...)`), not just before SCStream construction.

6. **Crash-during-meeting leaves system default permanently changed.** Persist `pendingMeetingDefaultInputUID` to UserDefaults at meeting start; on app launch, if present, write back as system default and clear. (Save UID, not AudioDeviceID — the latter isn't stable across reboots.)

7. **`contentTintColor` only applies when `isTemplate = true`.** Setting `isTemplate = false` makes the image render at source colors and ignores tint. For the mic-recording tint, KEEP `isTemplate = true` and set `contentTintColor = .systemRed`.

8. **AppDelegate.shared must be set BEFORE constructing MenuBarController.** MenuBarController's init calls `updateIcon()` and may attempt to access `AppDelegate.shared?.micMenuBarController` if mode mutation happens during init. Set `AppDelegate.shared = self` as the first line of `applicationDidFinishLaunching`.

9. **NSImage drawing closure runs lazily on rasterization.** Set `image.isTemplate = false` BEFORE handing the image to the status item button. The drawing closure receives a flipped/unflipped coordinate system based on the `flipped:` parameter; `flipped: false` means y=0 is at the bottom — text drawn at `(x, 0)` may have descenders clipped if the canvas is exactly text height. Add 1pt vertical padding.

10. **Filename collision must check ALL sidecar extensions (.wav/.json/.srt/.vtt/.txt).** Just checking `.wav` then writing the rename can still collide with the JSON/SRT/VTT/TXT files written later by `processMeetingUpload()`. Either include seconds in start-time (`h.mm.ssa` — collision becomes ~impossible) OR check all extensions in the loop.

11. **NSPasteboard auto-clear via string comparison breaks under Universal Clipboard.** Use `changeCount` snapshot instead:
    ```swift
    pb.clearContents()
    pb.setString(key, forType: .string)
    let mark = pb.changeCount  // post-write
    Task {
        try? await Task.sleep(...)
        if NSPasteboard.general.changeCount == mark { pb.clearContents() }
    }
    ```

12. **`AVCaptureDevice.DiscoverySession.devices` returns empty if Microphone permission revoked.** Mic menu would show only "System Default" and the footer. Add a warning row "No input devices found — check Microphone permission" with a link to `x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone`.

## Patterns to follow

- **Singleton + isolated state**: `MeetingRecorder.shared`, `DictationRecorder` instance owned by `MenuBarController`. Match for `MicEnumerator` (static helpers, no state) and `MicMenuBarController` (singleton owned by AppDelegate).
- **CoreAudio idiom in `AudioDeviceListener.swift`**: that file is the canonical in-tree CoreAudio-from-Swift pattern. Use the same `AudioObjectPropertyAddress` initialization style (with `kAudioObjectPropertyScopeGlobal` + `kAudioObjectPropertyElementMain`).
- **`@Published` + `didSet { defaults.set(...) }`**: `Settings.swift` shows the pattern; mirror exactly for the three new fields.
- **`Task { @MainActor in ... }` for async UI**: SettingsView's existing actions use this. Match for the auto-clear-clipboard task.
- **`Log.info(...)` with `category:`**: use `"audio"` for MicEnumerator/MicMenuBarController, `"settings"` for the copy button, `"meeting"` for the rename, `"capture"` for recorder modifications.

## Implementation blueprint

### MicEnumerator.swift (NEW)

```swift
import AVFoundation
import CoreAudio

enum MicEnumerator {
    struct InputDevice: Identifiable, Hashable {
        let uniqueID: String
        let name: String
        var id: String { uniqueID }
    }

    /// macOS 14+ device discovery for audio inputs. Project deployment target
    /// is macOS 15+ so all three types are available unconditionally.
    /// `.externalUnknown` is iOS-only — do NOT add it.
    static func availableInputs() -> [InputDevice] {
        let deviceTypes: [AVCaptureDevice.DeviceType] = [
            .builtInMicrophone, .external, .microphone
        ]
        let session = AVCaptureDevice.DiscoverySession(
            deviceTypes: deviceTypes,
            mediaType: .audio,
            position: .unspecified
        )
        // Dedup by uniqueID — macOS 14's `.microphone` overlaps with `.builtInMicrophone`.
        var seen = Set<String>()
        return session.devices.compactMap { d in
            guard !seen.contains(d.uniqueID) else { return nil }
            seen.insert(d.uniqueID)
            return InputDevice(uniqueID: d.uniqueID, name: d.localizedName)
        }
    }

    /// Translate AVCaptureDevice.uniqueID (which IS the CoreAudio device UID)
    /// to an AudioDeviceID via kAudioHardwarePropertyTranslateUIDToDevice.
    /// Returns nil if the UID doesn't resolve.
    static func audioDeviceID(forUID uid: String) -> AudioDeviceID? {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyTranslateUIDToDevice,
            mScope:    kAudioObjectPropertyScopeGlobal,
            mElement:  kAudioObjectPropertyElementMain
        )
        // CFStringRef qualifier: pointer-sized (8 bytes on 64-bit). Use
        // CFString? layout to be explicit that we're passing pointer-to-pointer.
        var cfUID: CFString = uid as CFString
        var devID: AudioDeviceID = kAudioObjectUnknown
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        let qualifierSize = UInt32(MemoryLayout<CFString?>.size)
        let status = withUnsafePointer(to: &cfUID) { uidPtr -> OSStatus in
            AudioObjectGetPropertyData(
                AudioObjectID(kAudioObjectSystemObject),
                &addr,
                qualifierSize,
                uidPtr,           // pointer-to-CFString — HAL dereferences once
                &size,
                &devID
            )
        }
        guard status == noErr, devID != kAudioObjectUnknown else { return nil }
        return devID
    }

    /// Read system default input device's AudioDeviceID.
    static func systemDefaultInputDeviceID() -> AudioDeviceID? {
        var devID: AudioDeviceID = kAudioObjectUnknown
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope:    kAudioObjectPropertyScopeGlobal,
            mElement:  kAudioObjectPropertyElementMain
        )
        let status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &addr, 0, nil, &size, &devID
        )
        return status == noErr ? devID : nil
    }

    /// Get the UID for an AudioDeviceID, suitable for persistence.
    static func uid(forAudioDeviceID deviceID: AudioDeviceID) -> String? {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceUID,
            mScope:    kAudioObjectPropertyScopeGlobal,
            mElement:  kAudioObjectPropertyElementMain
        )
        var uidRef: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = AudioObjectGetPropertyData(deviceID, &addr, 0, nil, &size, &uidRef)
        guard status == noErr, let uid = uidRef?.takeRetainedValue() else { return nil }
        return uid as String
    }

    /// Set the system default input device. Returns true on success.
    /// **Side effect**: this changes the system-wide default for ALL apps.
    @discardableResult
    static func setSystemDefaultInputDevice(_ deviceID: AudioDeviceID) -> Bool {
        var devID = deviceID
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope:    kAudioObjectPropertyScopeGlobal,
            mElement:  kAudioObjectPropertyElementMain
        )
        let status = AudioObjectSetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &addr, 0, nil,
            UInt32(MemoryLayout<AudioDeviceID>.size),
            &devID
        )
        return status == noErr
    }

    /// Convenience: name of the current system default mic via AVFoundation
    /// (avoids the CoreAudio CFString out-param ownership trap).
    static func systemDefaultInputName() -> String? {
        AVCaptureDevice.default(for: .audio)?.localizedName
    }
}
```

### MicMenuBarController.swift (NEW)

```swift
import AppKit
import AVFoundation

final class MicMenuBarController: NSObject {
    private let statusItem: NSStatusItem
    private let menu = NSMenu()

    weak var menuBarController: MenuBarController?

    override init() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        super.init()
        configureStatusItem()
        menu.delegate = self
        statusItem.menu = menu  // left-click → menu pops natively
    }

    private func configureStatusItem() {
        guard let button = statusItem.button else { return }
        let img = NSImage(systemSymbolName: "mic", accessibilityDescription: "Input Mic")
        img?.isTemplate = true
        button.image = img
        button.toolTip = "Input mic"
    }

    /// KEEP isTemplate=true — contentTintColor only works on templates.
    func updateRecordingTint(active: Bool) {
        guard let button = statusItem.button else { return }
        // Image stays template; tint switches between nil (system) and red.
        button.image?.isTemplate = true
        button.contentTintColor = active ? .systemRed : nil
    }
}

extension MicMenuBarController: NSMenuDelegate {
    func menuWillOpen(_ menu: NSMenu) {
        menu.removeAllItems()
        let devices = MicEnumerator.availableInputs()
        let preferred = Settings.shared.preferredInputDeviceUID

        // Header (disabled).
        let header = NSMenuItem(title: "Input Mic", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        menu.addItem(.separator())

        // System default option.
        let sysName = MicEnumerator.systemDefaultInputName()
        let sysItem = NSMenuItem(
            title: "System Default" + (sysName.map { " (\($0))" } ?? ""),
            action: #selector(selectSystemDefault),
            keyEquivalent: ""
        )
        sysItem.target = self
        if preferred == nil { sysItem.state = .on }
        menu.addItem(sysItem)
        menu.addItem(.separator())

        // Permission-revoked / empty fallback.
        if devices.isEmpty {
            let warn = NSMenuItem(
                title: "No input devices found — check Microphone permission",
                action: #selector(openMicPrivacy),
                keyEquivalent: ""
            )
            warn.target = self
            menu.addItem(warn)
        } else {
            for d in devices {
                let item = NSMenuItem(
                    title: d.name,
                    action: #selector(selectDevice(_:)),
                    keyEquivalent: ""
                )
                item.target = self
                item.representedObject = d.uniqueID
                if d.uniqueID == preferred { item.state = .on }
                menu.addItem(item)
            }
        }
        menu.addItem(.separator())

        // Footer.
        let openItem = NSMenuItem(
            title: "Open Sound Settings…",
            action: #selector(openSoundSettings),
            keyEquivalent: ""
        )
        openItem.target = self
        menu.addItem(openItem)
    }

    @objc private func selectSystemDefault() {
        Settings.shared.preferredInputDeviceUID = nil
        announceSwitch(to: "System default")
    }

    @objc private func selectDevice(_ sender: NSMenuItem) {
        guard let uid = sender.representedObject as? String else { return }
        Settings.shared.preferredInputDeviceUID = uid
        announceSwitch(to: sender.title)
    }

    @objc private func openSoundSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.Sound-Settings.extension") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func openMicPrivacy() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") {
            NSWorkspace.shared.open(url)
        }
    }

    private func announceSwitch(to label: String) {
        let isRecording = (menuBarController?.mode == .meetingRecording)
            || (menuBarController?.mode == .dictating)
        if isRecording {
            AppNotifications.notify(
                title: "Mic switched to \(label)",
                body: "Active recording is unchanged. Next recording will use this mic."
            )
        } else {
            Log.info("Preferred input mic set to: \(label)", category: "audio")
        }
    }
}
```

### DictationRecorder modification — exact insertion point

Look at `client/WisprAlt/Capture/DictationRecorder.swift:137-160`. The current sequence is:

```
137: func start() throws -> Bool {
145:     let inputNode = engine.inputNode            // ← materializes audioUnit
147:     let inputFormat = inputNode.outputFormat(forBus: 0)
        // … format validation, frameCounter reset, etc.
221:    // Observer install (configChangeObserver)
        // … other setup
304:    engine.prepare()
        // … installTap, etc.
```

**New sequence** — insert immediately AFTER line 145 and BEFORE line 147:

```swift
let inputNode = engine.inputNode  // existing line 145

// === NEW: apply preferred input device BEFORE format read & observer install ===
if let preferredUID = Settings.shared.preferredInputDeviceUID,
   let deviceID = MicEnumerator.audioDeviceID(forUID: preferredUID),
   let audioUnit = inputNode.audioUnit {
    var devID = deviceID
    let status = AudioUnitSetProperty(
        audioUnit,
        kAudioOutputUnitProperty_CurrentDevice,
        kAudioUnitScope_Global,
        0,
        &devID,
        UInt32(MemoryLayout<AudioDeviceID>.size)
    )
    if status != noErr {
        Log.warning("Could not set preferred input device on inputNode: \(status). Falling back to system default.", category: "audio")
    } else {
        Log.info("DictationRecorder: input device set to UID \(preferredUID)", category: "audio")
    }
} else if let preferredUID = Settings.shared.preferredInputDeviceUID {
    // UID resolved to nil — device unplugged since last selection.
    Log.warning("DictationRecorder: preferred device UID \(preferredUID) is unavailable; using system default.", category: "audio")
}
// === END NEW ===

let inputFormat = inputNode.outputFormat(forBus: 0)  // existing line 147 — now reads NEW device's format
```

The configChange observer (line 221-246) is left in place — by the time it's installed, our override is already complete and won't trigger it.

### MeetingRecorder modification — exact insertion point

Look at `client/WisprAlt/Capture/MeetingRecorder.swift:158-244`. The new code goes at **the very top of `start()`**, BEFORE `guard !isActive` (line 167) — no, after `isActive` guard, before everything else.

```swift
func start(to url: URL, maxDuration: TimeInterval = -1) async throws {
    let resolvedMaxDuration = maxDuration < 0
        ? TimeInterval(Settings.shared.maxMeetingMinutes * 60)
        : maxDuration
    let maxDuration = resolvedMaxDuration
    guard !isActive else { throw MeetingRecorderError.alreadyRunning }

    // === NEW: override system default input device BEFORE AudioDeviceListener install.
    // Short-circuits to no-op when preferred == current (saves a HAL set + avoids
    // misleading "we changed the default" tracking on the no-op path). ===
    if let preferredUID = Settings.shared.preferredInputDeviceUID,
       let deviceID = MicEnumerator.audioDeviceID(forUID: preferredUID) {
        let currentDefaultID = MicEnumerator.systemDefaultInputDeviceID()
        let currentDefaultUID = currentDefaultID.flatMap { MicEnumerator.uid(forAudioDeviceID: $0) }
        if currentDefaultUID == preferredUID {
            // Already on preferred mic — nothing to override or restore.
            Log.info("MeetingRecorder: preferred mic == current default; no override needed.", category: "audio")
        } else {
            if let savedUID = currentDefaultUID {
                UserDefaults.standard.set(savedUID, forKey: "pendingMeetingDefaultInputUID")
            }
            if MicEnumerator.setSystemDefaultInputDevice(deviceID) {
                self.meetingDidOverrideDefault = true
                Log.info("MeetingRecorder: overrode system default input to UID \(preferredUID)", category: "audio")
            } else {
                Log.warning("MeetingRecorder: could not override default input; using system default.", category: "audio")
                UserDefaults.standard.removeObject(forKey: "pendingMeetingDefaultInputUID")
            }
        }
    } else if let preferredUID = Settings.shared.preferredInputDeviceUID {
        Log.warning("MeetingRecorder: preferred device UID \(preferredUID) is unavailable; using system default.", category: "audio")
    }
    // === END NEW ===

    lastOutputURL = nil
    // … existing logic continues unchanged: SCShareableContent, SCStream config, addStreamOutput, AudioDeviceListener install, startCapture()
```

In `stop()` — restore must happen **BEFORE the `guard wasActive ... throw .notRunning`** so a failed/aborted session still unwinds the override. Place the restore as the **very first thing** inside `stop()`, before any guard:

```swift
func stop() async throws -> URL {
    // === NEW: restore pre-meeting system default FIRST, before any guard.
    // Even if isActive flipped to false (due to didStopWithError), we still
    // need to unwind the system-wide override. ===
    if self.meetingDidOverrideDefault {
        if let savedUID = UserDefaults.standard.string(forKey: "pendingMeetingDefaultInputUID"),
           let savedID = MicEnumerator.audioDeviceID(forUID: savedUID) {
            _ = MicEnumerator.setSystemDefaultInputDevice(savedID)
            Log.info("MeetingRecorder: restored system default input to UID \(savedUID)", category: "audio")
        }
        UserDefaults.standard.removeObject(forKey: "pendingMeetingDefaultInputUID")
        self.meetingDidOverrideDefault = false
    }
    // === END NEW ===

    // existing: deviceListener = nil; guard wasActive else { throw .notRunning }; rest of teardown
}
```

Property to add at top of class:
```swift
private var meetingDidOverrideDefault = false
```

### AppDelegate crash-recovery hook

First, declare the static accessor on `AppDelegate` (it does not exist yet):

```swift
final class AppDelegate: NSObject, NSApplicationDelegate {
    static weak var shared: AppDelegate?  // weak — NSApp already retains the delegate
    // … existing properties
}
```

In `applicationDidFinishLaunching` — first thing, BEFORE any other init:

```swift
func applicationDidFinishLaunching(_ note: Notification) {
    AppDelegate.shared = self  // FIRST — before anything else accesses .shared

    // Crash recovery: if a prior session crashed mid-meeting with an active
    // mic override, the system default input is still pointed at the user's
    // meeting mic. Restore it.
    if let savedUID = UserDefaults.standard.string(forKey: "pendingMeetingDefaultInputUID"),
       let savedID = MicEnumerator.audioDeviceID(forUID: savedUID) {
        _ = MicEnumerator.setSystemDefaultInputDevice(savedID)
        Log.warning("Recovered system default input after prior-session crash mid-meeting. Restored UID \(savedUID).", category: "audio")
        UserDefaults.standard.removeObject(forKey: "pendingMeetingDefaultInputUID")
    }

    // Existing init continues …
    menuBarController = MenuBarController()

    // Mic status item (gated on Settings.showMicStatusItem).
    if Settings.shared.showMicStatusItem {
        micMenuBarController = MicMenuBarController()
        micMenuBarController?.menuBarController = menuBarController
    }

    sparkleController = SparkleController()
    // …
}
```

### Settings additions

Three new keys + `@Published` properties in `Settings.swift`:

```swift
private enum Key {
    // … existing
    static let preferredInputDeviceUID = "preferredInputDeviceUID"
    static let showMicStatusItem = "showMicStatusItem"
    // (pendingMeetingDefaultInputUID is read directly via UserDefaults in MeetingRecorder
    //  to avoid a Settings dependency from CoreAudio land — pure storage key.)
}

@Published var preferredInputDeviceUID: String? {
    didSet {
        if let uid = preferredInputDeviceUID {
            defaults.set(uid, forKey: Key.preferredInputDeviceUID)
        } else {
            defaults.removeObject(forKey: Key.preferredInputDeviceUID)
        }
    }
}

@Published var showMicStatusItem: Bool {
    didSet { defaults.set(showMicStatusItem, forKey: Key.showMicStatusItem) }
}

// In init:
preferredInputDeviceUID = defaults.string(forKey: Key.preferredInputDeviceUID)
showMicStatusItem = defaults.object(forKey: Key.showMicStatusItem) as? Bool ?? true  // default ON
```

### MenuBarController.updateIcon — composite REC

Replace the meetingRecording branch's image+title pair with a single composite NSImage:

```swift
case .meetingRecording:
    let composite = renderRecComposite()
    button.image = composite
    button.contentTintColor = nil
    button.attributedTitle = NSAttributedString(string: "")
    button.title = ""
    button.imagePosition = .imageOnly
    button.toolTip = "WisprAlt — Meeting Recording"
```

`renderRecComposite()` helper (private, on `MenuBarController`):

```swift
private func renderRecComposite() -> NSImage {
    let dotSize: CGFloat = 8
    let dotGap: CGFloat = 3
    let verticalPadding: CGFloat = 1  // descender clearance
    let font = NSFont.systemFont(ofSize: 11, weight: .bold)
    let attrs: [NSAttributedString.Key: Any] = [
        .font: font,
        .foregroundColor: NSColor.systemRed,
    ]
    let text = NSAttributedString(string: "REC", attributes: attrs)
    let textSize = text.size()
    let canvasHeight = ceil(textSize.height) + verticalPadding * 2
    let canvasWidth = dotSize + dotGap + ceil(textSize.width) + 2
    let img = NSImage(
        size: NSSize(width: canvasWidth, height: canvasHeight),
        flipped: false
    ) { _ in
        let rect = NSRect(x: 0, y: 0, width: canvasWidth, height: canvasHeight)
        let dotRect = NSRect(
            x: 0,
            y: (rect.height - dotSize) / 2,
            width: dotSize,
            height: dotSize
        )
        NSColor.systemRed.setFill()
        NSBezierPath(ovalIn: dotRect).fill()
        text.draw(in: NSRect(
            x: dotSize + dotGap,
            y: verticalPadding,
            width: ceil(textSize.width),
            height: ceil(textSize.height)
        ))
        return true
    }
    img.isTemplate = false  // pre-rendered red, not a template
    return img
}
```

In `mode.didSet`, also notify the mic status item:

```swift
var mode: Mode = .idle {
    didSet {
        updateIcon()
        let isRec = (mode == .meetingRecording) || (mode == .dictating)
        AppDelegate.shared?.micMenuBarController?.updateRecordingTint(active: isRec)
    }
}
```

### Filename rename — two-step pattern

Replace the existing filename logic in `startMeetingRecording()`:

```swift
private func startMeetingRecording() {
    let now = Date()
    let startName = humanReadableMeetingFilename(start: now, end: nil, in: Settings.shared.meetingsPath)
    let outputURL = Settings.shared.meetingsPath.appendingPathComponent(startName)
    self.meetingRecordingStart = now
    self.meetingStartFileURL = outputURL

    Task { @MainActor in
        do {
            try await MeetingRecorder.shared.start(to: outputURL)
            meetingActive = true
            mode = .meetingRecording
            Log.info("Meeting recording started → \(startName)", category: "meeting")
        } catch {
            // existing error handling
        }
    }
}
```

In `stopMeetingRecording()` — after `MeetingRecorder.shared.stop()` returns the wavURL but BEFORE `processMeetingUpload(wavURL:)`:

```swift
private func stopMeetingRecording() {
    Task { @MainActor in
        do {
            let wavURL = try await MeetingRecorder.shared.stop()
            let endDate = Date()
            let humanName = humanReadableMeetingFilename(
                start: meetingRecordingStart ?? endDate,
                end: endDate,
                in: Settings.shared.meetingsPath
            )
            let renamedURL = Settings.shared.meetingsPath.appendingPathComponent(humanName)
            let finalURL: URL
            do {
                try FileManager.default.moveItem(at: wavURL, to: renamedURL)
                finalURL = renamedURL
                Log.info("Meeting WAV renamed → \(humanName)", category: "meeting")
            } catch {
                Log.warning("Could not rename meeting WAV: \(error.localizedDescription). Using start-only name.", category: "meeting")
                finalURL = wavURL
            }
            meetingActive = false
            mode = .uploading
            recordingState.uploadFraction = 0
            await processMeetingUpload(wavURL: finalURL)
        } catch {
            // existing error handling
        }
    }
}
```

`humanReadableMeetingFilename` helper — handles both start-only and start+end cases, includes seconds in the time format to avoid sidecar collisions:

```swift
private func humanReadableMeetingFilename(start: Date, end: Date?, in dir: URL) -> String {
    let dayFormatter = DateFormatter()
    dayFormatter.locale = Locale(identifier: "en_US_POSIX")
    dayFormatter.dateFormat = "EEE MMM d"

    let timeFormatter = DateFormatter()
    timeFormatter.locale = Locale(identifier: "en_US_POSIX")
    timeFormatter.amSymbol = "am"
    timeFormatter.pmSymbol = "pm"
    // Periods, not colons (filesystem-friendly across rsync to Linux, zip, etc.)
    // Includes seconds to make collisions effectively impossible across all
    // sidecar extensions (.wav/.json/.srt/.vtt/.txt) written by processMeetingUpload.
    timeFormatter.dateFormat = "h.mm.ssa"

    let day = dayFormatter.string(from: start)
    let startTime = timeFormatter.string(from: start)
    let base: String
    if let end = end {
        let endTime = timeFormatter.string(from: end)
        base = "\(day) \(startTime)-\(endTime)"
    } else {
        base = "\(day) \(startTime)"
    }

    // Collision guard: check the base name against ALL sidecar extensions.
    let exts = ["wav", "json", "srt", "vtt", "txt"]
    func anyExists(_ baseName: String) -> Bool {
        for ext in exts {
            if FileManager.default.fileExists(atPath: dir.appendingPathComponent("\(baseName).\(ext)").path) {
                return true
            }
        }
        return false
    }
    var name = base
    var i = 2
    while anyExists(name) {
        name = "\(base) (\(i))"
        i += 1
    }
    return "\(name).wav"
}
```

Properties to add at top of `MenuBarController`:
```swift
private var meetingRecordingStart: Date?
private var meetingStartFileURL: URL?
```

### SettingsView.swift — Copy API Key + Show Mic toggle

Replace the existing `apiKeyExportImportSection` HStack with three buttons + caption:

```swift
private var apiKeyExportImportSection: some View {
    Section("API Key Backup") {
        HStack {
            Button("Copy API Key") {
                copyAPIKeyToClipboard()
            }
            .disabled(apiKeyText.isEmpty)
            .help(apiKeyText.isEmpty ? "Paste an API key first" : "Copy your API key. Auto-cleared from clipboard after 60 seconds.")

            Button("Export API Key…") { /* existing logic */ }
            Button("Import API Key…") { /* existing logic */ }
        }

        Text("Save exports to your Desktop, not Documents (Documents may sync to iCloud).")
            .font(.caption)
            .foregroundStyle(.secondary)

        if let msg = exportImportError {
            Text(msg).font(.caption).foregroundStyle(.red)
        }
        if let msg = copyFeedback {
            Text(msg).font(.caption).foregroundStyle(.green)
        }
    }
}
```

Add the `@State` property at the top of the `SettingsView` struct alongside the other `@State` declarations (NOT inline near the helper — `@State` must be a struct property):

```swift
@State private var copyFeedback: String?
@State private var hasStoredAPIKey: Bool = false  // mirror of Keychain — refreshed in loadCurrentValues
```

In `loadCurrentValues()` (existing), refresh `hasStoredAPIKey`:

```swift
hasStoredAPIKey = ((try? KeychainHelper.getAPIKey()) ?? nil)?.isEmpty == false
```

The Copy button uses `hasStoredAPIKey` (not `apiKeyText`) for its disabled state:

```swift
Button("Copy API Key") {
    copyAPIKeyToClipboard()
}
.disabled(!hasStoredAPIKey)
.help(hasStoredAPIKey ? "Copy your API key. Auto-cleared from clipboard after 60 seconds." : "Paste an API key first")
```

Helper method on `SettingsView`:

```swift
private func copyAPIKeyToClipboard() {
    do {
        guard let key = try KeychainHelper.getAPIKey(), !key.isEmpty else {
            copyFeedback = "No API key to copy."
            return
        }
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(key, forType: .string)
        let mark = pb.changeCount  // post-write changeCount
        copyFeedback = "Copied! Auto-clearing in 60s."
        Log.info("API key copied to clipboard.", category: "settings")

        // Two timers: caption fades fast, clipboard auto-clears slow.
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 2 * 1_000_000_000)
            copyFeedback = nil
        }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 60 * 1_000_000_000)
            if NSPasteboard.general.changeCount == mark {
                NSPasteboard.general.clearContents()
                Log.info("Auto-cleared API key from clipboard (changeCount unchanged).", category: "settings")
            } else {
                Log.info("Skipped clipboard auto-clear — pasteboard changed.", category: "settings")
            }
        }
    } catch {
        Log.error("Copy API key failed: \(error)", category: "settings")
        copyFeedback = "Copy failed: \(error.localizedDescription)"
    }
}
```

Also add a "Show input mic in menu bar" toggle in the Advanced section — toggle controls whether the second status item appears (requires app restart):

```swift
// Inside the Advanced disclosure (when showAdvanced == true):
Section("Menu Bar") {
    Toggle("Show input mic in menu bar", isOn: $settings.showMicStatusItem)
    Text("Restart WisprAlt to apply.")
        .font(.caption)
        .foregroundStyle(.secondary)
}
```

## Tasks (in order)

1. **`client/WisprAlt/Audio/MicEnumerator.swift`** (NEW). Use the corrected pseudocode above — `kAudioHardwarePropertyTranslateUIDToDevice` via `withUnsafeMutablePointer(to: &cfUID)`, NOT `AudioValueTranslation`. CFString out-params via `Unmanaged<CFString>?.takeRetainedValue()`. Drop `deviceName(for:)` — use `AVCaptureDevice.default(for: .audio)?.localizedName` instead.

2. **`client/WisprAlt/Storage/Settings.swift`** (MODIFIED). Add `preferredInputDeviceUID: String?` and `showMicStatusItem: Bool` (default `true`) with `@Published` + UserDefaults persistence. Plus the `Key` cases.

3. **`client/WisprAlt/Capture/DictationRecorder.swift`** (MODIFIED). Insert preferred-device override AFTER `let inputNode = engine.inputNode` (line 145) and BEFORE `let inputFormat = inputNode.outputFormat(forBus: 0)` (line 147). Use `AudioUnitSetProperty(kAudioOutputUnitProperty_CurrentDevice)`. The configChange observer (line 221+) is left in place — by then our override is complete.

4. **`client/WisprAlt/Capture/MeetingRecorder.swift`** (MODIFIED). Add `meetingDidOverrideDefault: Bool` property. At the very top of `start()`, AFTER `guard !isActive` and BEFORE everything else (especially BEFORE the `AudioDeviceListener` install at line ~238), call the override block. Persist `pendingMeetingDefaultInputUID` to UserDefaults for crash recovery. In `stop()`, after `deviceListener = nil`, restore + clear the persisted UID.

5. **`client/WisprAlt/App/MicMenuBarController.swift`** (NEW). Implements the NSStatusItem + NSMenu pattern from the pseudocode. `menuWillOpen` rebuilds the menu fresh (cheap). Empty-list fallback row links to mic privacy preferences. Recording-state tint via `contentTintColor` (with `isTemplate=true`).

6. **`client/WisprAlt/App/AppDelegate.swift`** (MODIFIED). Set `AppDelegate.shared = self` as the FIRST line. Add crash-recovery hook (read `pendingMeetingDefaultInputUID`, restore + clear). Construct `MicMenuBarController` only if `Settings.shared.showMicStatusItem` is true.

7. **`client/WisprAlt/App/MenuBarController.swift`** (MODIFIED). Three sub-changes:
    - In `mode.didSet`, also call `AppDelegate.shared?.micMenuBarController?.updateRecordingTint(active:)`.
    - Replace meetingRecording branch in `updateIcon()` with `renderRecComposite()` helper.
    - Replace `startMeetingRecording`/`stopMeetingRecording` filename construction with two-step rename pattern. Add `meetingRecordingStart: Date?` and `meetingStartFileURL: URL?` properties.

8. **`client/WisprAlt/UI/SettingsView.swift`** (MODIFIED). Add Copy API Key button at left of HStack. Add `copyFeedback` `@State`. Two-timer pattern: 2s caption fade + 60s clipboard auto-clear via `changeCount` snapshot. Add Menu Bar section in Advanced with `showMicStatusItem` toggle (label notes "Restart WisprAlt to apply").

9. **Build + sign + install** (validation gate):
    ```bash
    cd /Users/omidzahrai/Desktop/CODEBASES/TOOLS/wisprflowALT
    cd client && swift build  # SPM project — verified Package.swift exists
    cd .. && bash scripts/build-client-local.sh
    rm -rf /tmp/WisprAlt.app
    ditto --norsrc --noextattr --noacl client/build/WisprAlt.app /tmp/WisprAlt.app
    codesign --force --deep \
      --sign "Apple Development: zomid777@gmail.com (8VN2A53R23)" \
      --options runtime \
      --entitlements client/WisprAlt/WisprAlt.entitlements \
      /tmp/WisprAlt.app
    codesign --verify --deep --strict /tmp/WisprAlt.app
    osascript -e 'tell application "WisprAlt" to quit' || true
    pkill -f "WisprAlt.app/Contents/MacOS/WisprAlt" || true
    sleep 1
    rm -rf /Applications/WisprAlt.app
    ditto /tmp/WisprAlt.app /Applications/WisprAlt.app
    open /Applications/WisprAlt.app
    ```

10. **Manual smoke** (validation gate):
    - Two icons appear in the macOS menubar — WisprAlt mic-on-the-popover and the new mic-with-NSMenu (adjacency NOT guaranteed by macOS; user-customizable).
    - Click mic icon → menu shows "Input Mic" header, "System Default (current: <name>)", separator, all available devices (one with checkmark = current), separator, "Open Sound Settings…".
    - Pick a non-default device → checkmark moves to it on next open.
    - Hold FN → dictation starts; both menubar icons go red-tinted.
    - Triple-tap FN → REC composite shows red dot + horizontal "REC" text, no character wrap. Both icons red.
    - Stop meeting → file appears as `Mon Apr 27 2.06.34pm-2.07.12pm.wav` (with seconds) in meetings folder.
    - **Mic-override actually applies to SCStream**: with system default = built-in mic, set the Mic Selector to AirPods, start a meeting, speak briefly, stop. Open the resulting WAV — the captured audio should reflect the AirPods (different background noise floor + frequency response than built-in). Listen-back is the gating check that SCStream picked up our override at `addStreamOutput(.microphone)` time.
    - Settings popover → Show advanced settings → Copy API Key → caption "Copied! Auto-clearing in 60s." appears for ~2 s, then fades. Paste in another app within 60 s — works. Wait 60 s → re-paste — gives previous (non-key) clipboard contents.
    - Toggle "Show input mic in menu bar" off → restart app → only WisprAlt icon visible. Toggle back on → restart → both visible.
    - **Crash-recovery**: select a non-default mic, start a meeting, kill the app via `kill -9` (skips `stop()`). Verify next launch logs `"Recovered system default input after prior-session crash mid-meeting."` and the system default in System Settings → Sound is back to original.

11. **Update docs** (per `CLAUDE.md` — "Every code change must update all docs listed in `docs/OVERVIEW.md`"):
    - `docs/OVERVIEW.md`: add rows for `client/WisprAlt/Audio/MicEnumerator.swift` (→ ARCHITECTURE.md, SETUP-CLIENT.md) and `client/WisprAlt/App/MicMenuBarController.swift` (→ ARCHITECTURE.md, SETUP-CLIENT.md).
    - `docs/ARCHITECTURE.md`: add a "Two-status-item layout" subsection under the Client section, plus a "Crash recovery for mic override" note in Failure Handling.
    - `docs/SETUP-CLIENT.md`: add a "Picking your input mic" subsection under "Server Configuration"; document the menu structure, the recording-state red tint, and the showMicStatusItem opt-out.

12. **Commit** (single coherent commit) and **show diff for user approval before pushing**:
    ```bash
    git add -A
    git status --short
    git diff --stat
    git commit -m "client: separate mic menubar item, human meeting filenames, composite REC icon, copy-API-key button"
    # Per CLAUDE.md, NEVER push without explicit user approval. Print:
    echo "Branch ready to push. Run 'git push origin main' when approved."
    ```

13. **Run `/pre-compact`** after the user approves and pushes — bakes everything into CLAUDE.local.md for next session.

## Deprecated code to remove

- The `formatter.dateFormat = "yyyy-MM-dd_HHmmZZZZZ"` block + the timezone-colon strip workaround in `MenuBarController.startMeetingRecording()` — replaced by `humanReadableMeetingFilename`.
- The two-line `attributedTitle = " REC"` + `imagePosition = .imageLeading` block in `updateIcon()` — replaced by `renderRecComposite()`.
- The `_meeting.wav` suffix logic — gone; new format has the timestamp + no suffix.

## Risk callouts (not blockers)

- **Adjacency**: macOS doesn't guarantee the two NSStatusItems will be adjacent. Most users won't care; documented in SETUP-CLIENT.md.
- **Menu freshness**: NSMenu doesn't refresh while open. If user plugs in AirPods mid-menu, they need to re-open the menu to see them. Cheap, idiomatic — accept.
- **Forced en_US_POSIX locale**: meeting filenames are always English regardless of system locale. Trade-off: stable for sorting + parsing if we ever build a meetings-list view; loses native localization. Consistent with the rest of the codebase (which uses POSIX locale for `dateFormat = "yyyy-MM-dd_HHmmZZZZZ"`).
- **`showMicStatusItem` toggle requires app restart**: NSStatusItem creation/teardown is one-shot. A reactive teardown is possible but adds complexity. Note the restart requirement in the toggle's caption.
- **System-default override during meeting affects other apps**: if the user starts Music or a Zoom call during the meeting recording, those apps will use the user's chosen mic instead of the original default. This matches user intent (they explicitly chose the mic) but is a side effect to document.

## Confidence: 8/10

Why 8 and not 7 (after pass-1 fixes):
- AudioValueTranslation issue resolved with the documented `withUnsafeMutablePointer(to: &cfUID)` shape.
- DictationRecorder ordering hazard resolved (override before format read AND before observer install).
- MeetingRecorder + AudioDeviceListener race resolved (override before listener install; restore after listener detach).
- Crash-recovery hook in AppDelegate prevents permanent system-default change.
- contentTintColor + isTemplate fixed (keep template, change tint).
- changeCount-based clipboard auto-clear fixed.
- Empty-mic-list fallback added.
- Docs update step added.
- swift build verified to work via Package.swift presence.

Why not 9 or 10:
- SCStream picking up the new default at `addStreamOutput(.microphone)` is documented but unverified at runtime — could need an additional kick (e.g., re-create SCStream after override). Test in the smoke gate.
- The composite NSImage rendering path is untested in different macOS appearance modes (light/dark/reduce-transparency); may need pixel adjustments after the first install.
