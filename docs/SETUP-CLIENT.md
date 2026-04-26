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
2. In the WisprAlt menubar popover, open **Settings**:
   - Paste the `https://` URL into the **Server URL** field and press Return.
   - Paste the API key into the **API Key** field and press Return. It is stored in the macOS Keychain under service `co.wispralt` and never written to disk as plain text.
3. Click **Test Connection** to verify `/healthz` and both `/readyz` endpoints are reachable.

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

## Auto-Updates (Sparkle 2)

WisprAlt checks for updates via a Sparkle 2 EdDSA-signed appcast hosted on GitHub Pages. Updates are never applied automatically during a meeting recording — the sheet is deferred until recording stops. The user must click **Restart Now** explicitly (`SUAutomaticallyUpdate = NO`).

To check for updates manually: click the WisprAlt menubar icon → **Check for Updates…**

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
