# Plan: Employee-handoff hardening — role leaks, install.sh cleanup, in-app updater, user-history surfacing

> **Version:** v3 (round 2 review applied — 25 round-2 findings, 4 promoted blockers)
> **Brief:** [./tmp/briefs/2026-05-19-employee-handoff-role-tighten.md](../briefs/2026-05-19-employee-handoff-role-tighten.md)
> **Target release:** v0.5.0 (minor bump — four workstreams)
> **Score:** 9.4/10 confidence for one-pass implementation
> **Changelog v1 → v2:** SQL ORDER BY tie-break + deleted_at filter folded into pseudocode; Swift `LastDictationAPI.fetch()` rewritten to use real `ServerClient.buildRequest`/`execute` + real `ServerError.server(status:body:)`; `dictations.id` CAST TO TEXT; `JSONResponse` import added; `compositeDot` uses `NSStatusBar.system.thickness`; `pkill -f` narrowed to `co.wispralt.WisprAlt`; `mdfind` post-filtered + explicit fallback paths; `UpdateChecker` badge gated on `serverURL != nil`; HTTP status check in `fetchLatestTag`; AppleScript TCC fallback to clipboard; release ordering flipped (mini deploy first); Task 4a added for `ServerLogSheet` 403 handling.
> **Changelog v2 → v3:** Settings.swift additions rewritten to use `self._published = Published(initialValue:)` pattern (the existing init pattern at lines 167-178, not post-init didSet-triggering assignment); `compositeDot` rewritten to draw the SF Symbol with `NSColor.controlTextColor` explicit fill so dot icons render correctly on dark menubars (Round 2 blocker #25); `should_skip_install` now also requires `pgrep` to confirm no running instance before short-circuiting (catches hung-but-version-matched processes); mutex acquisition moved to top of `main()` to cover the skip-install code path; `find -maxdepth 3` added as third orphan-discovery source for Spotlight-unindexed bundles; `sleep 1` moved INSIDE `quit_all_installs` AFTER AppleScript-quit and BEFORE pkill (so Terminal.app spawning from the dying parent finishes); `-1744` added to TCC fallback error codes; `-600` comment fixed; Task 4a rewritten with full switch-on-ServerError pseudocode; mdfind quoting fixed (`\"` not `'`); §1 stale "Decision must resolve" paragraph removed; new NEW small server change: `/me/login` accepts `?next=` URL param for deep-link to `/me/history` after auth (5-line server edit); "Open My Dictations" button now opens `<serverURL>/me/login?next=/me/history` so first-time UX lands on history after login; release rollback gap documented; route-order "place ABOVE /history" claim downgraded to stylistic; final task list renumbered to monotonic 1-32.

---

## Verified Repo Truths

These are anchor facts the implementer can rely on. Each verified by reading the cited file/line.

| Fact | Evidence | Implication |
|---|---|---|
| `routes/admin.py:42` — `router = APIRouter()` with no path prefix | `server/src/wispralt_server/routes/admin.py:42` | `@router.get("/metrics", ...)`, `@router.get("/admin/active", ...)`, etc. are full paths. |
| `routes/admin.py:162,304,358` — three `dependencies=[Depends(require_api_key)]` decorators | Read in plan-prep | Three line edits is the whole Workstream 1 server change. |
| `routes/admin_ui.py:76-79` — `authed_router` already uses `Depends(require_admin)` at router level | Reviewer audit | All `/admin/users/*`, `/admin/data` are already correctly gated. NO additional sweep needed. |
| `routes/admin_ui.py:482` — `GET /admin/me` deliberately uses `require_api_key` | Reviewer 1 finding #30 | This is INTENTIONAL — `/admin/me` is the per-employee self-service surface. Do NOT swap it to `require_admin`. |
| `routes/me.py:60` — `router = APIRouter(prefix="/me", tags=["me"])` | Read in plan-prep | New `@router.get("/dictations/last")` resolves to `/me/dictations/last`. |
| `routes/me.py:33-34` — imports only `HTMLResponse`, `RedirectResponse` from `fastapi.responses` | Read in plan-prep | Adding `JSONResponse` requires extending the import. |
| `jobs/store.py:194` — `self.con` is the single shared sqlite3 connection (not a pool) | Read in plan-prep | Use `self.con.execute(...)` — NOT `with self._connect() as conn:` (no such method). |
| `jobs/store.py:197` — `id INTEGER PRIMARY KEY AUTOINCREMENT` on `dictations` | Read in plan-prep | Must `CAST(id AS TEXT) AS id` in SELECT to match Swift `String` decode. |
| `jobs/store.py:201` — `text TEXT NOT NULL` | Read in plan-prep | `text IS NOT NULL` filter is redundant. `text != ''` is still meaningful. |
| `jobs/store.py:240` — `idx_dictations_api_key_active ... WHERE deleted_at IS NULL` | Read in plan-prep | "Most recent" must mean "most recent NOT soft-deleted". |
| `routes/me.py:324` — `/me/history` exists and filters by `api_key_id` | Plan-prep + reviewer | Workstream 4 reuses `/me/history` for the portal view — no new template work. |
| `client/WisprAlt/Server/ServerError.swift` — cases: `.missingConfiguration`, `.invalidServerURL`, `.unauthorized`, `.rateLimited(retryAfter:)`, `.meetingInProgress`, `.uploadTooLarge`, `.uploadTruncated`, `.server(status:body:)`, `.decoding(_)`, `.transport(_)` | File read | NO `.notFound`, `.invalidResponse`, `.httpStatus` cases. 404 → match `.server(status: 404, body: _)`. |
| `client/WisprAlt/Server/MeAPI.swift:20-31` — `static func get()` uses `ServerClient.shared.buildRequest(path:method:)` then `execute(_:)` returning `(Data, HTTPURLResponse)` | File read | Match this pattern in new `DictationAPI.lastDictation()`. |
| `client/WisprAlt/Storage/Settings.swift:23-33` — `private enum Key` string-keyed; properties are `@Published var X` with `didSet { defaults.set(...) }` | File read | NO `@UserDefault` property wrapper exists. Match the existing pattern. |
| `client/WisprAlt/UI/SettingsView.swift:312-317` — `appVersionString` already exists as `static` returning bundled `CFBundleShortVersionString` | File read | Reuse `Self.appVersionString` in the new update section — DO NOT redefine. |
| `client/WisprAlt/UI/SettingsView.swift:719` — `copyToast: (button: String, message: String)?` exists | File read | New "last-dictation" toast uses this tuple type — no new state needed. |
| `client/WisprAlt/App/MenuBarController.swift:557` — `.meetingRecording` is a separate `switch mode` case that bypasses the default branch | File read | The dot composite in the default branch already won't render during meetings — explicit guard unneeded. |
| `install.sh:15` — `BUNDLE_ID="co.wispralt.WisprAlt"` (the CLIENT bundle id) | Read in plan-prep | The mini's server runs as `co.wispralt.server` (different bundle). `pkill -f "co.wispralt.WisprAlt"` (broad) would catch both. Narrow to `co.wispralt.WisprAlt` to avoid killing server processes on a dev box running both. |
| `client/WisprAlt/Update/SparkleController.swift:33-37` — Sparkle is intentionally disabled (`startingUpdater: false`) | File read | Leave SparkleController as-is. New `UpdateChecker.swift` is a parallel module. |
| `releases/latest` URL: `https://api.github.com/repos/omdiidi/miniWhisper/releases/latest` returns `tag_name` like `v0.4.6` | install.sh:89-154 pattern + reviewer | UpdateChecker strips leading `v`. SemVer numeric-only (no `-rc` suffix because release-client.sh only ships `vX.Y.Z`). |

> **Decision locked here (resolved in v2, expanded in v3):** `/admin/server-log/{job_id}` employees-get-403 behavior is the intended semantic. Task 4a (below) handles the client-side surface so the in-app View Server Log sheet renders a friendly "Admin-only — ask your administrator" message rather than a raw error.

> **Decision locked here (added in v3):** "Open My Dictations" button uses `/me/login?next=/me/history` to deep-link past the default `/me/insights` landing. Adds ~10 lines server-side (`?next=` URL param honored on POST + same-origin guard). Closes the discoverability gap raised in the brief.

---

## Overview

Four workstreams ship together as v0.5.0:

1. **Role tightening** — swap `require_api_key` → `require_admin` on `/admin/active`, `/admin/server-log/{job_id}`, `/metrics` in `server/src/wispralt_server/routes/admin.py`. Server-side, ~30 min, mini redeploy required.

2. **install.sh hardening** — rewrite discovery + kill + cleanup to be bundle-ID-driven instead of path-locked. New behavior: `mdfind` enumerates ALL WisprAlt copies anywhere, AppleScript-quit each one, force-kill stragglers, delete all bundles, sweep LaunchAgents, then install canonical to `/Applications/`. Closes the "broken old install left on employee's Mac" hole.

3. **In-app updater** — new `Update/UpdateChecker.swift` Swift module. Auto-check `releases/latest` 60s after first launch. If newer, set a subtle menubar badge (a small dot overlay on the mic icon) AND show "Update available: vX.Y.Z" + "Install now…" button in Settings → Advanced. "Install now" shells out to `Terminal.app` with the canonical curl one-liner. No Sparkle — reuses install.sh as the canonical install path.

4. **User-history surfacing (added mid-draft per user)** — `/me/history` already exists in the portal (per `routes/me.py:324` — full search/filter/pagination by user, dictations + meetings, scoped by `api_key_id`). The features missing are (a) **discoverability**: from the menubar popover, employees need a button that takes them directly to their own history page, and (b) **Copy last dictation**: a menubar button at the very top of the popover that copies the most recent dictation's text. To support the latter, add a new JSON endpoint `GET /me/dictations/last` and a small `get_most_recent_dictation` repository function. No new portal page, no new HTML template — re-uses what's there.

After all four land + the post-implement reviewer says CLEAN, push to main (user pre-approved), run `release-client.sh 0.5.0` to publish a GitHub Release, redeploy the mini, and hand the resulting curl one-liner back to the user.

---

## Architecture Overview

```
Workstream 1: ROLE TIGHTENING (server)
    routes/admin.py
        @router.get("/metrics", dependencies=[require_api_key])      → require_admin
        @router.get("/admin/active", dependencies=[require_api_key]) → require_admin
        @router.get("/admin/server-log/{job_id}", deps=[..._api_key])→ require_admin
    Deploy: mini gets `git pull && launchctl kickstart -k gui/$UID/co.wispralt.server`
    Verify: hit each route with a NON-admin token → expect 403.

Workstream 2: install.sh CLEANUP REWRITE (client/installer)
    Old:  detect_reinstall() stats /Applications/WisprAlt.app           ┐
          install_bundle() pkill -f "/Applications/...MacOS/WisprAlt"   ├─ path-locked
          provision_credentials() tccutil reset only if IS_REINSTALL=1  ┘
    New:  enumerate_existing_installs()  ← uses mdfind by bundle id
          quit_all_installs()             ← AppleScript graceful → pkill -f co.wispralt
          remove_all_installs()           ← rm -rf each enumerated bundle
          sweep_launch_agents()           ← launchctl unload + rm co.wispralt*.plist
          tccutil reset gated on "any old install was found", not "/Applications/ reinstall"
          install_bundle()                ← cp to canonical /Applications/ (unchanged target)
    Test: plant a decoy ~/Applications/WisprAlt.app on dev Mac; run install.sh; verify
          decoy is gone AND /Applications/WisprAlt.app is the new build.

Workstream 3: IN-APP UPDATER (client Swift)
    NEW: client/WisprAlt/Update/UpdateChecker.swift  ← module-level actor
            checkLatestRelease() async throws -> Release   (hits GitHub releases/latest)
            isNewer(remote:bundled:) -> Bool                (SemVer-aware compare)
            triggerInstall()                                (NSWorkspace.open Terminal w/ curl)
        Settings.shared:
            +updateAvailable: String?  (UserDefaults-backed, set when remote > bundled)
        MenuBarController:
            +updateBadgeVisible: Bool  → renders a subtle dot via NSImage compositing
                in updateIcon() switch
        SettingsView.swift:
            +updateSection (shown in advanced):
                "You're on vX.Y.Z" (latest)
                OR "Update available: vX.Y.Z → vN.N.N  [Install now]"
                Manual "Check for updates" button
        AppDelegate:
            launch UpdateChecker.check() async 60s after applicationDidFinishLaunching
                (60s avoids races with PermissionGate prompts + first /me call)
    Triggered install: NSWorkspace.shared.open(Terminal.app, ...) launches a NEW
        Terminal window with the canonical curl one-liner. install.sh kills the
        running WisprAlt, replaces /Applications/WisprAlt.app, relaunches.
        From user's POV: dot appears → click "Install now" → Terminal window opens →
        ~30s later new WisprAlt is in the menubar.

Workstream 4: USER-HISTORY SURFACING (server + client)
    Server side (~30 lines):
        NEW endpoint: GET /me/dictations/last
            JSON response: {"text": "...", "created_at": <epoch>, "id": "..."}
            Repo: jobs/store.py adds get_most_recent_dictation(api_key_id) -> Optional[Row]
            Auth: require_api_key (employee-scoped; admin gets THEIR OWN row, not anyone else's)
            404 if the user has zero dictations.
    Client side:
        DictationAPI.swift  ← new method: lastDictation() async throws -> LastDictation
        MenuBarController/SettingsView QuickActionsSection:
            NEW button at the VERY TOP of the popover content:
                "Copy last dictation"  [systemImage: "doc.on.clipboard"]
                On click: call DictationAPI.lastDictation(), copy `.text` to NSPasteboard,
                          show "Copied — N chars" toast directly beneath (matches the
                          existing copy-meeting + copy-custom pattern).
                Disabled state: when no last-dictation has ever been fetched in this
                          session AND the fetch fails → grey out with help "No dictations
                          yet — say something first."
            NEW button in the same QuickActionsSection (placed alongside Open Portal):
                "Open My Dictations"  [systemImage: "list.bullet.rectangle"]
                Routes to: <serverURL>/me/history (NOT /admin/login, NOT /admin/me)
                Discoverability fix: today the only way employees see /me/history is
                          via the /admin/me page that they land on after /admin/login
                          redirects them. Direct deep-link saves 1-2 clicks.
    Data model invariant:
        /me/history queries are already filtered by api_key_id (jobs/store.py:703
        confirms WHERE api_key_id = ?). New /me/dictations/last applies the same
        invariant. Admins hitting /me/dictations/last get THEIR OWN last dictation,
        not anyone else's. To see all employees, admins use /admin/data tiles.

Sequencing rationale
    Workstream 1 first (smallest, safest, immediately deployable; closes a security
                          hole before any new code is shipped).
    Workstream 2 second (server-independent; client-side script change only; doesn't
                          touch the running app).
    Workstream 4 third  (server + client change; small surface area; landing it before
                          workstream 3 means the in-app updater's "Install now…" path
                          is the LAST thing built so it can be smoke-tested with all
                          other features already live).
    Workstream 3 fourth (largest client change; depends on a working install.sh
                          underneath — if updater triggers a broken install.sh, sister
                          ends up with the same orphan-bundle problem).
```

---

## Files Being Changed

```
Repo root
├── install.sh                                                    ← MODIFIED (workstream 2)
│
├── server/src/wispralt_server/
│   ├── routes/
│   │   ├── admin.py                                              ← MODIFIED (workstream 1; 3 decorator swaps)
│   │   └── me.py                                                 ← MODIFIED (workstream 4; add GET /me/dictations/last)
│   └── jobs/
│       └── store.py                                              ← MODIFIED (workstream 4; add get_most_recent_dictation)
│
├── client/WisprAlt/
│   ├── Update/
│   │   ├── SparkleController.swift                               ← UNCHANGED (deliberately disabled; keep)
│   │   └── UpdateChecker.swift                                   ← NEW (workstream 3)
│   ├── Server/
│   │   └── DictationAPI.swift                                    ← MODIFIED (workstream 4; add lastDictation())
│   ├── UI/
│   │   └── SettingsView.swift                                    ← MODIFIED (workstreams 3 + 4; updateSection + Copy last dictation + Open My Dictations)
│   ├── Storage/
│   │   └── Settings.swift                                        ← MODIFIED (workstream 3; updateAvailable + lastUpdateCheck)
│   ├── App/
│   │   ├── AppDelegate.swift                                     ← MODIFIED (workstream 3; kick off UpdateChecker.check 60s after launch)
│   │   └── MenuBarController.swift                               ← MODIFIED (workstream 3; updateBadgeVisible + dot compositing)
│   └── Info.plist                                                ← MODIFIED (version bumped to 0.5.0 by release-client.sh — NOT manually)
│
├── docs/
│   ├── INSTALL.md                                                ← MODIFIED (note new mdfind-based discovery; troubleshoot section)
│   ├── ARCHITECTURE.md                                           ← MODIFIED (add "In-app updater" subsection + role-gate note)
│   ├── AUTH.md (if exists, else skip)                            ← MODIFIED (note /admin/active + /admin/server-log + /metrics admin-gated)
│   └── OVERVIEW.md                                               ← MODIFIED (file→doc map: add UpdateChecker.swift)
│
└── tmp/done-plans/
    └── (this plan moves here after /implement)
```

---

## Key Pseudocode (Hot Spots)

### 1. Role tightening — `routes/admin.py`

This is the trivial part. Three lines change. The dependency name is already imported at the top (`from ..auth import require_admin, require_api_key`).

```python
# Line 162 — /metrics
@router.get(
    "/metrics",
    dependencies=[Depends(require_admin)],   # was: require_api_key
    summary="Structured server observability metrics",
)

# Line 304 — /admin/active
@router.get(
    "/admin/active",
    dependencies=[Depends(require_admin)],   # was: require_api_key
    summary="List active (pending or running) jobs with rich phase projection",
)

# Line 358 — /admin/server-log/{job_id}
@router.get(
    "/admin/server-log/{job_id}",
    dependencies=[Depends(require_admin)],   # was: require_api_key
    summary="Return the slice of server.log bracketing a job_id's appearances",
)
```

**Client-side compatibility:** the in-app "View server log" sheet (`ServerLogSheet` in `SettingsView.swift:1028-1087`, called by `MeetingAPI.fetchServerLog`) currently uses the employee's regular API key. After this swap, employees hitting `/admin/server-log/{job_id}` will get 403. v2/v3 **resolved this via Task 4a** (rewrite `ServerLogSheet.reload()` to catch `ServerError.server(status: 403, _)` and render "Server logs are admin-only. Ask your administrator for help."). Pattern (b) — a per-user-scoped `/me/server-log/{job_id}` endpoint — was considered and deferred (out of scope; only build if employees actively report needing logs from the in-app sheet).

### 2. install.sh — bundle-ID-driven cleanup

The key insight: `mdfind kMDItemCFBundleIdentifier == "co.wispralt.WisprAlt"` returns one line per copy of any app with that bundle ID, anywhere on the filesystem Spotlight has indexed. Iterate over it, kill each, delete each, then install canonical.

```bash
# Replaces detect_reinstall + the kill+rm portion of install_bundle.
# Returns 0/1 in IS_REINSTALL; populates FOUND_BUNDLES array with full paths.

enumerate_existing_installs() {
    FOUND_BUNDLES=()
    # mdfind newline-separated. Post-filter to exclude:
    #   /Volumes/*       — mounted DMGs, external drives, Time Machine snapshots
    #   *Backups.backupdb*  — Time Machine local store
    #   */.Trash/*       — bundles the user already deleted
    # The post-filter avoids rm-rf'ing things that are not real installs.
    local line
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        [[ "$line" == /Volumes/* ]] && continue
        [[ "$line" == *Backups.backupdb* ]] && continue
        [[ "$line" == */.Trash/* ]] && continue
        [[ -d "$line" ]] || continue
        [[ -x "$line/Contents/MacOS/WisprAlt" ]] || continue
        FOUND_BUNDLES+=("$line")
    done < <(mdfind "kMDItemCFBundleIdentifier == \"${BUNDLE_ID}\"" 2>/dev/null)

    # Belt-and-suspenders: explicit fallback paths in case Spotlight has them
    # excluded or hasn't reindexed yet. Append only if not already in
    # FOUND_BUNDLES.
    local p
    for p in "/Applications/WisprAlt.app" "$HOME/Applications/WisprAlt.app"; do
        [[ -d "$p" ]] || continue
        [[ -x "$p/Contents/MacOS/WisprAlt" ]] || continue
        local already=0
        local b
        for b in "${FOUND_BUNDLES[@]}"; do
            [[ "$b" == "$p" ]] && already=1 && break
        done
        (( already == 0 )) && FOUND_BUNDLES+=("$p")
    done

    # v3: third orphan-discovery source via `find`. Catches WisprAlt copies
    # in non-canonical user paths (~/Downloads, ~/Desktop, ~/Applications)
    # that Spotlight hasn't indexed yet (fresh download, indexing disabled,
    # etc). Depth-3 cap so we don't crawl deep trees. /Applications already
    # covered above; this widens to user-writeable directories.
    local found
    while IFS= read -r found; do
        [[ -z "$found" ]] && continue
        [[ -d "$found" ]] || continue
        [[ -x "$found/Contents/MacOS/WisprAlt" ]] || continue
        local already=0
        local b
        for b in "${FOUND_BUNDLES[@]}"; do
            [[ "$b" == "$found" ]] && already=1 && break
        done
        (( already == 0 )) && FOUND_BUNDLES+=("$found")
    done < <(find "$HOME/Downloads" "$HOME/Desktop" "$HOME/Applications" \
        -maxdepth 3 -name "WisprAlt*.app" -type d 2>/dev/null)

    if (( ${#FOUND_BUNDLES[@]} > 0 )); then
        IS_REINSTALL=1
        info "Found ${#FOUND_BUNDLES[@]} existing WisprAlt install(s):"
        local b
        for b in "${FOUND_BUNDLES[@]}"; do
            info "  - $b"
        done
    fi
}

# Idempotency early-exit: if the only found install is the canonical path AND
# its CFBundleShortVersionString matches RELEASE_TAG (stripped of leading 'v')
# AND no WisprAlt process is currently running (avoids leaving a hung instance
# in place under a "no-op" disguise), skip the whole quit/remove/install dance.
# Saves a TCC reset on no-op re-runs.
should_skip_install() {
    # Only valid as a check AFTER fetch_release_metadata.
    (( ${#FOUND_BUNDLES[@]} == 1 )) || return 1
    [[ "${FOUND_BUNDLES[0]}" == "/Applications/WisprAlt.app" ]] || return 1
    # v3: also require no running instance. If the canonical app is running we
    # still want to kill+replace so a hung process doesn't survive the no-op.
    pgrep -f "co.wispralt.WisprAlt" >/dev/null 2>&1 && return 1
    local installed
    installed="$(defaults read /Applications/WisprAlt.app/Contents/Info CFBundleShortVersionString 2>/dev/null || true)"
    local target="${RELEASE_TAG#v}"
    [[ -n "$installed" && "$installed" == "$target" ]]
}

quit_all_installs() {
    (( ${#FOUND_BUNDLES[@]} > 0 )) || return 0
    info "Quitting running WisprAlt instances (graceful)..."
    # Graceful AppleScript-quit by bundle id — works regardless of bundle path.
    osascript -e "tell application id \"${BUNDLE_ID}\" to quit" 2>/dev/null || true
    # v3: give Terminal.app time to receive its `do script` Apple Event before
    # we kill the WisprAlt parent that spawned us. macOS cold-launches Terminal
    # in 2-5s; 2.5s is the sweet spot between snappy and reliable.
    sleep 2.5
    # Grace window. Poll for any binary still running with our bundle id pattern.
    local i
    for i in 1 2 3 4; do
        # pgrep -f matches against full command path.
        pgrep -f "co.wispralt.WisprAlt" >/dev/null 2>&1 || return 0
        sleep 0.5
    done
    # Hard kill for stragglers. Narrow pattern (not "co.wispralt") so we don't
    # accidentally kill the mini's wispralt_server process on a dev box running
    # both client and server.
    warn "Sending SIGTERM to remaining WisprAlt processes..."
    pkill -TERM -f "co.wispralt.WisprAlt" 2>/dev/null || true
    sleep 1
    if pgrep -f "co.wispralt.WisprAlt" >/dev/null 2>&1; then
        warn "Sending SIGKILL to remaining WisprAlt processes..."
        pkill -KILL -f "co.wispralt.WisprAlt" 2>/dev/null || true
    fi
}

remove_all_installs() {
    local b
    for b in "${FOUND_BUNDLES[@]}"; do
        info "Removing $b ..."
        rm -rf "$b" || warn "Could not remove $b (continuing)."
    done
}

sweep_launch_agents() {
    # Only touches $HOME/Library/LaunchAgents — we never had write access to
    # /Library/LaunchAgents/ from a curl-bash flow anyway. The client app does
    # NOT install LaunchAgent plists (LoginItem is via SMAppService, not plist),
    # so this sweep is mostly defensive against future versions or third-party
    # tooling that might have planted one. Specifically EXCLUDES the mini's
    # `co.wispralt.server` and `co.wispralt.cloudflared` plists which only
    # exist on the server host, not the client (`server-launchd.sh`).
    local agent_dir="$HOME/Library/LaunchAgents"
    local plist
    shopt -s nullglob
    for plist in "$agent_dir"/co.wispralt*.plist; do
        info "Unloading + removing $(basename "$plist") ..."
        launchctl unload "$plist" 2>/dev/null || true
        rm -f "$plist" 2>/dev/null || true
    done
    shopt -u nullglob
}
```

The new `main()` flow (v2 — adds idempotency early-exit + 1s grace before pkill):

```bash
main() {
    preflight
    # v3: mutex acquisition moved up so it covers BOTH the skip-install code path
    # and the full install path. Previously inside install_bundle, which the skip
    # path bypassed — two concurrent same-version reruns could race on
    # provision_credentials. cleanup() trap unconditionally rmdir's the lock.
    local lock_dir="/tmp/wispralt-install.lock"
    mkdir "$lock_dir" 2>/dev/null \
        || die "Another WisprAlt install appears to be in progress (lock at $lock_dir). If stale, remove with: rmdir $lock_dir"
    enumerate_existing_installs       # populates IS_REINSTALL + FOUND_BUNDLES
    fetch_release_metadata
    if should_skip_install; then
        info "Already on $RELEASE_TAG at the canonical path with no running instance. Skipping reinstall."
        provision_credentials         # still write Keychain if WISPRALT_API_KEY was passed
        open_app_and_verify_launch
        print_next_steps
        return 0
    fi
    download_and_verify
    quit_all_installs                 # AppleScript-quit + 2.5s Terminal-grace + SIGTERM/SIGKILL
    remove_all_installs               # rm -rf every found bundle
    sweep_launch_agents               # plus LaunchAgent plists in $HOME/Library/LaunchAgents/
    install_bundle                    # cp -R into /Applications/ (canonical) — mutex already held
    provision_credentials             # tccutil reset gated on IS_REINSTALL=1
    open_app_and_verify_launch
    print_next_steps
}
```

**install_bundle simplification:** since the mutex is now in `main()`, `install_bundle` can drop its own `mkdir "$lock_dir"` line. Keep the `hdiutil attach/detach`, `cp -R`, and `xattr -dr` operations — those don't need re-locking.

Note `install_bundle` is now simplified — it no longer does discovery/kill/rm. It just: hdiutil mount → cp -R to `/Applications/` → strip xattr.

### 3. UpdateChecker.swift

```swift
import Foundation
import AppKit

/// Lightweight GitHub-Releases poller. Fires once 60s after launch (avoids
/// PermissionGate races + the first /me probe). Compares the bundled
/// CFBundleShortVersionString to the latest GitHub Release tag. If newer,
/// flips Settings.shared.updateAvailable and asks MenuBarController to show
/// a subtle dot on the menubar icon.
///
/// "Install now" UI lives in SettingsView (advanced section). On click,
/// triggerInstall() launches Terminal.app running the canonical curl
/// one-liner. install.sh handles kill+replace; this checker does NOT.
final class UpdateChecker {
    static let shared = UpdateChecker()

    private static let repoOwner = "omdiidi"
    private static let repoName = "miniWhisper"
    private static let installURL = "https://raw.githubusercontent.com/\(repoOwner)/\(repoName)/main/install.sh"

    // Once per launch is enough; debounce to 6h via UserDefaults
    // to be polite to the GitHub API rate limit.
    private let debounceInterval: TimeInterval = 6 * 60 * 60

    func checkSoon() {
        // Fire 60s after launch. UpdateChecker does nothing if a check happened
        // within debounceInterval.
        Task.detached(priority: .utility) { [weak self] in
            try? await Task.sleep(nanoseconds: 60 * 1_000_000_000)
            await self?.check()
        }
    }

    func check(force: Bool = false) async {
        if !force, let last = Settings.shared.lastUpdateCheck,
           Date().timeIntervalSince(last) < debounceInterval {
            Log.info("UpdateChecker: skipping (debounced).", category: "update")
            return
        }
        do {
            let remote = try await fetchLatestTag()
            let bundled = bundledVersion()
            Log.info("UpdateChecker: bundled=\(bundled) remote=\(remote)", category: "update")
            await MainActor.run {
                Settings.shared.lastUpdateCheck = Date()
                if Self.isNewer(remote: remote, bundled: bundled) {
                    Settings.shared.updateAvailable = remote
                    // First-run UX: don't badge until the user has a server URL set
                    // AND has at least one successful /me call this session (handled
                    // upstream by AppDelegate gating UpdateChecker.shared.checkSoon()).
                    // If `serverURL` is nil here, we still set updateAvailable so
                    // Settings → Advanced shows it, but skip the menubar dot.
                    let canBadge = Settings.shared.serverURL != nil
                    MenuBarController.shared?.setUpdateBadge(visible: canBadge)
                } else {
                    Settings.shared.updateAvailable = nil
                    MenuBarController.shared?.setUpdateBadge(visible: false)
                }
            }
        } catch {
            Log.warning("UpdateChecker failed: \(error.localizedDescription)", category: "update")
            // Silent failure. lastUpdateCheck is NOT set so the next launch retries.
        }
    }

    /// User clicked "Install now…". Open Terminal with the canonical curl one-liner.
    /// We do NOT pipe the curl output back into the app — visible Terminal output
    /// is intentional debuggability for non-technical employees.
    ///
    /// If macOS Automation TCC denies Terminal access (the user clicked "Don't
    /// Allow" on the first-time prompt), fall back to copying the install command
    /// to NSPasteboard and surfacing a toast asking the user to paste it.
    func triggerInstall() {
        // Hardcoded AppleScript — no string interpolation of `cmd` into the
        // AppleScript body since `cmd` is itself a constant. Defense against
        // future contributor adding query params that would break the quoting.
        let scriptSource = """
        tell application "Terminal"
            activate
            do script "curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh | bash"
        end tell
        """
        var error: NSDictionary?
        if let script = NSAppleScript(source: scriptSource) {
            script.executeAndReturnError(&error)
            if let error {
                let errNum = error[NSAppleScript.errorNumber] as? Int ?? 0
                Log.error("UpdateChecker triggerInstall failed: \(error)", category: "update")
                // v3 fallback codes:
                //   -1743  errAEEventNotPermitted     (Automation TCC denied)
                //   -1744  errAEUserCanceled          (user clicked Don't Allow / Cancel)
                //   -1728  errAEParametersNotFound    (rare; Terminal not installed at all)
                if errNum == -1743 || errNum == -1744 || errNum == -1728 {
                    let cmd = "curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh | bash"
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(cmd, forType: .string)
                    NotificationCenter.default.post(
                        name: .updaterFallbackToClipboard, object: nil
                    )
                }
            }
        }
    }

    // MARK: - Private

    private func bundledVersion() -> String {
        return (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "0.0.0"
    }

    private func fetchLatestTag() async throws -> String {
        let url = URL(string: "https://api.github.com/repos/\(Self.repoOwner)/\(Self.repoName)/releases/latest")!
        var req = URLRequest(url: url)
        req.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        req.timeoutInterval = 10
        let (data, response) = try await URLSession.shared.data(for: req)
        // Check HTTP status before decode so rate-limit (403) and unexpected
        // server errors surface as distinct errors, not as JSON decode failures.
        guard let http = response as? HTTPURLResponse,
              (200..<300).contains(http.statusCode) else {
            let code = (response as? HTTPURLResponse)?.statusCode ?? -1
            throw UpdateCheckerError.badStatus(code)
        }
        struct Release: Decodable { let tag_name: String }
        let r = try JSONDecoder().decode(Release.self, from: data)
        // Strip leading "v" if present.
        return r.tag_name.hasPrefix("v") ? String(r.tag_name.dropFirst()) : r.tag_name
    }

    enum UpdateCheckerError: Error { case badStatus(Int) }

    /// Tolerant SemVer comparison. Handles "0.5.0" vs "0.4.6" as well as build
    /// suffixes ("0.5.0+1") by stripping the suffix.
    static func isNewer(remote: String, bundled: String) -> Bool {
        func parts(_ s: String) -> [Int] {
            let base = s.split(separator: "+").first.map(String.init) ?? s
            return base.split(separator: ".").compactMap { Int($0) }
        }
        let r = parts(remote)
        let b = parts(bundled)
        let n = max(r.count, b.count)
        for i in 0..<n {
            let rv = i < r.count ? r[i] : 0
            let bv = i < b.count ? b[i] : 0
            if rv != bv { return rv > bv }
        }
        return false
    }
}
```

### 4. MenuBarController dot compositing (v2 — proportional, not hardcoded 22×22)

Adds a small orange dot to the top-right of the existing template icon when `updateBadgeVisible == true`. Sizing is derived from `NSStatusBar.system.thickness` so the dot scales correctly across Retina and the various macOS menubar heights.

```swift
// In MenuBarController.swift

private(set) var updateBadgeVisible: Bool = false

func setUpdateBadge(visible: Bool) {
    guard updateBadgeVisible != visible else { return }
    updateBadgeVisible = visible
    updateIcon()
}

// In updateIcon() — augment the DEFAULT branch (not meeting-recording, which
// has its own composite). The .meetingRecording case at line 557 returns before
// reaching this code path, so no explicit guard needed.
//
// After: let image = NSImage(systemSymbolName: ..., accessibilityDescription: ...)
//        image?.isTemplate = true
//        button.image = image
// Change to:
//        let baseImage = NSImage(systemSymbolName: symbolName, accessibilityDescription: accessibilityLabel)
//        baseImage?.isTemplate = true
//        button.image = updateBadgeVisible
//            ? Self.compositeDot(on: baseImage)
//            : baseImage
//
// And add the helper:

private static func compositeDot(on base: NSImage?) -> NSImage? {
    guard let base else { return nil }
    // Match the system status bar thickness so the composite matches the
    // surrounding chrome on Retina / non-Retina / various menubar densities.
    // Fall back to 22 (the macOS default) if thickness returns 0 (rare).
    let dim = max(NSStatusBar.system.thickness, 22)
    let size = NSSize(width: dim, height: dim)
    let composite = NSImage(size: size)
    composite.lockFocus()
    // CRITICAL (v3 fix for round-2 blocker #25): when we set `isTemplate = false`
    // on the composite below, macOS will NOT auto-tint the underlying SF Symbol
    // for dark/light mode. If we just draw the template `base` here, it renders
    // as pure black on the dark menubar (invisible). We must tint the symbol
    // manually before drawing. `NSColor.controlTextColor` is dynamic — black on
    // light mode, white on dark mode — so the result mirrors macOS's own
    // template tint behavior.
    if let tintedBase = base.copy() as? NSImage {
        tintedBase.lockFocus()
        NSColor.controlTextColor.set()
        NSRect(origin: .zero, size: tintedBase.size).fill(using: .sourceAtop)
        tintedBase.unlockFocus()
        tintedBase.isTemplate = false
        tintedBase.draw(
            in: NSRect(origin: .zero, size: size),
            from: .zero,
            operation: .sourceOver,
            fraction: 1.0
        )
    } else {
        // Fallback if copy fails (shouldn't happen for system symbols).
        base.draw(in: NSRect(origin: .zero, size: size))
    }
    // Top-right dot, sized ≈30% of the icon and offset so it sits inside the
    // canvas, not on the edge. Proportional so it scales with `dim`.
    let dotSide = dim * 0.30
    let dotRect = NSRect(
        x: dim - dotSide - 1,
        y: dim - dotSide - 1,
        width: dotSide,
        height: dotSide
    )
    // 1px white halo so the orange remains legible on both light and dark menubars.
    NSColor.white.setFill()
    NSBezierPath(
        ovalIn: dotRect.insetBy(dx: -1, dy: -1)
    ).fill()
    NSColor.systemOrange.setFill()
    NSBezierPath(ovalIn: dotRect).fill()
    composite.unlockFocus()
    composite.isTemplate = false  // colored dot ⇒ explicit non-template
    return composite
}
```

**Alternative if the tint trick breaks** (unlikely but possible on future macOS versions): use SF Symbol's variableValue/palette rendering directly via `NSImage.SymbolConfiguration(paletteColors: [...])`. Keep the current draw approach as the primary; document this as a Plan B in the implementer's notes.

### 5. SettingsView update section

```swift
// In SettingsView.swift, inside `if showAdvanced { ... }` block (line 88-93),
// add updateSection at the top of the advanced block:

private var updateSection: some View {
    Section("Updates") {
        if let remote = settings.updateAvailable {
            HStack {
                Image(systemName: "arrow.down.circle.fill")
                    .foregroundStyle(.orange)
                Text("Update available: v\(remote)")
                    .font(.body.weight(.medium))
                Spacer()
                Button("Install now…") {
                    UpdateChecker.shared.triggerInstall()
                }
                .buttonStyle(.borderedProminent)
                .tint(.orange)
            }
            Text("Opens Terminal to download and install the latest WisprAlt. The current version will quit during the install.")
                .font(.caption)
                .foregroundStyle(.secondary)
        } else {
            HStack {
                Text("You're on v\(Self.appVersionString)")
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Check for updates") {
                    Task { await UpdateChecker.shared.check(force: true) }
                }
                .buttonStyle(.bordered)
            }
        }
    }
}

// Then in the body: if showAdvanced { updateSection ; serverSection ; ... }
```

### 6. Settings.swift additions (v2 — matches the existing `@Published + didSet` pattern)

Settings.swift has no `@UserDefault` property wrapper. Match the existing pattern:
add two `Key` enum members + two `@Published` properties + one `loadX()` private helper per key.

```swift
// In Storage/Settings.swift, inside `private enum Key { ... }` (around line 23-33),
// add two new keys:

    static let updateAvailable = "updateAvailable"
    static let lastUpdateCheck = "lastUpdateCheck"

// Then add the published properties (place near the existing `displayName`
// declaration so they group with the user-visible-only state):

/// Latest GitHub Release tag that's newer than the bundled version, or nil
/// when up-to-date. Set by UpdateChecker on each successful poll. UI surfaces
/// this in Settings → Updates and the menubar dot.
@Published var updateAvailable: String? {
    didSet {
        if let value = updateAvailable {
            defaults.set(value, forKey: Key.updateAvailable)
        } else {
            defaults.removeObject(forKey: Key.updateAvailable)
        }
    }
}

/// Epoch timestamp of the last successful UpdateChecker poll. Used to
/// debounce checks to 6h. Nil means "never checked".
@Published var lastUpdateCheck: Date? {
    didSet {
        if let value = lastUpdateCheck {
            defaults.set(value.timeIntervalSince1970, forKey: Key.lastUpdateCheck)
        } else {
            defaults.removeObject(forKey: Key.lastUpdateCheck)
        }
    }
}

// In init() — MATCH the existing _published wrapper pattern at Settings.swift:167-178.
// Place these alongside the other `self._meetingsPath = Published(initialValue: ...)` lines:

let storedUpdate = suite.string(forKey: Key.updateAvailable)
let storedLastEpoch = suite.object(forKey: Key.lastUpdateCheck) as? Double
self._updateAvailable = Published(initialValue: storedUpdate)
self._lastUpdateCheck = Published(
    initialValue: storedLastEpoch.map { Date(timeIntervalSince1970: $0) }
)
```

**Why the wrapper pattern matters:** Settings.swift comment at line 167 explicitly says "@Published properties must be set before the object is fully initialised; assign directly via stored property (bypasses didSet observers)". A naive `self.updateAvailable = ...` in init body fires didSet, which calls `defaults.set(...)` on its own property — harmless but wasteful AND breaks if defaults isn't yet writable. Use the wrapper.

### 7. AppDelegate hook

```swift
// In AppDelegate.applicationDidFinishLaunching, AFTER PermissionGate.checkAll
// launches its async Task:

UpdateChecker.shared.checkSoon()  // fires 60s later
```

### 8. Workstream 4 — `/me/dictations/last` endpoint (v2 — verified)

```python
# In server/src/wispralt_server/routes/me.py
# Extend the fastapi.responses import (line 33-34) to add JSONResponse.
# Place the new route near me_root / get_me (around line 98) — route order
# does not matter to FastAPI's exact-path dispatcher.

from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse  # ← add JSONResponse

@router.get("/dictations/last")
async def me_last_dictation(
    request: Request,
    user: User = Depends(require_api_key),
) -> JSONResponse:
    """Return the most recent dictation row for the authenticated user.

    Used by the menubar "Copy last dictation" button. The endpoint is
    employee-scoped: every user gets their OWN last row, never anyone
    else's. Admin role does not bypass this — admins also see their own
    last dictation (for cross-user analytics they use /admin/data).

    Returns 404 if the user has zero (non-deleted, non-empty) dictations.
    Break-glass admin (id < 0) gets 403 — no personal dictation row exists
    for the env-derived admin.
    """
    if user.id < 0:
        raise HTTPException(403, "Break-glass admin has no personal dictations")

    job_store: JobStore = request.app.state.job_store
    row = await asyncio.to_thread(
        job_store.get_most_recent_dictation, user.id,
    )
    if row is None:
        raise HTTPException(404, "No dictations yet")

    # `id` is CAST to TEXT inside the repo function to match Swift String decode.
    return JSONResponse({
        "id": row["id"],
        "text": row["text"],
        "created_at": row["created_at"],   # epoch float (SQLite REAL → JSON number)
    })
```

And in `server/src/wispralt_server/jobs/store.py`, add a small repository
function (mirrors `transcripts_in_range_filtered`):

```python
def get_most_recent_dictation(self, api_key_id: int) -> dict | None:
    """Return the newest non-deleted dictation row for *api_key_id*, or None.

    Selects from the same ``dictations`` table that ``/me/history`` reads.
    Always filtered by ``api_key_id`` — no cross-user leak possible.
    Honors the soft-delete semantics: rows with ``deleted_at IS NOT NULL``
    are excluded (matches the ``idx_dictations_api_key_active`` partial
    index used by ``/me/history``).

    The ``id`` is CAST to TEXT in the SELECT so the JSON response shape
    matches the Swift ``LastDictation.id: String`` decoder. Tie-break by
    ``id DESC`` on identical ``created_at`` ensures deterministic results
    (float-precision collisions on streaming-finalize timestamps are rare
    but possible).
    """
    cur = self.con.execute(
        "SELECT CAST(id AS TEXT) AS id, text, created_at "
        "FROM dictations "
        "WHERE api_key_id = ? AND text != '' AND deleted_at IS NULL "
        "ORDER BY created_at DESC, id DESC "
        "LIMIT 1",
        (api_key_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "text": row[1], "created_at": row[2]}
```

### 9. Workstream 4 — `DictationAPI.lastDictation()` (v2 — matches real ServerClient API)

```swift
// In client/WisprAlt/Server/DictationAPI.swift — add alongside the existing
// transcribe(...) methods. Mirrors MeAPI.get() pattern exactly.

struct LastDictation: Decodable {
    let id: String
    let text: String
    let created_at: Double
}

enum LastDictationAPI {
    /// `GET /me/dictations/last` — fetch the caller's most recent (non-deleted)
    /// dictation. Returns the row text + epoch timestamp. Throws
    /// `ServerError.server(status: 404, body: _)` when the user has no
    /// dictations yet.
    static func fetch() async throws -> LastDictation {
        let request = try ServerClient.shared.buildRequest(
            path: "/me/dictations/last",
            method: "GET"
        )
        let (data, _) = try await ServerClient.shared.execute(request)
        do {
            return try JSONDecoder().decode(LastDictation.self, from: data)
        } catch {
            throw ServerError.decoding(error)
        }
    }
}
```

Notes:
- `ServerClient.shared.buildRequest(path:method:)` and `execute(_:)` are the canonical helpers used by `MeAPI.swift:20-31` and `DictationAPI.swift`. They handle bearer-auth, base-URL, and status-code → `ServerError.*` mapping internally. The execute helper throws `.server(status: 404, body: _)` on 404 — caller pattern-matches that case (see §10 `performLastDictationCopy`).
- New namespace `LastDictationAPI` rather than extending `DictationAPI` so the new method doesn't get tangled in the transcribe call-site.

### 10a. Workstream 4 — `/me/login` `?next=` deep-link support (v3 — 5-line server edit)

In `server/src/wispralt_server/routes/me.py`, after a successful POST to `/me/login`, the
current code (line 236) does `RedirectResponse(url="/me/insights", status_code=303)`. Extend
this to honor an optional `next` query param:

```python
# In the POST /me/login handler — locate the existing line:
#     resp = RedirectResponse(url="/me/insights", status_code=303)
# Replace with:
next_url = request.query_params.get("next", "/me/insights")
# Safety: only allow same-origin relative paths under /me/ to prevent open-redirect.
if not next_url.startswith("/me/"):
    next_url = "/me/insights"
resp = RedirectResponse(url=next_url, status_code=303)
```

Same applies to the GET form: when the form is rendered, pass `next` into the template
context so the form's POST action preserves it, OR have the form POST hardcode `?next=`
into its `action` attribute from the GET's query param. Simplest: GET handler reads
`request.query_params.get("next")`, passes it to template ctx, the template renders
`<form action="/me/login?next={{ next }}" method="POST">`. If `next` is None, omit the
query param.

This is ~10 lines of Python total + a 1-line template change. Closes the
discoverability gap: clicking "Open My Dictations" from the menubar takes a
first-time user to login → after submitting their token they land directly on
`/me/history` instead of having to navigate from `/me/insights`.

### 10. Workstream 4 — Menubar "Copy last dictation" + "Open My Dictations" (v3 — uses /me/login?next=)

In `client/WisprAlt/UI/SettingsView.swift` `QuickActionsSection`, add a
new button at the VERY TOP of the section body (above the active-job
banner and the "Transcribe file…" button):

```swift
// Above the `if hasInFlightJob { inFlightSection }` line:

Button("Copy last dictation", systemImage: "doc.on.clipboard") {
    performLastDictationCopy()
}
.buttonStyle(.bordered)
.help("Copies the text of your most recent persisted dictation. May lag your current dictation by a second or two while it finalizes server-side.")

if let toast = copyToast, toast.button == "last-dictation" {
    Text(toast.message)
        .font(.caption)
        .foregroundStyle(.secondary)
        .transition(.opacity)
}

// And in the same Section, alongside the existing "Open Portal" button at
// the bottom (line 807-819), add a NEW button labeled "Open My Dictations".
// Note: the redirect chain on first click goes to /me/login (NOT /admin/login)
// because we deep-link directly into /me/history. After the 8h session cookie
// expires, the next click bounces back through /me/login.

// v3: route through /me/login?next=/me/history so first-time users (no session
// cookie) land on the login form and, after auth, get bounced directly to their
// dictation history (instead of /me/insights, the default landing). The 5-line
// server-side change in routes/me.py honors the `next` URL param on POST.
Button("Open My Dictations", systemImage: "list.bullet.rectangle") {
    guard let base = settings.serverURL else { return }
    // Use URLComponents so the `next` query param is correctly URL-encoded.
    var comps = URLComponents(url: base.appendingPathComponent("me/login"),
                              resolvingAgainstBaseURL: false)
    comps?.queryItems = [URLQueryItem(name: "next", value: "/me/history")]
    guard let url = comps?.url else { return }
    NSWorkspace.shared.open(url)
    Log.info("Opened /me/login?next=/me/history: \(url.absoluteString)", category: "settings")
}
.buttonStyle(.bordered)
.disabled(settings.serverURL == nil)
.help(
    settings.serverURL == nil
        ? "Set a Server URL under Advanced first."
        : "Opens your personal dictation + meeting history in the browser."
)
```

And the action handler (place near the existing `performCopy(button:source:)`):

```swift
private func performLastDictationCopy() {
    Task { @MainActor in
        do {
            let last = try await LastDictationAPI.fetch()
            let pb = NSPasteboard.general
            pb.clearContents()
            pb.setString(last.text, forType: .string)
            copyToast = ("last-dictation", "Copied — \(last.text.count.formatted(.number)) chars")
        } catch ServerError.server(status: 404, _) {
            copyToast = ("last-dictation", "No dictations yet — say something first.")
        } catch ServerError.unauthorized {
            copyToast = ("last-dictation", "API key rejected.")
        } catch {
            Log.warning("Copy-last-dictation failed: \(error)", category: "settings")
            copyToast = ("last-dictation", "Couldn't fetch — try again.")
        }
        try? await Task.sleep(nanoseconds: 1_800_000_000)
        if copyToast?.button == "last-dictation" { copyToast = nil }
    }
}
```

---

## Tasks

Ordered by workstream + dependencies. Each task is a single file-level change.

### Workstream 1 — Role tightening (server-side)

1. Edit `server/src/wispralt_server/routes/admin.py` line 162 — change `dependencies=[Depends(require_api_key)]` to `dependencies=[Depends(require_admin)]` on `/metrics`.
2. Edit `server/src/wispralt_server/routes/admin.py` line 304 — same swap on `/admin/active`.
3. Edit `server/src/wispralt_server/routes/admin.py` line 358 — same swap on `/admin/server-log/{job_id}`.
4. **Verification gate (local):** start `uvicorn wispralt_server.main:app` against the dev DB. With a non-admin token (mint one via the admin UI, or just use any existing employee token from local dev), hit each of the 3 routes via `curl -H "Authorization: Bearer <employee_token>"` and confirm 403. With the admin token, confirm 200.
4a. **Client-side 403 surfacing (added in v2, expanded in v3):** the in-app `ServerLogSheet` (SettingsView.swift:1028-1087) calls `MeetingAPI.fetchServerLog(_:)` which previously returned 200 for any authed user. After workstream 1 mini deploy, employees calling this from inside the app will get `ServerError.server(status: 403, body: _)`. Edit `client/WisprAlt/UI/SettingsView.swift` `ServerLogSheet.reload()` (around line 1071-1086) to switch on the typed error:
    ```swift
    private func reload() async {
        guard let raw = jobIDProvider() else {
            loadError = "No active job to fetch logs for."
            return
        }
        isLoading = true
        loadError = nil
        defer { isLoading = false }
        do {
            logText = try await MeetingAPI.fetchServerLog(JobID(raw: raw))
        } catch let error as ServerError {
            if case .server(let status, _) = error, status == 403 {
                loadError = "Server logs are admin-only. Ask your administrator for help."
            } else {
                loadError = "Failed to fetch log: \(error.localizedDescription)"
            }
            Log.warning("ServerLogSheet fetch failed: \(error)", category: "ui")
        } catch {
            loadError = "Failed to fetch log: \(error.localizedDescription)"
            Log.warning("ServerLogSheet fetch failed: \(error)", category: "ui")
        }
    }
    ```
    Small refactor — the existing `catch` block becomes a `catch let error as ServerError` + a generic `catch`. ~7-line change. Without it, employees clicking View Log see a raw "Server error 403" string.
5. **Mini deploy:**
   - Run `/macmini paste 'cd ~/wispralt && git pull --ff-only origin main && launchctl kickstart -k gui/$UID/co.wispralt.server'` (single bundled command via gist transport since `$` is a shifted char that CRD can't type).
   - Confirm new server PID via reverse-gist (`/macmini paste 'pgrep -f wispralt_server | head -1 > /tmp/wispralt-pid && cat /tmp/wispralt-pid'`).
   - From the dev box: `curl -fsS -H "Authorization: Bearer <employee_token>" https://transcribe.integrateapi.ai/admin/active` → expect 403.
   - Same with admin token → expect 200.

### Workstream 2 — install.sh cleanup rewrite

> **Status:** Tasks 6-11 implemented (install.sh rewritten per §2 pseudocode; `bash -n` clean; +174/-30 lines). Task 12 (decoy test) is a manual user-run step, deferred.

6. Edit `install.sh` — add the four new helper functions: `enumerate_existing_installs`, `quit_all_installs`, `remove_all_installs`, `sweep_launch_agents`. Pseudocode above is canonical — implementer can copy verbatim.
7. Edit `install.sh` `install_bundle()` — strip out the kill+rm portion (lines 192-205); leave only the hdiutil mount + cp -R + xattr strip + lock-dir mutex.
8. Edit `install.sh` `provision_credentials()` line 233 — keep the `IS_REINSTALL == 1` gate on tccutil but understand the semantics changed: `IS_REINSTALL` now means "any old WisprAlt was found anywhere," not just `/Applications/`.
9. Edit `install.sh` `main()` — reorder to: `preflight → enumerate_existing_installs → fetch_release_metadata → download_and_verify → quit_all_installs → remove_all_installs → sweep_launch_agents → install_bundle → provision_credentials → open_app_and_verify_launch → print_next_steps`.
10. Edit `install.sh` `preflight()` — add `mdfind`, `osascript`, `launchctl` to the required-tools list (they ship with macOS but should be explicit in the preflight check).
11. Remove old function `detect_reinstall` (entirely replaced by `enumerate_existing_installs`).
12. **Verification gate (decoy test on dev Mac):**
    - Before running anything: `cp -R /Applications/WisprAlt.app ~/Applications/WisprAlt-decoy.app && plutil -replace CFBundleShortVersionString -string "0.0.99" ~/Applications/WisprAlt-decoy.app/Contents/Info.plist` (note: we plant the decoy with a different display version so we can confirm it's identified, but keep the bundle id so `mdfind` finds it). Actually — `mdfind` indexes by `kMDItemCFBundleIdentifier`, so the decoy must have the same `co.wispralt.WisprAlt` bundle id, which the copy already does. Skip the plutil step.
    - Restart Spotlight indexing if the decoy is brand new: `mdimport ~/Applications/WisprAlt-decoy.app` (forces immediate indexing of the planted decoy so `mdfind` sees it without waiting for the daily Spotlight refresh).
    - Run `bash install.sh` from the repo root.
    - Confirm: `~/Applications/WisprAlt-decoy.app` is removed AND `/Applications/WisprAlt.app` is the freshly-installed bundle with the current CFBundleShortVersionString. Also confirm no orphan LaunchAgent plists remain (`ls ~/Library/LaunchAgents/co.wispralt*.plist` should return nothing — though the app re-registers on first launch, so this is racy; check AFTER deleting `~/Library/LaunchAgents/...` manually and BEFORE running install.sh).

### Workstream 4 — User-history surfacing

12a. Edit `server/src/wispralt_server/jobs/store.py` — add `get_most_recent_dictation(api_key_id: int) -> dict | None` per pseudocode. Place it near `transcripts_in_range_filtered` (lines 700-740 area). Filter strictly by `api_key_id` AND non-empty text. SQL pattern matches the existing repo style.
12b. Edit `server/src/wispralt_server/routes/me.py` — add `GET /me/dictations/last` route per pseudocode. Place ABOVE the `/history` endpoint (around line 320). Use `Depends(require_api_key)`. Reject break-glass admin (`user.id < 0`) with 403 — same pattern as `me_history`. 404 when no dictations exist.
12c. **Verification gate (local):** start uvicorn, hit `/me/dictations/last` with an employee token that has at least one dictation → expect 200 with `{"id": ..., "text": ..., "created_at": ...}`. Hit it with a fresh employee token (no dictations) → expect 404. Hit it without auth → expect 401.
12d. Edit `client/WisprAlt/Server/DictationAPI.swift` — add `LastDictation` struct + `lastDictation() async throws` static method per pseudocode. Match the existing DictationAPI auth + URL-building pattern (look at how the existing `transcribe(_:)` method assembles its URLRequest).
12e. Edit `client/WisprAlt/UI/SettingsView.swift` `QuickActionsSection` body — add the "Copy last dictation" button at the VERY TOP of the section (above `if hasInFlightJob { inFlightSection }`). Add the toast caption right below. Add the action handler `performLastDictationCopy()` near the existing `performCopy(button:source:)` method.
12f. Edit `client/WisprAlt/UI/SettingsView.swift` `QuickActionsSection` body — add the "Open My Dictations" button alongside the existing "Open Portal" button. Targets `<serverURL>/me/login?next=/me/history` via URLComponents (see §10 pseudocode).
12f-bis. Edit `server/src/wispralt_server/routes/me.py` to honor `?next=` URL param on `/me/login` POST (see §10a pseudocode). ~10 lines. Templates may need a 1-line tweak to preserve `next` across the login form submit.
12g. **Verification gate (manual on dev Mac):**
    - Build client via `./scripts/build-client-local.sh`.
    - Launch the new build, hold FN, dictate a quick "test dictation" — confirm it lands in the Mac mini's `/transcribe/dictate` and persists to the dictations table (check `~/Library/Logs/WisprAlt/server.err.log` on the mini OR query directly).
    - In the menubar popover, click "Copy last dictation" — confirm the toast shows "Copied — N chars" and `pbpaste` returns the dictation text.
    - Click "Open My Dictations" — confirm browser opens to `https://transcribe.integrateapi.ai/me/history` and shows the rows (will require an active session cookie — if employee isn't logged into the portal yet, they'll be redirected to /admin/login which is the expected behavior).

### Workstream 3 — In-app updater

13. Create `client/WisprAlt/Update/UpdateChecker.swift` — paste the pseudocode above. Module: `final class UpdateChecker { static let shared = UpdateChecker() }`. Uses `URLSession.shared` for the GitHub API call (no auth needed for public releases; rate limit is 60 req/hour per IP unauthenticated, which is fine for a 6h-debounced check).
14. Edit `client/WisprAlt/Storage/Settings.swift` — add two properties: `updateAvailable: String?` and `lastUpdateCheck: Date?`. Match the existing property-wrapper pattern in that file (re-read it first to confirm — if there's no wrapper, use `UserDefaults.standard.string(forKey:)` / `set(_:forKey:)` accessors).
15. Edit `client/WisprAlt/App/MenuBarController.swift` line ~553 (`updateIcon()`) — add `updateBadgeVisible` property and `setUpdateBadge(visible:)` method as shown in pseudocode. Inject the `compositeDot(on:)` helper at the class level. Modify the default branch of the switch to call `compositeDot(...)` when `updateBadgeVisible == true`. Important: when in `.meetingRecording` mode, suppress the badge — meeting recording's red REC composite is too important to overlay; check by guarding with `updateBadgeVisible && mode != .meetingRecording`.
16. Edit `client/WisprAlt/UI/SettingsView.swift`:
    - Add `private var updateSection: some View { ... }` matching pseudocode.
    - In `body` at line 88, change `if showAdvanced { ... }` to start with `updateSection` BEFORE the existing `serverSection`.
    - Add `@EnvironmentObject` reference (already there) — settings.updateAvailable will be Optional<String>.
17. Edit `client/WisprAlt/App/AppDelegate.swift` `applicationDidFinishLaunching(_:)` — add `UpdateChecker.shared.checkSoon()` after the PermissionGate.checkAll Task spawns. Specifically: search for `PermissionGate.checkAll` invocation and add the new call on the line below it (so it runs in parallel with permission checks — they don't depend on each other).
18. **Verification gate (manual on dev Mac):**
    - Build the client: `./scripts/build-client-local.sh` — confirm it compiles.
    - Launch from `client/build/WisprAlt.app`.
    - Manually flip `Settings.shared.updateAvailable = "9.9.9"` via the Settings UI's "Check for updates" button OR by stashing a `defaults write co.wispralt.WisprAlt updateAvailable -string 9.9.9` and re-launching. Confirm:
      - Menubar dot appears within ~60s (or immediately if force-checked).
      - Settings → Advanced → "Updates" shows "Update available: v9.9.9".
      - "Check for updates" button works manually.
    - Reset via `defaults delete co.wispralt.WisprAlt updateAvailable` and confirm dot clears.
    - Click "Install now…" with the dev build — confirm Terminal opens with the curl command. **DO NOT actually run the install at this verification step** — that would overwrite the build under test. Just verify the Terminal command appears and matches the canonical one. Cmd-W the Terminal window.

### Documentation

19. Edit `docs/INSTALL.md` — add a paragraph in the troubleshooting section noting that v0.5.0+ removes old WisprAlt installs from ANY location (including `~/Applications/`) and that the curl command is idempotent.
20. Edit `docs/ARCHITECTURE.md` — add a new "In-app updater" subsection under the client architecture section, ~5 sentences: where checks run, how the dot is rendered, where Install now leads (Terminal + canonical curl).
21. Edit `docs/ARCHITECTURE.md` — add a note in the auth section that `/admin/*` routes (including `/metrics`, `/admin/active`, `/admin/server-log/{job_id}`) require admin role; only `/me/*` is employee-accessible.
22. Edit `docs/OVERVIEW.md` (the file-to-doc map) — add an entry mapping `client/WisprAlt/Update/UpdateChecker.swift` to `docs/ARCHITECTURE.md`.
22a. Edit `docs/API.md` — add a section documenting `GET /me/dictations/last`. Brief: auth requirement, response shape, 404 semantics, scoping invariant.
22b. Edit `docs/ARCHITECTURE.md` — under "User-facing client" subsection, mention the menubar "Copy last dictation" button and the "Open My Dictations" deep link to `/me/history`.

### Release + push (v2 — REORDERED: mini deploy before tag/release publish)

The order below closes the window where a fresh employee install pulling v0.5.0 would hit `/me/dictations/last` returning 404 (because the mini server is still on v0.4.6 server code without that endpoint). By deploying the mini FIRST, the new endpoint is live before the DMG is publishable.

23. Confirm `git status` is clean (no uncommitted tmp/* changes blocking release-client.sh's clean-tree check). If matrix files or anything else is dirty, commit them in a separate `chore(*)` commit first.
24. Run `git push origin main` (user pre-approved push; show the diff summary before pushing per CLAUDE.md hard rule).
25. **Mini redeploy (server-side changes go live FIRST):** `/macmini paste` the canonical `cd ~/wispralt && git pull --ff-only origin main && launchctl kickstart -k gui/$UID/co.wispralt.server` sequence (gist transport per CLAUDE.md — CRD strips `$`/`/`). Reverse-gist confirm: `git rev-parse HEAD` matches the head you just pushed AND `pgrep -f wispralt_server` returns the new PID.
26. **End-to-end live verify:** with an employee token, hit each of the 3 tightened routes → expect 403. With admin token → expect 200. Hit `/me/dictations/last` with an employee that has dictations → expect 200. With one that doesn't → 404. Run a tiny `/transcribe/dictate` roundtrip to confirm nothing regressed.
27. **Build + publish the GitHub Release:** Run `./scripts/release-client.sh 0.5.0` — bumps Info.plist to 0.5.0, builds DMG, signs, tags `v0.5.0`, pushes the tag, creates the GitHub Release with DMG + sha256 sidecar. (Script accepts bare `0.5.0` per recent commit history e.g. v0.4.5.)
28. **Verify release published:** `curl -fsSL https://api.github.com/repos/omdiidi/miniWhisper/releases/latest | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['tag_name'])"` → expect `v0.5.0`.
29. **Sanity-check curl install:** `curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh > /tmp/install-check.sh && grep -q "v0.5.0" /tmp/install-check.sh || head -20 /tmp/install-check.sh` — verifies the install.sh on `main` is the new bundle-id-driven version (look for `mdfind` mentions). Not strictly required since `releases/latest` already returns v0.5.0, but good belt-and-suspenders.
30. **Final handoff:** Print the curl command for the user: `curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh | bash`. Mention that for new employees, the admin still mints the token in `/admin/users` first and gets a pre-baked curl one-liner from the `employee_added.html.j2` template (with `WISPRALT_API_KEY=<token>` env prefix). For existing employees, the keyless form above re-uses their Keychain.

---

## Validation Gates (executable)

| Gate | When | Pass criteria |
|---|---|---|
| Server unit reachability | After workstream 1 mini deploy | `curl -H "Authorization: Bearer <employee>" https://transcribe.integrateapi.ai/admin/active` returns 403. Same with admin token returns 200. |
| Server unit reachability | After workstream 1 mini deploy | `curl -H "Authorization: Bearer <employee>" https://transcribe.integrateapi.ai/metrics` returns 403. |
| Server unit reachability | After workstream 1 mini deploy | `curl -H "Authorization: Bearer <employee>" https://transcribe.integrateapi.ai/admin/server-log/<any-job-id>` returns 403. |
| Server regression | After workstream 1 mini deploy | A tiny WAV `/transcribe/dictate` roundtrip with an employee token still succeeds. |
| install.sh decoy clean | After workstream 2 | After planting `~/Applications/WisprAlt-decoy.app` and running `bash install.sh`, the decoy is gone. |
| install.sh canonical install | After workstream 2 | `/Applications/WisprAlt.app` exists, runs, and shows the current version in Settings → Advanced. |
| install.sh LaunchAgent sweep | After workstream 2 | Planted `~/Library/LaunchAgents/co.wispralt.decoy.plist` is removed by the sweep. |
| /me/dictations/last roundtrip | After workstream 4 mini deploy | Authed employee with a dictation → 200 JSON. Authed employee without → 404. Unauthed → 401. Break-glass admin → 403. |
| Menubar "Copy last dictation" | After workstream 4 client build | After a fresh FN-dictation roundtrip, clicking the button toasts "Copied — N chars" and pbpaste contains that text. |
| Menubar "Open My Dictations" | After workstream 4 client build | Click opens `<serverURL>/me/history` in the browser (may bounce through /admin/login if no session cookie yet). |
| Client compile | After workstream 3 | `./scripts/build-client-local.sh` builds without errors. |
| In-app updater detect | After workstream 3 manual smoke | With a stashed `updateAvailable = "9.9.9"`, menubar dot appears and Settings section shows the banner. |
| In-app updater action | After workstream 3 manual smoke | "Install now…" opens Terminal with the canonical curl one-liner (do not execute). |
| Release published | After workstream 3 | `releases/latest` returns `v0.5.0`. |
| Final handoff | After workstream 3 | The `curl ... | bash` one-liner installs v0.5.0 cleanly on a fresh-feeling dev Mac (decoy bundle on it removed too). |

---

## Risks & Mitigations

1. **`mdfind` indexing delay** — if Spotlight hasn't indexed a freshly-planted bundle, `mdfind` returns nothing and the decoy survives. Mitigation: the test gate includes `mdimport <path>` to force immediate indexing of the planted decoy. Real-world employees won't have this issue because their old install has been on disk long enough to be indexed.
2. **`pkill -f "co.wispralt.WisprAlt"` is broad** — could in theory kill an unrelated process whose command line happens to contain "co.wispralt" (e.g., a developer running `grep co.wispralt` in another terminal). Mitigation: the AppleScript graceful quit runs first and is bundle-id-scoped. `pkill` is only the fallback after the 2s grace window. Risk is real but tiny.
3. **`SMAppService` registration carryover after delete** — when we remove `~/Applications/WisprAlt-decoy.app`, the LoginItem entry pointing at that path may persist in `SMAppService` state. macOS usually invalidates these but not always. Mitigation: the app re-registers on first launch via `SMAppService.mainApp.register()` (AppDelegate.swift:62) with the canonical `/Applications/` path. Stale entries pointing at gone paths are inert.
4. **Server log fetch from employee app now 403s** — `ServerLogSheet` in `SettingsView.swift` will start returning 403 for non-admin users. Mitigation: catch 403 in `MeetingAPI.fetchServerLog` and surface "Admin-only — ask your administrator" in the sheet. ALTERNATIVE: build `/me/server-log/{job_id}` — deferred to a follow-up plan if this becomes a real ask.
5. **GitHub API rate limit on update check** — unauthenticated requests get 60/hour per IP. With 6h debounce per machine, we use 4 req/day/machine — well under limit. Multiple employees on the same office IP are fine too.
6. **NSAppleScript Terminal.app launch on macOS Sonoma+ Sequoia/Tahoe** — Terminal automation requires Automation permission. First-time use shows a TCC prompt. Mitigation: this is expected and a one-time user gesture — the prompt copy clearly says "WisprAlt wants to control Terminal" which is what we want.
7. **Menubar dot template-image conflict** — `compositeDot` returns `isTemplate = false` because the orange dot is non-grayscale. macOS will render the icon WITHOUT the system's auto-tint when this is false, which means the underlying mic glyph won't switch to black/white on dark/light mode. Mitigation: only the badged version loses templating; un-badged version stays template. The orange dot is intentionally visible on both light and dark menubars.
8. **release-client.sh version check** — refuses if the tag already exists locally or on GitHub. Confirm `v0.5.0` does not exist before running.
9. **install.sh `osascript -e 'tell application id ...'` requires AppleScript permission?** — AppleScript run from a curl-piped bash script does NOT require explicit user grants (it's not the same as scripting another GUI app's internals). The `quit` event is a public Apple Event and works without any TCC prompts. Confirmed against macOS 14+ behavior.
10. **What if mdfind is disabled (Spotlight off)?** — Vanishingly rare on employee Macs. Mitigation: `mdfind` returning empty just means no installs detected; we fall through to clean install at `/Applications/`. Worst case: orphan at the non-canonical path survives. Document as a known limitation in `docs/INSTALL.md`.
11. **`get_most_recent_dictation` SQL ordering on identical timestamps** — if two dictations share the same `created_at` (possible with float precision), the `ORDER BY created_at DESC LIMIT 1` is non-deterministic. Mitigation: tie-break by `id DESC` — change ORDER BY clause to `ORDER BY created_at DESC, id DESC`. Edge-case but cheap to fix.
12. **`Open My Dictations` requires portal session cookie** — clicking the button without a prior `/admin/login` POST means the user lands on the login form, not `/me/history`. Mitigation: this is the existing behavior of all portal links and matches what `Open Portal` already does. After first login the cookie persists for 8h. Acceptable UX.
13. **Last-dictation could be very large** — a 10-minute dictation can produce thousands of chars. Copying that to the pasteboard is fine (NSPasteboard handles megabytes), but the toast "Copied — N chars" formatting via `.formatted(.number)` is decimal-grouped which is what we want. No size cap needed.
14. **Streaming-finalized rows may not be "current"** — streaming writes the row at finalize-POST time. If the user holds FN, releases, immediately clicks "Copy last dictation" within the 1-3s the finalize POST is in flight, `/me/dictations/last` returns the PREVIOUS dictation. Mitigation: the help text on the button warns "may lag your current dictation by a second or two." User can dictate again and re-click; not a correctness bug.
15. **v0.4.6 employees do NOT auto-get the install.sh cleanup** — they have no in-app updater yet, so they must manually re-run the curl one-liner to get the orphan-bundle cleanup. Mitigation: document this in the v0.5.0 release notes + "tell employees to re-run the curl command once" instruction in the final handoff. After v0.5.0, the in-app updater closes this loop automatically.
16. **`releases/latest` cached aggressively by GitHub edges** — usually fine but can be ~30s stale right after publish. Validation gate at step 28 should retry with `--max-time 10 --retry 3 --retry-delay 5` if the first attempt sees the old tag.
17. **Release-script failure mid-rollout** — if Task 27 (`release-client.sh 0.5.0`) fails AFTER Task 25's mini deploy, the system is in a partial state: mini server runs v0.5.0 code (3 routes now require admin role; `/me/dictations/last` exists; `/me/login?next=` honored) but no v0.5.0 DMG is published. v0.4.6 employees in this window will (a) see "Server error 403" in the View Server Log sheet because they don't yet have the 4a 403-handling code, and (b) still have orphan-bundle risk because they don't yet have the new install.sh.
   **Rollback recipe:** SSH to mini → `cd ~/wispralt && git log --oneline -5` to identify the v0.4.6 commit → `git reset --hard <v0.4.6-commit> && launchctl kickstart -k gui/$UID/co.wispralt.server`. After rolling back the mini, fix whatever broke release-client.sh (most commonly: Tahoe codesign xattr race per CLAUDE.local.md seq 21; recover by `git checkout HEAD -- client/WisprAlt/Info.plist` and re-running). Retry release-client.sh until it succeeds, then `git push origin main` + redeploy mini in the original v3 order.
   **Better mitigation (deferred to a follow-up):** ship the role-tightening 4a client-side error handling as a back-portable v0.4.7 client first so v0.4.6 employees can update BEFORE the server role-gate change goes live. Out of scope for this plan — accept the rollback as the contingency.

---

## Deprecated Code (To Remove)

- `detect_reinstall()` function in `install.sh` (replaced by `enumerate_existing_installs`).
- The hard-coded pkill pattern `/Applications/WisprAlt.app/Contents/MacOS/WisprAlt` in `install.sh:194,197,200,202` (replaced by bundle-id-driven `pkill -f co.wispralt`).
- Nothing client-side is deprecated. `SparkleController.swift` stays as-is (disabled but useful documentation of the path-not-taken).

---

## Out of Scope (Future Work)

- **`/me/server-log/{job_id}` per-user log endpoint** — only build if employees actively report missing logs from the in-app sheet.
- **Self-serve signup** — admin-mediated onboarding is the intended model.
- **Code-signed appcast / Sparkle path** — requires Apple Developer Program enrollment + notarization. Not blocked on; just a different deployment philosophy.
- **Automatic install on update detection** (skip the "click to install" gesture) — explicitly rejected per brief; visible Terminal install is more debuggable for non-technical employees.
- **In-app version-downgrade guard** — not blocking. Add if/when downgrade burns someone.
- **GitHub Action that publishes release on tag push** — currently `release-client.sh` does this locally; CI version is a nice-to-have but not blocking.

---

## Implementation Sequence Summary

```
Workstream 1 (server role tighten)        — ~30 min  → mini redeploy → curl verify 403s
Workstream 2 (install.sh hardening)       — ~1.5h    → decoy test on dev Mac
Workstream 4 (user-history surfacing)     — ~1h      → server+client; uvicorn-local-test + menubar smoke
Workstream 3 (in-app updater)             — ~2-3h    → build → manual smoke → release-client.sh 0.5.0
Push to main + final curl one-liner        — ~10 min
```

Total wall-clock: ~5-6 hours.

---

## Final Deliverable

After all gates pass:

```bash
# Hand this to employees (and sister):
curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh | bash
```

(Per-employee tokens come from the admin UI mint flow; the curl one-liner above is the no-token form for users who already have their Keychain set up. Token-baked-in form for new employees is generated by `routes/admin_ui.py:_build_install_command()`.)
