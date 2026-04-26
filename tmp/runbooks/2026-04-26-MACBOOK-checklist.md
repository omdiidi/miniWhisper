# MacBook side — your manual checklist

Estimated time: ~25 minutes total. Copy-paste each block. Stop and tell me if anything throws.

---

## 0. Pre-flight (1 min)

```bash
cd /Users/omidzahrai/Desktop/CODEBASES/TOOLS/wisprflowALT

# Confirm working tree state
git status --short | head -25

# Confirm Apple Development cert exists. If empty, do step 0a before continuing.
security find-identity -v -p codesigning | grep 'Apple Development'
```

### 0a. (only if cert is missing) — Sign into Xcode

1. Open Xcode (App Store → Xcode if not installed; ~10 GB).
2. Xcode → Settings (`⌘,`) → Accounts.
3. Click `+` → Apple ID → sign in with your existing Apple ID (`zomid777@gmail.com` is fine, or any).
4. Wait for "Manage Certificates" to populate. If "Apple Development" doesn't appear, click `Manage Certificates...` → `+` → "Apple Development".
5. Re-run `security find-identity -v -p codesigning | grep 'Apple Development'`. Should now print one line.

---

## 1. Reset stale TCC (1 min)

```bash
tccutil reset Accessibility   co.wispralt.WisprAlt
tccutil reset ListenEvent     co.wispralt.WisprAlt
tccutil reset ScreenCapture   co.wispralt.WisprAlt
tccutil reset Microphone      co.wispralt.WisprAlt
```

---

## 2. Rebuild + reinstall (3 min)

```bash
cd /Users/omidzahrai/Desktop/CODEBASES/TOOLS/wisprflowALT

# Build with Apple Development cert (will fail clearly if cert is missing or ambiguous)
bash scripts/build-client-local.sh

# Verify the binary is signed with Apple Development (NOT ad-hoc)
codesign -dv client/build/WisprAlt.app 2>&1 | grep 'Authority='

# Replace the running app
pkill -9 -f /Applications/WisprAlt.app/Contents/MacOS/WisprAlt 2>/dev/null; sleep 1
rm -rf /Applications/WisprAlt.app
cp -R client/build/WisprAlt.app /Applications/

# Launch
open -a /Applications/WisprAlt.app
```

**Stop here.** Tell me what `codesign -dv` printed — I want to see the Authority chain to confirm Apple Development is actually wired.

---

## 3. Grant 4 permissions (5 min — last time, hopefully)

When the app is running, hit each toggle as the system prompts you, OR open these directly:

```bash
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
open "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
```

In each pane: find WisprAlt, toggle ON. After Input Monitoring, macOS may ask to quit-and-reopen — say yes.

---

## 4. Verify SMAppService login-launch (1 min)

```bash
# 1. CLI check — should NOT include co.wispralt.WisprAlt in the disabled list
launchctl print-disabled "gui/$UID" | grep -i co.wispralt || echo "✓ not disabled"

# 2. Visual check — open Login Items pane
open "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"
```

Visual: confirm "WisprAlt" is in the **Allow in the Background** list with toggle ON.

In WisprAlt menubar → Settings → "Launch at login" should also show ON.

---

## 5. Manual functional tests (8 min)

For each test, watch for the expected toast (top-right corner) and observe the menubar icon state.

**5a. Dictation roundtrip (baseline)**
- Open any text app (Notes, Messages, etc.).
- Hold FN, say "Hello world this is a test", release.
- Expected: text appears in the focused app within ~500ms.

**5b. Mic-switch during dictation**
- Hold FN, start speaking.
- While holding, switch input device: System Settings → Sound → Input → pick a different mic (or yank/insert AirPods).
- Expected: "Dictation Cancelled" toast. App returns to idle. No partial text injected.

**5c. Meeting recording roundtrip**
- Triple-tap FN within 400ms. Speak for ~10 seconds. Triple-tap again.
- Expected: "Meeting Saved" toast. File at `~/Documents/WisprAlt/Meetings/` with current timestamp.

**5d. Mic-switch during meeting** (the new feature)
- Triple-tap FN. Speak for ~5 seconds.
- Switch input device mid-recording (System Settings → Sound → Input).
- Expected: "Meeting Cancelled" toast. App returns to idle. **Verify NO partial WAV in `~/Documents/WisprAlt/Meetings/`:**
  ```bash
  ls -la ~/Documents/WisprAlt/Meetings/ | tail -5
  ```
  No new file should have appeared after the abort.

**5e. Login-launch persistence test**
- Quit WisprAlt (menubar → Quit, or `pkill WisprAlt`).
- Log out (Apple menu → Log Out) → log back in.
- Expected: WisprAlt menubar icon appears within ~3 seconds of login completing.

**5f. Toggle-off persists across launches** (the Codex bug we fixed)
- WisprAlt menubar → Settings → toggle "Launch at login" OFF.
- Quit WisprAlt → relaunch.
- Expected: toggle stays OFF (NOT silently re-enabled).
- Re-toggle to ON for daily use.

---

## 6. API key export round-trip test (3 min)

**6a. Export**
- WisprAlt menubar → Settings → "Export API Key…"
- Save to Desktop with default name `wispralt-api-key.wispralt-key`.

```bash
# Verify file exists at 0600
stat -f '%Mp%Lp %N' ~/Desktop/wispralt-api-key.wispralt-key

# Verify format
head -3 ~/Desktop/wispralt-api-key.wispralt-key
# Should show: # WisprAlt API key export
#              # Format: v1
#              wispralt_api_key=<your-key>
```

**6b. Import round-trip**
- Settings → "Import API Key…" → select the same file.
- Expected: no error. Click "Test Connection" → green check.

**6c. Robustness — corrupted file** (sanity check the new error path)
```bash
echo "garbage" > ~/Desktop/bad.wispralt-key
```
Settings → Import API Key → select `bad.wispralt-key`.
Expected: red caption "Import failed: Export file is not a valid WisprAlt key file." and your Keychain key remains intact.
```bash
rm ~/Desktop/bad.wispralt-key
```

---

## 7. End report

Reply to me with:
- Which steps PASSED (just say "1-7 all good" if so).
- Any step that FAILED, with the actual output / what you saw.
- Any TCC re-grant prompt that appeared during step 5 (shouldn't, but document if it does).

Once you confirm the MacBook side is solid, I'll drive the Mac mini side via CRD-over-DevTools — see `2026-04-26-MACMINI-test-plan.md`.
