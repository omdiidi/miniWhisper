# Brief: Fix the `curl | bash` installer so it works on a truly fresh Mac

## Why
The `install.sh` one-liner (`curl -fsSL .../main/install.sh | ... bash`) was run on a Mac mini
that had **never** had WisprAlt installed and failed. Two real blockers + one cosmetic noise line
showed up — and the through-line is that the v0.5.0 installer was never exercised on a genuinely
fresh machine:

- Run 1 died at preflight: `python3 not available. Run: xcode-select --install`.
- Run 2 (after CLT install) got to "Verifying SHA256" then crashed: `bash: line 202:
  FOUND_BUNDLES[@]: unbound variable`.
- Both runs printed a confusing first line: `curl: (6) Could not resolve host:` (empty host).

Goal: make the installer Just Work on a clean Apple-Silicon Mac with zero pre-steps.

## Context
- Repo: `omdiidi/miniWhisper` (the user calls it "whispralt"). Cloned to the working dir.
- The published one-liner fetches `install.sh` from **`main`** (raw.githubusercontent.com/.../main/install.sh),
  NOT from a release asset. So fixing `install.sh` on `main` makes the installer live immediately —
  **no new DMG release needed** for the script fix. (The DMG release v0.5.0 is the app binary, separate.)
- `install.sh` uses `set -euo pipefail` (line 5). macOS ships **bash 3.2.57** forever; the script must
  assume 3.2. Local machine is also bash 3.2.57, so all fixes were reproduced/verified here.
- The client app is a **native Swift package** — 51 `.swift` files under `client/WisprAlt` +
  `client/WisprAltCore`, ships as a compiled `WisprAlt.app` in the DMG. **Zero** python references
  anywhere in the client source (verified by grep). The app does NOT need python3 to open.
