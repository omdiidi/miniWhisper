#!/usr/bin/env bash
# release-client.sh — Local-only release script.
#
# Omid runs this on his MacBook to ship a WisprAlt release to GitHub.
# Bumps Info.plist version, builds the signed .app via build-client-local.sh,
# packages a DMG, computes a SHA256 sidecar (bare filename so `shasum -c`
# works in the employee's CWD), tags, pushes, and creates a GitHub Release.
#
# Usage:
#   ./scripts/release-client.sh 0.2.0
#
#   # Allow releasing from a non-main branch (e.g. testing):
#   ALLOW_BRANCH=1 ./scripts/release-client.sh 0.2.0
#
# Pre-flight guards:
#   - Refuses to release from anything other than `main` (override: ALLOW_BRANCH=1).
#   - Refuses if the working tree is dirty.
#   - Refuses if the tag already exists locally OR on GitHub.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

VERSION="${1:?usage: $0 <version, e.g. 0.2.0>}"
TAG="v${VERSION}"
REPO_SLUG="omdiidi/miniWhisper"

# 0. Pre-flight guards.

# 0a. Must be on main (or explicit ALLOW_BRANCH=1 override).
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "${CURRENT_BRANCH}" != "main" && "${ALLOW_BRANCH:-0}" != "1" ]]; then
    echo "Refusing to release from branch '${CURRENT_BRANCH}'." >&2
    echo "Switch to main, or set ALLOW_BRANCH=1 to override." >&2
    exit 1
fi

# 0b. Working tree must be clean — release-client.sh commits the
#     Info.plist version bump, so we cannot have unrelated mods.
if ! git diff-index --quiet HEAD --; then
    echo "Working tree is dirty. Commit or stash before releasing." >&2
    git status --short >&2
    exit 1
fi

# 0c. Refuse to overwrite an existing tag — fail BEFORE building.
if git rev-parse "${TAG}" >/dev/null 2>&1; then
    echo "Tag ${TAG} already exists locally. Bump VERSION." >&2
    exit 1
fi
if gh release view "${TAG}" --repo "${REPO_SLUG}" >/dev/null 2>&1; then
    echo "Release ${TAG} already exists on GitHub. Bump VERSION." >&2
    exit 1
fi

# 1. Bump CFBundleShortVersionString + CFBundleVersion in Info.plist.
#    Both keys move together so notarization + Sparkle see a monotonic build
#    number instead of "1" forever. Sparkle uses CFBundleVersion for update
#    comparison; same-value-as-short-string is simplest and works.
echo "==> Bumping Info.plist version to ${VERSION}"
plutil -replace CFBundleShortVersionString -string "${VERSION}" \
  client/WisprAlt/Info.plist
plutil -replace CFBundleVersion -string "${VERSION}" \
  client/WisprAlt/Info.plist

# 2. Build signed .app via existing build-client-local.sh.
echo "==> Building signed WisprAlt.app"
bash scripts/build-client-local.sh

APP_PATH="client/build/WisprAlt.app"
if [[ ! -d "${APP_PATH}" ]]; then
    echo "Build did not produce ${APP_PATH}." >&2
    exit 1
fi

# 3. Package as DMG.
DMG_NAME="WisprAlt-${VERSION}.dmg"
DMG_DIR="/tmp/wispralt-release-${VERSION}"
DMG_PATH="${DMG_DIR}/${DMG_NAME}"
rm -rf "${DMG_DIR}" && mkdir -p "${DMG_DIR}"
echo "==> Creating DMG at ${DMG_PATH}"
hdiutil create -volname WisprAlt -srcfolder "${APP_PATH}" \
  -fs HFS+ -format UDZO -ov "${DMG_PATH}"

# 4. Compute SHA256 sidecar — write only the BARE filename so the
#    employee's `shasum -c` finds the file by name in their CWD.
echo "==> Computing SHA256 sidecar"
(cd "${DMG_DIR}" && shasum -a 256 "${DMG_NAME}" > "${DMG_NAME}.sha256")

# 5. Tag + push + GitHub Release with both assets.
echo "==> Committing version bump"
git add client/WisprAlt/Info.plist
git -c user.email="zomid777@gmail.com" -c user.name="omid zahrai" \
    commit -m "release: ${TAG}" || true

echo "==> Tagging ${TAG}"
git tag "${TAG}"

echo "==> Pushing branch and tag"
git push origin "${CURRENT_BRANCH}"
git push origin "${TAG}"

echo "==> Creating GitHub Release"
gh release create "${TAG}" \
  --repo "${REPO_SLUG}" \
  --title "WisprAlt ${VERSION}" \
  --notes "$(printf "WisprAlt %s\n\nSHA256:\n\n\`\`\`\n%s\n\`\`\`\n" \
    "${VERSION}" "$(cat "${DMG_PATH}.sha256")")" \
  "${DMG_PATH}" "${DMG_PATH}.sha256"

echo ""
echo "Release ${TAG} shipped."
echo "Employees can install via the curl one-liner in docs/INSTALL.md or run /wispralt-update in Claude Code."
