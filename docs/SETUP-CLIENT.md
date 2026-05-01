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

The WisprAlt menubar popover has an **Input Mic** section near the top:

```
Input Mic
  ┌─────────────────────────────────────┐
  │ System Default (MacBook Mic)     ▼ │
  └─────────────────────────────────────┘
```

The dropdown lists every input device macOS sees (built-in, AirPods, USB interfaces) plus a "System Default" entry at the top. Selecting a device sets `Settings.preferredInputDeviceUID`.

- **Scope**: this picker affects **dictation only**. Meeting recording always uses the macOS system default input — SCStream has no per-stream mic API, and WisprAlt deliberately does not override the system-wide default (would affect every other app on the system). If you want a specific mic for meetings, set it in System Settings → Sound.
- **Application**: WisprAlt sets the dictation engine's input via `AudioUnitSetProperty(kAudioOutputUnitProperty_CurrentDevice)` on the AVAudioEngine input node, with no system-wide side effect.
- **Live changes**: changing the picker mid-idle takes effect on the next dictation. Mid-recording changes don't disturb the active session — the existing audio-device-change abort logic handles legitimate device disconnects (AirPods unplugged etc.).
- **Permission**: if mic permission is revoked, the dropdown only shows "System Default" with no other options. Re-grant in System Settings → Privacy & Security → Microphone.

Device enumeration lives in `client/WisprAlt/Audio/MicEnumerator.swift` (AVFoundation discovery + CoreAudio HAL property reads).

### Meeting filenames

Saved meetings land in your meetings folder with human-readable names:

```
Mon Apr 27 2.06pm-2.07pm.wav
Mon Apr 27 2.06pm-2.07pm.json
Mon Apr 27 2.06pm-2.07pm.srt
Mon Apr 27 2.06pm-2.07pm.vtt
Mon Apr 27 2.06pm-2.07pm.txt
```

- Format: `EEE MMM d h.mma-h.mma.<ext>` in POSIX (English) locale for stable sorting.
- Periods instead of colons for filesystem-friendliness across rsync, zip, and Windows.
- Two meetings ending at the same minute get a `(2)`, `(3)`, etc. suffix.
- If the rename fails (rare — concurrent file appearance), WisprAlt falls back to the start-only name and logs a warning.

### Copy your API key

In the popover → Show advanced settings → API Key Backup section, the **Copy API Key** button puts your token on the clipboard for one-shot pastes. The clipboard auto-clears after 60 seconds, but only if you haven't copied something else in the meantime (uses `NSPasteboard.changeCount` snapshot — survives Universal Clipboard correctly).

### Smart formatting

The popover has a **Smart formatting** toggle in the Settings section. When ON, every dictation is sent to the server with the `X-Smart-Format: true` header; for dictations of at least 100 words, the server hands the raw Parakeet output to OpenRouter Mercury 2, which fixes punctuation and casing, removes obvious fillers ("um", "uh", repeated words), and adds bullet-list formatting where you're clearly enumerating items. Meaning is preserved — no rephrasing, no summarization. Below 100 words the call short-circuits and you get raw Parakeet output. The threshold targets long-form dictation (e.g. LLM prompts, notes) where the cleanup is actually visible.

- **Default**: OFF. Raw Parakeet output is what gets injected.
- **Latency cost**: ~250ms additional wall-clock per dictation when the cleanup runs (the Mercury call is fail-soft — a timeout or HTTP error returns the raw text and the response field `smart_formatted: false`).
- **Server requirement**: the operator must have set `OPENROUTER_API_KEY` in `server/.env`. If the key is missing, every dictation comes back as raw text regardless of the toggle — there is no client-visible error, the toggle just silently does nothing. Ask your admin if cleaned output is what you expect but you keep getting raw output.
- **Pricing**: per-cleanup is roughly $0.0001 against your operator's OpenRouter credit. Negligible for individual employees; visible in OpenRouter usage if you batch-dictate hundreds of clips.
- **Privacy note**: smart formatting sends your raw transcript to OpenRouter (cloud). If you handle confidential material, leave the toggle OFF.