- `python3` in `install.sh` is used for exactly two installer-time things: the preflight gate (line 64)
  and parsing the GitHub `/releases/latest` JSON (lines 266–284). Nothing else. (The FastAPI **server**
  on the mini is python, but that's installed by `setup-server` and is unrelated to client `install.sh`.)
- `osascript` (`/usr/bin/osascript`) is already in the installer's required-tools preflight list (line 68).

### Verified facts (reproduced on bash 3.2.57)
- Empty-array value expansion `"${arr[@]}"` under `set -u` in **bash 3.2** throws `unbound variable`.
  The *count* `${#arr[@]}` is fine; the *value* expansion is not. (Fixed in bash 4.4; macOS never ships it.)
- `FOUND_BUNDLES=()` at line 30 (with its comment "initialized so set -u doesn't bite") is a
  **label-not-mechanism** trap: initializing the array does NOT make `"${FOUND_BUNDLES[@]}"` safe in 3.2.
- Both fix idioms verified working in 3.2: count-guard `(( ${#A[@]} > 0 ))` and
  expansion idiom `${A[@]+"${A[@]}"}`.
- JXA replacement verified: `osascript -l JavaScript` parses the release JSON, extracts
  `tag_name` + `.dmg` url + `.dmg.sha256` url, enforces the no-whitespace guard, and emits the same
  tab-delimited `tag\tdmg\tsha` that the existing `IFS=$'\t' read` (line 291) already consumes.

## Decisions

### Fix 1 — `FOUND_BUNDLES[@]: unbound variable` (THE blocker; bash 3.2 empty-array crash)
- **`remove_all_installs()` (line ~202):** add `(( ${#FOUND_BUNDLES[@]} > 0 )) || return 0` as the first
  line — mirrors the guard `quit_all_installs()` already has at line 172. This is the crash the user hit:
  on a fresh Mac nothing is found, so the array is empty and the `for b in "${FOUND_BUNDLES[@]}"` loop
  blows up. Reasoning: nothing to remove → return early; matches the existing sibling pattern.
- **`enumerate_existing_installs()` loops at lines 111 and 129:** harden the dedup loops to
  `for b in ${FOUND_BUNDLES[@]+"${FOUND_BUNDLES[@]}"}; do`. Narrower trigger (an app on disk that
  Spotlight hasn't indexed yet, so the explicit/`find` path fires while `FOUND_BUNDLES` is still empty),
  but it's the same latent landmine — fix in the same pass.
- Line 140's loop is already guarded by the line-136 count check — safe; leave it (optionally normalize
  for consistency).
- Update/remove the misleading comment at lines 28–30 so the next person doesn't re-trust
  `FOUND_BUNDLES=()` as the safety mechanism.

### Fix 2 — Drop python3 → JXA (`osascript -l JavaScript`)
- **Verified safe** (app is python-free; replacement tested). Removes the Xcode-CLT requirement so a
  fresh Mac installs with zero extra steps — directly serves the goal.
- Delete the python3 preflight gate (lines 63–64).
- Replace the python3 heredoc (lines 266–284) with a JXA script that reads the release JSON file
  (pass `$json_file` as argv → `function run(argv){ ... }`, read `argv[0]`), does `JSON.parse`,
  selects the `.dmg` (excluding `appcast`) and `.dmg.sha256` assets, runs the `/[\t\r\n]/` whitespace
  guard, and prints `tag\tdmg\tsha`. Downstream `IFS=$'\t' read -r RELEASE_TAG DMG_URL SHA_URL` is unchanged.
- No preflight change for tools — `osascript` is already required (line 68).
- **Docs:** update `docs/INSTALL.md` §2 — remove the "Xcode Command Line Tools / python3" prerequisite
  (lines ~61–65).

### Fix 3 — `curl: (6) Could not resolve host:` (empty host) — NOT a script bug
- **Root cause (subagent-confirmed):** the `curl` on the LEFT of the user's pipe reads `~/.curlrc` at
  startup; a stray `url =` / `proxy =` (empty value) line on the mini makes curl queue a host-less URL,
  print `curl: (6) Could not resolve host: ` (empty host), then service the real URL. Decisive clue:
  it's line 1, before any `[wispralt]` output, once per run — and `install.sh` makes no curl call before
  its first output and never curls an empty host. It's **pure cosmetic stderr noise; it does NOT break
  the install** (the script ran past it both times; all three internal curls succeeded).
- **User action on the mini** (diagnose + clean):
  ```sh
  cat ~/.curlrc 2>/dev/null; echo "CURL_HOME=$CURL_HOME"; cat "$CURL_HOME/.curlrc" 2>/dev/null
  env | grep -i proxy
  curl -v -o /dev/null https://example.com 2>&1 | head -20   # reproduces the (6) line if ~/.curlrc is the cause
  ```
  Then delete the malformed line from `~/.curlrc` (any bare `url`/`url =`/`proxy =`/trailing valueless
  line), or `rm ~/.curlrc` if it should be empty. `curl -q ...` bypasses `.curlrc` to confirm the culprit.
- **Optional repo hardening:** add `-q` to the script's internal `curl` calls (and/or the published
  one-liner) so a broken user `~/.curlrc` can never interfere with the installer's own fetches. Quality-of-life,
  not required — flag for the user to decide.

### Fix 4 — Tighten cleanup so ANY prior install (any age / setup state) is removed for a clean install
Chosen scope: **tighten discovery + TCC** (NOT the opt-in full clean-slate). Audit findings: the script
removes the `.app` bundle robustly (3 discovery sources + quit→SIGTERM→SIGKILL + `rm -rf` + legacy
LaunchAgent sweep), and a different/newer version always takes the full remove-then-reinstall path. But two
gaps let a prior install survive "no matter what state":
- **Discovery skips half-installed/corrupt bundles.** Lines 98/108/126 require an executable
  `Contents/MacOS/WisprAlt`; a mid-copy or damaged bundle fails that and is left behind. Also an
  unindexed bundle outside `~/Downloads`/`~/Desktop`/`~/Applications` is missed by all three sources.
- **TCC reset is gated on `IS_REINSTALL`, which is only set when a bundle is found** (line 137 → 359). A
  manually-trashed prior install leaves stale Accessibility/Mic/Input-Monitoring/Screen-Recording grants.

Fix:
- **Discovery (explicit-path + `find`-sweep sources):** replace the strict `-x executable` requirement with
  a bundle-identity check that ALSO catches incomplete bundles — accept the path if
  `Contents/MacOS/WisprAlt` is executable **OR** `Contents/Info.plist`'s `CFBundleIdentifier` ==
  `co.wispralt.WisprAlt` (read via `defaults read .../Contents/Info CFBundleIdentifier` or PlistBuddy).
  For the `mdfind` source, Spotlight already matched on `kMDItemCFBundleIdentifier == co.wispralt.WisprAlt`,
  so the `-x` gate can simply be dropped there. **Safety constraint:** never `rm -rf` a path that can't be
  confirmed a WisprAlt bundle by name + bundle-id — keep the existing `/Volumes`, Time-Machine, `.Trash`
  exclusions. (A near-empty `WisprAlt.app` shell at a canonical path with no Info.plist is the one
  ambiguous case; treat a name match at `/Applications` or `~/Applications` as ours, but do not widen the
  `find` roots to arbitrary dirs on a name-only match.)
