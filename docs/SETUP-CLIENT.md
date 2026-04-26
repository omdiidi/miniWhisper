---
title: Client Setup
---

# Client Setup

## Prerequisites

- macOS 14.0 or later (Apple Silicon or Intel)
- The WisprAlt server running and reachable (see [SETUP-SERVER.md](SETUP-SERVER.md))
- Apple Developer ID signing (for local builds; pre-built DMGs are already signed)

---

## Install from DMG (Recommended)

1. Download `WisprAlt-<version>.dmg` from the [GitHub Releases](https://github.com/your-org/wisprflowALT/releases) page.
2. Open the DMG; drag `WisprAlt.app` to `/Applications`.
3. On first launch, macOS Gatekeeper may show "WisprAlt cannot be opened because Apple cannot check it for malicious software." Click **Cancel**, then open **System Settings > Privacy & Security**, scroll to the bottom, and click **Open Anyway**.
4. The four-step permission wizard starts automatically (see below).

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

### Personal use (no Apple Developer ID)

If you only need WisprAlt on your own Mac and don't plan to distribute it, use the local build flow.

**One-time setup** (avoids the TCC re-grant loop on every rebuild):
```bash
./scripts/setup-local-codesign.sh
```
This creates a persistent self-signed cert in your login keychain and trusts it as a System code-signing root. Requires sudo once — after that, every future rebuild reuses the same identity, so macOS TCC keeps your Accessibility / Input Monitoring / Microphone / Screen Recording grants. Without this step, TCC sees each rebuild as a fresh app and re-prompts for all four permissions.

**Build:**
```bash
./scripts/build-client-local.sh
```
Output: `client/build/WisprAlt.app`. The script auto-detects whether the persistent identity is set up; otherwise falls back to ad-hoc and prints a hint pointing at the setup script. Right-click → Open the first time to bypass Gatekeeper.

Caveats:
- Sparkle auto-update will not work for self-signed builds.
- The bundled `Sparkle.framework` rpath is set via `Package.swift` `linkerSettings` so dyld can resolve it; the script also verifies this before signing.
- Without the setup-local-codesign step, every code change forces a fresh re-grant of all four TCC permissions.

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
