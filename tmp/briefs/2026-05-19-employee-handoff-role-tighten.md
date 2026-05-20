# Brief: Employee-handoff hardening — role leaks, install hygiene, in-app updater

## Why
User wants to be confident WisprAlt is ready to hand to employees. Three threads surfaced during the discussion:

1. **Role leaks** — verification turned up 3 `/admin/*` routes gated only by `require_api_key` (not `require_admin`); a normal employee token can read them. Sister will be the test subject on a fresh Mac and we want only real bugs surfacing, not pre-known holes.
2. **Install hygiene** — an employee had an old broken WisprAlt sitting on his machine that the curl one-liner did not clean up. The current `install.sh` only kill+replaces `/Applications/WisprAlt.app` and misses user-scoped, renamed, or differently-pathed installs entirely.
3. **In-app update button** — once employees are on board, they shouldn't need an admin to re-deliver a curl command for each release. A menubar/Settings button that invokes the install flow should be a one-click "update to latest" path.

## Context

### Onboarding flow (verified end-to-end, works)
- Admin → `/admin/login` → `POST /admin/users/new` (`routes/admin_ui.py:348`) → `users_store.mint()` (`users/store.py:107`) generates 64-hex token, sha256-hashes it, inserts into `wispralt.users` with `role='employee'`, returns plaintext once.
- Server renders `employee_added.html.j2` with pre-baked curl one-liner from `_build_install_command()` (`routes/admin_ui.py:324-331`).
- Admin texts/Slacks that string to employee.
- Employee runs it → `install.sh` (root of repo) does: preflight → fetch `releases/latest` from GitHub → sha256 sidecar verify → mutex-locked install → pkill+SIGKILL → rm → hdiutil mount → cp -R → xattr strip → TCC reset on reinstall → Keychain write with `-T` ACL pre-auth → `defaults write` → open app → verify launch.
- App launches → 4-permission wizard (Accessibility, Input Monitoring, Mic, Screen Recording) via `PermissionGate.checkAll()` (`AppDelegate.swift:121`).
- App auto-registers LoginItem via `SMAppService.mainApp.register()` (`AppDelegate.swift:62`), gated by `co.wispralt.didAutoRegisterLoginItem` flag.
- First `MeAPI.get()` call attributes employee; they appear in `/admin/users` with `last_seen_at` populated.
- Dictations attribute to their `user_id` via `main.py:730-755` usage queue → `usage/writer.py:38`.

### Role gating (mostly airtight, 3 leaks)
- `require_admin` (`auth.py:209-213`) checks `user.role == "admin"` — applied at router level to `admin_ui.py:76-79` `authed_router`, so `/admin/users`, `/admin/users/new`, `/admin/data`, `/admin/rotate-key` are all gated.
- `/me/*` SQL queries in `jobs/store.py` are all keyed by `api_key_id` — no cross-employee data reads.
- Break-glass `user.id = -1` only fires on Postgres-down AND token matches `app.state.break_glass_token_hash`; doesn't bypass revocation. `/me/login` explicitly rejects break-glass (`routes/me.py:223-229`).

**The 3 leak paths** (require_api_key, should be require_admin):
- `GET /admin/active` (`routes/admin.py:302-304`) — lists all non-terminal jobs with `wav_path` and `job.id`. No transcript content but leaks cross-employee job metadata.
- `GET /admin/server-log/{job_id}` (`routes/admin.py:356-358`) — could leak log lines around any guessable job_id.
- `GET /metrics` (`routes/admin.py:160-162`) — server-wide observability counters.

### Token cache TTL
- `auth.py:48` — 60s cache. After admin revokes a token, the revoked employee can still authenticate for up to 60s. Admin revoke flow calls `auth.token_cache.invalidate(th)` (referenced at `routes/admin.py:150`) to short-circuit.

### install.sh cleanup gaps (audit findings)
The current `install.sh` only knows about `/Applications/WisprAlt.app`. Specifically:

