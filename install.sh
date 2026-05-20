#!/bin/bash
# WisprAlt installer — curl | bash kernel.
# See tmp/ready-plans/2026-04-30-curl-installer-kernel.md (§4.1, §5).

set -euo pipefail

# ───────────────────────────────────────────────────────────────────────────────
# Readonly constants
# ───────────────────────────────────────────────────────────────────────────────
readonly REPO="omdiidi/miniWhisper"
readonly DEFAULT_SERVER_URL="https://transcribe.integrateapi.ai"
readonly KEYCHAIN_SERVICE="co.wispralt"
readonly KEYCHAIN_ACCOUNT="default"
readonly USERDEFAULTS_DOMAIN="co.wispralt.WisprAlt"
readonly BUNDLE_ID="co.wispralt.WisprAlt"

# ───────────────────────────────────────────────────────────────────────────────
# Globals — initialized so `set -u` doesn't bite later.
# ───────────────────────────────────────────────────────────────────────────────
IS_REINSTALL=0
WISPRALT_API_KEY_WAS_SET=0
INSTALL_TMP=""
RELEASE_TAG=""
DMG_URL=""
SHA_URL=""
DMG_PATH=""
SHA_PATH=""
# Populated by enumerate_existing_installs(); referenced before that call by
# should_skip_install() in some code paths, so initialize for `set -u`.
FOUND_BUNDLES=()

# ───────────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────────
info() { printf '\033[0;36m[wispralt]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[wispralt]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31m[wispralt]\033[0m %s\n' "$*" >&2; exit 1; }

cleanup() {
    if [[ -n "$INSTALL_TMP" && -d "$INSTALL_TMP" ]]; then
        rm -rf "$INSTALL_TMP"
    fi
    # Also clean the install-bundle mutex unconditionally — rmdir is idempotent
    # on a non-existent dir (returns nonzero, swallowed). This eliminates a race
    # where SIGINT between mkdir and trap re-register would orphan the lock.
    rmdir /tmp/wispralt-install.lock 2>/dev/null || true
}
trap cleanup EXIT INT TERM PIPE

