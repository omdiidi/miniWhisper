---
title: Client Setup
---

# Client Setup

## Prerequisites

- macOS 14.0 or later (Apple Silicon or Intel)
- The WisprAlt server running and reachable (see [SETUP-SERVER.md](SETUP-SERVER.md))

### Code-signing prerequisite for local builds

Local builds require a **free Apple Development certificate** — no Apple Developer Program enrollment ($99/yr) needed.

**One-time setup (per build machine):**
1. Open Xcode.
2. Go to **Settings → Accounts** and click **+** to sign in with any Apple ID.
3. Xcode automatically creates a Personal Team and issues an `Apple Development` certificate to your login keychain. It auto-renews each year while you stay signed in.
4. `scripts/build-client-local.sh` picks up the identity automatically from `security find-identity`.

If you have multiple Apple IDs with separate identities, set the `SIGN_IDENTITY` environment variable explicitly when building:
```bash
SIGN_IDENTITY="Apple Development: you@example.com (TEAMID)" bash scripts/build-client-local.sh
```

> **Note:** The script will print the full list of found identities and fail clearly if more than one is present and `SIGN_IDENTITY` is not set.

---

## Install from DMG (Recommended)

1. Download `WisprAlt-<version>.dmg` from the [GitHub Releases](https://github.com/your-org/wisprflowALT/releases) page.
2. Open the DMG; drag `WisprAlt.app` to `/Applications`.
3. **Remove the quarantine flag** before first launch (required for Apple Development-signed builds not distributed through the App Store):
   ```bash
   xattr -dr com.apple.quarantine /Applications/WisprAlt.app
   open /Applications/WisprAlt.app
   ```
   Alternatively: right-click the app in Finder → **Open** → click **Open** in the Gatekeeper dialog. You only need to do this once per version.
4. The four-step permission wizard starts automatically (see below).

### Installing on a friend's Mac

Your friend installs only the client — they connect to your existing Mac mini server. They do not need their own server, their own cloudflared, or their own API key.

Send your friend:
- The `WisprAlt.dmg` download link (or the built `.app` directly).
- Your server URL (`https://transcribe.yourdomain.com`).
- Your API key (use Settings → Export API Key to generate a `.wispralt-key` file; see [Backing up your API key](#backing-up-your-api-key)).

Your friend will see the same Gatekeeper warning and the same permission wizard described above. The `xattr -dr` step applies to their machine too.

---

## First-Launch Permission Wizard

The wizard runs sequentially. Each missing permission shows an NSAlert with an **Open System Settings** button. After granting a permission in System Settings, return to the app and click **Continue Anyway** or **Re-check All** in the Permissions view.

### Step 1 — Accessibility

Required for AXUIElement text injection (inserting transcribed text at your cursor).

- System Settings > Privacy & Security > Accessibility
- Toggle WisprAlt on.

### Step 2 — Input Monitoring

Required for CGEventTap FN-key detection (dictation holds and triple-tap for meetings).

- System Settings > Privacy & Security > Input Monitoring
- Toggle WisprAlt on.

**macOS 14.4+ Quit and Reopen Required:**
After granting Input Monitoring on macOS 14.4 or later, the permission does not take effect until the app restarts. WisprAlt will show a blocking sheet titled **"Quit and Reopen Required"** with a **Quit Now** button. Click it, then relaunch WisprAlt. The wizard will resume at Step 3.

### Step 3 — Microphone

Required for AVAudioEngine dictation recording and SCStream microphone capture in meeting mode.

- System Settings > Privacy & Security > Microphone
- Toggle WisprAlt on (the system prompt appears automatically; click Allow).

### Step 4 — Screen Recording

Required for capturing system audio (channel 2) during meeting recording via SCStream.

- System Settings > Privacy & Security > Screen Recording
- Toggle WisprAlt on.

---

## Server Configuration

After the permission wizard, the app is ready for server configuration.

1. On your Mac mini, the `setup-server.sh` script prints a one-liner at the end:
   ```
   SERVER_URL=https://transcribe.example.com
   API_KEY=<your-32-byte-hex-key>
   ```
2. Click the WisprAlt menubar icon → toggle **Show advanced settings**.
   The advanced section reveals:
   - **Server URL** — paste the `https://` URL and press Return.
   - **API Key** — paste the bearer token and press Return. Stored in the macOS Keychain under service `co.wispralt` and never written to disk as plain text.
3. Collapse advanced settings (you don't need it for daily use). At the top:
   - **Open Portal** — opens `<server>/admin/login` in your browser. Admins land on the global dashboard; employees land on their own usage page. Same button for both roles.
   - **Open Meetings Folder** — reveals your meeting transcripts directory in Finder.
4. Click **Test Connection** under the **Connection** section. It calls `/healthz`, `/readyz/dictation`, `/readyz/meeting` in parallel and shows a single status line:
   - **Green** "Connected — dictation + meeting ready"
   - **Orange** "Connected — meeting pipeline still loading" (or similar warming variants)
   - **Red** when the host is unreachable or the API key is rejected

### Menubar icon states

| Mode | Icon | Notes |
|---|---|---|
| Idle | mic outline | Default |
| Dictating | mic filled | While holding FN |
| Meeting recording | red filled dot + bold red **REC** label | Triple-tap FN to start; cannot be missed in a dense menubar |
| Uploading | upload-cloud | After meeting stops, while uploading |
| Processing | waveform | While server is transcribing |
| Done | checkmark | Briefly, before returning to Idle |

The REC indicator is rendered as a single composite NSImage (NSBezierPath red dot + bold red "REC" attributed text drawn into one bitmap). The earlier image+attributedTitle pair was character-wrapping in cramped menubars (R/E/C stacked vertically); the composite-bitmap approach takes layout decisions away from the menubar layout engine entirely.

### Picking your input mic

A second menubar icon (mic SF symbol) sits next to the WisprAlt icon. Click it to drop a native menu:

```
Input Mic
─────────────────
System Default (current: <name>)
─────────────────
✓ MacBook Pro Microphone
  AirPods Pro
  USB Audio Interface
─────────────────
Open Sound Settings…
```

- **Click any device** → WisprAlt records from that device for both dictation and meetings.
- **Dictation**: applied via `AudioUnitSetProperty(kAudioOutputUnitProperty_CurrentDevice)` on the AVAudioEngine input node. No system-wide side effect.
- **Meeting recording**: SCStream has no per-stream mic API, so WisprAlt temporarily overrides the system-wide default input device for the duration of the recording, restoring on stop. **Side effect**: other apps using audio during the meeting will also use your chosen mic. Acceptable because you explicitly picked it.
- **Crash recovery**: if the app crashes mid-meeting, the system default is restored on the next launch via `pendingMeetingDefaultInputUID` UserDefaults persistence.
- **Recording state**: while a meeting or dictation is active, the mic icon turns red (`contentTintColor = .systemRed`) so you can spot recording state at a glance.
- **No input devices found**: the menu shows a fallback row linking to System Settings → Privacy & Security → Microphone if mic permission is revoked.
- **Hide the icon**: open the menubar popover → Show advanced settings → toggle off "Show input mic in menu bar". Restart WisprAlt to apply.

The mic selector lives in `client/WisprAlt/App/MicMenuBarController.swift`. Device enumeration goes through `client/WisprAlt/Audio/MicEnumerator.swift` which bridges AVFoundation discovery to CoreAudio HAL property reads/writes.

### Meeting filenames

Saved meetings land in your meetings folder with human-readable names:

```
Mon Apr 27 2.06.34pm-2.07.12pm.wav
Mon Apr 27 2.06.34pm-2.07.12pm.json
Mon Apr 27 2.06.34pm-2.07.12pm.srt
Mon Apr 27 2.06.34pm-2.07.12pm.vtt
Mon Apr 27 2.06.34pm-2.07.12pm.txt
```

- Format: `EEE MMM d h.mm.ssa-h.mm.ssa.<ext>` in POSIX (English) locale for stable sorting.
- Periods instead of colons for filesystem-friendliness across rsync, zip, and Windows.
- Seconds in both timestamps eliminate sidecar collisions across all five extensions.
- If the rename fails (rare — concurrent file appearance), WisprAlt falls back to the start-only name and logs a warning.

### Copy your API key

In the popover → Show advanced settings → API Key Backup section, the **Copy API Key** button puts your token on the clipboard for one-shot pastes. The clipboard auto-clears after 60 seconds, but only if you haven't copied something else in the meantime (uses `NSPasteboard.changeCount` snapshot — survives Universal Clipboard correctly).

---

## Launch at Login

WisprAlt registers itself as a login item the first time it launches. Your menubar icon will reappear automatically after every login without any manual steps.

**To enable or disable the login item:**

- In the WisprAlt settings popover, use the **Launch at login** toggle.
- Or: **System Settings → General → Login Items & Extensions** → find WisprAlt in the list and toggle it on or off.

If the toggle shows "requires approval" in System Settings, click it to turn it on. If the entry is missing entirely after a reinstall, run `sfltool resetbtm` in Terminal and reboot — this clears a stale Launch Services database.

---

## Backing up your API key

Your API key is stored in the macOS Keychain. To move it to another Mac or share it with a friend, use the export/import flow in Settings.

**Export:**
1. Open the WisprAlt settings popover → **Export API Key…**
2. Save the file to your **Desktop** (the default location).
3. The file is a plain text `.wispralt-key` file set to mode `0600`.

**IMPORTANT — treat this file like a password. Never save the export file to:**
- `~/Documents/` (likely synced to iCloud Drive)
- iCloud Drive
- Dropbox
- Google Drive
- Any cloud-synced location

Once you have moved the file to the destination Mac (e.g. via AirDrop or a USB drive), delete the export file from your Desktop.

**Import:**
1. On the destination Mac, open Settings → **Import API Key…**
2. Select the `.wispralt-key` file. The key is written to the Keychain immediately.
3. Click **Test Connection** to confirm the key works.

### Restoring on a new Mac

1. Export the key from your current Mac (Settings → Export API Key).
2. Transfer via AirDrop or a USB drive. **Do not email or message it.**
3. Import on the new Mac (Settings → Import API Key).
4. Delete the export file from both Macs once the import succeeds.

---

## Dictation Usage

1. Hold the **FN** key (Globe key on M-series Macs). The menubar icon turns to a filled mic.
2. Speak. Release FN to send audio to the server.
3. Transcribed text is injected at the cursor (AXUIElement primary; clipboard + Cmd+V fallback for Electron apps).
4. Round-trip latency: ~250–400ms p50.

**Note:** Holding FN while a meeting recording is active is a no-op (with a warning in the log). This is the mic mutual exclusion rule.

---

## Meeting Recording Usage

1. Triple-tap the **FN** key within 400ms. The menubar icon changes to a recording circle.
2. Speak normally. The app captures both your microphone (channel 1) and system audio (channel 2) in a dual-channel 16kHz WAV.
3. Triple-tap FN again (or click the menubar item) to stop recording.
4. The UI moves through three explicit states: **Uploading (with %)** → **Processing** → **Done**.
5. Completed transcripts are saved to `~/Documents/WisprAlt/Meetings/` as `.json`, `.srt`, `.vtt`, and `.txt`.

### Recording Limits

Meeting recording is capped at **90 minutes** by default (configurable via Settings → Max meeting length). At the 60-minute mark, a system notification fires warning that the recording will auto-stop in 30 minutes. After the cap is reached, recording stops automatically and the upload begins immediately. You can change the cap (5–240 minutes) in the WisprAlt settings popover.

### In-Person Mode Auto-Detection

If channel 2 (system audio) is silent for more than 90% of 100ms frames during the recording, the server automatically uses single-channel diarization with "Speaker 1", "Speaker 2", … labels instead of "You" / "Other".

---

## Speaker Rename (Offline-Capable)

1. Click the WisprAlt menubar icon and select the transcript from the list.
2. In the Transcript Detail view, click a speaker name and type the new name.
3. The app atomically rewrites all four local files (JSON, SRT, VTT, TXT) without any server round-trip. This works entirely offline.

---

## Auto-Updates

**Tier 1.5 distribution** (the current path) uses GitHub Releases via
the `/wispralt-update` Claude Code slash command. Sparkle is **disabled**
in be720a1 — the framework is bundled but the auto-updater never runs:

- `SUEnableAutomaticChecks=false` in `Info.plist`
- `SPUStandardUpdaterController(startingUpdater: false)` in
  `SparkleController.init()`
- `didAbortWithError` / `didFinishUpdateCycleFor` only log; no
  user-facing notifications

The previous `Info.plist` had a placeholder `SUFeedURL` pointing at
`https://omid.example/wispralt/appcast.xml` that triggered "Unable to
Check For Updates" alerts every few hours. Disabling Sparkle removes the
dialog completely.

**To update:** in Claude Code, run `/wispralt-update`. It diffs the
installed `CFBundleShortVersionString` against the latest GitHub
Release tag, downloads + verifies + replaces the app, and runs a
TCC reset cycle if the cdhash changed.

When we move to Tier 2 distribution (Apple Developer Program +
notarized builds + signed appcast), flip `SUEnableAutomaticChecks=true`
and `startingUpdater: true`, populate `SUFeedURL` and `SUPublicEDKey`,
and re-enable the user-facing update notifications.

---

## Employee install (recommended)

This is the one-command install path for a teammate who has been given a
WisprAlt API key. It lives in the user's `~/.claude-dotfiles/commands/` —
it is **not** a project-scoped slash command — so it works on a fresh Mac
without cloning this repo.

**Prerequisites:**
- macOS 14.0 or later.
- [Claude Code](https://claude.com/claude-code) installed.
- `gh auth login` once (so the slash command can call `gh release
  download` non-interactively).

**Install:**
1. Open Claude Code.
2. Run `/wispralt-setup`.

What it does (full pseudocode in
`~/.claude-dotfiles/commands/wispralt-setup.md`):

1. Verifies macOS ≥ 14, installs Homebrew + `gh` if missing.
2. Picks the latest GitHub Release tag for `omdiidi/miniWhisper`.
3. Downloads the DMG and its `.sha256` sidecar.
4. Verifies the SHA256.
5. Mounts, copies `WisprAlt.app` to `/Applications`, unmounts, strips
   quarantine.
6. Opens the app — `PermissionGate.swift` walks four macOS permissions.
7. Prints: "Now paste the API key Omid texted you in Settings → API Key."

**Update path:** later, run `/wispralt-update` to pull the next release.
That command diffs the installed `CFBundleShortVersionString` against
the latest tag, replaces the app if newer, and runs `tccutil reset` for
all four permissions if the cdhash changed (Apple-Development-signed
re-builds get a new cdhash and re-prompt for permissions on every
install).

The build-from-source flow below is for **admin-grade** local builds
(Omid's MacBook, contributor machines). Employees should not need it.

---

## Building from Source

See [client/README.md](../client/README.md) for the build/run quickstart.

### Personal use (free Apple Development certificate)

If you only need WisprAlt on your own Mac and don't plan to distribute it, use the local build flow. You need a **free Apple Development certificate** (see [Code-signing prerequisite for local builds](#code-signing-prerequisite-for-local-builds) above).

**Build:**
```bash
bash scripts/build-client-local.sh
```
Output: `client/build/WisprAlt.app`. Copy it to `/Applications/`, strip the quarantine flag (`xattr -dr com.apple.quarantine /Applications/WisprAlt.app`), and open.

**Per-version TCC re-grant:** Each new build changes the binary's cdhash. macOS sees the new binary as a new app and re-prompts for the four permissions (Accessibility, Input Monitoring, Microphone, Screen Recording). This is Apple-enforced behavior — not a bug. See [DEPLOYMENT-NOTES.md](DEPLOYMENT-NOTES.md) for the full explanation and the `tccutil reset` recovery commands.

**Annual cert renewal:** Free Apple Development certs expire after one year. Xcode auto-renews silently while you stay signed in with your Apple ID, but the renewed cert has a new SHA-1 and a new Designated Requirement. This triggers the same TCC re-grant cycle as a binary rebuild — once a year. Expected behavior; not a bug.

Caveats:
- Sparkle auto-update will not work for local builds (EdDSA key not provisioned).
- The bundled `Sparkle.framework` rpath is set via `Package.swift` `linkerSettings` so dyld can resolve it; the script also verifies this before signing.

### Distribution build (signed + notarized DMG)

For a signed, notarized DMG suitable for sharing:
```bash
./scripts/build-client.sh "$DEVELOPER_ID_APP"
```
Required environment variables: `APPLE_ID`, `APP_SPECIFIC_PASSWORD`, `TEAM_ID`, `DEVELOPER_ID_APP`, `SPARKLE_ED_PRIVATE_KEY`. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full secrets list.

---

## Uninstall

Open the WisprAlt settings popover → **Uninstall…** The in-app flow:
1. Stops any active recording.
2. Confirms deletion of `~/Documents/WisprAlt/` (with a dialog).
3. Deletes the Keychain item (service `co.wispralt`).
4. Removes the UserDefaults domain (`co.wispralt.WisprAlt`).
5. Moves `WisprAlt.app` to Trash.

Alternatively run `./scripts/uninstall-client.sh` from the repo root.
