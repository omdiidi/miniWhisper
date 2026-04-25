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

Signing and notarization require enrollment in the Apple Developer Program ($99/yr at developer.apple.com/enroll). The CI workflow does not support ad-hoc signing for distribution — use ad-hoc only for local development builds.

## Apple Developer Program

Signing and notarization require enrollment in the [Apple Developer Program](https://developer.apple.com/enroll) ($99/yr). The CI workflow does not support ad-hoc signing for distribution — use ad-hoc only for local development builds that will not be distributed.

**Ad-hoc fallback for personal builds:**

```bash
codesign --sign - --force --deep WisprAlt.app
```

Ad-hoc signed builds work only on the machine they were built on and cannot be notarized.

---

## Code Style

**Server (Python):**
- Formatter: `ruff format` (line length 100)
- Linter: `ruff check` + `pyright --strict`
- Run both before submitting: `cd server && uv run ruff format . && uv run ruff check . && uv run pyright`

**Client (Swift):**
- SwiftLint is not configured; follow [Swift API Design Guidelines](https://www.swift.org/documentation/api-design-guidelines/).
- Prefer `final class` for all non-subclassed types.
- Use `os.Logger` (via `client/WisprAlt/Util/Logger.swift`) not `print`.

---

## Documentation Discipline

Every code change must update the documentation files listed for that source file in [docs/OVERVIEW.md](OVERVIEW.md). That file contains the file-to-doc map.

From Claude Code, use `/docs-check` to diff the file-to-doc map against recent edit timestamps and find stale docs.

---

## PR Workflow

1. Fork the repository and create a branch from `main`.
2. Make changes; run `ruff`, `pyright`, and SwiftLint (if available) locally.
3. Update relevant docs per `docs/OVERVIEW.md`.
4. Submit a pull request with a clear description of what changed and why.
5. **Never push directly to `main`** without owner approval. This applies to all branches and remotes — no exceptions.

Commit messages should be in the present tense: "Add X", "Fix Y", "Remove Z".
