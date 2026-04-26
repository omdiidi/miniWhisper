---
description: Install and configure the WisprAlt macOS client app — download or build DMG, walk the user through 4 permissions, paste server config, test connection.
---

# /setup-client

Interactive client install on a Mac (MacBook Air or any device the user wants to dictate from).

## Apple Development identity prerequisite (one time per build machine)

Required only if building from source (not needed for DMG installs from a release).

WisprAlt uses `SMAppService.mainApp.register()` for login-at-startup. This API requires an Apple-issued code-signing identity — ad-hoc and self-signed identities silently fail at registration time (Apple Developer Forums thread 799910). A **free Apple Development certificate** from the Personal Team tier is sufficient; no Apple Developer Program enrollment ($99/yr) is needed.

**Setup:**
1. Open Xcode.
2. Go to Settings → Accounts → tap "+" → sign in with any Apple ID.
3. Xcode auto-creates a Personal Team and issues an `Apple Development` certificate to your login keychain. It auto-renews while you stay signed in.
4. Verify: `security find-identity -v -p codesigning | grep 'Apple Development'` should return at least one line.

## Pre-flight checks

1. `sw_vers -productVersion` — require ≥ 14.0 (`captureMicrophone` needs Sonoma+). If 13.x, abort with a clear message.
2. Check for `tmp/client-config.txt` (created by `/setup-server`). If present, read `SERVER_URL` and `API_KEY` from it and offer to use them automatically.

## Acquire the app

Try in this order:

1. **GitHub Release (DMG)**: `gh release download --pattern '*.dmg' -O /tmp/WisprAlt.dmg` — requires `gh auth status` to be valid and a published release.
2. **Local build**: if no release available or `gh` not authed, prompt the user. If they want to build:
   - Confirm the Apple Development identity is available (see prerequisite above).
   - Run `bash scripts/build-client-local.sh` from the repo root.
   - The built app lands at `client/build/WisprAlt.app`.

## Install

### From DMG (release download)

```bash
hdiutil attach /tmp/WisprAlt.dmg
cp -R /Volumes/WisprAlt/WisprAlt.app /Applications/
hdiutil detach /Volumes/WisprAlt
```

Remove the quarantine attribute that Gatekeeper sets on downloaded files:

```bash
xattr -dr com.apple.quarantine /Applications/WisprAlt.app
```

Without this step, macOS shows "WisprAlt cannot be opened because the developer cannot be verified" on the first launch. Right-click → Open → Open Anyway is the manual alternative, but the `xattr -dr` one-liner is faster.

### From local build

```bash
pkill -9 -f /Applications/WisprAlt.app/Contents/MacOS/WisprAlt 2>/dev/null || true
rm -rf /Applications/WisprAlt.app
cp -R client/build/WisprAlt.app /Applications/
```

No quarantine removal needed for local builds (the app was never downloaded).

## Reset stale TCC entries (re-installs only)

Required when re-installing over an existing app (rebuilds, version updates, friend re-installs). Stale TCC database entries from a previous binary's cdhash cause the well-known "I granted permissions but the app still says denied" pathology. Run BEFORE the first launch:

```bash
tccutil reset Accessibility   co.wispralt.WisprAlt
tccutil reset ListenEvent     co.wispralt.WisprAlt
tccutil reset ScreenCapture   co.wispralt.WisprAlt
tccutil reset Microphone      co.wispralt.WisprAlt
```

Skip on a truly fresh install (no prior `/Applications/WisprAlt.app` ever existed). When in doubt, run them — `tccutil reset` is harmless on a non-existent entry.

## Walk the 4-permission wizard

Open System Settings to each pane in order — instruct the user to grant permission, then continue:

1. **Accessibility** — `open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"` — required for AXUIElement text injection.
2. **Input Monitoring** — `open "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"` — required for FN key detection.
   - **macOS 14.4+ note**: After granting, the app will require a Quit-and-Reopen. Tell the user to expect a sheet.
3. **Microphone** — `open "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"`.
4. **Screen Recording** — `open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"` — required for `SCStream` system audio capture.

## Paste server config

Launch the app (`open /Applications/WisprAlt.app`). Open Settings (menubar icon → Settings).

Paste:
- **Server URL**: `$SERVER_URL` (e.g. `https://transcribe.example.com`)
- **API Key**: `$API_KEY`

Click **Test Connection**. Expect a green check.

## Verify login-launch

WisprAlt registers itself as a login item on its first launch (via `SMAppService.mainApp.register()` in `AppDelegate`).

**IMPORTANT — do NOT use the `swift -e` REPL one-liner**: `SMAppService.mainApp` resolves to the *calling* process's main bundle. From the `swift` REPL this is the swift binary itself, NOT WisprAlt — so the result is meaningless. Codex review caught this; the previous version of this doc had the wrong test.

Use these checks instead, in order of reliability:

1. **System Settings (most reliable, GUI)**: System Settings → General → Login Items & Extensions → confirm "WisprAlt" appears in the list with its toggle on. If it shows but is off, the SMAppService entry exists but the user disabled it — toggle on and the in-app Settings toggle will reflect it.

2. **In-app toggle**: Open WisprAlt menubar → Settings → "Launch at login" toggle. Reads `SMAppService.mainApp.status` from inside the WisprAlt process — this IS the right bundle. Should be on after first launch.

3. **`launchctl print-disabled` (CLI)**: `launchctl print-disabled "gui/$UID" | grep co.wispralt.WisprAlt`. If the entry is in the disabled list, it was registered but explicitly disabled. If absent, that means EITHER registered+enabled OR never-registered — distinguish with check 1 or 2.

4. **`sfltool dumpbtm` (cross-check, unstable)**: Private Apple tool, output format varies between Sonoma/Sequoia/Tahoe — use for debugging only:

```bash
sfltool dumpbtm | grep co.wispralt.WisprAlt
```

## Friend installs (DMG without the slash command)

Friends installing a shared DMG should run the quarantine removal immediately after copying to `/Applications/`:

```bash
xattr -dr com.apple.quarantine /Applications/WisprAlt.app
```

Then open and grant the 4 permissions as above. The server URL and API key are shared by the user — friends do not run their own server.

## Final smoke test

```bash
curl --max-time 5 -fsS -H "Authorization: Bearer $API_KEY" https://transcribe.integrateapi.ai/healthz
# expect: {"status":"ok"}
```

If `200` returned, dictation is ready. Tell the user:

> Hold FN to dictate. Release to inject text at the cursor. Triple-tap FN within 400ms to start/stop a meeting recording.

## Never

- Do not push to GitHub without explicit user approval.
- Do not store the API key anywhere outside the macOS Keychain (the app handles this automatically when the user enters it in Settings).
- Do not reference `scripts/setup-client.sh` — that file does not exist. This slash command IS the install specification.
