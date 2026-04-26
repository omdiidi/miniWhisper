# Deployment Notes — Lessons from a Real First Install

This document captures the issues actually encountered the first time someone bootstrapped WisprAlt on a Mac mini, and the fixes applied. Use this as a supplement to `SETUP-SERVER.md` — every entry below maps to a real failure mode.

## Server bootstrap (`scripts/setup-server.sh`)

### Dependency conflict: `numpy` ranges between MLX, WhisperX, and DeepFilterNet

**Symptom:** `uv sync` resolves to no candidate version because `parakeet-mlx==0.5.1` wants `numpy<=2.5`, `deepfilternet==0.5.6` wants `numpy<2.5`, and `whisperx>=3.8.5` indirectly pulls a different range.

**Root cause:** the original pin set was unsatisfiable on Apple Silicon with Python 3.11.

**Fix applied:** dropped `deepfilternet` from `server/pyproject.toml` and stubbed `server/src/wispralt_server/audio/denoise.py` to a no-op. Meeting transcription still works (Pyannote diarization + WhisperX is unaffected); only the explicit denoise pass is skipped. Mic input that is already clean transcribes identically.

**Trade-off accepted:** slightly noisier audio in low-SNR meeting recordings. Re-add DeepFilterNet later if needed via a separate venv invoked over subprocess.

### `huggingface-hub` not in declared deps

**Symptom:** `uvicorn` boots but immediately errors with `ImportError: cannot import name 'cached_download' from 'huggingface_hub'` (or similar) when bootstrapping models.

**Root cause:** `parakeet-mlx`, `whisperx`, and `pyannote.audio` all transitively depend on `huggingface_hub`, but with different version ranges, and none of the resolved versions exposed the symbol the resident code expected.

**Fix applied:** added `huggingface-hub>=0.30.2` as an explicit top-level dep in `server/pyproject.toml`.

### `pyannote.audio` with PyTorch 2.6: `torch.load` weights_only default change

**Symptom:** server crashes on `pipeline = Pipeline.from_pretrained(...)` with `_pickle.UnpicklingError: Weights only load failed`.

**Root cause:** PyTorch 2.6 flipped `weights_only=True` as the default for `torch.load`, which breaks loading of the legacy pyannote checkpoint format (the tensors are wrapped in pickled objects).

**Fix applied:** patched `server/src/wispralt_server/meeting/__init__.py` to monkey-patch `torch.load` with `weights_only=False` only during pyannote pipeline init, then restore the default.

### Pyannote 0.5+ renamed `use_auth_token` → `token`

**Symptom:** `Pipeline.from_pretrained(..., use_auth_token=...)` raises `TypeError: from_pretrained() got an unexpected keyword argument 'use_auth_token'`.

**Fix applied:** the patched init in `meeting/__init__.py` now calls `from_pretrained(..., token=hf_token)`.

### `huggingface-cli` renamed to `hf` in 1.x

**Symptom:** `scripts/download-models.sh` errors with `huggingface-cli: command not found` after a fresh `uv sync`.

**Fix applied:** the script now invokes `huggingface-cli download` only when that binary is on PATH; otherwise falls back to the new `hf download` CLI. Both are documented at <https://huggingface.co/docs/huggingface_hub>.

## Cloudflare Tunnel

This was the most painful part of the install. The flow that **actually works** on macOS in late 2025/2026:

1. **Create the tunnel** via the Zero Trust dashboard → Networks → Connectors → Cloudflared. The dashboard generates a long base64 token (`eyJhIjoi...`).
2. **Add a Published Application Route** mapping your hostname (e.g. `transcribe.example.com`) to `http://localhost:8080`.  
   ⚠️ `8080` is the default FastAPI port we ship with — match it to whatever the LaunchAgent actually binds.
3. **Run cloudflared** on the mini in a way that survives reboots. Use a **user-level LaunchAgent**, not the system LaunchDaemon installed by `cloudflared service install`.

### Why not `cloudflared service install <TOKEN>`?

In our testing on macOS 14/15+ (Apple Silicon), `sudo cloudflared service install <TOKEN>` wrote a `/Library/LaunchDaemons/com.cloudflare.cloudflared.plist` whose `ProgramArguments` was just `["cloudflared"]` — without the `tunnel run --token <T>` arguments. The result is `cloudflared` starts with no args, prints help, and exits. The Cloudflare dashboard reports the tunnel as INACTIVE indefinitely.

This is a known issue with older `cloudflared` versions and/or with the mac install path interacting with launchd.

### What works: a user LaunchAgent

Create `~/Library/LaunchAgents/co.wispralt.cloudflared.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>co.wispralt.cloudflared</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>-c</string>
        <string>exec /opt/homebrew/bin/cloudflared tunnel run --token "$(grep ^TUNNEL_TOKEN $HOME/wispralt/tmp/credentials.txt | cut -d= -f2)"</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>/Users/USERNAME/Library/Logs/WisprAlt/cloudflared.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/USERNAME/Library/Logs/WisprAlt/cloudflared.log</string>
</dict>
</plist>
```