The toggle does NOT affect meeting recordings — those run through WhisperX server-side and are never routed through Mercury.

### Your name

The first time you launch WisprAlt after installing, a small sheet asks for your **display name**. This is the friendly name your operator sees in the admin UI alongside your `label` (typically your email). It is your own — only you can edit it.

- Skip the sheet to leave `display_name` as `null`. The admin UI will show your `label` until you fill it in later.
- Edit anytime from the popover → Settings → **Identity** section. The change is sent via `PATCH /me` and takes effect immediately.
- Constraints: 1–40 characters, no control chars (newline/tab/NUL). Validation happens both client-side and at the SQL CHECK constraint level.

The first-launch sheet is driven by `client/WisprAlt/UI/FirstLaunchCoordinator.swift`, which calls `GET /me` once on launch and presents the sheet if `display_name` is `null`.

### App icon

WisprAlt now ships with a real icon (dark mic + chat bubble brand mark). You'll see it in:
- **Finder Get Info** when inspecting `/Applications/WisprAlt.app`.
- **Launchpad** and **Spotlight** results.
- The macOS notification banners.

The icon set is generated by `scripts/build-icon.sh` from a master source and lives at `client/WisprAlt/Resources/Assets.xcassets/AppIcon.appiconset/`. If you build from source the script regenerates the 10 required PNG sizes automatically; you should not need to run it manually unless you're customizing the icon.

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

The canonical install path is the curl one-liner — pure bash + native
macOS tools. **No Claude Code, no Homebrew, no `gh`, no sudo, no auth.**
Works on a fresh Mac without cloning this repo. See
[INSTALL.md](INSTALL.md) for the full guide and troubleshooting.

**Prerequisites:**
- macOS 14.0 or later (Sonoma+).
- Apple Silicon (current builds are arm64-only).
- Internet (anonymous; no GitHub auth needed since the repo is public).
- Xcode Command Line Tools (or `python3` will trigger the install dialog
  on first invocation — install ahead of time via `xcode-select --install`).

**Install:**

```bash
curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh \
  | WISPRALT_API_KEY=sk_xxx WISPRALT_SERVER=https://transcribe.integrateapi.ai bash
```

What `install.sh` does (full source at the repo root):

1. Refuses to run as root or under sudo (Keychain + UserDefaults are user-scoped).
2. Verifies macOS ≥ 14 and Apple Silicon; bails clearly otherwise.
3. Fetches the latest GitHub Release JSON anonymously via the public API.
4. Downloads the DMG and its `.sha256` sidecar; validates the DMG is real
   via `hdiutil imageinfo` (catches captive-portal HTML); verifies SHA256.
5. Mounts the DMG, replaces `/Applications/WisprAlt.app` cleanly (`rm -rf`
   then `cp -R` — never nests), unmounts, strips `com.apple.quarantine`.
6. On re-install, runs `tccutil reset All co.wispralt.WisprAlt` to clear
   stale TCC entries (cdhash changes on every Apple-Development build).
7. Writes the API key to the Keychain (`security add-generic-password
   -s co.wispralt -a default -U`) and the server URL to UserDefaults
   (`defaults write co.wispralt.WisprAlt serverURL ...`). Both are
   skipped silently if the corresponding env var is unset.
8. Flushes `cfprefsd` so the app reads the new defaults on first launch.
9. Opens the app — `PermissionGate.swift` walks four macOS permissions.
10. Polls (up to 10s) to confirm the app process actually started; warns
    if it didn't (likely a Gatekeeper dialog needing right-click → Open).

**Update path:** re-run the same curl one-liner. `install.sh` is
idempotent and pulls the latest release each time. The Claude Code
`/wispralt-update` slash command remains as a developer convenience for
teammates who already use Claude Code; it's no longer the primary path.

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
