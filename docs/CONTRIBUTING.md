---
title: Contributing
---

# Contributing

## Required GitHub Secrets

The CI workflow (`build-client.yml`) requires the following secrets to be set in your fork's repository settings under **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
|---|---|
| `DEVELOPER_ID_APP` | Full name of your Developer ID Application certificate, e.g. `Developer ID Application: Jane Smith (XXXXXXXXXX)` |
| `DEVELOPER_ID_APP_CERT_P12` | Base64-encoded `.p12` export of the Developer ID Application certificate + private key |
| `DEVELOPER_ID_APP_CERT_PASSWORD` | Password for the `.p12` export |
| `APPLE_ID` | Apple ID email used for notarization |
| `APP_SPECIFIC_PASSWORD` | App-specific password generated at appleid.apple.com for the `APPLE_ID` |
| `TEAM_ID` | Apple Developer Team ID (10-character alphanumeric) |
| `SPARKLE_ED_PRIVATE_KEY` | EdDSA private key for signing the Sparkle appcast (base64) |

All notarization calls use `xcrun notarytool submit --apple-id ... --password ... --team-id ...` (not `--keychain-profile`, which does not persist in CI).

## Sparkle Key Management

Sparkle 2 uses EdDSA (Ed25519) for appcast signing. The public key is embedded in `Info.plist` under `SUPublicEDKey` and is permanent for the lifetime of that major version. The private key must never be committed to the repository.

**Generating a new key pair:**

```bash
./client/.build/checkouts/Sparkle/bin/generate_keys
```

This prints both keys. Store the private key in your password manager (e.g. 1Password vault) and immediately add it as the `SPARKLE_ED_PRIVATE_KEY` GitHub secret. Paste the public key into `Info.plist` as `SUPublicEDKey`.

**Key rotation:**

Rotating the Sparkle signing key requires a major-version release. The new public key must be included in an update signed by the old private key so existing installs can transition. Do not rotate the key without a documented migration path.

**Signing an appcast manually:**

```bash
./client/.build/checkouts/Sparkle/bin/sign_update /path/to/WisprAlt.dmg "$SPARKLE_ED_PRIVATE_KEY"
```

Paste the printed `sparkle:edSignature` and `length` values into `appcast.xml`.

## Apple Developer Program

Signing and notarization require enrollment in the [Apple Developer Program](https://developer.apple.com/enroll) ($99/yr). The CI workflow does not support ad-hoc signing for distribution — use ad-hoc only for local development builds that will not be distributed.

**Ad-hoc fallback for personal builds:**

```bash
codesign --sign - --force --deep WisprAlt.app
```

Ad-hoc signed builds work only on the machine they were built on and cannot be notarized.

### Legacy: self-signed local certificate (removed)

A script that generated a self-signed code-signing certificate (and trusted it as a System code-signing root) was removed in Part 5; it was no longer wired into any build. It was originally intended to make TCC permission grants survive client rebuilds.

**Why it doesn't work across rebuilds:** macOS keys self-signed apps in TCC by `cdhash` (a hash of the binary), not by certificate identity. Even with a trusted self-signed cert, each new binary has a new cdhash, so TCC treats it as a new app and re-prompts for all three permissions. The cert helps within a single build (TCC remembers the grant across kill+relaunch of the same binary), but not across rebuilds.

**Current status:** the build flow uses a free **Apple Development certificate** from Xcode (see [SETUP-CLIENT.md](SETUP-CLIENT.md)), which is required for `SMAppService.mainApp.register()` (login-at-startup). The self-signed cert path cannot support SMAppService.

**Without an Apple ID:** create a self-signed code-signing identity in Keychain Access yourself and pass it as `SIGN_IDENTITY` to `scripts/build-client-local.sh` (no login-at-startup support).

See [DEPLOYMENT-NOTES.md](DEPLOYMENT-NOTES.md) "TCC permissions and Apple Development-signed builds" for the full TCC behavior explanation.

---

## Code Style

**Server (Python):**
- Formatter: `ruff format` (line length 100)
- Linter: `ruff check` + `pyright --strict`
- Dependencies are locked in `server/uv.lock`; run `uv lock` in `server/` after changing `pyproject.toml`.
- Run both before submitting: `cd server && uv run --extra dev --extra host ruff format . && uv run --extra dev --extra host ruff check . && uv run --extra dev --extra host pyright` (name both extras so `uv run` does not strip the MLX stack)

**Client (Swift):**
- SwiftLint is not configured; follow [Swift API Design Guidelines](https://www.swift.org/documentation/api-design-guidelines/).
- Prefer `final class` for all non-subclassed types.
- Use `os.Logger` (via `client/WisprAlt/Util/Logger.swift`) not `print`.

---

## Documentation Discipline

Every code change must update the documentation files listed for that source file in [docs/OVERVIEW.md](OVERVIEW.md). That file contains the file-to-doc map.

From Claude Code, use `/docs-check` to diff the file-to-doc map against recent edit timestamps and find stale docs. `.claude/settings.json` is the shared Claude Code permission allow-list (personal overrides go in the git-ignored `.claude/settings.local.json`). `scripts/README.md` describes each script.

---

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main` that touch `server/**`, `client/**`, `scripts/test-client.sh`, `scripts/check-log-privacy.py` or the workflow itself. It has two jobs:

- **server** (ubuntu-latest, Python 3.11): installs `uv` and `ffmpeg`, then runs `scripts/test-server.sh -v` (minimal test deps, Apple-Silicon-only MLX modules stubbed). No Hugging Face token is needed; the same script runs locally.
- **client** (macos-latest): `bash scripts/test-client.sh`: `swift build && swift test` in `client/` (Swift Testing; the Keychain tests are skipped because `CI` is set), then the log-privacy gate (`scripts/check-log-privacy.py`; see [TESTING.md](TESTING.md#log-privacy-gate)). New `Log.` calls must log counts, status codes or `ErrorLog.describe(error)`, never text, paths, URLs or error strings.

`build-client.yml` (signed release build, on `v*` tags) and `lint-install.yml` are separate. The latter runs on macOS's bash 3.2 when `install.sh` or `scripts/**` change: `bash -n` on `install.sh` and every `scripts/*.sh`, a sourced-function smoke of `install.sh`, and a check that `install.sh` never calls `python3`.

## PR Workflow

1. Fork the repository and create a branch from `main`.
2. Make changes; run `ruff`, `pyright`, and SwiftLint (if available) locally.
3. Update relevant docs per `docs/OVERVIEW.md`.
4. Submit a pull request with a clear description of what changed and why.
5. **Never push directly to `main`** without owner approval. This applies to all branches and remotes — no exceptions.

Commit messages should be in the present tense: "Add X", "Fix Y", "Remove Z".