# ───────────────────────────────────────────────────────────────────────────────
# preflight — sanity-check the host before doing anything destructive.
# ───────────────────────────────────────────────────────────────────────────────
preflight() {
    [[ "$EUID" != "0" ]] || die "Refusing to run as root. Run this without sudo."
    [[ -z "${SUDO_USER:-}" ]] || die "Refusing to run under sudo. Run this as your normal user."

    [[ "$(uname -m)" == "arm64" ]] || die "Apple Silicon (arm64) required. Detected: $(uname -m)."

    local os_major
    os_major="$(sw_vers -productVersion | cut -d. -f1)"
    [[ "$os_major" -ge 14 ]] || die "macOS 14+ required. Detected: $(sw_vers -productVersion)."

    # Force python3 to actually resolve (Xcode CLT stub returns 0 from `command -v` but errors on use).
    python3 --version >/dev/null 2>&1 || die "python3 not available. Run: xcode-select --install"

    local missing=()
    local tool
    for tool in curl shasum security defaults hdiutil xattr tccutil pkill pgrep file awk mdfind osascript launchctl find; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done
    if (( ${#missing[@]} > 0 )); then
        die "Missing required tools: ${missing[*]}. These ship with macOS — your install may be broken."
    fi

    INSTALL_TMP="$(mktemp -d /tmp/wispralt-install.XXXXXX)"
    info "Temp dir: $INSTALL_TMP"
}

# ───────────────────────────────────────────────────────────────────────────────
# enumerate_existing_installs — find every WisprAlt.app on disk via Spotlight
# (bundle-ID query) plus a belt-and-suspenders explicit-path fallback and a
# `find`-based orphan sweep across user-writable dirs. Populates FOUND_BUNDLES.
# ───────────────────────────────────────────────────────────────────────────────
enumerate_existing_installs() {
    FOUND_BUNDLES=()
    # mdfind newline-separated. Post-filter to exclude:
    #   /Volumes/*         — mounted DMGs, external drives, Time Machine snapshots
    #   *Backups.backupdb* — Time Machine local store
    #   */.Trash/*         — bundles the user already deleted
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

    # v3: third orphan-discovery source via `find`. Catches WisprAlt copies in
    # non-canonical user paths (~/Downloads, ~/Desktop, ~/Applications) that
    # Spotlight hasn't indexed yet (fresh download, indexing disabled, etc).
    # Depth-3 cap so we don't crawl deep trees. /Applications already covered
    # above; this widens to user-writeable directories.
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

# ───────────────────────────────────────────────────────────────────────────────
# should_skip_install — idempotency early-exit. Returns 0 (skip) ONLY when:
#   1 found bundle, AT /Applications/WisprAlt.app, no running instance, and
#   CFBundleShortVersionString matches the target release. Saves a TCC reset
#   on no-op re-runs while still healing a hung/duplicate situation.
# ───────────────────────────────────────────────────────────────────────────────
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

# ───────────────────────────────────────────────────────────────────────────────
# quit_all_installs — graceful AppleScript-quit by bundle id, then escalate to
# SIGTERM and finally SIGKILL. Narrow pkill/pgrep pattern (co.wispralt.WisprAlt,
# NOT co.wispralt) so we don't accidentally kill the mini's wispralt_server or
# cloudflared processes on a dev box running both client and server.
# ───────────────────────────────────────────────────────────────────────────────
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
    # Hard kill for stragglers.
    warn "Sending SIGTERM to remaining WisprAlt processes..."
    pkill -TERM -f "co.wispralt.WisprAlt" 2>/dev/null || true
    sleep 1
    if pgrep -f "co.wispralt.WisprAlt" >/dev/null 2>&1; then
        warn "Sending SIGKILL to remaining WisprAlt processes..."
        pkill -KILL -f "co.wispralt.WisprAlt" 2>/dev/null || true
    fi
}

# ───────────────────────────────────────────────────────────────────────────────
# remove_all_installs — rm -rf every bundle enumerate_existing_installs found.
# Warns (does not die) on individual failures so a single locked bundle doesn't
# block the rest of the cleanup.
# ───────────────────────────────────────────────────────────────────────────────
remove_all_installs() {
    local b
    for b in "${FOUND_BUNDLES[@]}"; do
        info "Removing $b ..."
        rm -rf "$b" || warn "Could not remove $b (continuing)."
    done
}

# ───────────────────────────────────────────────────────────────────────────────
# sweep_launch_agents — unload + delete any co.wispralt*.plist in the user
# LaunchAgents dir. Defensive: the current client app does NOT install plists
# (LoginItem is via SMAppService), but a stale plist from a past version or
# third-party tooling would still resurrect a dead bundle path. EXCLUDES the
# mini's co.wispralt.server and co.wispralt.cloudflared plists which only
# exist on the server host, not on a client install.
# ───────────────────────────────────────────────────────────────────────────────
sweep_launch_agents() {
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

# ───────────────────────────────────────────────────────────────────────────────
# fetch_release_metadata — query GitHub for the latest tag + DMG + sha256 URLs.
# ───────────────────────────────────────────────────────────────────────────────
fetch_release_metadata() {
    local releases_api="https://api.github.com/repos/${REPO}/releases/latest"
    local headers_file="$INSTALL_TMP/headers"
    local json_file="$INSTALL_TMP/release.json"

    info "Fetching latest release from $REPO..."

    local http_code
    local curl_args=(-fsSL --max-time 30 -D "$headers_file" -o "$json_file"
                     -w "%{http_code}" -H "Accept: application/vnd.github+json")
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        curl_args+=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
    fi

    http_code="$(curl "${curl_args[@]}" "$releases_api" || true)"

    if [[ "$http_code" == "403" ]]; then
        if grep -qi '^X-RateLimit-Remaining: 0' "$headers_file" 2>/dev/null; then
            local reset_epoch reset_human
            reset_epoch="$(awk -F': ' 'tolower($1)=="x-ratelimit-reset"{gsub(/[\r\n]/,"",$2); print $2}' "$headers_file" | head -1)"
            if [[ -n "$reset_epoch" ]]; then
                reset_human="$(date -r "$reset_epoch" 2>/dev/null || echo "$reset_epoch")"
            else
                reset_human="unknown"
            fi
            die "GitHub API rate limit exceeded (resets at: $reset_human). Set GITHUB_TOKEN=<personal-access-token> and re-run to bypass."
        fi
        die "GitHub returned HTTP 403. See $headers_file for details."
    fi

    [[ "$http_code" == "200" ]] || die "GitHub returned HTTP $http_code (expected 200)."

    local parsed
    parsed="$(python3 -c '
import json, sys
r = json.load(sys.stdin)
tag = r.get("tag_name", "")
dmg = ""
sha = ""
for a in r.get("assets", []):
    name = a.get("name", "")
    url = a.get("browser_download_url", "")
    if name.endswith(".dmg") and "appcast" not in name:
        dmg = url
    elif name.endswith(".dmg.sha256"):
        sha = url
for v in (tag, dmg, sha):
    if any(c in v for c in ("\t", "\r", "\n")):
        sys.stderr.write("Tag or asset URL contains forbidden whitespace.\n")
        sys.exit(1)
print(f"{tag}\t{dmg}\t{sha}")
' < "$json_file")" || die "Failed to parse release JSON."

    local nfields
    nfields="$(awk -F'\t' '{print NF}' <<< "$parsed")"
    [[ "$nfields" == "3" ]] || die "Unexpected release-metadata shape (got $nfields fields, expected 3)."

    # Use herestring (<<<) — process substitution under bash 3.2 + set -e silently exits.
    IFS=$'\t' read -r RELEASE_TAG DMG_URL SHA_URL <<< "$parsed"

    [[ -n "$RELEASE_TAG" ]] || die "Release JSON missing tag_name."
    [[ -n "$DMG_URL" ]]     || die "Release JSON missing .dmg asset URL."
    [[ -n "$SHA_URL" ]]     || die "Release JSON missing .dmg.sha256 asset URL."

    info "Latest release: $RELEASE_TAG"
}

# ───────────────────────────────────────────────────────────────────────────────
# download_and_verify — download DMG + sha256 sidecar, validate.
# ───────────────────────────────────────────────────────────────────────────────
download_and_verify() {
    DMG_PATH="$INSTALL_TMP/$(basename "$DMG_URL")"
    SHA_PATH="$INSTALL_TMP/$(basename "$SHA_URL")"

    info "Downloading $RELEASE_TAG..."
    curl -fsSL --retry 3 --retry-delay 2 --max-time 300 -o "$DMG_PATH" "$DMG_URL" \
        || die "Failed to download DMG from $DMG_URL"
    curl -fsSL --retry 3 --retry-delay 2 --max-time 30 -o "$SHA_PATH" "$SHA_URL" \
        || die "Failed to download SHA256 sidecar from $SHA_URL"

    # `hdiutil imageinfo` is the authoritative DMG validator. `file` is heuristic.
    hdiutil imageinfo "$DMG_PATH" >/dev/null 2>&1 \
        || die "Downloaded file is not a valid DMG: $DMG_PATH"

    info "Verifying SHA256..."
    ( cd "$INSTALL_TMP" && shasum -a 256 -c "$(basename "$SHA_PATH")" >/dev/null ) \
        || die "SHA256 verification failed — DMG corrupt or tampered."
}

# ───────────────────────────────────────────────────────────────────────────────
# install_bundle — atomically replace /Applications/WisprAlt.app.
# ───────────────────────────────────────────────────────────────────────────────
install_bundle() {
    local app_path="/Applications/WisprAlt.app"
    local mount_point="/tmp/wispralt-mount.$$"

    # Note: the install-wide mutex is acquired in main() so it covers BOTH the
    # skip-install fast path and this full path. The cleanup() trap rmdir's it
    # on every exit. Discovery / quit / remove all happen in main() before we
    # get here; install_bundle now just mounts → copies → strips quarantine.

    # Defensive: stale mount from a previous failed run.
    hdiutil detach "$mount_point" >/dev/null 2>&1 || true

    info "Mounting DMG..."
    hdiutil attach "$DMG_PATH" -nobrowse -mountpoint "$mount_point" >/dev/null \
        || die "Failed to mount DMG at $mount_point."

    info "Copying WisprAlt.app to /Applications..."
    if ! cp -R "$mount_point/WisprAlt.app" "/Applications/"; then
        hdiutil detach "$mount_point" >/dev/null 2>&1 || true
        die "Failed to copy WisprAlt.app into /Applications."
    fi

    hdiutil detach "$mount_point" >/dev/null 2>&1 || true

    [[ -d "$app_path" ]] || die "Install failed — $app_path not present."

    # Strip quarantine attribute so Gatekeeper accepts our notarized build immediately.
    xattr -dr com.apple.quarantine "$app_path" 2>/dev/null || true
}

# ───────────────────────────────────────────────────────────────────────────────
# provision_credentials — TCC reset (reinstall), Keychain write, defaults write.
# ───────────────────────────────────────────────────────────────────────────────
provision_credentials() {
    if (( IS_REINSTALL == 1 )); then
        info "Resetting TCC permissions for $BUNDLE_ID..."
        local tcc_out
        tcc_out="$(tccutil reset All "$BUNDLE_ID" 2>&1 || true)"
        if ! grep -Eqi 'successful|reset' <<< "$tcc_out"; then
            local svc
            for svc in Microphone ListenEvent Accessibility ScreenCapture; do
                tccutil reset "$svc" "$BUNDLE_ID" 2>/dev/null || true
            done
        fi
    fi

    if [[ -n "${WISPRALT_API_KEY:-}" ]]; then
        # Track BEFORE unset, so print_next_steps can refer to it without leaking the key.
        WISPRALT_API_KEY_WAS_SET=1

        info "Storing API key in Keychain..."
        local keychain_err
        # -T pre-authorizes WisprAlt.app to read the entry without an
        # additional GUI prompt on first launch. The bundle is already
        # at /Applications/WisprAlt.app at this point (install_bundle ran
        # first in main()), so the path resolves and ACL applies.
        keychain_err="$(security add-generic-password \
            -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT" -w "$WISPRALT_API_KEY" -U \
            -T /Applications/WisprAlt.app/Contents/MacOS/WisprAlt \
            -T /usr/bin/security 2>&1 1>/dev/null)" || true
        if [[ -n "$keychain_err" ]]; then
            warn "Keychain write reported: $keychain_err"
        fi

        # Roundtrip verify only when interactive — under `curl | bash`, stdin is the script body.
        if [[ -t 0 ]]; then
            local readback
            readback="$(security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT" -w 2>/dev/null || true)"
            if [[ "$readback" != "$WISPRALT_API_KEY" ]]; then
                unset WISPRALT_API_KEY
                die "Keychain roundtrip failed — written value did not match."
            fi
            info "Keychain write verified."
        else
            info "Skipped Keychain roundtrip (non-interactive — running under curl | bash)."
        fi

        unset WISPRALT_API_KEY
    fi

    local server_url="${WISPRALT_SERVER:-$DEFAULT_SERVER_URL}"
    [[ "$server_url" =~ ^https:// ]] || die "WISPRALT_SERVER must be an https:// URL. Got: $server_url"

    info "Writing serverURL to user defaults: $server_url"
    defaults write "$USERDEFAULTS_DOMAIN" serverURL -string "$server_url"

    # cfprefsd caches preferences; bouncing it ensures the new value is durable.
    killall cfprefsd 2>/dev/null || true

    # Poll for cfprefsd respawn — up to 2s.
    local i
    local current=""
    for i in 1 2 3 4; do
        sleep 0.5
        current="$(defaults read "$USERDEFAULTS_DOMAIN" serverURL 2>/dev/null || true)"
        [[ "$current" == "$server_url" ]] && break
    done
    if [[ "$current" != "$server_url" ]]; then
        warn "Could not verify serverURL persisted — defaults read returned: $current"
    fi
}

# ───────────────────────────────────────────────────────────────────────────────
# open_app_and_verify_launch — open the app and confirm the process exists.
# ───────────────────────────────────────────────────────────────────────────────
open_app_and_verify_launch() {
    info "Opening WisprAlt..."
    open /Applications/WisprAlt.app

    # 10-second poll. PermissionGate Task + bundle load can take 4-8s on a cold mini.
    local launched=0
    local _
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if pgrep -f /Applications/WisprAlt.app/Contents/MacOS/WisprAlt >/dev/null 2>&1; then
            launched=1
            break
        fi
        sleep 1
    done

    if [[ "$launched" != "1" ]]; then
        warn "WisprAlt did not appear to start within 10s — open it manually from /Applications."
    fi
}

# ───────────────────────────────────────────────────────────────────────────────
# print_next_steps — final user-facing banner.
# ───────────────────────────────────────────────────────────────────────────────
print_next_steps() {
    echo ""
    echo "════════════════════════════════════════════════════════════════"
    echo "  WisprAlt $RELEASE_TAG installed."
    echo "════════════════════════════════════════════════════════════════"
    echo ""
    echo "The app is opening now. You'll see prompts for 4 macOS permissions:"
    echo "  1. Accessibility       — required to inject text at the cursor"
    echo "  2. Input Monitoring    — required to detect FN-key holds"
    echo "  3. Microphone          — required to capture your voice"
    echo "  4. Screen Recording    — required for meeting recordings"
    echo ""
    echo "After granting all four, hold FN to dictate. Release to inject text."
    echo "Triple-tap FN quickly to start a meeting recording."
    echo ""
    # Only WISPRALT_API_KEY_WAS_SET is the meaningful gate — server URL has a
    # default fallback so its presence does NOT mean "ready to dictate".
    # Compare to literal "1" — `-n "0"` is ALWAYS true.
    if [[ "$WISPRALT_API_KEY_WAS_SET" == "1" ]]; then
        echo "Server + API key were pre-configured. You're ready to dictate."
    else
        echo "Open WisprAlt → Settings → Advanced and paste your API key."
    fi
    echo ""
    echo "Trouble? Run:"
    echo "  open /Applications/WisprAlt.app"
    echo "If macOS says 'cannot be opened because Apple cannot check it…',"
    echo "right-click the app in Finder → Open → Open Anyway."
    echo "Full troubleshooting at docs/INSTALL.md."
    echo ""
}

# ───────────────────────────────────────────────────────────────────────────────
# main — atomic streaming entry point.
# ───────────────────────────────────────────────────────────────────────────────
main() {
    preflight
    # v3: mutex acquisition moved up so it covers BOTH the skip-install code
    # path and the full install path. Previously inside install_bundle, which
    # the skip path bypassed — two concurrent same-version reruns could race
    # on provision_credentials. cleanup() trap unconditionally rmdir's the
    # lock on every exit path.
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

main "$@"