- **Path-locked discovery** (`install.sh:80,182`) — only checks `/Applications/WisprAlt.app`. Never looks at `~/Applications/WisprAlt.app`, never uses `mdfind kMDItemCFBundleIdentifier == "co.wispralt.WisprAlt"`, never scans renamed copies (`WisprAlt 2.app`, `WisprAlt copy.app`).
- **Path-locked pkill** (`install.sh:194-202`) — kill pattern is the exact string `/Applications/WisprAlt.app/Contents/MacOS/WisprAlt`. A WisprAlt running from `~/Applications`, `~/Downloads`, or a DMG mount **survives**. No `pkill -f co.wispralt` or `osascript ... quit by bundle id` fallback.
- **No graceful quit** — straight to SIGTERM/SIGKILL. A graceful AppleScript quit by bundle ID would let `SMAppService` cleanly unregister.
- **No LaunchAgent plist sweep** — `~/Library/LaunchAgents/co.wispralt*.plist` is never enumerated, removed, or `launchctl unload`'d.
- **No LoginItem deregistration** — `SMAppService` registration pointing at a now-deleted bundle (especially the `~/Applications` orphan case) is not cleaned up. macOS usually invalidates these but not always.
- **TCC reset gated on `/Applications/` install** (`install.sh:233-243`) — if the old install was user-scoped, TCC for the old bundle persists and may conflict with the new install's grants.
- **No version check / downgrade guard** — `RELEASE_TAG` is fetched but never compared to the installed `CFBundleShortVersionString`. Silent downgrade is possible if the wrong curl URL is used.
- **No orphan-bundle removal** — even when `/Applications/WisprAlt.app` is correctly replaced, stale `~/Applications/WisprAlt.app` or `WisprAlt 2.app` copies are left for the user to discover.

**Concrete impact on the broken-old-install employee:** if his copy was at `~/Applications/`, the v0.4.6 curl command would have installed a SECOND copy into `/Applications/`, leaving the broken one untouched. macOS Spotlight + LoginItem behavior can then route him back to the old broken one on next login.

### In-app updater (currently does not exist)
- No "Check for updates" button anywhere in `SettingsView.swift` or `MenuBarController.swift`.
- `releases/latest` on GitHub already returns the current version + DMG URL + sha256, so the metadata side is solved.
- The `/wispralt-update` slash command in `~/.claude-dotfiles/commands/` exists as a developer convenience but it's not user-facing in the app.
- Sparkle (`https://sparkle-project.org/`) is the standard macOS auto-update framework but requires a hosted `appcast.xml`. Re-using the existing `install.sh` curl flow is simpler and keeps the install path canonical.

## Decisions

### Role leaks
- **Tighten all 3 leak paths in one go** — swap `require_api_key` → `require_admin` on `/admin/active`, `/admin/server-log/{job_id}`, `/metrics` (`routes/admin.py:160`, `:302`, `:356`). User confirmed during discussion: chose "Tighten now (Recommended)" over leaving /metrics open for Prometheus.

### install.sh hardening
- **Bundle-ID-based discovery, not path-based.** Use `mdfind 'kMDItemCFBundleIdentifier == "co.wispralt.WisprAlt"'` to enumerate ALL WisprAlt copies anywhere on the filesystem. Iterate over the result, killing each running process AND deleting each bundle. Targets the root cause: an old broken install at any non-canonical path.
- **Graceful quit before SIGTERM.** Use `osascript -e 'tell application id "co.wispralt.WisprAlt" to quit'` first, then fall back to pkill -f co.wispralt after a 2s grace window. Lets `SMAppService` cleanly deregister.
- **LaunchAgent sweep.** `launchctl unload ~/Library/LaunchAgents/co.wispralt*.plist 2>/dev/null; rm -f ~/Library/LaunchAgents/co.wispralt*.plist` — purges any stale agent plists from old installs.
- **TCC reset on ANY found bundle, not just /Applications.** Move the `tccutil reset All "$BUNDLE_ID"` call OUT of the `IS_REINSTALL` gate so it fires whenever any old copy was deleted.
- **Always install to `/Applications/`.** After cleanup, `cp -R` the new bundle to `/Applications/WisprAlt.app` regardless of where the old copy was. Canonical path.
- **Preserve Keychain + Logs + latency.json.** Reinstall should not nuke `~/Library/Caches/WisprAlt/latency.json` (user's dictation telemetry ring buffer) or the `co.wispralt` Keychain entry (API key). These already survive — keep that behavior explicit.
- **No version-downgrade guard for now.** Adding `defaults read .../CFBundleShortVersionString` + version compare is a nice-to-have but not blocking. If we get burned, add it then.

### In-app updater
- **Shell out to the canonical curl one-liner, don't reinvent.** Add a "Check for updates…" button to Settings → Advanced section. On click:
  1. Fetch `https://api.github.com/repos/omdiidi/miniWhisper/releases/latest` → compare `tag_name` against bundled `CFBundleShortVersionString`.
  2. If equal: show "You're on the latest version (vX.Y.Z)" toast.
  3. If newer: show "Update available: vX.Y.Z → vX.Y.Z" with "Install now" and "Later" buttons.
  4. On "Install now": launch `Terminal.app` with the curl one-liner via `NSWorkspace.shared.open(_:configuration:completionHandler:)` so the user sees the install progress in a visible Terminal window. Terminal runs curl → install.sh quits the running WisprAlt → installs new → launches it. From the user's POV: click Install → WisprAlt closes → Terminal scrolls → new WisprAlt launches ~30s later.
- **Why Terminal, not silent background install?** Two reasons: (a) sudo-equivalent TCC prompts may pop up and need user attention; (b) if anything fails, the user has a visible log of the error to send back. Silent installs are scarier than visible ones.
- **Auto-check on launch, no nag.** Once per launch, fire the version check 60s after first dictation (avoid race with the menubar showing up). If newer, change the menubar icon to a subtle dot and add "Update available" at the top of the Settings → Advanced section. No popups, no modal, no toasts. User notices when they look. Plan A telemetry can track adoption.
- **No Sparkle.** Two reasons: (a) requires hosting an `appcast.xml` which is one more piece of infra; (b) the install.sh flow already handles process kill, code-sign verify, TCC reset, Keychain pre-auth — all things Sparkle would need to be re-taught.

### Already settled
- **Sister gets the test, not synthetic users** — user has no second Mac and won't dogfood on the mini (mini runs the model, can't risk it). Sister's fresh Mac = real second user, real Keychain, real TCC prompts.
- **No "sister handoff" doc** — user will wing the walkthrough live.
- **Keep admin-mediated onboarding** — no self-serve signup, no magic-link, no invite-link feature.

