# Plan: Fix the WisprAlt `curl | bash` installer for a truly fresh Mac

## Goal

Make `install.sh` (fetched from `main` by the published one-liner) install cleanly on an
Apple-Silicon Mac that has **never** had WisprAlt — no Xcode Command Line Tools, no prior install,
no surprises. Kill the bash-3.2 empty-array crash, remove the python3/Xcode-CLT dependency, tighten
prior-install cleanup so a leftover install in *any* state is reset before the new app goes in,
silence the cosmetic empty-host `curl` line for future installs, and add a fail-closed CI guard so
the crash class can't reship.

## Summary

All changes in `install.sh` plus doc edits and one new CI workflow:

1. **Crash fix** — guard every empty-`FOUND_BUNDLES` value-expansion so `set -u` on bash 3.2 can't
   throw `unbound variable` (the actual blocker). Add a source-guard so the functions are testable.
2. **Drop python3** — replace the GitHub-release JSON parse with `osascript -l JavaScript` (JXA);
   delete the python3 preflight gate + the now-obsolete docs.
3. **Tighten cleanup** — discover incomplete bundles by `Info.plist` bundle-id; reset TCC even after
   a manual delete; **add `rm -rf "$app_path"` before `cp -R`** so a discovery miss can never nest
   the bundle; and **stop the same-version no-op re-run from re-prompting all 4 TCC permissions**.
   Keychain/UserDefaults wipe intentionally stays out of the default path.
4. **`curl -q` hardening + the noise line** — `-q` on the installer's own fetches AND on the
   published one-liner (the only lever that actually silences the empty-host `curl: (6)` line);
   replace the python3 troubleshooting entry with a curl-noise note.
5. **Fail-closed CI guard** — a tiny `lint-install.yml` (macos-14 → bash 3.2) running `bash -n` + an
   empty-`FOUND_BUNDLES` smoke test, so the silent crash class can't reship.

Because the one-liner pulls `install.sh` from `main`, committing + pushing makes the fix live — **no
new DMG release required** — and a post-push wire check proves the patched bytes actually reached
`raw.githubusercontent.com`.

## Intent / Why

- Employee-facing install is the `install.sh` curl one-liner (`CLAUDE.md:24`). A broken installer
  blocks every new employee onboarding.
- v0.5.0's installer shipped untested on a genuinely fresh Mac: it dies at the python3 preflight
  (run 1) and crashes on `FOUND_BUNDLES[@]: unbound variable` (run 2) — both "fresh machine" bugs.
- What must remain true regardless of implementation: runs under macOS's frozen **bash 3.2.57** with
  `set -euo pipefail`; needs no non-stock tooling; never destroys a *working* API key on a key-less
  re-run; never re-prompts TCC permissions on a true no-op re-run.

## Source Artifacts

- Brief / intent artifact: `./tmp/briefs/2026-06-18-fix-curl-install.md`
- Research dossier: none (investigation inline in the brief; key assumptions pre-verified — see
  Known Gotchas, every item tagged "pre-verified" was run on this bash-3.2.57 machine).

## What

After this change, on a fresh Apple-Silicon Mac with no Xcode CLT and no prior install, the
one-liner installs WisprAlt end-to-end with zero extra steps and no crash. A prior install in any
state (complete, half-copied, or manually-trashed) is cleaned/reset before the new app is installed.
A routine same-version re-run does not re-prompt permissions. The installer's own fetches and the
published command both ignore a broken `~/.curlrc`.

### Success Criteria

- [ ] `bash -n install.sh` passes.
- [ ] With the source-guard added, sourcing `install.sh` and calling `remove_all_installs` /
      `enumerate_existing_installs` with an **empty** `FOUND_BUNDLES` under `set -euo pipefail` on
      bash 3.2.57 does NOT throw `unbound variable` and does NOT run `main`.
- [ ] `grep -n python3 install.sh` is empty.
- [ ] JXA parse extracts `tag\tdmg\tsha` from a real `/releases/latest` payload, ignores appcast,
      exits non-zero on malformed JSON.
- [ ] `is_wispralt_bundle` returns 0 for a real `WisprAlt.app` (or one with our `Info.plist`
      bundle-id) and non-zero for a decoy dir named `WisprAlt.app` with no exec and no matching id.
