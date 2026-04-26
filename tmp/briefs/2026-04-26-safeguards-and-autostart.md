# Brief: Safeguards, Auto-Start, and Multi-Device Hand-Off

## Why
The dictation pipeline now works end-to-end on a single hand-built install, but the system is not "always ready." After a Mac mini reboot, neither the FastAPI server nor the Cloudflare tunnel come back up. After a MacBook reboot, the menubar client must be launched manually. The mic-switch UX fix from commit `1dd7a54` is committed but not yet in the running app. Documentation oversells TCC permission persistence, which will mislead the user and any friends who install this. The user wants to hand this off to friends and run it on additional Macs of his own without paying the $99/yr Apple Developer ID fee, which constrains the design (TCC re-grants on rebuild are unavoidable; we work around that).

## Context

### Repo
- Path: `/Users/omidzahrai/Desktop/CODEBASES/TOOLS/wisprflowALT`
- GitHub: `omdiidi/miniWhisper` (default branch `main`)
- Last commit: `1dd7a54` — mic-switch UX fix on disk, not yet rebuilt+installed
- Working tree clean, no uncommitted changes

### Current auto-start posture
| Component | Survives reboot? | Mechanism | Gap |
|---|---|---|---|
| Server FastAPI | ❌ | `~/Library/LaunchAgents/co.wispralt.server.plist` | `RunAtLoad: false` (one-line fix) |
| Cloudflared | ❌ | `sudo cloudflared service install` is broken on macOS 14/15 — generates a plist with `ProgramArguments: ["cloudflared"]` (no `tunnel run --token <T>`) | No auto-generated user-level LaunchAgent replacement |
| Client menubar app | ❌ | None — user manually opens `/Applications/WisprAlt.app` after every login | No SMAppService / login-item integration |
| TCC permissions | ✅ same binary across reboot, ❌ rebuilds | Self-signed `WisprAlt Local Dev` cert in System trust + cdhash matching | Apple-enforced; only Developer ID survives rebuilds |

### Mic-switch fix state (commit `1dd7a54`)
- `client/WisprAlt/Capture/DictationRecorder.swift:5-12` — `Notification.Name.dictationConfigChanged` extension
- `client/WisprAlt/Capture/DictationRecorder.swift:221-246` — `AVAudioEngineConfigurationChange` observer in `start()`
- `client/WisprAlt/Capture/DictationRecorder.swift:383-386` — observer torn down in `stop()`
- `client/WisprAlt/Capture/DictationRecorder.swift:466-468` — observer torn down in `deinit`
- `client/WisprAlt/App/MenuBarController.swift:101-106` — `dictationConfigChanged` observer registered
- `client/WisprAlt/App/MenuBarController.swift:113-132` — handler stops recorder, resets mode to `.idle`, posts user toast
- **Gap:** `MeetingRecorder.swift` (SCStream-based) has no equivalent config-change abort. Different mechanism (SCStreamDelegate) but same UX problem if the user switches input mid-meeting.

### Server auto-start details (current)
- `scripts/server-launchd.sh` writes `co.wispralt.server.plist` with:
  - `KeepAlive: { SuccessfulExit: false }` (restarts on crash, not on normal exit)
  - `ThrottleInterval: 30`
  - `ExitTimeOut: 15`
  - `RunAtLoad: false` ← root cause of reboot survival failure
  - `ProgramArguments`: uvicorn invocation with `wispralt_server.main:app` on `127.0.0.1:8000`, single worker
  - Logs: `~/Library/Logs/WisprAlt/server.{log,err.log}`
  - Working dir: `$REPO_ROOT/server` (relative `.env` lookup)
- Bootstrapped via `launchctl bootstrap gui/$UID` in setup script

### Cloudflared details
- `setup-cloudflared.sh` runs `brew install cloudflared` then `sudo cloudflared service install <TOKEN>` — the latter is broken on macOS 14/15 Apple Silicon
- Documented workaround in `docs/DEPLOYMENT-NOTES.md`: hand-write `~/Library/LaunchAgents/co.wispralt.cloudflared.plist` reading token from `~/wispralt/tmp/credentials.txt`
- Token is currently kept out of files at setup time (stdin only); cloudflared's keychain-based persistence assumes the broken `service install` path worked. We need a deliberate token-storage decision for the user-level LaunchAgent path.

### TCC persistence (the honest answer)
- `scripts/setup-local-codesign.sh` generates a persistent self-signed cert (`WisprAlt Local Dev`, SHA-1 `4D3F41270D478A29DDB3A8B7CCF87AD3D70C0EE6`) in login keychain + System keychain trustRoot for codeSign
- Effect: TCC grants survive kill+relaunch of the **same** binary
- Limitation: Every rebuild changes the binary's cdhash, so macOS treats it as a new app and re-prompts for all four permissions (Accessibility, Input Monitoring, Screen Recording, Microphone)
- Without Developer ID, this is unfixable. Mitigation: rebuild less frequently, batch changes, document the cycle clearly for friends

### Install UX gaps (Claude Code flow)
`/setup-server` (`/setup-server.sh` + slash command):
- Validates macOS 13+, Python 3.11, Xcode CLT, Homebrew, uv ✓
- Prompts for `HF_TOKEN`, Cloudflare token (stdin) ✓
- Downloads ~5.6 GB of models ✓
- Generates `WISPRALT_API_KEY` ✓
- Registers server LaunchAgent ✓
- **Gaps:** HF account creation + gated-model acceptance is manual; CF tunnel creation in dashboard is manual; cloudflared LaunchAgent broken (see above); reboot survival not tested

