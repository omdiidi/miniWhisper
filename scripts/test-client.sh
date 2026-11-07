#!/usr/bin/env bash
# Build the macOS client and run its Swift Testing suite (WisprAltCoreTests).
# Every commit runs this plus the server pytest runner.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Keychain-touching tests run locally only; CI runners have no login keychain.
if [ -z "${CI:-}" ]; then
  export WISPRALT_KEYCHAIN_TESTS=1
fi

# Without full Xcode, Swift Testing lives in the CommandLineTools frameworks
# folder, which swift test does not search by default. That copy also ships
# _Testing_Foundation without a module, so the Testing+Foundation
# cross-import overlay is turned off there.
TEST_FLAGS=()
if [[ "$(xcode-select -p)" == *CommandLineTools* ]]; then
  F="$(xcode-select -p)/Library/Developer/Frameworks"
  TEST_FLAGS=(-Xswiftc "-F$F" -Xlinker "-F$F" -Xlinker -rpath -Xlinker "$F"
    -Xswiftc -Xfrontend -Xswiftc -disable-cross-import-overlays)
fi

(cd "$ROOT/client" && swift build && swift test ${TEST_FLAGS[@]+"${TEST_FLAGS[@]}"})
