---
description: Install and configure the WisprAlt macOS client app — download or build DMG, walk the user through 4 permissions, paste server config, test connection.
---

# /setup-client

Interactive client install on a Mac (MacBook Air or any device the user wants to dictate from).

## Pre-flight checks

1. `sw_vers -productVersion` — require ≥ 14.0 (`captureMicrophone` needs Sonoma+). If 13.x, abort with a clear message.
2. Check for `tmp/client-config.txt` (created by `/setup-server`). If present, read `SERVER_URL` and `API_KEY` from it and offer to use them automatically.

## Acquire the DMG

Try in this order:

1. **GitHub Release**: `gh release download --pattern '*.dmg' -O /tmp/WisprAlt.dmg` — requires `gh auth status` to be valid and a published release.
2. **Local build**: if no release available or `gh` not authed, prompt the user. If they want to build:
   - Confirm `DEVELOPER_ID_APP` env var is set (e.g. `"Developer ID Application: Your Name (TEAMID)"`).
   - Confirm `APPLE_ID`, `APP_SPECIFIC_PASSWORD`, `TEAM_ID` are set for notarization.
   - Run `bash scripts/build-client.sh "$DEVELOPER_ID_APP"`.
   - Use the generated `client/build/WisprAlt.dmg`.

## Install

Mount the DMG (`hdiutil attach /tmp/WisprAlt.dmg`) and copy `WisprAlt.app` to `/Applications/`. Detach.

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

## Final test

```bash
curl -fsS -H "Authorization: Bearer $API_KEY" "$SERVER_URL/healthz"
# expect: {"status":"ok"}
```

If `200` returned, dictation is ready. Tell the user:

> Hold FN to dictate. Release to inject text at the cursor. Triple-tap FN within 400ms to start/stop a meeting recording.

## Never

- Do not push to GitHub without explicit user approval.
- Do not store the API key anywhere outside the macOS Keychain (the app handles this automatically when the user enters it in Settings).