- **TCC ungating — broaden "is this a reinstall?" detection:** set `IS_REINSTALL=1` if a bundle was found
  **OR** orphaned state exists — a Keychain `co.wispralt` entry (`security find-generic-password -s
  co.wispralt`), or the `co.wispralt.WisprAlt` UserDefaults/Preferences plist
  (`~/Library/Preferences/co.wispralt.WisprAlt.plist` / `defaults read co.wispralt.WisprAlt`). Then the
  existing TCC reset fires for manually-deleted priors too. A genuinely-fresh machine has none of these →
  stays `IS_REINSTALL=0` → no TCC reset → no spurious 4-permission re-prompt. No new UX regression; just
  wider, more honest reinstall detection.
- **Leave Keychain + UserDefaults wipe OUT** (provision already overwrites `serverURL`, and the key is only
  overwritten when a new one is passed). Wiping them unconditionally would destroy a working API key on the
  key-less self-install re-run (`INSTALL.md` §5) — unsafe as a default.

## Rejected Alternatives
- **Keep python3 as a documented prerequisite (minimal patch):** smallest change, only fix the crash.
  Rejected because the stated goal is "works on a fresh Mac with nothing installed," and requiring a
  ~700MB Xcode-CLT download just to parse one JSON blob defeats that.
- **Parse the release JSON with grep/sed/awk:** fragile against JSON formatting/ordering; rejected in
  favor of a real JSON parser (JXA) that ships on every Mac.
- **Chase the empty-host curl as a script bug:** rejected — proven environmental (`~/.curlrc` on the mini).
  install.sh can't emit it; only optional `-q` hardening lives in the repo.
- **Full clean-slate wipe as the DEFAULT (delete Keychain + UserDefaults domain + `~/Library` state on every
  run):** rejected as default — a key-less self-install re-run (`INSTALL.md` §5) would destroy a working API
  key, and a blanket TCC reset would re-prompt 4 permissions on every routine update. The opt-in
  `WISPRALT_CLEAN=1` "factory fresh" variant was offered and **deferred** (not in this scope) — revisit only
  if a true uninstall path is later needed.

## Where Reasoning Clashed
- **Drop python3 vs. minimal crash-only patch.** A reasonable person optimizing for the *smallest, safest
  diff* would fix only `FOUND_BUNDLES` and leave python3 (it's already documented, and the JXA rewrite adds
  its own parsing/edge-case surface to verify). We chose to drop python3 because the user's explicit目标 is a
  zero-pre-step fresh-Mac install, and the JXA replacement was verified end-to-end including the security
  whitespace guard. Both positions are defensible; we picked the one that matches the stated goal, having
  retired most of the risk by testing the replacement first.

## One Thing to Do First
Add `(( ${#FOUND_BUNDLES[@]} > 0 )) || return 0` as the first line of `remove_all_installs()` in
`install.sh` — that single guard stops the crash the user actually hit on the fresh mini.

## Direction
Patch `install.sh` on `main` in one pass: (1) make every empty-`FOUND_BUNDLES` value-expansion
bash-3.2-safe; (2) replace python3 JSON parsing with JXA `osascript -l JavaScript` and drop the
python3/Xcode-CLT preflight gate + docs prerequisite; (3) tighten cleanup so any prior install is removed
regardless of setup state — accept incomplete bundles by Info.plist bundle-id (not just an executable) and
broaden `IS_REINSTALL` detection (orphaned Keychain/defaults) so TCC gets reset even after a manual delete,
while leaving Keychain/UserDefaults wipe out of the default path. The empty-host `curl` line is the user's
`~/.curlrc` on the mini (cosmetic) — clean it there; optionally add `-q` to the installer's curls as
belt-and-suspenders. Because the one-liner pulls from `main`, committing+pushing makes the fix live with no
new release.
