# Plan: Safeguards, Auto-Start, and Multi-Device Hand-Off

## Goal
Make WisprAlt "always ready" on both the Mac mini server and the MacBook client. After any reboot or login, both halves should come back up automatically with no manual steps. Use SMAppService for native login-launch UX. Tighten the install flow for friend hand-off. Make the documentation honest about TCC permission persistence. Land the mic-switch UX fix that's already on disk and extend the same protection to meeting recording.

## Why
Today the system is reboot-fragile: server LaunchAgent has `RunAtLoad: false`, cloudflared is installed via the broken `sudo cloudflared service install` path that doesn't survive reboots on macOS 14/15, and the client has no login-item integration so the menubar app must be launched manually after every login. The docs imply TCC grants persist in ways they don't. The mic-switch fix from commit `1dd7a54` is committed but never installed. Meeting recording has no equivalent input-device-change protection.

## Codesigning decision: Option A (SMAppService) — locked

`SMAppService.mainApp.register()` requires a stable Designated Requirement, which only an Apple-issued code-signing identity provides (Apple Developer Forums thread 799910). We use a **free Apple Development certificate** issued via Xcode's Personal Team tier — no Apple Developer Program enrollment, no $99/yr fee.

**Setup prerequisite (one time per build machine):**
1. Open Xcode → Settings → Accounts → sign in with any Apple ID.
2. Xcode auto-creates a Personal Team and issues an `Apple Development` certificate to the login keychain. Auto-renews while signed in.
3. The build script picks up the identity from `security find-identity` automatically.

