# Install

Canonical install guide for the WisprAlt macOS client.

## 1. The one-liner

Open Terminal on the Mac you want to dictate from and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh \
  | WISPRALT_API_KEY=sk_xxx WISPRALT_SERVER=https://transcribe.integrateapi.ai bash
```

That's it. The script downloads the latest signed build, drops it in
`/Applications`, writes the API key into the macOS Keychain, writes the server
URL into UserDefaults, and launches the app.

### History cleanup (recommended)

zsh does **not** ignore leading-space-prefixed commands by default
(`HIST_IGNORE_SPACE` is OFF unless you've explicitly enabled it), and bash
behaves the same way. After the install completes, scrub the key from your
shell history:

```bash
unset WISPRALT_API_KEY
# zsh 5.3+ and bash:
history -d $(history | tail -1 | awk '{print $1}')
```

If you want to verify it's gone: `history | grep WISPRALT_API_KEY` should
return nothing.

### Paranoid pattern (env-file, no shell history exposure)

If you'd rather never type the key on a command line at all:

```bash
umask 077
cat > /tmp/wispralt-key <<'EOF'
export WISPRALT_API_KEY=sk_xxx
export WISPRALT_SERVER=https://transcribe.integrateapi.ai
EOF
chmod 0600 /tmp/wispralt-key
source /tmp/wispralt-key
curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh | bash
shred -u /tmp/wispralt-key 2>/dev/null || rm -P /tmp/wispralt-key
unset WISPRALT_API_KEY WISPRALT_SERVER
```

(macOS doesn't ship `shred`; the `rm -P` fallback overwrites the file before
unlinking.)

## 2. Prerequisites

- **macOS 14 (Sonoma) or newer** — earlier versions are not supported.
- **Apple Silicon** — current builds are arm64-only. Intel Macs are not
  supported; contact your admin if this is a blocker.
- **~200 MB free disk space** for the app bundle.
- **Internet** — anonymous; no GitHub auth needed for the download itself.
- **Xcode Command Line Tools** — install with `xcode-select --install` if
  you've never used the developer tools on this Mac. The first-run `python3`
  invocation will pop the install dialog automatically and add ~700 MB of
  tooling (~5 minutes), so installing it ahead of time keeps the install
  flow clean.

## 3. The 4 macOS permissions WisprAlt needs

Apple's TCC (Transparency, Consent, and Control) framework will not let any
script auto-grant these — that's a security boundary, not a bug in the
installer. The app's PermissionGate UI walks you through each pane on first
launch.

- **Accessibility** — required to inject transcribed text at the cursor in
  any app via AXUIElement.
- **Input Monitoring** — required to detect FN-key holds (the global hotkey
  that triggers dictation).
- **Microphone** — required to capture your voice.
- **Screen Recording** — required for meeting-mode recording, which uses
  ScreenCaptureKit to capture system audio alongside the mic.

On macOS 14.4+, granting Input Monitoring requires a quit-and-reopen of the
app before the OS will treat the grant as effective.

## 4. Per-employee install (admin texted you the command)

This is the common case. Your admin generated a token for you and texted
you a one-liner. Paste it into Terminal exactly as received:

```bash
curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh \
  | WISPRALT_API_KEY=sk_yourtoken WISPRALT_SERVER=https://transcribe.integrateapi.ai bash
```

Then run the [history cleanup](#history-cleanup-recommended) above.

## 5. Self-install (you have the URL but no key)

If you have the server URL but no API key, run the installer with only the
server env var:

```bash
WISPRALT_SERVER=https://transcribe.integrateapi.ai \
  curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh | bash
```

The app will install and launch normally. Open WisprAlt menubar icon →
Settings → Advanced and paste your API key there. The app stores it in the
Keychain via the same code path the installer uses.

## 6. Updating

**Re-running the same one-liner is the update path.** `install.sh` is
idempotent: it downloads the latest signed build, replaces
`/Applications/WisprAlt.app`, and re-applies your config.

> **⚠️ WARNING — Keychain rotation.**
> Re-running with a different `WISPRALT_API_KEY` **silently rotates** the
> stored Keychain entry. This is by design — it's the rotation flow.
> Useful when your admin issues you a new token; surprising if you
> didn't mean to.

> **⚠️ WARNING — Server URL overwrite.**
> Re-running with no `WISPRALT_SERVER` env var **silently overwrites** any
> custom serverURL back to the default
> (`https://transcribe.integrateapi.ai`). If you've pointed the client at
> a staging server via Settings → Advanced, you must re-set it after each
> install/update, or pass `WISPRALT_SERVER=...` on the install command.

If you only want to update the binary without touching either, run the
installer with both env vars set to your existing values.

## 7. What gets stored where

Everything the installer writes is user-scoped — nothing goes into
`/Library/`, nothing requires sudo.