Then load it (no sudo):

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/co.wispralt.cloudflared.plist
launchctl print gui/$UID/co.wispralt.cloudflared    # verify state=running
```

The tunnel registers immediately, persists across reboots and crashes, and you can rotate tokens by editing `tmp/credentials.txt` and `launchctl kickstart -k gui/$UID/co.wispralt.cloudflared`.

### Don't forget: `brew upgrade cloudflared`

The Cloudflare dashboard shows a banner when the connector version is old. Older `cloudflared` (before about 2024.10) doesn't accept `tunnel run --token <T>` syntax — instead, running `cloudflared tunnel run --token …` dumps the help text and exits. Always upgrade before troubleshooting:

```bash
brew upgrade cloudflared
cloudflared --version    # should show 2024.10.x or newer
```

### Diagnosing 530 / Cloudflare error 1033

If `https://your-host/healthz` returns HTTP 530 with `error code: 1033`, walk the chain in this order:

1. **Local FastAPI:** `curl http://127.0.0.1:8080/healthz` on the mini. Expect `{"status":"ok"}`. If 200 → FastAPI is fine, problem is in cloudflared.
2. **Cloudflared process:** `pgrep -lf cloudflared`. If empty → cloudflared isn't running. If a PID exists, check `lsof -nP -p <PID> | grep ESTABLISHED` — there should be outbound connections to Cloudflare's edge (port 7844 or 443).
3. **Tunnel registration:** Cloudflare dashboard → your tunnel → Overview tab. Status must be HEALTHY with at least one connector listed. If INACTIVE, the running cloudflared isn't successfully registering — check `~/Library/Logs/WisprAlt/cloudflared.log` for errors like `Couldn't fetch tunnel`, `Login required`, or `Tunnel ID invalid`.
4. **Route mapping:** Published Application Routes tab. The route must point to the EXACT host:port that FastAPI binds to. `http://localhost:8080` matches a server bound to `127.0.0.1:8080`; `http://localhost:8765` would not.

## Secrets handling

### Generated artifacts during setup

The setup writes three files containing secrets. None are committed:

| File | Mode | Contains |
|---|---:|---|
| `~/wispralt/server/.env` | 0600 | `HF_TOKEN`, `WISPRALT_API_KEY` |
| `~/wispralt/tmp/credentials.txt` | 0600 | `HF_TOKEN`, `TUNNEL_TOKEN`, `SERVER_URL` |
| `~/.config/wispralt/client-config.txt` | 0600 | `SERVER_URL`, `WISPRALT_API_KEY` (for paste-into-client) |

`.gitignore` covers all three. Cloudflare tunnel token is **never** stored in `server/.env` — it lives only in `tmp/credentials.txt` and is read at LaunchAgent start time.

### Rotating tokens

After any setup involving copy/paste into shared tools (chat, screen recordings, etc.), rotate:

- **HF token:** revoke at <https://huggingface.co/settings/tokens>, generate a new one, update `server/.env`, restart the FastAPI LaunchAgent.
- **Cloudflare tunnel token:** in the Zero Trust dashboard → tunnel → Configure → Rotate token. Update `tmp/credentials.txt`. `launchctl kickstart -k gui/$UID/co.wispralt.cloudflared` to pick up the new value.
- **`WISPRALT_API_KEY`:** `curl -X POST -H "Authorization: Bearer $OLD" $SERVER_URL/admin/rotate-key` returns `{"rotated": true}` and writes the new key to the server log; copy it into the client.

## Client (`client/`) — Swift package gotchas

If you build with Swift 6.2 / macOS 26, expect the following compile errors in the v1 codebase. These are pinned to Swift toolchain changes and must be fixed manually until upstream patches land:

- **`Settings { … }` shadowed by a local `Settings` class** in `WisprAltApp.swift`. Qualify as `SwiftUI.Settings { … }`.
- **`SPUUpdater` has no `delegate` property** in modern Sparkle 2.x. Pass the delegate to `SPUStandardUpdaterController(updaterDelegate: self, …)` at construction; you cannot mutate it after.
- **`OSAllocatedUnfairLock` not found**: add `import os.lock` at the top of files that use it (Capture/MeetingRecorder.swift).
- **`captureMicrophone`, `microphone` (SCStream)** are macOS 15+. If you're targeting 14, gate with `if #available(macOS 15, *)` or bump `Package.swift` `platforms: [.macOS(.v15)]`.
- **Optional-binding** in `MeetingAPI.swift` and `ServerClient.swift` — Swift 6 enforces this more strictly. Add explicit `if let` unwraps where the source assumed implicit-non-optional.
- **`isActive` get-only setters** in `MeetingRecorder.swift` — change `private(set) var isActive` to `var isActive` (or wrap setters in a method), or refactor the three call sites to use a separate state machine.
- **Strict concurrency warnings** under `-strict-concurrency=complete` are noisy but mostly non-blocking; the few real ones live in `TranscriptStore.swift`'s `DispatchQueue.main.async { [weak self] … }` blocks where captured `summaries`/`newIndex` need `let` bindings outside the closure.

