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
    for tool in curl shasum security defaults hdiutil xattr tccutil pkill pgrep file awk; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done
    if (( ${#missing[@]} > 0 )); then
        die "Missing required tools: ${missing[*]}. These ship with macOS — your install may be broken."
    fi

    INSTALL_TMP="$(mktemp -d /tmp/wispralt-install.XXXXXX)"
    info "Temp dir: $INSTALL_TMP"
}

# ───────────────────────────────────────────────────────────────────────────────
# detect_reinstall — flag if we're replacing an existing install.
# ───────────────────────────────────────────────────────────────────────────────
detect_reinstall() {
    if [[ -d "/Applications/WisprAlt.app" ]]; then
        IS_REINSTALL=1
        info "Existing WisprAlt detected — will replace it."
    fi
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
    local lock_dir="/tmp/wispralt-install.lock"

    # mkdir-mutex against concurrent invocations.
    # The cleanup() function (registered at script top) unconditionally rmdir's
    # this lock on every exit path — no trap re-register needed, no race window.
    mkdir "$lock_dir" 2>/dev/null \
        || die "Another WisprAlt install appears to be in progress (lock at $lock_dir). If stale, remove with: rmdir $lock_dir"

    if (( IS_REINSTALL == 1 )); then
        info "Stopping running WisprAlt..."
        pkill -TERM -f "/Applications/WisprAlt.app/Contents/MacOS/WisprAlt" 2>/dev/null || true
        local i
        for i in 1 2 3 4; do
            pgrep -f "/Applications/WisprAlt.app/Contents/MacOS/WisprAlt" >/dev/null 2>&1 || break
            sleep 0.5
        done
        if pgrep -f "/Applications/WisprAlt.app/Contents/MacOS/WisprAlt" >/dev/null 2>&1; then
            warn "WisprAlt did not exit cleanly — sending SIGKILL."
            pkill -KILL -f "/Applications/WisprAlt.app/Contents/MacOS/WisprAlt" 2>/dev/null || true
        fi
        # Remove old bundle; cp -R into an existing dir nests instead of replacing.
        rm -rf "$app_path" || die "Could not remove existing $app_path."
    fi

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
        keychain_err="$(security add-generic-password \
            -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT" -w "$WISPRALT_API_KEY" -U 2>&1 1>/dev/null)" || true
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
    detect_reinstall
    fetch_release_metadata
    download_and_verify
    install_bundle
    provision_credentials
    open_app_and_verify_launch
    print_next_steps
}

main "$@"