## Rejected Alternatives
- **Self-serve employee signup (email + OTP / magic link)** — rejected. User confirmed admin-mediated is the intended model.
- **Invite-link with embedded token** — rejected. Same out-of-band delivery problem as current Slack-the-curl-string.
- **Skip the /metrics tightening (leave it open for Prometheus)** — rejected. If a scraper needs it later, give it its own admin-role token.
- **Synthetic test via curl from dev box** — would not exercise Keychain, TCC, or LoginItem flows. Sister-on-fresh-Mac is the real test.
- **Sparkle for in-app updates** — rejected. Re-uses install.sh path; less infra; preserves canonical install behavior.
- **Silent background updater** — rejected. Visible Terminal install is debuggable and matches the install ceremony.
- **Aggressive popup nag for available updates** — rejected. Subtle menubar dot + Settings affordance is enough; nag dialogs train people to dismiss.
- **Version-downgrade guard in install.sh** — deferred. Not blocking. Add if/when downgrade burns someone.
- **Migrating old install path to canonical** (e.g., move `~/Applications/WisprAlt.app` → `/Applications/WisprAlt.app`) — rejected. Just delete the old one; canonical is whatever the new install writes.

## Where Reasoning Clashed
**Auto-check cadence** — could check every launch, every 24h, or only on user click. Argued for "once per launch, 60s delay, subtle indicator." Reasonable case for "once per 24h" via timer to avoid spamming the GitHub API, but realistically once-per-launch is fine since WisprAlt is a daemon — most users launch once per day. If GitHub rate limits become a problem, add a 24h debounce to UserDefaults.

## One Thing to Do First
**Edit `server/src/wispralt_server/routes/admin.py` and change `Depends(require_api_key)` → `Depends(require_admin)` on the 3 leaky route handlers** (`/metrics`, `/admin/active`, `/admin/server-log/{job_id}`). Smallest, safest, immediately deployable change. Run `uvicorn` locally, hit each route with a non-admin token, confirm 403. Then redeploy to mini and verify on the live system. Install.sh hardening + in-app updater are larger workstreams that should follow as separate plans.

## Direction
Three-step rollout:

1. **Role tightening (today, ~30 min)** — 3 line-decorator swaps in `routes/admin.py`, deploy to mini via CRD + /macmini gist transport, verify with curl. Trivial change, zero migration risk.

2. **install.sh hardening (1-2 hour, can land same day)** — rewrite the discovery + kill + cleanup phases to be bundle-ID-driven instead of path-locked. New sequence: `mdfind` → for each found bundle: `osascript quit` → 2s wait → `pkill -f co.wispralt` → `rm -rf` → LaunchAgent sweep → `tccutil reset` → install canonical → Keychain pre-auth → launch. Existing release artifacts unchanged. Test by manually planting an `~/Applications/WisprAlt.app` decoy on the dev Mac and verifying the new install.sh removes it.

3. **In-app updater (1 evening, separate plan)** — add "Check for updates…" button + auto-check-on-launch + subtle menubar indicator. Shells out to Terminal with the curl one-liner. New `Updater.swift` + Settings entry + menubar badge state. Doesn't touch install.sh or server; pure client work.

Once all three ship: mint a real token for sister, send her the curl one-liner, walk her through the 4-permission wizard live. The system is handoff-ready when she can update herself from the menubar later without admin involvement.
