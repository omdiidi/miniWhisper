# Deploy: Team Distribution Operator Guide

Owner-side runbook for shipping client releases and onboarding the team to a
Local host. Installing the host itself is [INSTALL.md](INSTALL.md)
(`./install.sh --role host`); the portal is [ADMIN.md](ADMIN.md).

---

## Shipping a release

`scripts/release-client.sh` is the one-shot release tool. It runs on a Mac
with an Apple Development identity and ships the result to GitHub Releases of
the clone's `origin` (`REPO_SLUG` overrides it).

```bash
bash scripts/release-client.sh 0.2.0
```

What it does, in order:

1. **Pre-flight guards.**
   - Refuse unless on `main` (override: `ALLOW_BRANCH=1`).
   - Refuse if working tree is dirty (it commits the version bump, so
     unrelated changes would get folded in).
   - Refuse if the tag `v0.2.0` already exists locally OR on GitHub.
2. **Bump `CFBundleShortVersionString` and `CFBundleVersion`** in
   `client/WisprAlt/Info.plist` to `0.2.0` via two `plutil -replace`
   calls. Both keys move together so the build number stays monotonic.
3. **Build** via `scripts/build-client-local.sh` (Apple Development
   signing, see [SETUP-CLIENT.md](SETUP-CLIENT.md)). The build stamps the
   `WisprAltReleaseRepo` key the app's update check polls.
4. **Package as DMG** under `/tmp/wispralt-release-0.2.0/WisprAlt-0.2.0.dmg`
   via `hdiutil create -format UDZO`.
5. **Compute SHA256 sidecar** — written with the BARE filename so
   `shasum -c WisprAlt-*.dmg.sha256` works in the employee's CWD.
6. **Tag, push, create GitHub Release** via `gh release create` with
   both the DMG and the sha256 sidecar attached. The release notes
   embed the SHA256 in a fenced code block.

After the script prints `Release v0.2.0 shipped.`, the app shows "Update
available" within six hours; each Mac updates with `wispralt update --client`
(Settings > Updates copies that command).

If the build fails partway through (codesign xattr race on Tahoe,
notably), the working tree remains on the bumped Info.plist commit but
no tag is pushed. Re-run; the pre-flight tag check will refuse, so
either roll back the commit (`git reset --hard HEAD~1`) or bump VERSION.

---

## Adding a new employee

Use the portal: **People > Add employee** (or an invite from **Invites**). See
[ADMIN.md](ADMIN.md#adding-a-new-employee). The key is shown once; send it by
Signal / iMessage, never email. The employee clones the repo, runs
`./install.sh --role client --client-mode local`, and pastes the server URL and
key into the app's Settings. From the host's terminal the same is
`wispralt users add <label>`.

## Revoking an employee

**People > the person > Disable** (or `wispralt users disable <id>`). Keys are
checked against the database on every request (no cache), so the next request
from any of their devices gets 401. Revoking a single device is **Revoke** on
its row, or `wispralt keys revoke <key_id>`.