**What this gets us:**
- `SMAppService.mainApp.register()` works → System Settings → Login Items entry + in-app toggle.
- Apple-issued cert chain (Gatekeeper still warns first-time on a quarantined download because it isn't a paid Developer ID, but `xattr -dr com.apple.quarantine` removes the warning, and once approved each version opens cleanly thereafter).

**Out-of-scope this round:** Apple Developer Program enrollment ($99/yr), notarized DMG, App Store distribution, hardened-runtime entitlements that need a team identifier.

**Per-version friction (accepted by user):** Each new build the user (or a friend) installs requires (a) the Gatekeeper "Open Anyway" right-click flow on first open from a download, and (b) re-granting the 4 TCC permissions because the rebuild changes the cdhash. Documented honestly. After first open, all subsequent launches and login-launches are silent until the next version ships.

---

## Multi-device hand-off architecture

This plan assumes friends install the **client only** and connect to the user's existing Mac mini server. Friends do not run their own server, do not run their own cloudflared, and do not own the API key. The user issues each friend the same `WISPRALT_API_KEY` (or rotates and re-issues if compromised). The API key export/import flow in this plan is for moving a key between the user's own Macs and for handing the key to a friend without phone-snap-and-retype.

If at any point a friend wants their own server, they run `/setup-server` independently — that flow is unchanged by this plan.

---

## Architecture overview

Three reliability layers:

1. **Process auto-start.** Two LaunchAgents on the Mac mini (`co.wispralt.server`, `co.wispralt.cloudflared`), both with `RunAtLoad: true` + `KeepAlive`. SMAppService entry on each client Mac for the menubar app.
2. **Mid-session resilience.** Dictation already aborts on mic switch (commit `1dd7a54`). Meeting flow gets the same protection via `AudioObjectAddPropertyListener` on `kAudioHardwarePropertyDefaultInputDevice` (CoreAudio HAL) — SCStream emits no device-change callback and the meeting flow doesn't use AVAudioEngine.
3. **Operational hygiene.** Cloudflared token moves to a 0600 file at `~/.config/wispralt/cloudflare-token` (read by the LaunchAgent via `--token-file` on cloudflared ≥ 2025.4.0; otherwise embedded in a 0600 plist). API key export/import in client Settings. TCC honesty pass in docs.

### Data flow after this plan

```
Mac mini reboot
  ↓
launchd starts user session
  ↓
launchd (gui/<UID>) bootstraps:
  - co.wispralt.server.plist     RunAtLoad=true → uvicorn 127.0.0.1:8000
  - co.wispralt.cloudflared.plist RunAtLoad=true → cloudflared tunnel run --token-file ~/.config/wispralt/cloudflare-token
  ↓
https://transcribe.integrateapi.ai is reachable

MacBook login
  ↓
SMAppService entry (registered by AppDelegate at first run) launches /Applications/WisprAlt.app
  ↓
WisprAlt menubar icon appears, ready for FN-hold dictation

Mid-recording mic switch (e.g., AirPods → MacBook mic)
  ↓
DictationRecorder: AVAudioEngineConfigurationChange fires → posts .dictationConfigChanged → MenuBarController stops + toasts
MeetingRecorder: kAudioHardwarePropertyDefaultInputDevice listener fires → posts .meetingConfigChanged → MenuBarController stops, deletes partial WAV, toasts
```

---

## Files Being Changed

```
wisprflowALT/
├── CLAUDE.md                                            ← MODIFIED (slash command index gets verify-autostart; tunnel-token convention bullet rewritten)
├── client/
│   └── WisprAlt/
│       ├── App/
│       │   ├── AppDelegate.swift                        ← MODIFIED (call SMAppService.mainApp.register() at applicationDidFinishLaunching)
│       │   └── MenuBarController.swift                  ← MODIFIED (observe .meetingConfigChanged, dispatch handler)
│       ├── Capture/
│       │   ├── AudioDeviceListener.swift                ← NEW (CoreAudio HAL listener wrapper, file-scope C callback)
│       │   └── MeetingRecorder.swift                    ← MODIFIED (instantiate AudioDeviceListener; declare .meetingConfigChanged here to match dictation pattern)
│       ├── Storage/
│       │   ├── KeychainHelper.swift                     ← MODIFIED (export/import pair for API key)
│       │   └── Settings.swift                           ← MODIFIED (launchAtLogin computed wrapper around SMAppService.status)
│       └── UI/
│           └── SettingsView.swift                       ← MODIFIED (Launch-at-login toggle + Export/Import API Key buttons)
├── docs/
│   ├── ARCHITECTURE.md                                  ← MODIFIED (auto-start layer; AudioDeviceListener row; meeting config-change row)
│   ├── DEPLOYMENT-NOTES.md                              ← MODIFIED (TCC honesty pass; cloudflared LaunchAgent + rotation; client SMAppService; remove tmp/credentials.txt secrets-table row)
│   ├── OVERVIEW.md                                      ← MODIFIED (add AudioDeviceListener.swift, setup-local-codesign.sh, scripts/setup-cloudflared.sh updates)
│   ├── SETUP-CLIENT.md                                  ← MODIFIED (Apple Development cert prerequisite; login-item toggle; export/import; xattr quarantine note for friends)
│   ├── SETUP-SERVER.md                                  ← MODIFIED (RunAtLoad: true; cloudflared LaunchAgent path; rotation procedure)
│   └── TROUBLESHOOTING.md                               ← MODIFIED (TCC honesty; cloudflared not running; client not launching; expired/missing token; offline boot)
├── scripts/
│   ├── build-client-local.sh                            ← MODIFIED (require Apple Development identity, fail on missing/multi; remove ad-hoc fallback entirely; pass full quoted identity name)
│   ├── server-launchd.sh                                ← MODIFIED (RunAtLoad → true; add bootstrap-test case entry to the existing case "$CMD" block)
│   └── setup-cloudflared.sh                             ← MODIFIED (replace sudo install with user-level LaunchAgent; two heredocs; EnvironmentVariables/PATH; read -r -s rotation; trap on EXIT; KeepAlive dict form; check both bootout labels)
└── .claude/commands/
    ├── setup-client.md                                  ← MODIFIED (Apple ID + Xcode prerequisite; cp -R install logic; tccutil reset cycle; SMAppService verification; export/import callout; xattr quarantine note)
    ├── setup-server.md                                  ← MODIFIED (cloudflared LaunchAgent verification step; rotation reference)
    └── verify-autostart.md                              ← NEW (non-destructive reboot-survival test with retry loops)

NOTE: scripts/setup-client.sh does NOT exist in the repo. The /setup-client slash
command IS the script (it's an executable .md spec for Claude Code to follow).
All "setup-client.sh" install logic from prior plan revisions lives in the
.claude/commands/setup-client.md walkthrough.
```

---

## Key Pseudocode

### §1. Server `RunAtLoad: true`

`scripts/server-launchd.sh:111-112`:
```bash
# OLD
        <key>RunAtLoad</key>
        <false/>

# NEW
        <key>RunAtLoad</key>
        <true/>
```

After change, `bash scripts/server-launchd.sh install` regenerates the plist; new `bootstrap-test` subcommand exercises a `bootout` + `bootstrap` cycle then polls `/healthz` with retry. The current `case "$CMD"` block (server-launchd.sh:123) only handles `install|start|stop|status|uninstall|help`; we add a new case entry:

```bash
case "$CMD" in
    # ... existing cases ...
    bootstrap-test)
        # Simulate reboot-survival without an actual reboot.
        launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
        launchctl bootstrap "gui/$UID" "$PLIST"
        echo "Polling /healthz..."
        if curl --max-time 5 --retry 6 --retry-delay 2 \
                -fsS http://127.0.0.1:8000/healthz >/dev/null; then
            echo "✓ server reboot-survival OK"
        else
            echo "✗ server failed to come up — check $LOG_DIR/server.err.log" >&2
            exit 1
        fi
        ;;
    *) help; exit 1 ;;
esac
```

### §2. Cloudflared user-level LaunchAgent

`scripts/setup-cloudflared.sh` — replaces the entire `sudo cloudflared service install` path. Two key correctness fixes from review:
- **No XML-in-bash-variable hacks.** The `--token-file` vs `--token` branch produces two separate heredocs.
- **Pipe order in feature probe is load-bearing** (grep last → exit code from grep → safe with `set -euo pipefail`).
- **`EnvironmentVariables/PATH`** matches the pattern in `server-launchd.sh:82-87`.
- **Rotation uses `read -r -s`**, never `echo -n` (which leaks to shell history).

```bash
# Defense-in-depth: ensure token vars are scrubbed on ANY exit path,
# including unexpected `set -e` aborts between read and unset.
trap 'unset CF_TOKEN TOKEN_VALUE NEW_TOKEN' EXIT

# Read token via stdin (silent)
read -r -s -p "Cloudflare Tunnel token: " CF_TOKEN
echo

# Persist token to a 0600 file
TOKEN_DIR="$HOME/.config/wispralt"
TOKEN_FILE="$TOKEN_DIR/cloudflare-token"
mkdir -p "$TOKEN_DIR"
chmod 700 "$TOKEN_DIR"
install -m 0600 /dev/null "$TOKEN_FILE"
printf '%s' "$CF_TOKEN" > "$TOKEN_FILE"
unset CF_TOKEN

# Setup paths
LABEL="co.wispralt.cloudflared"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/WisprAlt"
mkdir -p "$LOG_DIR"

# Detect cloudflared binary path
CLOUDFLARED_BIN="$(command -v cloudflared)"
if [ -z "$CLOUDFLARED_BIN" ]; then
    echo "cloudflared not found — install with: brew install cloudflared" >&2
    exit 1
fi

# Build PATH for the LaunchAgent — minimal launchd default plus Homebrew
HOMEBREW_PREFIX="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
LAUNCHD_PATH="$HOMEBREW_PREFIX/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Feature-probe --token-file support.
# CRITICAL: grep must be the LAST command in the pipe. Under `set -euo pipefail`,
# the pipe's exit status is the last command's exit code — a non-zero `--help`
# produces no match, grep exits 1, the `if` correctly takes the false branch.
# DO NOT reorder this pipe.
SUPPORTS_TOKEN_FILE=false
if cloudflared tunnel run --help 2>&1 | grep -q -- '--token-file'; then
    SUPPORTS_TOKEN_FILE=true
fi

# Tear down any stale plist before regenerating
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true

if [ "$SUPPORTS_TOKEN_FILE" = "true" ]; then
    # Modern path: token stays in the 0600 file, plist references it
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>            <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$CLOUDFLARED_BIN</string>
        <string>tunnel</string>
        <string>--loglevel</string>
        <string>info</string>
        <string>run</string>
        <string>--token-file</string>
        <string>$TOKEN_FILE</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>         <string>$LAUNCHD_PATH</string>
    </dict>
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>   <false/>
        <key>NetworkState</key>     <true/>
    </dict>
    <key>ThrottleInterval</key> <integer>10</integer>
    <key>StandardOutPath</key>  <string>$LOG_DIR/cloudflared.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/cloudflared.err.log</string>
</dict>
</plist>
EOF
else
    # Legacy path: cloudflared < 2025.4.0 doesn't support --token-file.
    # Inline the token into ProgramArguments. Plist gets mode 0600 to limit exposure.
    TOKEN_VALUE="$(cat "$TOKEN_FILE")"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>            <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$CLOUDFLARED_BIN</string>
        <string>tunnel</string>
        <string>--loglevel</string>
        <string>info</string>
        <string>run</string>
        <string>--token</string>
        <string>$TOKEN_VALUE</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>         <string>$LAUNCHD_PATH</string>
    </dict>
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>   <false/>
        <key>NetworkState</key>     <true/>
    </dict>
    <key>ThrottleInterval</key> <integer>10</integer>
    <key>StandardOutPath</key>  <string>$LOG_DIR/cloudflared.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/cloudflared.err.log</string>
</dict>
</plist>
EOF
    unset TOKEN_VALUE
fi
chmod 0600 "$PLIST"

# Bootstrap (modern macOS 15+)
launchctl bootstrap "gui/$UID" "$PLIST"

# Verify with retry (cloudflared takes a beat to dial out)
for i in $(seq 1 10); do
    if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
        echo "✓ cloudflared LaunchAgent loaded"
        exit 0
    fi
    sleep 1
done
echo "✗ cloudflared LaunchAgent failed to load — check $LOG_DIR/cloudflared.err.log" >&2
exit 1
```

**Cleanup of the broken `sudo cloudflared service install` daemon (runs once on first migration):**
```bash
# The label may be either form depending on which cloudflared version installed it
sudo launchctl bootout system/com.cloudflare.cloudflared 2>/dev/null || true
sudo launchctl bootout system/cloudflared              2>/dev/null || true
sudo rm -f /Library/LaunchDaemons/com.cloudflare.cloudflared.plist
sudo rm -f /Library/LaunchDaemons/cloudflared.plist
```

**Token rotation (canonical, replaces the existing `kickstart -k` doc snippet):**
```bash
# 1. Read new token silently into a variable
read -r -s -p "New Cloudflare Tunnel token: " NEW_TOKEN
echo

# 2. Atomically replace the token file
TMP="$(mktemp)"
chmod 0600 "$TMP"
printf '%s' "$NEW_TOKEN" > "$TMP"
mv "$TMP" ~/.config/wispralt/cloudflare-token
unset NEW_TOKEN

# 3. If cloudflared supports --token-file: bootout/bootstrap restarts the process
#    which re-reads the token file. No plist regeneration needed.
launchctl bootout gui/$UID/co.wispralt.cloudflared
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/co.wispralt.cloudflared.plist

# 4. If cloudflared does NOT support --token-file (legacy path):
#    The token is baked into the plist. Re-run setup-cloudflared.sh entirely
#    so the plist is regenerated. bootout/bootstrap of the same plist won't pick
#    up the new token because the plist still has the old one inlined.
```

`docs/DEPLOYMENT-NOTES.md` rotation section gets both branches with a one-liner check: `if cloudflared tunnel run --help 2>&1 | grep -q -- '--token-file'; then`.

### §3. SMAppService client login-launch

`client/WisprAlt/App/AppDelegate.swift` (or equivalent in `WisprAltApp.swift`'s `@NSApplicationDelegateAdaptor`):
```swift
import ServiceManagement

func applicationDidFinishLaunching(_ notification: Notification) {
    // ... existing setup ...

    let service = SMAppService.mainApp
    do {
        switch service.status {
        case .notRegistered, .notFound:
            try service.register()
            Log.info("SMAppService: registered for launch at login.", category: "lifecycle")
        case .enabled:
            break  // already wired
        case .requiresApproval:
            Log.warning("SMAppService: requires approval in System Settings → Login Items.", category: "lifecycle")
        @unknown default:
            break
        }
    } catch {
        Log.warning("SMAppService.register() failed: \(error). Verify the app is signed with Apple Development identity.", category: "lifecycle")
    }
}
```

`client/WisprAlt/Storage/Settings.swift` — computed wrapper. The extension is `@MainActor`-isolated so `objectWillChange.send()` always fires on the main thread, even if some future caller mutates `launchAtLogin` from a background `Task`:
```swift
import ServiceManagement

@MainActor
extension Settings {
    var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            do {
                if newValue {
                    try SMAppService.mainApp.register()
                } else {
                    try SMAppService.mainApp.unregister()
                }
                objectWillChange.send()
            } catch {
                Log.warning("Launch-at-login toggle failed: \(error)", category: "lifecycle")
            }
        }
    }
}
```

`client/WisprAlt/UI/SettingsView.swift`:
```swift
@EnvironmentObject var settings: Settings

// In body:
Toggle("Launch at login", isOn: Binding(
    get:  { settings.launchAtLogin },
    set:  { settings.launchAtLogin = $0 }
))
if SMAppService.mainApp.status == .requiresApproval {
    Button("Open Login Items in System Settings") {
        SMAppService.openSystemSettingsLoginItems()
    }
}
```

`scripts/build-client-local.sh` — replace the existing identity selection (current lines 117–125, which falls back to ad-hoc `"-"`) with a hard-fail Apple Development requirement. The ad-hoc fallback **must be removed entirely** — silent ad-hoc signing causes `SMAppService.mainApp.register()` to silently fail at runtime:
```bash
# Find ALL Apple Development identities. security find-identity output looks like:
#   1) <SHA1HASH> "Apple Development: user@example.com (TEAMID)"
APPLE_DEV_IDENTITIES="$(security find-identity -v -p codesigning \
    | sed -n 's/.*"\(Apple Development:[^"]*\)".*/\1/p')"

NUM_IDENTITIES="$(printf '%s\n' "$APPLE_DEV_IDENTITIES" | grep -c 'Apple Development:' || true)"

if [ "$NUM_IDENTITIES" -eq 0 ]; then
    cat >&2 <<'EOM'
ERROR: WisprAlt requires an Apple Development code-signing identity.

Setup (one time, no Apple Developer Program enrollment needed):
  1. Open Xcode.
  2. Settings → Accounts → "+" → sign in with any Apple ID.
  3. Xcode auto-creates a Personal Team and issues an Apple Development cert.
  4. Re-run this script.

Why required: SMAppService.mainApp.register() (login-at-startup) refuses
ad-hoc / self-signed identities. Apple Developer Forums thread 799910.
EOM
    exit 1
fi

if [ "$NUM_IDENTITIES" -gt 1 ] && [ -z "${SIGN_IDENTITY:-}" ]; then
    cat >&2 <<EOM
ERROR: Multiple Apple Development identities found. Set SIGN_IDENTITY explicitly:

$APPLE_DEV_IDENTITIES

Example:
  SIGN_IDENTITY="Apple Development: you@example.com (TEAMID)" bash $0
EOM
    exit 1
fi

# Single identity OR explicit override
SIGN_IDENTITY="${SIGN_IDENTITY:-$(printf '%s' "$APPLE_DEV_IDENTITIES" | head -1)}"
echo "Step 3/4: Codesigning with '$SIGN_IDENTITY'..."

# DELETE the existing block (current lines 117–125 of build-client-local.sh):
#   LOCAL_IDENTITY="WisprAlt Local Dev"
#   if security find-identity -v -p codesigning 2>/dev/null | grep -q "$LOCAL_IDENTITY"; then
#       SIGN_IDENTITY="$LOCAL_IDENTITY"
#       ...
#   else
#       SIGN_IDENTITY="-"   # ← THIS ad-hoc fallback must be removed
#       ...
```

The legacy `scripts/setup-local-codesign.sh` (self-signed cert in System trust) is no longer wired into the build flow. It stays in the repo only for any future `--ad-hoc` developer-mode fallback, documented in `docs/CONTRIBUTING.md`.

**Annual cert renewal warning.** Free Apple Development certs auto-renew yearly while signed into Xcode. The renewed cert has a new SHA-1 → new Designated Requirement → TCC re-grant cycle, identical to a rebuild. Documented in `docs/DEPLOYMENT-NOTES.md`.

### §4. MeetingRecorder mic-switch parity (CoreAudio HAL)

**Notification location.** To match the existing pattern (`dictationConfigChanged` lives in `Capture/DictationRecorder.swift:5-12`, NOT in `Util/Notifications.swift`), the new `meetingConfigChanged` is declared in `client/WisprAlt/Capture/MeetingRecorder.swift` near the top of the file:
```swift
extension Notification.Name {
    static let meetingConfigChanged = Notification.Name("co.wispralt.meetingConfigChanged")
}
```

`client/WisprAlt/Capture/AudioDeviceListener.swift` — NEW. Critical Swift constraints from review:
- `AudioObjectPropertyListenerProc` is a C function pointer and **cannot be stored on a Swift class instance**. Must be declared at file scope as a `private let` constant and referenced by name in both `Add` and `Remove`.
- The property address struct is immutable after init; declare as `let`, not `var`.
- The context pointer holds the closure. Use a `class` (reference-typed) context wrapper rather than a struct so the closure capture lifetime is unambiguous and the value semantics aren't accidentally copied across threads.
- The comment about `AudioObjectAddPropertyListenerBlock` is corrected: the Block API works in Swift, but explicit C-pointer + lifetime-managed context gives us deterministic teardown in `deinit`. That is the actual reason we use it.

```swift
import CoreAudio
import Foundation

private final class AudioDeviceListenerContext {
    let onChange: () -> Void
    init(onChange: @escaping () -> Void) { self.onChange = onChange }
}

// File-scope C function pointer — stable, referenced by both Add and Remove.
private let audioDeviceListenerCallback: AudioObjectPropertyListenerProc = { _, _, _, clientData in
    guard let clientData else { return noErr }
    let ctx = Unmanaged<AudioDeviceListenerContext>.fromOpaque(clientData).takeUnretainedValue()
    DispatchQueue.main.async { ctx.onChange() }
    return noErr
}

final class AudioDeviceListener {
    private let address: AudioObjectPropertyAddress = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultInputDevice,
        mScope:    kAudioObjectPropertyScopeGlobal,
        mElement:  kAudioObjectPropertyElementMain
    )
    // Optional so deinit can detect a failed init that already rolled back the retain.
    // After successful init, both context and contextPtr are non-nil. After init throws,
    // contextPtr is set back to nil before the throw, so deinit knows to skip release.
    private var context: AudioDeviceListenerContext?
    private var contextPtr: UnsafeMutableRawPointer?
    private var registered: Bool = false

    enum Error: Swift.Error { case cannotAdd(OSStatus) }

    init(onChange: @escaping () -> Void) throws {
        let ctx = AudioDeviceListenerContext(onChange: onChange)
        // Retain the context so the C callback holds a stable raw pointer to it.
        let ptr = Unmanaged.passRetained(ctx).toOpaque()
        self.context = ctx
        self.contextPtr = ptr

        var addr = address  // local mutable copy for inout argument
        let status = AudioObjectAddPropertyListener(
            AudioObjectID(kAudioObjectSystemObject),
            &addr,
            audioDeviceListenerCallback,
            ptr
        )
        guard status == noErr else {
            // Roll back the retain on failure, then null out so deinit doesn't double-release.
            Unmanaged<AudioDeviceListenerContext>.fromOpaque(ptr).release()
            self.contextPtr = nil
            self.context = nil
            throw Error.cannotAdd(status)
        }
        registered = true
    }

    deinit {
        guard let ptr = contextPtr else { return }  // init failed; rollback already done
        if registered {
            var addr = address
            AudioObjectRemovePropertyListener(
                AudioObjectID(kAudioObjectSystemObject),
                &addr,
                audioDeviceListenerCallback,
                ptr
            )
        }
        // Balance the retain exactly once on the success path.
        Unmanaged<AudioDeviceListenerContext>.fromOpaque(ptr).release()
    }
}
```

`client/WisprAlt/Capture/MeetingRecorder.swift` — listener setup BEFORE `stream.startCapture()` so a partial init failure can't leave an orphan SCStream. Listener is torn down in `stop()` and `deinit`. **Also expose a `lastOutputURL` accessor** so the abort handler can clean up a partial WAV even when SCStream's `didStopWithError` beat the device-change handler and `stop()` would throw `notRunning`.

```swift
private var deviceListener: AudioDeviceListener?

// Snapshot of the most recently configured output URL. Survives stop() and
// SCStream auto-teardown so handlers can delete a partial WAV even if isActive
// has already flipped to false. Cleared only at the start of the next start().
private(set) var lastOutputURL: URL?

func start(...) async throws {
    lastOutputURL = nil
    // ... existing setup of stream, configuration, output handlers ...
    lastOutputURL = outputURL  // capture as soon as we know the path

    // Install device-change listener BEFORE starting capture so partial init
    // failure can't leave an orphan SCStream.
    deviceListener = try AudioDeviceListener { [weak self] in
        guard let self, self.isActive else { return }
        Log.info("Meeting: input device changed, aborting", category: "capture")
        NotificationCenter.default.post(name: .meetingConfigChanged, object: nil)
    }

    try await stream?.startCapture()
}

func stop() async throws -> URL {
    deviceListener = nil  // tear down first
    // ... existing teardown returning the WAV URL ...
}

deinit { deviceListener = nil }
```

`client/WisprAlt/App/MenuBarController.swift` — observer + handler. Mirrors the existing dictation pattern exactly and includes the mode guard, the `meetingActive = false` reset, and **deletion of the partial WAV** so aborted meetings don't leave junk in `~/Documents/WisprAlt/Meetings/`:
```swift
// In configureMeetingCapObservers (or equivalent setup):
NotificationCenter.default.addObserver(
    self,
    selector: #selector(handleMeetingConfigChanged),
    name: .meetingConfigChanged,
    object: nil)

@objc private func handleMeetingConfigChanged() {
    Task { @MainActor in
        // Mirror handleDictationConfigChanged (MenuBarController.swift:113-132):
        // mode guard prevents stomping on an in-flight upload.
        guard self.mode == .meetingRecording else { return }

        // Snapshot the URL BEFORE calling stop() — if SCStream's didStopWithError
        // already flipped isActive to false, stop() will throw notRunning, but the
        // partial WAV is still on disk and must be cleaned up.
        let partialURL = MeetingRecorder.shared.lastOutputURL

        do {
            _ = try await MeetingRecorder.shared.stop()
        } catch {
            Log.warning("Meeting config-change abort: stop() threw \(error)", category: "capture")
        }

        // Delete the partial WAV — an interrupted meeting is not a valid recording.
        if let url = partialURL {
            try? FileManager.default.removeItem(at: url)
        }

        self.meetingActive = false
        self.mode = .idle

        AppNotifications.notify(
            title: "Meeting Cancelled",
            body: "Audio input device changed mid-recording. Triple-tap FN to start a new meeting.")
    }
}
```

### §5. API key export/import

`client/WisprAlt/Storage/KeychainHelper.swift` — also add a new `KeychainError.invalidExportFormat` case so import-parser failures surface a distinct error from "Keychain is empty":
```swift
// In the KeychainError enum (client/WisprAlt/Storage/KeychainHelper.swift:5-20), add a new case:
//
//   case invalidExportFormat
//
// AND add a matching branch to the existing exhaustive errorDescription switch so it stays exhaustive
// (the existing switch has no @unknown default — adding the case without a branch is a compile error):
//
//   case .invalidExportFormat:
//       return "Export file is not a valid WisprAlt key file."

private static let exportFileHeader =
    "# WisprAlt API key export\n# Format: v1\n"

static func exportAPIKey(to url: URL) throws {
    guard let key = try getAPIKey() else { throw KeychainError.itemNotFound }
    let payload = "\(exportFileHeader)wispralt_api_key=\(key)\n"
    try payload.write(to: url, atomically: true, encoding: .utf8)
    do {
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: url.path)
    } catch {
        Log.warning("Could not set 0600 on exported key file: \(error). File may be world-readable.", category: "storage")
    }
}

static func importAPIKey(from url: URL) throws {
    let raw = try String(contentsOf: url, encoding: .utf8)

    // Find the wispralt_api_key= line. Skip blank lines and comment lines (header lines start with #).
    let line = raw.split(separator: "\n", omittingEmptySubsequences: true)
        .map(String.init)
        .first { $0.hasPrefix("wispralt_api_key=") }

    guard let line, let eqIdx = line.firstIndex(of: "=") else {
        throw KeychainError.invalidExportFormat
    }
    let key = String(line[line.index(after: eqIdx)...])
    guard !key.isEmpty else { throw KeychainError.invalidExportFormat }

    try setAPIKey(key)
}
```

`client/WisprAlt/UI/SettingsView.swift` — buttons. Default save location is `~/Desktop/`, **not** `~/Documents/`, because Documents is iCloud-Drive-synced on most Macs and would silently upload the API key to Apple. File extension `.wispralt-key`. Confirm overwrite on import.

```swift
@State private var exportImportError: String?

Button("Export API Key…") {
    let panel = NSSavePanel()
    panel.allowedContentTypes = [.text]
    panel.nameFieldStringValue = "wispralt-api-key.wispralt-key"
    panel.directoryURL = FileManager.default.urls(for: .desktopDirectory, in: .userDomainMask).first
    panel.message = "Exports your API key. Treat this file like a password."
    guard panel.runModal() == .OK, let url = panel.url else { return }
    do {
        try KeychainHelper.exportAPIKey(to: url)
    } catch {
        Log.error("API key export failed: \(error)", category: "storage")
        exportImportError = "Export failed: \(error.localizedDescription)"
    }
}

Button("Import API Key…") {
    let panel = NSOpenPanel()
    panel.allowedContentTypes = [.text]
    panel.directoryURL = FileManager.default.urls(for: .desktopDirectory, in: .userDomainMask).first
    panel.message = "Importing replaces any existing API key in the Keychain."
    guard panel.runModal() == .OK, let url = panel.url else { return }
    do {
        try KeychainHelper.importAPIKey(from: url)
        exportImportError = nil
    } catch {
        Log.error("API key import failed: \(error)", category: "storage")
        exportImportError = "Import failed: \(error.localizedDescription)"
    }
}

if let msg = exportImportError {
    Text(msg).foregroundColor(.red).font(.caption)
}
```

`docs/SETUP-CLIENT.md` — adds a "Backing up your API key" section that warns: **never save the export file to `~/Documents/`, iCloud Drive, Dropbox, or Google Drive**; treat it like a password.

### §6. Documentation honesty pass

`CLAUDE.md` "Key conventions" — rewrite the tunnel-token bullet:
```
- Cloudflared tunnel token: stored in `~/.config/wispralt/cloudflare-token` (mode 0600)
  outside the repo. Read by the cloudflared LaunchAgent via `--token-file`. Never
  committed, never logged. The legacy `sudo cloudflared service install` flow is
  abandoned because its plist is broken on macOS 14/15. Rotation: see
  docs/DEPLOYMENT-NOTES.md.
```

`docs/DEPLOYMENT-NOTES.md`:
- Sharpen the existing TCC section (lines 192–200): keep the cdhash + Developer ID explanation. Add a new sub-section **"Re-grant on rebuild looks like a bug but isn't"** with the canonical fix:
  ```bash
  tccutil reset Accessibility   co.wispralt.WisprAlt
  tccutil reset ListenEvent     co.wispralt.WisprAlt
  tccutil reset ScreenCapture   co.wispralt.WisprAlt
  tccutil reset Microphone      co.wispralt.WisprAlt
  ```
- **Remove** the `~/wispralt/tmp/credentials.txt` row from the secrets table (line 123 of the existing file).
- Add a **"Cloudflared LaunchAgent (user-level)"** section: install, log paths, rotation (both `--token-file` and legacy paths).
- Add a **"Client login-launch via SMAppService"** section: how it works, how to disable, what happens on rebuild.
- Add a brief **"Quarantine on first download"** section explaining `xattr -dr com.apple.quarantine ~/Downloads/WisprAlt.app` for friends installing without `/setup-client`.
- Add an **"Annual cert renewal"** section: free Apple Development certs expire after 1 year. Xcode auto-renews silently while the user stays signed in with their Apple ID, but the renewed cert has a different SHA-1 → different Designated Requirement → TCC prompts return on the next rebuild. Same `tccutil reset` recovery as a normal rebuild. Not a bug; expected once a year.

`docs/TROUBLESHOOTING.md` — new entries:
- **"Cloudflared not running after Mac mini reboot"** — `launchctl print gui/$UID/co.wispralt.cloudflared`. Check `state = running`. If absent, re-run `setup-cloudflared.sh`. If `KeepAlive` is hot-looping, first check `~/Library/Logs/WisprAlt/cloudflared.err.log` for auth errors before assuming a process or network fault.
- **"Mac mini rebooted with no internet"** — cloudflared retries every 10s indefinitely under `KeepAlive: true` + `ThrottleInterval: 10`. Self-heals automatically when network returns; no manual intervention required.
- **"Token file missing or corrupted"** — symptom: `KeepAlive` loop with auth errors. Fix: `ls -la ~/.config/wispralt/cloudflare-token` to confirm; recreate via the rotation procedure in DEPLOYMENT-NOTES.
- **"Client menubar app didn't appear after login"** — System Settings → General → Login Items & Extensions → confirm WisprAlt is enabled. If `.requiresApproval`, toggle it on. If `.notFound`, run `sfltool resetbtm` and reboot (Apple-recommended for stale Launch Services records).
- **"Friend's Mac shows 'WisprAlt cannot be opened because the developer cannot be verified'"** — first-time-from-download Gatekeeper warning. Fix: right-click → Open → Open Anyway, OR `xattr -dr com.apple.quarantine /path/to/WisprAlt.app && open /path/to/WisprAlt.app`.
- **"TCC prompts returned out of nowhere — I didn't rebuild"** — likely the annual Apple Development cert renewal. `security find-certificate -c "Apple Development:" -p login.keychain` should show a Not Before date within the last year. Same fix as rebuild: `tccutil reset {Accessibility,ListenEvent,ScreenCapture,Microphone} co.wispralt.WisprAlt` and re-grant.

---

## Tasks (in implementation order, resequenced to minimize TCC re-grants)

### Phase 0 — Pre-flight (no code changes; ~5 min)
1. Confirm working tree clean: `git status --short` should be empty.
2. Confirm Apple Development identity is available: `security find-identity -v -p codesigning | grep -q 'Apple Development'`. If absent, sign into Xcode → Settings → Accounts FIRST.

### Phase 1 — All Swift edits (no rebuild yet; ~90 min)
**Resequenced from original plan to combine with Phase 5 — one rebuild, one TCC re-grant cycle.**
3. `client/WisprAlt/App/AppDelegate.swift`: add `import ServiceManagement` and the `SMAppService.mainApp.register()` block in `applicationDidFinishLaunching` per **§3**. Verified file exists at `client/WisprAlt/App/AppDelegate.swift` and is the active app entry point alongside the SwiftUI App struct in `WisprAltApp.swift`.
4. `client/WisprAlt/Storage/Settings.swift`: add the `launchAtLogin` computed property in an extension per **§3**.
5. `client/WisprAlt/UI/SettingsView.swift`: add the Launch-at-login toggle and the requires-approval branch per **§3**.
6. `client/WisprAlt/Capture/MeetingRecorder.swift`: declare `Notification.Name.meetingConfigChanged` near the top (matching DictationRecorder pattern), instantiate `AudioDeviceListener` BEFORE `stream.startCapture()` per **§4**, tear down in `stop()` and `deinit`.
7. `client/WisprAlt/Capture/AudioDeviceListener.swift`: create the new file per **§4** — file-scope C callback, `let` address, retained context via `Unmanaged.passRetained`.
8. `client/WisprAlt/App/MenuBarController.swift`: add observer + `handleMeetingConfigChanged` handler per **§4**, including mode guard, partial-WAV deletion, `meetingActive = false`, mode reset.
9. `client/WisprAlt/Storage/KeychainHelper.swift`: add `exportAPIKey(to:)` and `importAPIKey(from:)` per **§5** with the version-stamped header and `firstIndex(of: "=")` parser.
10. `client/WisprAlt/UI/SettingsView.swift`: add Export and Import buttons per **§5**, defaulting to `~/Desktop/` (NOT `~/Documents/`).
11. `swift build --package-path client` from the project root — confirm zero warnings/errors before rebuilding the app bundle.

### Phase 2 — Build script identity gate + single rebuild + reinstall (~20 min)
12. `scripts/build-client-local.sh`: add the Apple Development identity check per **§3**. Use `sed` to extract the full quoted identity name (NOT awk on the SHA1).
13. `bash scripts/build-client-local.sh`. Verify rpath: `otool -l client/build/WisprAlt.app/Contents/MacOS/WisprAlt | grep -A2 LC_RPATH`. Verify signing: `codesign -dv client/build/WisprAlt.app 2>&1 | grep 'Apple Development'`.
14. Replace `/Applications/WisprAlt.app`:
    ```bash
    pkill -9 -f /Applications/WisprAlt.app/Contents/MacOS/WisprAlt; sleep 1
    rm -rf /Applications/WisprAlt.app
    cp -R client/build/WisprAlt.app /Applications/
    ```
15. `tccutil reset` cycle for all four permissions, `open -a /Applications/WisprAlt.app`, re-grant 4 permissions. **One re-grant cycle covers all Phase 1 changes.**
16. Manual verification of all Swift changes in this single grant cycle:
    - Hold FN, talk, switch input source mid-recording → "Dictation Cancelled" toast.
    - Triple-tap FN, talk, switch input source mid-meeting → "Meeting Cancelled" toast, partial WAV NOT in `~/Documents/WisprAlt/Meetings/`.
    - System Settings → General → Login Items & Extensions → "WisprAlt" entry visible, toggle on.
    - In-app Settings → Launch at login toggle reflects state.
    - Settings → Export API Key → save to Desktop → confirm `wispralt-api-key.wispralt-key` exists at 0600 with the version header.
    - Settings → Import API Key → re-import the same file → confirm Keychain still has the key (no error, no overwrite damage).

### Phase 3 — Server reboot survival (~15 min)
17. `scripts/server-launchd.sh:111-112`: change `<false/>` → `<true/>` per **§1**.
18. Add a `bootstrap-test` subcommand to the same script: `launchctl bootout` → `launchctl bootstrap` → `curl --max-time 5 --retry 6 --retry-delay 2 http://127.0.0.1:8000/healthz`.
19. On Mac mini: `cd ~/wispralt && bash scripts/server-launchd.sh install` (regenerates plist with `RunAtLoad: true`). Verify: `bash scripts/server-launchd.sh bootstrap-test`.

### Phase 4 — Cloudflared user-level LaunchAgent (~45 min)
20. `scripts/setup-cloudflared.sh`: replace the `sudo cloudflared service install` path with the implementation in **§2** — two heredocs branching on `--token-file` support, `EnvironmentVariables/PATH`, retry-loop verification.
21. On Mac mini: tear down any pre-existing system LaunchDaemon under both possible labels:
    ```bash
    sudo launchctl bootout system/com.cloudflare.cloudflared 2>/dev/null || true
    sudo launchctl bootout system/cloudflared              2>/dev/null || true
    sudo rm -f /Library/LaunchDaemons/com.cloudflare.cloudflared.plist
    sudo rm -f /Library/LaunchDaemons/cloudflared.plist
    ```
22. Run the new flow on the Mac mini: `bash scripts/setup-cloudflared.sh`. Paste token at the silent prompt. Verify `https://transcribe.integrateapi.ai/healthz` returns 200 from the MacBook within 30s.

### Phase 5 — `/verify-autostart` slash command (~30 min)
23. Create `.claude/commands/verify-autostart.md` describing the non-destructive reboot-survival test:
    - **Mac mini side:** `bash scripts/server-launchd.sh bootstrap-test`. Then `launchctl bootout gui/$UID/co.wispralt.cloudflared && launchctl bootstrap gui/$UID ~/Library/LaunchAgents/co.wispralt.cloudflared.plist` then poll the public healthz with retry: `for i in $(seq 1 15); do curl --max-time 3 -s https://transcribe.integrateapi.ai/healthz | grep -q ok && echo OK && exit 0; sleep 2; done; echo FAIL; exit 1`.
    - **Client side:** Validate `SMAppService.mainApp.status == .enabled` via a tiny Swift snippet (most reliable). Cross-check with `launchctl print-disabled gui/$UID | grep co.wispralt.WisprAlt` (returns the disable list — entry should NOT be in it if enabled). `sfltool dumpbtm | grep co.wispralt.WisprAlt` is a useful but unstable third check (private tool, output format unstable). Also verify the menubar process is running via `pgrep -lf '/Applications/WisprAlt.app/Contents/MacOS/WisprAlt'` with a 10-iteration retry loop (1s sleep between attempts) — handles the asynchronous launch.
24. Add `/verify-autostart` row to `CLAUDE.md` slash-command index.

### Phase 6 — Setup-client polish (~45 min)
**Note: there is NO `scripts/setup-client.sh` in this repo.** `/setup-client` is the slash command (`.claude/commands/setup-client.md`) that Claude Code follows. All install logic lives in that markdown spec.
25. Update `.claude/commands/setup-client.md`:
    - Add Apple-Development identity precondition at the top (instructs the user to sign into Xcode with any Apple ID first).
    - For DMG path: existing flow plus `xattr -dr com.apple.quarantine /Applications/WisprAlt.app` after copy to suppress Gatekeeper on first open.
    - For local-build path: explicit `cp -R client/build/WisprAlt.app /Applications/` with existence checks (no AppleScript). The AppleScript drag-to-Applications variant is **dropped** — fragile on macOS 15+ due to `kTCCServiceAppleEvents` requirements.
    - Login-launch verification: a tiny Swift one-liner the user can run to read `SMAppService.mainApp.status` directly (most reliable check). Fallback for visual confirmation: System Settings → General → Login Items & Extensions → confirm WisprAlt is in the list. `sfltool dumpbtm | grep co.wispralt` is a useful but unstable cross-check (private tool, output format changed between Ventura/Sonoma) — document with that caveat.
    - `xattr -dr` quarantine note for friends installing the DMG by themselves without the slash command.
    - Final smoke test: `curl --max-time 5 https://transcribe.integrateapi.ai/healthz`.
26. Update `docs/SETUP-CLIENT.md` to match: Apple Development cert prerequisite, login-item toggle docs, export/import docs, quarantine note for download installs, "your friends will see this same flow" hand-off subsection. (Phase 7 task 32 references this file — covered here in task 26, NOT a separate task.)

### Phase 7 — Documentation honesty pass (~75 min)
28. `CLAUDE.md`: rewrite the tunnel-token convention bullet per **§6**.
29. `docs/DEPLOYMENT-NOTES.md`:
    - Sharpen TCC section + add "Re-grant on rebuild looks like a bug but isn't" + canonical `tccutil reset` snippet.
    - **Delete** the `~/wispralt/tmp/credentials.txt` row from the secrets table (line 123).
    - Add "Cloudflared LaunchAgent (user-level)" section + rotation (both branches).
    - Add "Client login-launch via SMAppService" section.
    - Add "Quarantine on first download" section.
    - Update the existing `kickstart -k` rotation reference (old line ~94) to point to the new bootout/bootstrap procedure.
30. `docs/TROUBLESHOOTING.md`: add the five new entries listed in **§6**.
31. `docs/SETUP-SERVER.md`: update for `RunAtLoad: true` behavior + new cloudflared LaunchAgent flow.
32. `docs/SETUP-CLIENT.md`: covered in task 26 above (no-op here).
33. `docs/ARCHITECTURE.md`:
    - New row in file→responsibility table for `Capture/AudioDeviceListener.swift`.
    - Update MeetingRecorder row to mention `.meetingConfigChanged` and partial-WAV cleanup.
    - New "Process auto-start" subsection showing the launchd hierarchy diagram.
34. `docs/OVERVIEW.md`:
    - Add explicit rows for `client/WisprAlt/Capture/AudioDeviceListener.swift` (→ ARCHITECTURE.md) and `.claude/commands/verify-autostart.md` (→ CLAUDE.md).
    - **Update the existing `scripts/build-client-local.sh` row (currently line 96).** The current description says "ad-hoc-signed `.app` for personal use; no Apple Developer ID required" — this is now stale. Replace with: "Apple-Development-signed `.app` for personal use; requires free Apple Development cert from Xcode (no Apple Developer Program enrollment); fails clearly if cert is missing or multiple ambiguous identities exist."
    - **Update the existing `scripts/setup-local-codesign.sh` row (currently line 97).** Current description claims it makes "TCC permission grants survive client rebuilds" — this is misleading (cdhash matching means the cert helps within a build but not across rebuilds). Replace with: "Legacy self-signed cert script; no longer wired into the build flow; retained for `--ad-hoc` developer fallback only; see CONTRIBUTING.md."
    - Remove any references to `client-launchd.sh` (Option B was rejected; that script is not created).
35. `docs/CONTRIBUTING.md` (file exists, currently no `setup-local-codesign.sh` mention): append a new subsection under the existing ad-hoc-build documentation explaining `setup-local-codesign.sh`'s legacy status — generates a self-signed cert that helps within a single build but not across rebuilds; superseded by the Apple Development identity flow; retained only for developers who explicitly want to build without an Apple ID.
36. `scripts/build-client-local.sh` lines 109–116 — the existing comment block ("Prefer a persistent self-signed identity so the cdhash stays stable...") describes the OLD identity strategy and is now actively misleading. Replace the comment block with: "Use Apple Development identity (free, from Xcode → Settings → Accounts → any Apple ID). Required for SMAppService.mainApp.register() — see Apple Developer Forums thread 799910. Multiple identities trigger an explicit-disambiguation error; set SIGN_IDENTITY env var to override."

### Phase 8 — Final verification (~30 min)
37. `swift build --package-path client` again — zero warnings.
38. End-to-end manual: hold FN dictation; switch mic mid-dictation; triple-tap meeting; switch mic mid-meeting; verify partial WAV deleted; quit menubar app; logout/login Mac → confirm app reappears via Login Items entry within 2s.
39. Mac mini: `bash scripts/server-launchd.sh bootstrap-test` → expect 200; `launchctl print gui/$UID/co.wispralt.cloudflared | grep state` → expect `state = running`.
40. Run `/verify-autostart` end-to-end.
41. Run `/docs-check` to confirm no stale doc files vs the file→doc map. **Sequencing note:** `/docs-check` must run AFTER Phase 7 tasks 34–36 land the `OVERVIEW.md` and `CONTRIBUTING.md` updates. Running it earlier (e.g., immediately after Phase 1 creates `AudioDeviceListener.swift`) would produce a false-clean result.

### Phase 9 — Commit + push (~10 min)
42. Stage and commit with a single coordinated commit titled "Auto-start, mic-switch parity, install polish, doc honesty pass". Show diff stat. **Wait for explicit push approval per project rule.**

---

## Deprecated code to remove

- The `sudo cloudflared service install <TOKEN>` invocation in `scripts/setup-cloudflared.sh` (replaced by the user-level LaunchAgent).
- All references to `~/wispralt/tmp/credentials.txt` as a token-storage location (replaced by `~/.config/wispralt/cloudflare-token`). Remove from `docs/DEPLOYMENT-NOTES.md` secrets table; remove from `CLAUDE.md` conventions; do NOT keep as a historical note (review fix #18).
- `scripts/setup-local-codesign.sh` is no longer wired into the build flow but stays in the repo as a `--ad-hoc` developer fallback only. `docs/CONTRIBUTING.md` documents this.
- The old `kickstart -k` cloudflared rotation snippet in `DEPLOYMENT-NOTES.md` (~line 94) — replaced by the bootout/bootstrap procedure that handles both `--token-file` and legacy paths.

---

## Risks and Mitigations

- **SMAppService fails despite the Apple Development cert.** Mitigation: the `register()` call logs status; failure mode is "menubar still works, just doesn't auto-launch." Documented `sfltool resetbtm` recovery in TROUBLESHOOTING.md. If it persistently fails on a specific Mac, fall back to the manual login-item method (System Settings → General → Login Items & Extensions → "+" → select WisprAlt.app); document but don't auto-script.
- **Cloudflared token in plist (legacy path).** Mode 0600 on plist; user's home directory is not world-readable; acceptable for personal use. Upgrading cloudflared to ≥ 2025.4.0 + re-running `setup-cloudflared.sh` migrates to `--token-file` automatically.
- **`AudioObjectAddPropertyListener` retain leak.** Mitigated by `Unmanaged.passRetained` on init paired with explicit `release()` in deinit and on init-failure rollback. Validation: trigger 100 mic switches; `leaks <pid>` shows no growth.
- **`KeepAlive: true` cloudflared hot-loop on bad token.** `ThrottleInterval: 10` enforces a minimum 10s between restarts. Documented symptom + log location.
- **TCC re-grant on rebuild.** Apple-enforced; cannot be fixed without Developer ID. Documented honestly in `DEPLOYMENT-NOTES.md` "Re-grant on rebuild looks like a bug but isn't".
- **Apple Development cert renews annually with a NEW SHA-1.** Auto-renews while user stays signed into Xcode with Apple ID, but the renewed cert has a different fingerprint → different Designated Requirement → triggers a TCC re-grant cycle just like a binary rebuild. Documented honestly in `SETUP-CLIENT.md` and `DEPLOYMENT-NOTES.md` so the user is not surprised every ~12 months.
- **Multiple Apple Development identities in keychain.** Build script fails clearly with the list of identities and instructs setting `SIGN_IDENTITY` explicitly via env var. Prevents non-deterministic identity selection between Apple-ID accounts.

---

## Validation Gates (executable, with retry loops)

```bash
# Build
swift build --package-path client && bash scripts/build-client-local.sh

# Server reboot survival simulation (uses the new bootstrap-test subcommand)
bash scripts/server-launchd.sh bootstrap-test

# Cloudflared reboot survival simulation, with retry
launchctl bootout gui/$UID/co.wispralt.cloudflared
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/co.wispralt.cloudflared.plist
ok=0
for i in $(seq 1 15); do
    if curl --max-time 3 -s https://transcribe.integrateapi.ai/healthz | grep -q '"status":"ok"'; then
        ok=1; break
    fi
    sleep 2
done
[ $ok -eq 1 ] && echo "✓ tunnel up" || { echo "✗ tunnel did not come up in 30s"; exit 1; }

# Client login-launch (SMAppService): three independent checks
# 1. launchctl print-disabled (stable across macOS versions)
launchctl print-disabled "gui/$UID" 2>/dev/null \
    | grep -q '"co.wispralt.WisprAlt" => disabled' \
    && echo "✗ SMAppService is in disabled list" \
    || echo "✓ SMAppService not disabled"
# 2. sfltool dumpbtm — fast but private/unstable; cross-check only
sfltool dumpbtm 2>/dev/null | grep -q co.wispralt.WisprAlt \
    && echo "✓ sfltool sees the entry" \
    || echo "ℹ sfltool returned no entry (may be a Sonoma/Sequoia format change, not a real failure)"
# 3. Process running, with retry to handle async launch
ok=0
for i in $(seq 1 10); do
    if pgrep -lf '/Applications/WisprAlt.app/Contents/MacOS/WisprAlt' >/dev/null; then
        ok=1; break
    fi
    sleep 1
done
[ $ok -eq 1 ] && echo "✓ menubar app running" || echo "✗ menubar app not running"

# Mic-switch dictation parity (manual)
# Hold FN, talk, switch System Settings → Sound → Input mid-recording.
# Expect: "Dictation Cancelled" toast, mode returns to .idle, no transcript appears.

# Mic-switch meeting parity (manual)
# Triple-tap FN, talk, switch System Settings → Sound → Input mid-recording.
# Expect: "Meeting Cancelled" toast, mode returns to .idle, NO partial WAV in
# ~/Documents/WisprAlt/Meetings/ (verify with `ls -la ~/Documents/WisprAlt/Meetings/`).

# API key export round-trip (manual)
# Settings → Export API Key → save to ~/Desktop/test.wispralt-key.
# stat -f '%Mp%Lp' ~/Desktop/test.wispralt-key  → expect 0600
# head -3 ~/Desktop/test.wispralt-key            → expect "# WisprAlt API key export"
# Settings → Import API Key → select same file. Test dictation still works.

# Documentation lint (existing slash command, runs file→doc map check)
# Manual: invoke /docs-check
```

---

## Out of scope (explicit)

- Apple Developer Program enrollment ($99/yr), notarized DMG, App Store distribution, Sparkle EdDSA key generation.
- Test creation (no unit/integration tests per plan rules).
- Friend onboarding via `brew tap` or hosted DMG.
- Background URLSession resumption for meeting uploads (tracked in `CLAUDE.local.md` "Things To Fix Later").
- Migrating MeetingRecorder's mic capture from SCStream to AVAudioEngine (would simplify config-change detection but is a larger refactor).

---

## Confidence score: 8/10

Decision-locked: Option A SMAppService, free Apple Development cert. **Three reviewer passes** folded in — 27 findings from passes 1+2, then 22 more from pass 3 catching residual issues introduced by those fixes.

Confirmed-resolved issues:
- Pass 1+2: XML-in-bash injection, C-pointer storage, wrong recorder reference, mode guard + meetingActive reset, importAPIKey parsing, notification location, sequencing collapse to one TCC re-grant cycle.
- Pass 3: `Log.warn` → `Log.warning` API mismatch, `category:` enum vs string, `@unknown default`, double-release in `deinit`, ad-hoc fallback removal, multi-identity disambiguation, `bootstrap-test` case-block addition, `setup-client.sh` non-existence (folded into slash command), `lastOutputURL` accessor for SCStream race, `KeychainError.invalidExportFormat`, do/catch surfacing on Import button, `KeepAlive` dict form for rotation safety, trap on EXIT for token cleanup, `launchctl print-disabled` cross-check vs unreliable `sfltool`, annual cert renewal documentation, `OVERVIEW.md` row update, `/docs-check` sequencing note.

Remaining residual risk (the 2 points): the CoreAudio HAL listener is novel code that hasn't been validated on macOS 26 (Tahoe) — the `Unmanaged.passRetained` lifetime pattern is canonical and the rollback paths are correct, but real-world behavior under rapid mic-switch storms is unverified. Phase 8 manual verification (mic switch during meeting, verify partial WAV deleted) is the gate that catches this.

Sequencing requires only **one rebuild and one TCC re-grant cycle** for all client-side changes.
