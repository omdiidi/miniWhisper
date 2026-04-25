# WisprAlt Client

macOS 14+ menubar app. Hold FN to dictate; triple-tap FN to start/stop meeting recording.
Transcription is handled by the companion [WisprAlt server](../server/README.md).

---

## Requirements

- macOS 14.0+
- Xcode 15.3+ (for local builds)
- Swift 5.9+
- Apple Developer ID certificate (for signing/notarizing a distributable build)

---

## Build and Run (Development)

### Option A — Swift Package Manager (quickest)

```bash
cd client
swift build
# Run:
.build/debug/WisprAlt
```

Sparkle is fetched automatically from SPM on first build.

### Option B — Xcode Project

The `.xcodeproj` is generated from the SPM manifest; it is not checked in.

```bash
cd client
swift package generate-xcodeproj
open WisprAlt.xcodeproj
```

Then select the **WisprAlt** scheme and press **Cmd+R**.

> **Why no checked-in .xcodeproj?**
> `project.pbxproj` is a complex auto-generated file with UUID cross-references.
> Keeping it in source control causes frequent merge conflicts and no useful diff.
> Run `swift package generate-xcodeproj` once after cloning, or use `swift build` directly.
> When signing is needed (CI, distribution), use `scripts/build-client.sh` which calls
> `xcodebuild` with the full archive/export/notarize pipeline.

---

## Signed Distribution Build

```bash
# Requires: DEVELOPER_ID_APP, APPLE_ID, APP_SPECIFIC_PASSWORD, TEAM_ID, SPARKLE_ED_PRIVATE_KEY
./scripts/build-client.sh "$DEVELOPER_ID_APP"
```

Outputs: `build/WisprAlt.dmg` (signed, notarized, stapled).

---

## Project Layout

```
client/
├── Package.swift                  SPM manifest (macOS 14, Sparkle 2)
├── WisprAlt/
│   ├── WisprAltApp.swift          @main entry point (LSUIElement=true, no Dock icon)
│   ├── Info.plist                 Bundle ID, privacy strings, Sparkle keys
│   ├── WisprAlt.entitlements      audio-input, network.client, apple-events
│   ├── App/
│   │   ├── AppDelegate.swift      Launch: instantiate MenuBarController, run PermissionGate
│   │   ├── MenuBarController.swift NSStatusItem, mode state machine, mic-exclusion stub
│   │   └── PermissionGate.swift   4-step sequential wizard (incl. 14.4+ quit-reopen sheet)
│   ├── Storage/
│   │   ├── Settings.swift         UserDefaults (serverURL, paths, hotkey timing)
│   │   └── KeychainHelper.swift   API key in Keychain (service co.wispralt)
│   ├── Update/
│   │   └── SparkleController.swift Sparkle 2 wrapper (meeting-guard gated)
│   ├── UI/
│   │   ├── SettingsView.swift     Server URL, API key, folder, timing, Test Connection
│   │   └── PermissionsView.swift  Visual checklist with Re-check and per-row Settings links
│   └── Util/
│       └── Logger.swift           os.Logger wrapper (subsystem co.wispralt)
│
│   (Wave 1b — handled by other agents)
│   ├── Hotkeys/FNKeyMonitor.swift + HotkeyEvents.swift
│   ├── Capture/DictationRecorder.swift + MeetingRecorder.swift + ...
│   ├── Server/ServerClient.swift + DictationAPI.swift + MeetingAPI.swift + ...
│   └── Inject/TextInjector.swift + ...
```

---

## Permissions Required at Runtime

| Permission | Purpose | TCC Key |
|---|---|---|
| Accessibility | Insert text via AXUIElement | Privacy_Accessibility |
| Input Monitoring | Detect FN-key via CGEventTap | Privacy_ListenEvent |
| Microphone | Record dictation and meeting audio | Privacy_Microphone |
| Screen Recording | Capture system audio via SCStream | Privacy_ScreenCapture |

On **macOS 14.4+**, Input Monitoring requires a process restart after the first grant.
WisprAlt shows a "Quit and Reopen Required" sheet to enforce this.

---

## Key Architectural Notes

- `LSUIElement = true` — no Dock icon; menubar only.
- API key is stored exclusively in the macOS Keychain (`co.wispralt` service). It is never in `UserDefaults` or any plist.
- Dictation is a no-op if a meeting recording is active (mic mutual exclusion).
- Sparkle update prompts are deferred while a meeting is recording.

For full usage instructions see [docs/SETUP-CLIENT.md](../docs/SETUP-CLIENT.md).
