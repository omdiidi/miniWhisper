#!/usr/bin/env bash
# Build the macOS client, run its Swift Testing suite (WisprAltCoreTests),
# then the log-privacy gate.
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

# Log-privacy gate: no transcript text, bodies, server reasons, paths, URLs or
# keys in any Log call. The self-test proves the checker still catches leaks.
(cd "$ROOT" && python3 scripts/check-log-privacy.py --self-test && python3 scripts/check-log-privacy.py)