| What | Where | Inspect with |
|---|---|---|
| App bundle | `/Applications/WisprAlt.app` | `ls -la /Applications/WisprAlt.app` |
| API key | macOS Keychain (service `co.wispralt`, account `default`) | `security find-generic-password -s co.wispralt -a default -w` |
| Server URL | UserDefaults (`co.wispralt.WisprAlt`, key `serverURL`) | `defaults read co.wispralt.WisprAlt serverURL` |
| UserDefaults plist | `~/Library/Preferences/co.wispralt.WisprAlt.plist` | `plutil -p ~/Library/Preferences/co.wispralt.WisprAlt.plist` |
| Logs | `~/Library/Logs/WisprAlt/` | `ls ~/Library/Logs/WisprAlt/` |
| Cache | `~/Library/Caches/co.wispralt.WisprAlt/` | `ls ~/Library/Caches/co.wispralt.WisprAlt/` |

The Keychain inspect command will pop a dialog asking you to authenticate
with your login password — that's the OS, not WisprAlt.

## 8. Rotating the API key

When your admin issues a new token:

- **Either** re-run the install one-liner with the new `WISPRALT_API_KEY`
  value — the Keychain entry is silently rotated (see [Updating](#6-updating)
  above);
- **Or** open WisprAlt menubar icon → Settings → Advanced and paste the new
  token there. Same Keychain code path either way.

The old token continues to work until your admin revokes it server-side, so
you don't have to coordinate the rotation tightly.

## 9. Uninstalling

Copy-paste this block into Terminal. It quits the app, removes the bundle,
clears all four TCC permissions, deletes UserDefaults, removes the Keychain
entry, deletes logs and caches, and forces `cfprefsd` to forget the plist
cache:

```bash
pkill -f /Applications/WisprAlt.app/Contents/MacOS/WisprAlt 2>/dev/null
rm -rf /Applications/WisprAlt.app
tccutil reset All co.wispralt.WisprAlt 2>/dev/null
defaults delete co.wispralt.WisprAlt 2>/dev/null
security delete-generic-password -s co.wispralt -a default 2>/dev/null
rm -rf ~/Library/Logs/WisprAlt ~/Library/Caches/co.wispralt.WisprAlt
killall cfprefsd 2>/dev/null
```

After this, a fresh install will behave exactly as a first-time install on
this machine — TCC re-prompts and all.

## 10. Troubleshooting

### "I have an old / renamed WisprAlt copy somewhere — will the installer find it?"

Yes. As of v0.5.0 the installer is **bundle-ID-driven**: it uses `mdfind` to
enumerate every WisprAlt bundle anywhere on the filesystem (including
`~/Applications/`, renamed copies, or whatever path an earlier install dropped
it at), not just `/Applications/`. The curl one-liner is idempotent — re-running
it cleanly removes orphan bundles, gracefully quits running instances via
AppleScript (with a `pkill -f co.wispralt.WisprAlt` fallback for stragglers),
and reinstalls to the canonical `/Applications/WisprAlt.app` path. Any
`~/Library/LaunchAgents/co.wispralt*.plist` files matching the WisprAlt prefix
are also swept on install so a stale autostart entry from an old install
location cannot relaunch a deleted binary.

### "macOS says 'Apple cannot check it for malicious software'"

The signed build is notarized, but Gatekeeper occasionally surfaces this
warning on first launch (especially after a download via curl rather than
Safari). Right-click `/Applications/WisprAlt.app` in Finder → **Open** →
**Open Anyway** in the dialog.

If that doesn't clear it, strip the quarantine xattr and re-open:

```bash
xattr -cr /Applications/WisprAlt.app && open /Applications/WisprAlt.app
```

### "Test Connection failed"

Verify the local config is what you expect:

```bash
security find-generic-password -s co.wispralt -a default -w   # API key
defaults read co.wispralt.WisprAlt serverURL                  # server URL
```

If both look right, check the server is reachable:

```bash
curl -fsSL https://transcribe.integrateapi.ai/healthz
```

A `{"status":"ok"}` response means the server is up; the failure is
probably the token. Ask your admin to confirm it hasn't been revoked.

### "GitHub API rate limit"

The installer hits the GitHub API to find the latest release. Anonymous
calls are limited to **60 per hour per IP**. If you hit the limit (multiple
re-installs, shared NAT, etc.), either wait an hour or set a token:

```bash
GITHUB_TOKEN=ghp_yourtoken WISPRALT_API_KEY=sk_xxx \
  curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh | bash
```

A no-scope personal access token is fine; it just needs to authenticate the
rate-limit bucket.

### "Refusing to run as root" / "Refusing to run under sudo"

Don't `sudo` the curl pipe. The install is user-scoped by design — the
Keychain and UserDefaults are per-user, and running the installer as root
would write everything into the wrong account. The installer aborts on
purpose to prevent that footgun.

### "Apple Silicon required"

Current builds are arm64-only. Intel Macs aren't supported; contact your
admin.

### "python3 not available"

The installer uses `python3` for a tiny JSON parse. Run
`xcode-select --install`, accept the dialog, and re-run the install
one-liner.

### "App opens but the menubar icon doesn't appear"

LSUIElement (menubar-only) cold-start glitch — usually self-resolves on the
next launch. Force-restart:

```bash
pkill -9 -f WisprAlt && open /Applications/WisprAlt.app
```

If it persists, file an issue with `~/Library/Logs/WisprAlt/` attached.