- [ ] On a genuinely fresh machine both orphan probes exit non-zero → `IS_REINSTALL` stays 0.
- [ ] `IS_REINSTALL` becomes 1 when only orphaned Keychain/defaults exist (no bundle on disk).
- [ ] Same-version skip path leaves TCC untouched (no 4-permission re-prompt).
- [ ] `install_bundle` removes `/Applications/WisprAlt.app` before `cp -R` (no nesting possible).
- [ ] Every published `curl -fsSL …/install.sh` one-liner across README + docs uses `curl -q -fsSL`.
- [ ] Client docs no longer list Xcode CLT / python3 as a prerequisite; server-side Python refs
      untouched.
- [ ] `lint-install.yml` runs `bash -n` + the empty-array smoke on macos-14.
- [ ] Post-push: `curl -fsSL …/main/install.sh | grep -q python3` returns non-zero (no python3 on
      the wire).

## Verified Repo Truths

### Execution / Async Flow

- Fact: Script runs under `set -euo pipefail`; the last line is a bare `main "$@"` (not sourceable).
  Evidence: `install.sh:5`, `install.sh:517`.
  Implication: empty-array expansions are fatal; a source-guard is needed to unit-test the functions.

- Fact: `remove_all_installs()` value-expands `FOUND_BUNDLES` with no count guard.
  Evidence: `install.sh:202-208` (loop at `:204`).
  Implication: empty array on a fresh Mac → bash 3.2 `unbound variable` crash (the user's blocker).

- Fact: `enumerate_existing_installs()` value-expands `FOUND_BUNDLES` at `:111` and `:129` while it
  may still be empty; the loop at `:140` is guarded by the count check at `:136`;
  `should_skip_install` value-expands `${FOUND_BUNDLES[0]}` at `:155` but is protected by the
  short-circuit count check at `:154`.
  Evidence: `install.sh:111`, `:129`, `:136`, `:140`, `:154-155`.
  Implication: `:111`/`:129` are latent crashes; `:140`/`:155` are already guarded (preserve that
  ordering).

- Fact: `quit_all_installs()` guards with `(( ${#FOUND_BUNDLES[@]} > 0 )) || return 0`.
  Evidence: `install.sh:172`
  Implication: in-repo pattern to mirror.

### Entry Points / Integrations

- Fact: python3 is used only at the preflight gate and the release-JSON parse (parse reads stdin via
  `< "$json_file"`); the empty-field defense lives downstream at `:293-295`
  (`[[ -n "$RELEASE_TAG" ]] || die`, etc.), after the 3-field check at `:286-291`.
  Evidence: `install.sh:63-64`, `:266-284` (parse, stdin at `:284`), `:286-291`, `:293-295`.
  Implication: JXA replacement must emit the identical `tag\tdmg\tsha`; lines `:293-295` MUST be kept
  (they catch valid-but-wrong-shape JSON that yields empty fields).

- Fact: `osascript` is already required in preflight; `python3` is NOT in the tool list.
  Evidence: `install.sh:68`.
  Implication: switching to JXA needs no preflight tool-list change; only `:63-64` are removed.

- Fact: `defaults read <bundle>/Contents/Info <Key>` (path form, no `.plist`) is already used
  successfully in-repo.
  Evidence: `install.sh:160` (`defaults read /Applications/WisprAlt.app/Contents/Info
  CFBundleShortVersionString`).
  Implication: `is_wispralt_bundle` can reuse this exact form; no PlistBuddy needed.

### Data / State

- Fact: All three discovery sources gate on an executable `Contents/MacOS/WisprAlt`; the `mdfind`
  source already pre-filters by `kMDItemCFBundleIdentifier == co.wispralt.WisprAlt`.
  Evidence: `install.sh:98` (mdfind), `:108` (explicit), `:126` (find), `:100` (mdfind query).
  Implication: a half-copied bundle (no exec) is skipped; for mdfind Spotlight already proved the
  id, so the exec gate can be dropped there outright.

- Fact: `install_bundle()` runs `cp -R "$mount/WisprAlt.app" "/Applications/"` with NO prior
  `rm -rf` of the destination; removal depends entirely on `remove_all_installs`.
  Evidence: `install.sh:341-345`.
  Implication: if discovery misses a degenerate `/Applications/WisprAlt.app`, `cp -R` nests it as
  `WisprAlt.app/WisprAlt.app`. `docs/SETUP-CLIENT.md:340-341` already *promises* "rm -rf then cp -R
  — never nests," so the code currently contradicts the doc.

- Fact: `IS_REINSTALL` is set to 1 only when a bundle is found; the TCC reset in
  `provision_credentials` is gated on it; the same-version skip path calls `provision_credentials`.
  Evidence: `install.sh:137` (set), `:359-368` (gated reset), `:500-506` (skip path → provision).
  Implication: (a) a manually-trashed prior install leaves TCC un-reset; (b) a same-version no-op
  re-run currently DOES reset TCC and re-prompts 4 permissions (IS_REINSTALL is still 1).

- Fact: The script reads/writes Keychain service `co.wispralt` and UserDefaults domain
  `co.wispralt.WisprAlt`; it never deletes either.
  Evidence: `install.sh:12`, `:14`, `:381-384` (add `-U`), `:409` (`defaults write`).
  Search Evidence: `grep -nE 'delete-generic-password|defaults delete' install.sh` → no matches.
  Implication: orphaned Keychain/defaults are reliable "host had WisprAlt" signals; wiping them by
  default would be unsafe (key loss on key-less re-run).

### External / Operational Surface

- Fact: The internal curls are at `:247`, `:308`, `:310`. The published one-liner appears in 6 docs:
  `README.md`, `docs/DEPLOY-TEAM.md`, `docs/ADMIN.md`, `docs/SETUP-CLIENT.md`, `docs/INSTALL.md`
  (×5). CI has `build-client.yml` (macos-14) and `test-server.yml` (ubuntu); no shell-lint workflow.
  Evidence: `install.sh:247,308,310`; grep of `curl -fsSL …/install.sh` across `README.md` + `docs/`;
  `.github/workflows/`.
  Implication: `-q` belongs on internal curls (broken `~/.curlrc` can't break our fetches) AND on the
  published command (the empty-host line comes from the one-liner's left curl). macos-14 runners give
  a free bash-3.2 environment for the fail-closed guard.

### Frontend / UI (client python-dependency check)

- Fact: The client is a native Swift package (51 `.swift` files), zero python references.
  Evidence: `client/Package.swift`, `client/WisprAlt/`, `client/WisprAltCore/`.
  Search Evidence: `grep -rniE 'python|pythonkit|/usr/bin/python|Process\(' client/WisprAlt
  client/WisprAltCore` → no matches.
  Implication: dropping python3 from the installer cannot affect whether the app opens.

## Locked Decisions

- Fix all areas in one pass on `main`; no new release needed (one-liner pulls from `main`); prove the
  round-trip with a post-push wire check.
- **Cleanup scope = "tighten discovery + TCC"**; do NOT wipe Keychain/UserDefaults by default; the
  `WISPRALT_CLEAN=1` factory-reset variant is **deferred**.
- Replace python3 with **JXA** (pre-verified), not grep/sed/awk.
- **Add `-q` to the published one-liner** across all 6 docs (user decision) — silences the empty-host
  `curl: (6)` line that originates from the command's left curl.
- **Fix the same-version skip-path TCC re-prompt** (user decision) — set `IS_REINSTALL=0` on the true
  no-op skip path before `provision_credentials`.
- **Add `rm -rf "$app_path"` before `cp -R`** in `install_bundle` (review finding) — closes the
  nesting hole and makes the code match `SETUP-CLIENT.md:341`.
- **Add a source-guard + fail-closed CI smoke** for the empty-array class (review + user's
  verify-by-mechanism rule: silent-in-CI + recurrent → earns a machine-guard).
- No `/script` pre-flight (user decision) — biggest unknowns already empirically verified; manual
  fresh-Mac check on the mini covers the rest.

## Known Mismatches / Assumptions

- Mismatch: brief allowed "treat a name match at a canonical path as ours" for a degenerate empty
  `WisprAlt.app`. Plan resolves this more safely with the new unconditional `rm -rf "$app_path"` in
  `install_bundle` (closes the nesting hole regardless of discovery) rather than loosening
  `is_wispralt_bundle` to name-only matches that could delete a non-WisprAlt directory.
  Repo Evidence: `install.sh:341-345` (no pre-`cp` rm).
  Requirement Evidence: brief "Fix 4 … never rm -rf a path that can't be confirmed a WisprAlt bundle";
  `docs/SETUP-CLIENT.md:341` ("never nests").
  Planning Decision: keep `is_wispralt_bundle` conservative; guarantee no-nest via the install-time rm.

## Critical Codebase Anchors

- Anchor: `quit_all_installs()` count-guard.
  Evidence: `install.sh:172`
  Reuse / Watch for: mirror for `remove_all_installs` and the enumerate dedup loops (count-guard form,
  which is both `set -u`-safe AND space-safe — verified on 3.2.57).
- Anchor: `defaults read …/Contents/Info <Key>` path form.
  Evidence: `install.sh:160`
  Reuse / Watch for: reuse verbatim in `is_wispralt_bundle`.
- Anchor: `mdfind` post-filter (`/Volumes/*`, Time Machine, `.Trash`).
  Evidence: `install.sh:92-100`
  Reuse / Watch for: preserve these exclusions when dropping the mdfind exec gate.
- Anchor: helper trio `info()/warn()/die()`.
  Evidence: `install.sh:35-37`
  Reuse / Watch for: use for any new user-facing lines.

## All Needed Context

### Documentation & References

- Repo reference: `install.sh:233-298` (`fetch_release_metadata`) — python3 block + downstream
  tab-delimited contract (`:286-295`) to preserve.
- Repo reference: `install.sh:355-368` (`provision_credentials` TCC reset) — consumes `IS_REINSTALL`.
- Repo reference: `install.sh:488-515` (`main`) — insertion point for `detect_orphaned_setup` and the
  skip-path `IS_REINSTALL=0`.
- External: JXA Foundation bridge via `$` — `$.NSData.dataWithContentsOfFile(path)`,
  `$.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding).js`. Auto-bridged; pre-verified
  on this machine (parse, appcast-exclusion, whitespace guard, non-zero-on-bad-JSON, argv read).

### Files Being Changed

```
install.sh                      ← MODIFIED  (Fixes 1-3 + source-guard + install_bundle rm + skip-path TCC + curl -q internal)
.github/workflows/lint-install.yml ← NEW    (bash -n + empty-array smoke on macos-14)
docs/INSTALL.md                 ← MODIFIED  (drop CLT prereq; replace py3 troubleshooting w/ curl-noise note; -q on one-liners)
docs/SETUP-CLIENT.md            ← MODIFIED  (drop CLT/python3 prereq bullet :323-324; -q on one-liner)
README.md                       ← MODIFIED  (-q on one-liner)
docs/DEPLOY-TEAM.md             ← MODIFIED  (-q on one-liner)
docs/ADMIN.md                   ← MODIFIED  (-q on one-liner)
```

Explicitly NOT changed (server-side Python is legitimate):
- `docs/SETUP-SERVER.md` (server needs Python 3.11); `docs/TROUBLESHOOTING.md:28-42` (server
  `uv sync` native-wheel build); `CHANGELOG*.md` (historical record); `.github/workflows/test-server.yml`.

### Known Gotchas & Library Quirks

- **bash 3.2 empty-array trap (pre-verified):** under `set -u`, `"${arr[@]}"` / `${arr[0]}` on an
  *empty* array throws `unbound variable` on 3.2.57. `${#arr[@]}` (count) is safe. The count-guard
  `(( ${#arr[@]} > 0 )) && for … "${arr[@]}"` is both `set -u`-safe and space-safe (verified with a
  path containing a space) — standardize on it.
- **Source-guard (pre-verified):** `if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then main "$@"; fi`
  runs `main` under `cat file | bash` (piped stdin) AND `bash file.sh`, but NOT under `source` —
  verified all three modes. Required so the smoke test can source the real functions.
- **JXA argv read (pre-verified):** `osascript -l JavaScript -e 'function run(argv){…}' "$json_file"`
  → path in `argv[0]`; `$.NSData.dataWithContentsOfFile` + `.initWithDataEncoding(…,
  $.NSUTF8StringEncoding).js` → text; `JSON.parse` works; `throw` → non-zero exit (so `|| die` fires).
- **JXA input mechanism changes from stdin-redirect to argv-path** — the parser opens `$json_file`
  itself (file exists at `:236`). Behavior is equivalent-or-stricter (unreadable file → throw → die).
- **`curl -q` must be the FIRST argument** to skip `~/.curlrc`. Never move it into the `curl_args`
  array (it must literally precede the array expansion).
- **`defaults read <domain>` with no plist** exits non-zero + prints "domain not found" to stderr —
  use `>/dev/null 2>&1` as an existence probe.
- **`security find-generic-password -s co.wispralt`** (no `-a`, no `-w`) matches any account under the
  service and is non-interactive (no Keychain prompt) — keep it that way.

## Delta Design

### Execution / Control Flow — empty-array safety + source-guard

Existing: `remove_all_installs` (`:202-208`) and enumerate loops (`:111`,`:129`) value-expand
`FOUND_BUNDLES` unguarded; file ends in bare `main "$@"` (`:517`).
Change: add `(( ${#FOUND_BUNDLES[@]} > 0 )) || return 0` to `remove_all_installs`; wrap the `:111`
and `:129` dedup loops in `if (( ${#FOUND_BUNDLES[@]} > 0 )); then … fi`; rewrite the misleading
comments at `:18` and `:28-30`; replace `:517` with the source-guard.
Why: count-guard mirrors `quit_all_installs`, is uniform, and is space-safe; source-guard enables the
smoke test.
Risks: source-guard must preserve `curl | bash` — pre-verified.

### Entry Point / Integration Flow — python3 → JXA

Existing: preflight gate (`:63-64`); parse heredoc reading stdin (`:266-284`).
Change: delete `:63-64`; replace `:266-284` with the JXA block (reads `"$json_file"` as `argv[0]`,
prints `tag\tdmg\tsha`), keeping the trailing `|| die`. Preserve `:286-295` verbatim.
Why: removes the only fresh-Mac blocker forcing an Xcode-CLT download.
Risks: JXA bridge — retired by pre-verification + the `lint`/manual checks; downstream contract
unchanged (incl. `:293-295` empty-field defense).

### Data / State — cleanup tightening + no-nest + no-reprompt

Existing: three discovery sources gate on `-x`; `IS_REINSTALL` only from a found bundle; `cp -R` with
no prior rm; skip path resets TCC.
Change:
- `mdfind` source (`:98`): **drop** the `-x` gate (keep `[[ -d ]]` + the `/Volumes`/TM/`.Trash`
  exclusions) — Spotlight already proved the bundle id.
- Explicit (`:108`) + find (`:126`) sources: replace `-x` with `is_wispralt_bundle "$cand" || continue`.
- Add `detect_orphaned_setup()` (early-returns if `IS_REINSTALL==1`; else sets it to 1 when a
  `co.wispralt` Keychain entry or `co.wispralt.WisprAlt` defaults domain exists); call it in `main()`
  right after `enumerate_existing_installs`.
- Add `rm -rf "$app_path"` at the top of `install_bundle`'s copy step (before `cp -R`).
- On the skip path in `main()`, set `IS_REINSTALL=0` before `provision_credentials`.
Why: removes half-installed/corrupt bundles; resets stale permissions after a manual delete; never
nests; stops re-prompting on a true no-op — all without the unsafe blanket wipe.
Risks: `is_wispralt_bundle` must not widen `rm -rf` beyond confirmed bundles (kept by exec-or-id
check + existing path/name scoping); `detect_orphaned_setup` must be a no-op when a bundle is found
(early-return) so it doesn't widen TCC reset on the normal path.

### External / Operational Surface — curl + docs + CI guard

Existing: internal curls honor `~/.curlrc`; published one-liner has no `-q`; docs list CLT/python3 as
a client prereq; no shell-lint CI.
Change: prepend `-q` to `:247/:308/:310` AND to every published `curl -fsSL …/install.sh` across the 6
docs; remove the CLT/python3 client prereq (INSTALL.md §2 `:61-65`, SETUP-CLIENT.md `:323-324`);
replace the "python3 not available" troubleshooting entry (INSTALL.md `:255-259`) with an empty-host
`curl: (6)` note (check `~/.curlrc`/proxy envs; cosmetic); add `lint-install.yml`.
Why: a broken `~/.curlrc` must not break our fetches or print a scary line to employees; docs must
stop telling fresh-Mac users they need Xcode CLT; CI must fail-close the crash class.
Risks: none material; `-q` only ignores the user's curl config.

## Implementation Blueprint

### Architecture Overview

Single-file bash installer; no new runtime components. Two helper functions added
(`is_wispralt_bundle`, `detect_orphaned_setup`), one python3 heredoc swapped for JXA, several array
expansions made bash-3.2-safe, a source-guard at EOF, a destination `rm -rf` in `install_bundle`, and
a one-line skip-path reset. `main()` gains one new call. Docs get mechanical `-q` + prereq edits. One
new CI workflow runs `bash -n` + an empty-array smoke on macos-14.

### Key Pseudocode

```bash
# ── Fix 1: remove_all_installs guard (mirror quit_all_installs:172) ──
remove_all_installs() {
    (( ${#FOUND_BUNDLES[@]} > 0 )) || return 0      # NEW first line
    local b
    for b in "${FOUND_BUNDLES[@]}"; do
        info "Removing $b ..."; rm -rf "$b" || warn "Could not remove $b (continuing)."
    done
}

# ── Fix 1: enumerate dedup loops at :111 and :129 → count-guarded, quoted, space-safe ──
if (( ${#FOUND_BUNDLES[@]} > 0 )); then
    for b in "${FOUND_BUNDLES[@]}"; do [[ "$b" == "$p" ]] && already=1 && break; done
fi

# ── Fix 1: source-guard replacing bare `main "$@"` at :517 (all 3 modes pre-verified) ──
if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
    main "$@"
fi

# ── Fix 2: JXA release-JSON parse replacing python3 heredoc (266-284); keep :286-295 ──
parsed="$(osascript -l JavaScript -e '
function run(argv) {
  var data = $.NSData.dataWithContentsOfFile(argv[0]);
  if (!data) throw new Error("cannot read release json");
  var r = JSON.parse($.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding).js);
  var tag = r.tag_name || "", dmg = "", sha = "", a = r.assets || [];
  for (var i = 0; i < a.length; i++) {
    var n = a[i].name || "", u = a[i].browser_download_url || "";
    if (/\.dmg$/.test(n) && n.indexOf("appcast") === -1) dmg = u;
    else if (/\.dmg\.sha256$/.test(n)) sha = u;
  }
  [tag, dmg, sha].forEach(function (v) {
    if (/[\t\r\n]/.test(v)) throw new Error("forbidden whitespace in tag/url");
  });
  return [tag, dmg, sha].join("\t");
}' "$json_file")" || die "Failed to parse release JSON."
# NOTE: empty fields (valid-but-wrong-shape JSON) are intentionally caught downstream by :293-295.

# ── Fix 3: bundle-identity helper (explicit + find sources only; reuses :160 form) ──
is_wispralt_bundle() {
    local dir="$1"
    [[ -d "$dir" ]] || return 1
    [[ -x "$dir/Contents/MacOS/WisprAlt" ]] && return 0          # complete bundle
    local bid
    bid="$(defaults read "$dir/Contents/Info" CFBundleIdentifier 2>/dev/null || true)"
    [[ "$bid" == "$BUNDLE_ID" ]]                                 # half-copied: trust the plist id
}
# mdfind source (:98): DROP the -x gate entirely — keep only `[[ -d "$line" ]] || continue`
#   plus the existing /Volumes, Backups.backupdb, .Trash exclusions.

# ── Fix 3: widen reinstall detection (call from main() right after enumerate) ──
detect_orphaned_setup() {
    (( IS_REINSTALL == 1 )) && return 0          # bundle already found → no-op (NEVER widens normal path)
    if security find-generic-password -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1 \
       || defaults read "$USERDEFAULTS_DOMAIN" >/dev/null 2>&1; then
        IS_REINSTALL=1
        info "Detected prior WisprAlt config (no app bundle) — will reset permissions."
    fi
}

# ── Fix 3: no-nest guarantee in install_bundle (before the cp -R) ──
info "Copying WisprAlt.app to /Applications..."
rm -rf "$app_path"                                # NEW — matches SETUP-CLIENT.md:341 ("rm -rf then cp -R")
if ! cp -R "$mount_point/WisprAlt.app" "/Applications/"; then …

# ── Fix 3: stop same-version no-op re-run from re-prompting TCC (main() skip path) ──
if should_skip_install; then
    info "Already on $RELEASE_TAG ... Skipping reinstall."
    IS_REINSTALL=0                               # NEW — true no-op: keep the still-valid TCC grants
    provision_credentials
    open_app_and_verify_launch; print_next_steps; return 0
fi

# ── Fix 4: curl -q (first arg) on internal fetches ──
http_code="$(curl -q "${curl_args[@]}" "$releases_api" || true)"                  # :247
curl -q -fsSL --retry 3 --retry-delay 2 --max-time 300 -o "$DMG_PATH" "$DMG_URL"  # :308
curl -q -fsSL --retry 3 --retry-delay 2 --max-time 30  -o "$SHA_PATH" "$SHA_URL"  # :310
```

```yaml
# ── Fix 5: .github/workflows/lint-install.yml (fail-closed guard; macos-14 = bash 3.2) ──
name: Lint installer
on:
  push: { paths: ['install.sh', '.github/workflows/lint-install.yml'] }
  pull_request: { paths: ['install.sh'] }
jobs:
  lint:
    runs-on: macos-14
    steps:
      - uses: actions/checkout@v4
      - name: bash version
        run: bash --version | head -1   # must be 3.2.x
      - name: Syntax check
        run: bash -n install.sh
      - name: Empty-FOUND_BUNDLES smoke (set -u must not crash)
        run: |
          set -euo pipefail
          source ./install.sh            # source-guard prevents main() from running
          FOUND_BUNDLES=()
          remove_all_installs            # must be a clean no-op, not 'unbound variable'
          echo "empty-array smoke passed"
      - name: No python3 dependency
        run: if grep -n 'python3' install.sh; then echo "python3 reintroduced"; exit 1; fi
```

### Tasks (in implementation order)

Task 1 — empty-array crash fix + source-guard
Files: MODIFY `install.sh` — guard `remove_all_installs` (`:202-208`); count-guard the `:111`/`:129`
loops; rewrite comments at `:18` and `:28-30`; replace bare `main "$@"` (`:517`) with the source-guard.
Pattern: `install.sh:172`. Gotchas: don't touch the guarded `:140`/`:155`; source-guard must keep
`curl | bash` (pre-verified). DoD: `lint-install.yml` smoke passes locally (source + empty array).

Task 2 — drop python3 → JXA
Files: MODIFY `install.sh` — delete `:63-64`; replace parse heredoc (`:266-284`) with the JXA block;
keep `:286-295` byte-for-byte. Gotchas: JXA reads `argv[0]`; do NOT add python3 to the tool list.
DoD: `grep -n python3 install.sh` empty; JXA fixture + bad-JSON tests pass.

Task 3 — cleanup tightening (discovery + orphan TCC + no-nest + no-reprompt)
Files: MODIFY `install.sh` — add `is_wispralt_bundle()`; drop the mdfind `-x` gate (`:98`); swap
`:108`/`:126` to `is_wispralt_bundle`; add `detect_orphaned_setup()` + call it after
`enumerate_existing_installs` (`:498`); add `rm -rf "$app_path"` before `cp -R` (`:341-342`); add
`IS_REINSTALL=0` on the skip path (`:500-506`). Pattern: `install.sh:160` (defaults-read form),
`:172` (early-return). Gotchas: keep mdfind exclusions; `detect_orphaned_setup` early-returns when a
bundle exists; existence probes need `>/dev/null 2>&1`. DoD: `is_wispralt_bundle` truth table; orphan
flips `IS_REINSTALL`; fresh-machine probes non-zero; skip path leaves TCC alone.

Task 4 — curl -q + docs
Files: MODIFY `install.sh` (`-q` at `:247/:308/:310`); MODIFY `README.md`, `docs/DEPLOY-TEAM.md`,
`docs/ADMIN.md`, `docs/SETUP-CLIENT.md`, `docs/INSTALL.md` — `grep -rn 'curl -fsSL
https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh' README.md docs/` and prefix
each `curl -fsSL` → `curl -q -fsSL`; remove CLT prereq (INSTALL.md §2 `:61-65`, SETUP-CLIENT.md
`:323-324`); replace INSTALL.md python3 troubleshooting (`:255-259`) with an empty-host curl note.
Gotchas: `-q` first; do NOT touch SETUP-SERVER.md / TROUBLESHOOTING.md / CHANGELOGs. DoD: every
published one-liner uses `-q -fsSL`; client docs list only macOS 14+/Apple-Silicon/Internet prereqs.

Task 5 — fail-closed CI guard
Files: CREATE `.github/workflows/lint-install.yml` (above). Gotchas: must `runs-on: macos-14` for bash
3.2; relies on Task 1's source-guard. DoD: workflow file valid; steps are `bash -n`, source+empty-array
smoke, and the no-python3 grep.

### Integration Points

- Entry point: `main()` (`install.sh:488-515`) — one new call (`detect_orphaned_setup`) + skip-path
  `IS_REINSTALL=0`.
- Operator surface: 6 docs.
- External hook: GitHub `/releases/latest` JSON (JXA); the one-liner pulling from `main`; new CI.

## Validation

```bash
# 1. Syntax (shellcheck NOT installed; bash -n is the gate)
bash -n install.sh

# 2. Empty-array safety on bash 3.2.57 (enabled by the source-guard)
bash -c 'set -euo pipefail; source ./install.sh; FOUND_BUNDLES=(); remove_all_installs; \
         enumerate_existing_installs >/dev/null 2>&1 || true; echo "no unbound-variable crash"'

# 3. JXA parse fixture — good payload → tag<TAB>dmg<TAB>sha (appcast ignored); bad JSON → non-zero.
# 4. is_wispralt_bundle truth table — real(exec) / half(Info.plist id only) / decoy(neither) → 0,0,1.
# 5. Fresh-machine probes — on a domain/service that never existed:
defaults read co.wispralt.WisprAlt >/dev/null 2>&1; echo "defaults exit=$?"   # expect non-zero
security find-generic-password -s co.wispralt >/dev/null 2>&1; echo "kc exit=$?"  # expect non-zero
# 6. No python3:  grep -n python3 install.sh   # expect empty

# 7. POST-PUSH wire check (verify-by-mechanism round-trip):
curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh | grep -q 'python3' \
  && echo "FAIL: python3 still on the wire" || echo "OK: patched bytes served from main"
```

### Manual Checks (on the mini / a fresh Mac)

- Fresh Apple-Silicon Mac, no Xcode CLT, no prior install → installs end-to-end, no `xcode-select`
  prompt, no `unbound variable`, app launches.
- `rm -rf /Applications/WisprAlt.app` (leave Keychain+defaults), re-run → `detect_orphaned_setup`
  flips `IS_REINSTALL=1`, TCC reset runs, install completes.
- Half-copied `WisprAlt.app` (Info.plist present, no exec) in `~/Downloads`, run → discovered + removed.
- Re-run on the SAME installed version → "Skipping reinstall", NO 4-permission re-prompt.

### Factuality Checks

- Every `MODIFY`/`NEW` path exists or is intentionally new (README.md, docs/*, install.sh,
  .github/workflows/lint-install.yml) ✓.
- Negative claims carry search evidence (python3 not in tool list; no keychain/defaults delete) ✓.

## Open Questions

- None.

## Deprecated / Removed Code

- `install.sh:63-64` — python3 preflight gate (removed).
- `install.sh:266-284` — python3 JSON-parse heredoc (replaced by JXA).
- `install.sh:517` — bare `main "$@"` (replaced by source-guard).
- `docs/INSTALL.md` §2 Xcode-CLT prereq bullet (`:61-65`) + python3 troubleshooting entry (`:255-259`)
  (removed/replaced).
- `docs/SETUP-CLIENT.md:323-324` — CLT/python3 prereq bullet (removed).

## Anti-Patterns to Avoid

- Don't wipe Keychain/UserDefaults by default (key loss on key-less re-run).
- Don't widen `rm -rf` to name-only matches; require `is_wispralt_bundle` to pass (no-nest is handled
  by the install-time `rm -rf "$app_path"`, not by loosening discovery).
- Don't let `detect_orphaned_setup` run when a bundle was found (early-return) — it must not widen TCC
  reset on the normal update path.
- Don't move `-q` into the `curl_args` array (must be literally first).
- Don't touch server-side Python docs/setup/CI.
- Don't drop the mdfind `/Volumes`/Time-Machine/`.Trash` exclusions.
- Don't reintroduce a JSON parser dependency (python3/jq); JXA only.

## Confidence

**9/10** for one-pass success. Every load-bearing unknown that can be tested off-target was
empirically verified on this bash-3.2.57 machine: empty-array safety, the source-guard across all
three invocation modes, JXA parse incl. whitespace guard + non-zero-on-bad-JSON + argv read. The
source-guard + CI smoke fail-close the crash class; the post-push wire check proves the fix reached
the wire. The only residual is the full DMG-mount/cp/TCC/Keychain flow, exercisable only on a real
fresh Mac (the mini) — covered by Manual Checks.

## Criticer Notes

1. **[honest assessment]** `detect_orphaned_setup`'s early-return scopes it to the no-bundle case, but the brief's "no spurious 4-permission re-prompt" guarantee depends on a genuinely-fresh machine returning non-zero from BOTH probes — made an explicit Success Criterion + Validation step. The same-version skip path's pre-existing TCC reset is now also fixed (`IS_REINSTALL=0`).
2. **[premise check]** Fix 4's internal `-q` doesn't silence the user-visible `curl: (6)` line (it comes from the published one-liner's LEFT curl) — resolved by adding `-q` to the published command across all 6 docs.
3. **[cheap win]** Nothing proved the patched bytes reached `main` — added a post-push `curl … | grep -q python3` wire check.
4. **[biggest gap]** The validation harness couldn't source the shipped functions — added the source-guard (pre-verified across all 3 modes) + the `lint-install.yml` empty-array smoke as the fail-closed machine-guard.