These are documented in `docs/TROUBLESHOOTING.md` under "Client build errors on Swift 6+" once you fix them in your local copy.

## Build the client without an Apple Developer ID

You don't need a signed/notarized DMG for personal use. Three paths, in order of preference:

**Path A (recommended) — `scripts/build-client-local.sh`:**
```bash
./scripts/build-client-local.sh
```
The script handles everything: SPM release build, `.app` bundle assembly with bundled `Sparkle.framework`, ad-hoc codesign with entitlements, and an `otool` verification that the `@executable_path/../Frameworks` rpath is wired (without it, the app crashes at launch with `Library not loaded: @rpath/Sparkle.framework/...`). Output: `client/build/WisprAlt.app`. Right-click → Open the first time to bypass Gatekeeper.

The Sparkle rpath is set via `Package.swift` `linkerSettings` (`-Xlinker -rpath -Xlinker @executable_path/../Frameworks`). SPM does not add this for executable targets by default — that's the gotcha that breaks every hand-rolled local build.

**Path B — manual SPM + codesign:**
```bash
cd client && swift build -c release --arch arm64
# Wrap the executable in a .app bundle:
mkdir -p WisprAlt.app/Contents/MacOS WisprAlt.app/Contents/Frameworks
cp .build/arm64-apple-macosx/release/WisprAlt WisprAlt.app/Contents/MacOS/
cp -R .build/arm64-apple-macosx/release/Sparkle.framework WisprAlt.app/Contents/Frameworks/
cp WisprAlt/Info.plist WisprAlt.app/Contents/
codesign --force --sign - --entitlements WisprAlt/WisprAlt.entitlements WisprAlt.app
```
Right-click → Open the first time to bypass Gatekeeper. Skips Path A's automated rpath verification — if the build fails at launch, run `otool -l WisprAlt.app/Contents/MacOS/WisprAlt | grep -A2 LC_RPATH` to confirm `@executable_path/../Frameworks` is present.

**Path C — Xcode:**
```bash
xed client/Package.swift
```
Run with ⌘R. Xcode handles the bundling and ad-hoc signing automatically.

For distributing to friends, you do need a Developer ID — `scripts/build-client.sh` covers the full notarized-DMG flow (and inherits the same `Package.swift` rpath setting, plus a pre-notarization `otool` check).

## Audio capture: write Float32, NOT Int16

Two separate AVFoundation conversion bugs we hit and worked around:

1. **`AVAudioConverter` default channel-mix** sums input channels rather than averaging. Stereo input → mono produces peak floats ≈ 3.97. We dropped the converter entirely; server resamples + downmixes via librosa.

2. **`AVAudioFile.write(from:)` with Int16 commonFormat** applies a buggy ~140x amplification when the source buffer is Float32 and target settings ask for Int16. A clean 0.24-peak voice writes as Int16 ≈ 32750 (rail-clipped). Decoded fine on server, but Parakeet returned random one-word hallucinations from the destroyed audio. Fix: write Float32 at native sample rate, matching the tap buffer's format byte-for-byte. AVAudioFile then performs zero conversion. Server's `audio.py` reads Float32 via soundfile + librosa.

The diagnostic that caught it: log the **pre-write float peak** from inside the tap callback alongside the post-write file peak. When pre-write peak ≈ 0.24 but post-write Int16 peak ≈ 32750, the conversion stage is doing something it shouldn't.

## TCC permissions and ad-hoc / self-signed builds

macOS Tahoe (26) keys ad-hoc and self-signed apps in TCC by `cdhash` (a hash of the binary). Every code change → new cdhash → TCC sees a brand-new app and re-prompts for all four permissions (Accessibility, Input Monitoring, Microphone, Screen Recording). This is the Apple-enforced behavior; only **Developer ID** apps get team-identifier-based matching that survives binary changes.

`scripts/setup-local-codesign.sh` adds a self-signed code-signing cert to System trust as a code-signing root. This helps for **launches of the same build** (TCC remembers across kill+relaunch) but does **not** survive rebuilds — macOS still falls back to cdhash for self-signed identities. There's no workaround on the OS side.

Practical mitigation when iterating:
- Make code changes in batches, rebuild less frequently.
- Use `tccutil reset {Accessibility,ListenEvent,ScreenCapture,Microphone} co.wispralt.WisprAlt` to clear stale entries cleanly between rebuilds (sometimes UI shows toggles as "on" but TCC has the wrong cdhash internally — symptom is "I granted them but the app still says denied").

## What's NOT documented elsewhere

- **The user-level LaunchAgent for cloudflared** is the only reliable persistence path on Mac. The system LaunchDaemon path is broken on at least the `cloudflared service install` versions we tested. Document this prominently in any production fork.
- **Don't run `sudo cloudflared service install <TOKEN>` and `cloudflared tunnel run --token <T>` simultaneously** — they fight over the tunnel registration. The dashboard shows whichever one Cloudflare last heard from. Always pick one method.
- **Cloudflare's "INACTIVE" status takes ~30s to update** even after the connector starts emitting traffic. Wait before assuming it's broken.
