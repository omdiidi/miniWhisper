#!/bin/bash
# install.sh — set up WisprAlt on this Mac from a clone of the repository.
#
#   git clone <repo-url> wispralt && cd wispralt && ./install.sh
#   ./install.sh --role host --non-interactive --admin-label owner --token-file ~/wispralt-admin.key
#
# It does only what has to happen before Python exists: Homebrew, ffmpeg and uv,
# then `uv tool install` of the `wispralt` CLI from this clone. Everything else
# is `wispralt setup` (all arguments are passed through; see docs/INSTALL.md).
# Safe to rerun. Never uses sudo. Must stay bash-3.2 safe and never call Python
# (a fresh Mac has none); .github/workflows/lint-install.yml checks both.

set -euo pipefail

NON_INTERACTIVE=0
CLONE=""

info() { printf '\033[0;36m[wispralt]\033[0m %s\n' "$*"; }
die() { printf '\033[0;31m[wispralt]\033[0m %s\n' "$1" >&2; exit "${2:-1}"; }

# parse_flags — notice --non-interactive (everything else is for `wispralt setup`).
parse_flags() {
    local arg
    for arg in "$@"; do
        if [[ "$arg" == "--non-interactive" ]]; then NON_INTERACTIVE=1; fi
    done
    return 0
}

# require_user — never root, never sudo: everything lives in the user's account.
require_user() {
    [[ "$EUID" != "0" ]] || die "Do not run this as root or with sudo." 2
    [[ -z "${SUDO_USER:-}" ]] || die "Do not run this with sudo; run it as your normal user." 2
}

# require_clone — the CLI installs from this clone's server/ directory.
require_clone() {
    CLONE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
    if [[ ! -f "$CLONE/server/pyproject.toml" ]]; then
        die "Run install.sh from a clone of the WisprAlt repository (git clone <repo-url>, then ./install.sh)." 2
    fi
}

# ensure_brew — Homebrew provides ffmpeg and uv. Its installer needs your password,
# so a non-interactive run stops with instructions instead.
ensure_brew() {
    if ! command -v brew >/dev/null 2>&1 && [[ -x /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
    command -v brew >/dev/null 2>&1 && return 0
    if [[ "$NON_INTERACTIVE" == "1" ]]; then
        die "Homebrew is missing. Install it from https://brew.sh (it asks for your password), then rerun." 2
    fi
    info "Installing Homebrew (it will ask for your password) ..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)"
}

# ensure_tools — ffmpeg (audio decoding on the host) and uv (Python + the CLI).
ensure_tools() {
    local tool
    for tool in ffmpeg uv; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            info "brew install $tool"
            brew install "$tool"
        fi
    done
}

# install_cli — install (or refresh) the `wispralt` tool from this clone, keeping
# the Apple Silicon [host] extra when an earlier setup added it.
install_cli() {
    local extra=""
    if ls -d "$(uv tool dir)"/wispralt/lib/*/site-packages/parakeet_mlx >/dev/null 2>&1; then
        extra="[host]"
    fi
    info "uv tool install wispralt from $CLONE/server$extra"
    uv tool install --python 3.11 --force "$CLONE/server$extra"
}

main() {
    parse_flags "$@"
    require_user
    require_clone
    ensure_brew
    ensure_tools
    install_cli
    local bin
    bin="$(uv tool dir --bin)"
    case ":$PATH:" in
        *":$bin:"*) ;;
        *) info "Tip: run \`uv tool update-shell\` and open a new terminal so \`wispralt\` is on your PATH." ;;
    esac
    exec "$bin/wispralt" setup --source "$CLONE" "$@"
}

# Source-guard: run main only when executed, not when sourced (lint-install.yml
# sources this file to test the functions).
if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
    main "$@"
fi
