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
4. The three-step permission wizard starts automatically (see below).

### Installing on a friend's Mac

Your friend installs only the client — they connect to your existing Mac mini server. They do not need their own server, their own cloudflared, or their own API key.

Send your friend:
- The `WisprAlt.dmg` download link (or the built `.app` directly).
- Your server URL (`https://transcribe.yourdomain.com`).
- Your API key, over a private channel (see [Moving your API key](#moving-your-api-key)). They paste it into Settings → API Key.

Your friend will see the same Gatekeeper warning and the same permission wizard described above. The `xattr -dr` step applies to their machine too.

---

## First-Launch Permission Wizard

The wizard runs sequentially. Each missing permission shows an NSAlert with an **Open System Settings** button. After granting a permission in System Settings, return to the app and click **Continue Anyway** or **Re-check All** in the Permissions view.

### Step 1 — Accessibility

Required for AXUIElement text injection (inserting transcribed text at your cursor).

- System Settings > Privacy & Security > Accessibility
- Toggle WisprAlt on.

### Step 2 — Input Monitoring

Required for CGEventTap FN-key detection (dictation holds).

- System Settings > Privacy & Security > Input Monitoring
- Toggle WisprAlt on.

**macOS 14.4+ Quit and Reopen Required:**
After granting Input Monitoring on macOS 14.4 or later, the permission does not take effect until the app restarts. WisprAlt will show a blocking sheet titled **"Quit and Reopen Required"** with a **Quit Now** button. Click it, then relaunch WisprAlt. The wizard will resume at Step 3.

### Step 3 — Microphone

Required for AVAudioEngine dictation recording.

- System Settings > Privacy & Security > Microphone
- Toggle WisprAlt on (the system prompt appears automatically; click Allow).

---

## Mode: Online (OpenAI) or Local server

The top of the popover has a **Mode** picker: **Online (OpenAI)** sends dictation audio straight to OpenAI (`gpt-transcribe` by default) using your own OpenAI key; **Local server** uses your self-hosted WisprAlt server (configured below). The default is **Online**, on fresh installs and on upgrade (no probe of a saved server URL); pick Local if you run your own server. A change applies to the next dictation; a dictation already recording finishes on the mode it started in. The picker is locked only while a Local file is converting or uploading ("Finish or cancel the upload first").

Under the picker a status line shows `Online · gpt-transcribe · key saved` (or `key missing`) or `Local · <server host>` with `reachable` / `unreachable` (checked when the popover opens in Local or when you switch to Local), plus `transcribing N` while dictations are still being transcribed.

**Model.** Show advanced settings → **OpenAI Model** lists every OpenAI transcription model with its price per minute in small text (`≈` marks an estimate for token-billed models) and, for older models, the date OpenAI retires them (e.g. "retires Feb 26, 2027"). The default is `gpt-transcribe` ($0.0045/min). The choice applies to the next dictation; file transcription always uses a file model, and dictation stops at 10 minutes. If OpenAI reports the chosen model does not exist, the app switches back to `gpt-transcribe` and tells you.

In Online mode nothing is sent to your Local server, and the server-only rows are hidden (Connection / Test Connection, Server URL, API Key and its backup, Your name, Open Portal, Open My Dictations, the job Log button). **Transcribe file…** works in Online too (TXT + JSON only; see below). **Copy last dictation** works in both modes: it copies the last dictation's text, kept on this Mac for 7 days. If a dictation fails (or might be cut off) its audio is kept under **Retry** in the popover (last 2, 7 days, 16 kHz) with Retry, Export… and Discard. Both rely on FileVault for protection at rest. Switching to Online while a Local file job is being watched stops watching it without cancelling it on the server; switching back resumes watching it.

### OpenAI API key

Enter it in **Show advanced settings → OpenAI API Key** (Save / Remove), or from Terminal (works while the app runs; the next FN press picks it up):

```
security add-generic-password -U -s co.wispralt.openai -a default -w
```

The key lives only in the Keychain (service `co.wispralt.openai`, account `default`), in memory, and in the `Authorization: Bearer` header sent to `api.openai.com`. A key created from Terminal trusts Terminal, so each newly signed build asks once for Keychain access; choose **Always Allow**.

Use a **project-scoped** OpenAI key (a project just for WisprAlt) with a **monthly budget cap** set in the OpenAI dashboard, so a lost key or a runaway loop has a ceiling. Online pricing: `gpt-transcribe` is $0.0045 per audio minute. With Online selected and no key, holding FN does not record: a notice appears and the popover opens at the key field.

## Server Configuration

After the permission wizard, the app is ready for server configuration (Local mode).

1. On your Mac mini, the `setup-server.sh` script prints a one-liner at the end:
   ```
   SERVER_URL=https://transcribe.example.com
   API_KEY=<your-32-byte-hex-key>
   ```
2. Click the WisprAlt menubar icon → toggle **Show advanced settings**.
   The advanced section reveals:
   - **Server URL** — paste the `https://` URL and press Return. Plain `http://` is accepted only for a server on this Mac (`127.0.0.1`, `::1`, `localhost`); any other host needs https ("Use https://, or http:// only for this Mac"). For a LAN or tailnet server, put https in front of it, e.g. `tailscale serve`. The app's ATS setting is `NSAllowsLocalNetworking` only. A dictation always finishes on the server URL that was set when you pressed FN, even if you change it mid-dictation.
   - **API Key** — paste the bearer token and press Return. Stored in the macOS Keychain under service `co.wispralt` and never written to disk as plain text.
3. Collapse advanced settings (you don't need it for daily use). At the top:
   - **Open Portal** — opens `<server>/admin/login` in your browser. Admins land on the global dashboard; employees land on their own usage page. Same button for both roles.
   - **Open File Transcriptions** — reveals the `Custom Transcriptions` folder (one subfolder per file job) in Finder.
   - **Open Transcripts Folder** — reveals the transcripts folder chosen in Settings (default `~/Documents/WisprAlt/Transcripts/`; a Mac that already has `~/Documents/WisprAlt/Meetings/` keeps using it. The default is saved on first launch, so the folder never moves).
4. Click **Test Connection** under the **Connection** section. It calls `/healthz`, `/readyz/dictation` and the authenticated `/me` in parallel and shows a single status line:
   - **Green** "Connected as <you> — dictation ready."
   - **Orange** "Connected as <you> — dictation degraded (file job using memory)" or "dictation pipeline still loading"
   - **Red** when the host is unreachable or the API key is rejected

### Menubar icon states

| Mode | Icon | Notes |
|---|---|---|
| Idle | mic outline | Default |
| Dictating | mic filled | While holding FN |
| Converting | circular arrows | Preparing a file for upload |
| Uploading | upload-cloud | While a file uploads ("Transcribe file…") |
| Processing | waveform | While the server transcribes a Local file, or while an Online file job runs |
| Done | checkmark | Briefly, before returning to Idle |

### Picking your input mic

The WisprAlt menubar popover has an **Input Mic** section near the top:

```
Input Mic
  ┌─────────────────────────────────────┐
  │ System Default (MacBook Mic)     ▼ │
  └─────────────────────────────────────┘
```

The dropdown lists every input device macOS sees (built-in, AirPods, USB interfaces) plus a "System Default" entry at the top. Selecting a device sets `Settings.preferredInputDeviceUID`.

- **Scope**: this picker affects dictation (the only thing WisprAlt records). WisprAlt deliberately does not override the system-wide default input.
- **Application**: WisprAlt sets the dictation engine's input via `AudioUnitSetProperty(kAudioOutputUnitProperty_CurrentDevice)` on the AVAudioEngine input node, with no system-wide side effect.
- **Live changes**: changing the picker mid-idle takes effect on the next dictation. Mid-recording changes don't disturb the active session — the existing audio-device-change abort logic handles legitimate device disconnects (AirPods unplugged etc.).
- **Permission**: if mic permission is revoked, the dropdown only shows "System Default" with no other options. Re-grant in System Settings → Privacy & Security → Microphone.

Device enumeration lives in `client/WisprAlt/Audio/MicEnumerator.swift` (AVFoundation discovery + CoreAudio HAL property reads).

### Smart formatting

The popover has a **Smart formatting** toggle in the Quality section. When ON, the app itself (in both Online and Local mode) sends the finished transcript of every dictation of at least 20 words to OpenRouter Mercury 2 (`inception/mercury-2`), which fixes punctuation and casing, removes obvious fillers ("um", "uh", repeated words), and adds bullet-list formatting where you're clearly enumerating items. Meaning is preserved — no rephrasing, no summarization. A length check (cleaned text within 0.70–1.10× of the raw word count) rejects anything that changed too much.

- **Default**: OFF. The raw transcript is what gets injected.
- **Key**: your own OpenRouter key, pasted into the field under the toggle or added from Terminal: `security add-generic-password -U -s co.wispralt.openrouter -a default -w`. It lives only in the Keychain (`co.wispralt.openrouter`). Without it the toggle does nothing and the popover says "OpenRouter key missing".
- **Latency cost**: at most 2.5 s per dictation (hard deadline). Fail-soft: a timeout, HTTP error or rejected cleanup returns the raw text. The popover shows the last dictation's result ("formatted", "too short", "failed (timeout)", …).
- **Pricing**: per-cleanup is roughly $0.0001 of OpenRouter credit.
- **Privacy note**: smart formatting sends the transcript text (never audio) to OpenRouter (cloud). If you handle confidential material, leave the toggle OFF.

The client no longer asks the server to format (`smart_format` / `X-Smart-Format` are not sent since Part 3). The toggle does NOT affect file transcriptions — they are never routed through Mercury.

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

## Moving your API key

Your keys live only in the macOS Keychain (`co.wispralt` for the Local server key, `co.wispralt.openai` for OpenAI, `co.wispralt.openrouter` for Smart formatting). WisprAlt has no export, import or "Copy API Key" button: a key never lands in a file or on the clipboard from the app.

To set up another Mac, get the key again from where it was issued (your server operator for the Local key, the OpenAI dashboard for an OpenAI key) and paste it into Settings → Advanced on the new Mac, or add it from Terminal:

```bash
security add-generic-password -U -s co.wispralt.openai -a default -w
```

(`-w` with no value prompts for the key, so it never appears in your shell history.) Never email or message a key.

---

## Dictation Usage

1. Hold the **FN** key (Globe key on M-series Macs). The menubar icon turns to a filled mic.
2. Speak. Release FN to send the audio: in Online to OpenAI (16 kHz mono WAV, one request), in Local to your server (long dictations stream in chunks while you speak).
3. Transcribed text is injected at the cursor (AXUIElement primary; clipboard + Cmd+V fallback for Electron apps).
4. Round-trip latency (Local): ~250–400ms p50. A dictation stops and sends at 10 minutes.
5. If you press FN while the previous dictation is still being saved, a notice says "Still saving the last dictation — press again".

**Note:** an update relaunch is refused while a dictation (or its in-flight upload) or a file job is running, so an update never kills your work.

---

## File Transcription Usage

**Online mode.** Click **Transcribe file…** and pick an audio or video file. Nothing is copied: the file is read where it is.

1. Supported files (mp3, mp4, m4a, wav, webm, ogg, flac, mpeg, mpga) under 25 MB and 10 minutes go to OpenAI as is, in one request. Anything else (longer, larger, mov/caf/aiff/aac, video) is decoded on this Mac: the audio track is extracted from a video, then split at quiet moments into pieces of at most 10 minutes, each sent as 16 kHz mono with the end of the previous piece as context. The popover shows "Chunk i of n".
2. Only when every piece succeeded, `<name>.txt` and `<name>.json` are saved together in a new `Custom Transcriptions/<name>__<timestamp>/` folder (see [TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md#online-json)). A failure or Cancel saves nothing. Subtitles (SRT/VTT) need Local mode.
3. Quitting mid-file loses that job (its temp files are cleaned up at the next launch); pick the file again. A video with no audio track, or a file this Mac cannot decode, says so ("This file type needs Local mode").
4. Switching modes never stops a running Online file job. If a Local file job was saved and you switch to Local during the Online job, the Local job is watched again once the Online job ends.

**Local mode.**

1. Click the WisprAlt menubar icon → **Transcribe file…** and pick any audio or video file (m4a, mp3, mp4, mov, wav, aac, flac, opus, ogg, webm, m4v, caf, aiff).
2. The file is copied into its own folder under `Custom Transcriptions` and uploaded as-is (large files go up in chunks). The UI moves through **Uploading (with %)** → **Processing** → **Done**.
3. The server transcribes it with MLX Whisper (one mono stream, no speaker labels). The model loads on the first file job, so the first one after an idle spell takes longer.
4. `.json`, `.srt`, `.vtt` and `.txt` are downloaded next to the source file (see [TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md)), then the server copy is deleted. **Copy last custom transcription** puts the newest TXT on the clipboard.

**Cancel** stops the client at once and re-enables **Transcribe file…**. If the server was already transcribing, it finishes that Whisper call before discarding the result; a new file submitted in that window gets "previous transcription still finishing".

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
- No developer tooling — the installer uses only stock macOS utilities (no Xcode
  Command Line Tools, no Homebrew).

**Install:**

```bash
curl -q -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh \
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
9. Opens the app — `PermissionGate.swift` walks three macOS permissions.
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

**Per-version TCC re-grant:** Each new build changes the binary's cdhash. macOS sees the new binary as a new app and re-prompts for the three permissions (Accessibility, Input Monitoring, Microphone). This is Apple-enforced behavior — not a bug. See [DEPLOYMENT-NOTES.md](DEPLOYMENT-NOTES.md) for the full explanation and the `tccutil reset` recovery commands.

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