`/setup-client`:
- Validates macOS 14+ ✓
- Downloads or builds DMG ✓
- Walks 4 permission panes ✓
- Prompts for SERVER_URL + API key ✓
- Runs `/healthz` smoke test ✓
- **Gaps:** Drag-to-Applications is manual (could AppleScript it); no login-item registration; no post-grant verification beyond the smoke test; rebuild causes silent re-grant prompts

### Secrets posture (already clean)
- `server/.env` mode 0600, contains `HF_TOKEN`, `WISPRALT_API_KEY`
- Client Keychain service `co.wispralt`, account `default`, holds `WISPRALT_API_KEY` only
- Cloudflare tunnel token: stdin only at setup, stored by cloudflared in macOS system keychain (when its service install works)
- Minor open issues: token storage path for the user-level LaunchAgent fix; client-side API key recovery/export flow

## Decisions

- **No Apple Developer ID** — User declined the $99/yr fee. We design assuming TCC re-grants on every rebuild and document the friction honestly.
- **Mic-switch fix lands in the next rebuild** — Commit `1dd7a54` already on disk; rebuild + reinstall picks it up.
- **Mic-switch fix extends to MeetingRecorder** — Cheap to add in the same pass; same UX problem if input changes mid-meeting. Implementation differs (SCStream config-change vs AVAudioEngine notification) but the user-facing behavior matches: stop, reset to idle, toast.
- **Server `RunAtLoad: true`** — One-line plist change in `scripts/server-launchd.sh`. Server survives Mac mini reboot from this point on.
- **Auto-generate user-level cloudflared LaunchAgent** — Replace the broken `sudo cloudflared service install` path. New plist at `~/Library/LaunchAgents/co.wispralt.cloudflared.plist`, runs as the user (no sudo), reads token from a `0600` file at `~/.config/wispralt/cloudflare-token` (path TBD by plan; favor `~/.config/` over `~/wispralt/tmp/` for clarity). `RunAtLoad: true`, `KeepAlive: true`.
- **Client login-item via SMAppService** — Use `SMAppService.mainApp.register()` (macOS 13.2+, we target 14+). Add a "Launch at login" toggle in Settings, default on. App appears in menubar after every login automatically.
- **Documentation honesty pass** — Update `docs/DEPLOYMENT-NOTES.md` and `docs/TROUBLESHOOTING.md` to clarify TCC persistence: persists across reboot for same binary; resets on rebuild; only Developer ID solves rebuild-persistence. Remove any wording that implies otherwise.
- **Tighten install UX without Developer ID** — `/setup-client` adds an AppleScript or `cp -R` step to install to `/Applications` non-interactively; `/setup-server` prints clearer pre-flight checklist for HF + CF account creation; both add a post-install verify step that exercises the actual reboot-survival path (`launchctl bootout` + `bootstrap` cycle as a smoke test).
- **API key export/import in client Settings** — Friend-onboarding aid. Lets a friend back up their key and re-import after reinstall without re-prompting the server admin. Stored in Keychain only; export to a temporary file the user manages.
- **No code changes during this discussion** — All decisions captured here for the next `/plan` invocation.

## Rejected Alternatives

- **Apple Developer ID + notarized DMG** — Highest-leverage fix for cross-rebuild TCC persistence, Gatekeeper bypass on friends' Macs, and Sparkle auto-update friction. Rejected by user: $99/yr cost. We accept the rebuild-resets-permissions tax instead.
- **Hardened Runtime (`--options runtime`) on local builds** — Would require `disable-library-validation` entitlement to coexist with bundled Sparkle.framework signed under a different identity. Rejected: adds attack surface, only beneficial for distribution. Distribution build script keeps it for the eventual notarization path.
- **Server LaunchDaemon (vs LaunchAgent)** — Daemon would run before user login, but requires `sudo` to install and complicates the "user-scoped, no admin needed" install story. Rejected: LaunchAgent with `RunAtLoad: true` is sufficient because Mac mini boots into a logged-in session in the user's headless setup.
- **Tunneling without Cloudflare (Tailscale Funnel, ngrok)** — Out of scope for this round. Cloudflare Tunnel is already wired and working. Revisit only if cloudflared persistence remains brittle after the user-level LaunchAgent fix.
- **Auto-installing the client via `brew tap`** — Would simplify friend install but requires a Homebrew formula and a hosted DMG. Defer until the install flow is otherwise tightened; revisit when there's actually a tagged release.
- **Per-user `tccutil reset` automation in build script** — Would erase grants on every build to force a clean prompt. Rejected: makes the dev cycle slower for the maintainer (the user). Manual `tccutil reset` is good enough as a documented fallback.

## Direction

Land the mic-switch rebuild first to validate the existing fix on real hardware, then ship a single coordinated change set covering: server `RunAtLoad: true`, user-level cloudflared LaunchAgent, client SMAppService login item, MeetingRecorder mic-switch parity, documentation honesty pass on TCC, and install-flow polish (`/setup-server` + `/setup-client`). Treat Apple Developer ID as a deferred future option, not a blocker. Verify each change with a real reboot test on the Mac mini and a real logout/login on the MacBook before declaring done.
