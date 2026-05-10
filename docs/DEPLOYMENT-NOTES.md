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

## MLX Whisper dependency + model cache

The meeting/file transcription path uses [`mlx-community/whisper-large-v3-turbo`](https://huggingface.co/mlx-community/whisper-large-v3-turbo) on Apple Neural Engine via `mlx_whisper`. Both the Python dependency and the on-disk model cache have version footguns.

**Pinned dependencies in `server/pyproject.toml`:**

| Package | Pin | Why |
|---|---|---|
| `mlx-whisper` | `==0.4.2` | Hard pin — the tqdm-monkeypatch shape in `mlx_whisper_loader.py` is verified against this exact source. A minor bump can move `tqdm.auto.tqdm.update` and silently break the chunk-progress callback (UI shows no progress; transcription still works). |
| `huggingface-hub` | `>=1.12.0,<1.13` | Upper-bounded because `pyannote.audio==3.3.2` still calls the removed-in-2.0 `use_auth_token=` kwarg. The compat shim in `meeting/__init__.py install_compat_shims` translates `use_auth_token=` → `token=` at import time, and that shim is verified against the 1.12 surface. Bumping past 1.13 needs a fresh audit of the shim and the pyannote release notes. |

**Model cache:** Hugging Face stores snapshots under `~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo/`. Disk footprint is **~1.6 GB** (the `model.safetensors` weight file alone is ~1.5 GB; tokenizer + config + generation config make up the rest). The prefetch script asserts `model.safetensors > 800 MB` as a sanity check — see the recovery section below.

**Prefetch step:** run `server/scripts/prefetch-mlx-whisper.sh` once at deploy time. It calls `huggingface_hub.snapshot_download(repo_id=..., revision=..., resume_download=True)` then asserts the size of `model.safetensors`. Idempotent — re-running on a healthy cache is a no-op.

## `scripts/deploy-server.sh`

Versioned deploy script for the mini. Contract:

1. Reads server code from the dev box, tarballs it (excludes `__pycache__`, `.venv`, `*.log`), transfers to the mini via the `/macmini paste` skill's gist-transport mechanism.
2. On the mini: creates `.wf-deploy-backup-<epoch>/` (using `mkdir -p` first — BSD `cp -r src1 src2 dst` requires `dst` to be an existing dir), then `cp -r server scripts "$BACKUP/"`, then overwrites with the new tree.
3. Runs `uv sync` to install/upgrade pinned deps.
4. Runs `server/scripts/prefetch-mlx-whisper.sh` (idempotent).
5. `launchctl kickstart -k gui/$UID/co.wispralt.server`.
6. Polls `/healthz` for up to 60 s.

**`set -e` polling bug fix.** Earlier iterations of the deploy script wrote `code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/healthz)` inside the poll loop with `set -e` at the top. The first poll iteration runs while the server is still binding the port — `curl` exits with code 7 (connect refused) — and `set -e` aborts the entire deploy script. The deploy actually succeeded (the server came up seconds later), but the script falsely reported FAILED. **Fix:** always pair the poll with `|| echo "000"`:

```bash
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/healthz || echo "000")
```

Idempotency: the script is safe to re-run. The backup directory is timestamped per invocation; old backups accumulate and should be hand-pruned every few months.

## Recovery from corrupt mlx-whisper prefetch

**Signs of a torn snapshot:**

- `model.safetensors` smaller than ~800 MB (full file is ~1.5 GB).
- `mlx_whisper.transcribe` raises a safetensors deserialization error on the first call.
- `mlx_whisper_loader.load()` hangs or crashes during the silence-warmup pass.

**Manual recovery:**

```bash
# 1. Remove the bad snapshot
rm -rf ~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo

# 2. Re-run the prefetch (asserts > 800 MB on completion)
bash server/scripts/prefetch-mlx-whisper.sh

# 3. Kickstart the server to force the loader to re-load on first job
launchctl kickstart -k gui/$UID/co.wispralt.server
```

The prefetch script uses `resume_download=True`, so a network blip mid-download recovers on re-run without redownloading completed shards. If `resume_download` itself is broken (it has been flaky in past `huggingface_hub` releases), nuke the directory and start clean as above.

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

Run `scripts/setup-cloudflared.sh` — it creates and loads the LaunchAgent automatically. See the Cloudflared LaunchAgent (user-level) section below for full details on install paths, log locations, and token rotation.

After running the script, verify:

```bash
launchctl print gui/$UID/co.wispralt.cloudflared    # verify state=running
```

The tunnel registers immediately and persists across reboots and crashes.

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
| `~/.config/wispralt/cloudflare-token` | 0600 | Cloudflare tunnel token only |
| `~/.config/wispralt/client-config.txt` | 0600 | `SERVER_URL`, `WISPRALT_API_KEY` (for paste-into-client) |

`.gitignore` covers all three. Cloudflare tunnel token is **never** stored in `server/.env` — it lives in `~/.config/wispralt/cloudflare-token` and is read by the cloudflared LaunchAgent via `--token-file` at start time.

### Rotating tokens

After any setup involving copy/paste into shared tools (chat, screen recordings, etc.), rotate:

- **HF token:** revoke at <https://huggingface.co/settings/tokens>, generate a new one, update `server/.env`, restart the FastAPI LaunchAgent.
- **Cloudflare tunnel token:** in the Zero Trust dashboard → tunnel → Configure → Rotate token. See "Cloudflared LaunchAgent (user-level)" below for the rotation procedure — the steps differ depending on whether your cloudflared version supports `--token-file`.
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
The script handles everything: SPM release build, `.app` bundle assembly with bundled `Sparkle.framework`, **Apple Development codesign** with entitlements (requires a free Apple Development certificate from Xcode → Settings → Accounts — see [Code-signing prerequisite](SETUP-CLIENT.md#code-signing-prerequisite-for-local-builds)), and an `otool` verification that the `@executable_path/../Frameworks` rpath is wired (without it, the app crashes at launch with `Library not loaded: @rpath/Sparkle.framework/...`). Output: `client/build/WisprAlt.app`. Right-click → Open the first time to bypass Gatekeeper. If you have multiple `Apple Development` identities in your keychain, set `SIGN_IDENTITY` explicitly: `SIGN_IDENTITY="Apple Development: you@example.com (TEAMID)" ./scripts/build-client-local.sh`.

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
Run with ⌘R. Xcode handles the bundling automatically and signs with whichever identity your Personal Team provides — typically an `Apple Development` certificate (free, issued automatically when you sign into Xcode with any Apple ID at Settings → Accounts). This matches Path A's signing requirement for `SMAppService.mainApp.register()`.

For distributing to friends without a per-machine Apple ID setup, you'd need an Apple Developer Program enrollment ($99/yr) and `scripts/build-client.sh`'s notarized-DMG flow (inherits the same `Package.swift` rpath setting, plus a pre-notarization `otool` check). The free Apple Development cert is sufficient for personal use across your own Macs.

## Audio capture: write Float32, NOT Int16

Two separate AVFoundation conversion bugs we hit and worked around:

1. **`AVAudioConverter` default channel-mix** sums input channels rather than averaging. Stereo input → mono produces peak floats ≈ 3.97. We dropped the converter entirely; server resamples + downmixes via librosa.

2. **`AVAudioFile.write(from:)` with Int16 commonFormat** applies a buggy ~140x amplification when the source buffer is Float32 and target settings ask for Int16. A clean 0.24-peak voice writes as Int16 ≈ 32750 (rail-clipped). Decoded fine on server, but Parakeet returned random one-word hallucinations from the destroyed audio. Fix: write Float32 at native sample rate, matching the tap buffer's format byte-for-byte. AVAudioFile then performs zero conversion. Server's `audio.py` reads Float32 via soundfile + librosa.

The diagnostic that caught it: log the **pre-write float peak** from inside the tap callback alongside the post-write file peak. When pre-write peak ≈ 0.24 but post-write Int16 peak ≈ 32750, the conversion stage is doing something it shouldn't.

## TCC permissions and Apple Development-signed builds

macOS Tahoe (26) keys Apple Development-signed apps in TCC by `cdhash` (a hash of the binary) rather than by team identifier. Only paid **Developer ID** apps get team-identifier-based matching that survives binary changes. This means:

- Every new build → new cdhash → TCC sees a brand-new app → re-prompts for all four permissions (Accessibility, Input Monitoring, Microphone, Screen Recording).
- Kill + relaunch of the **same build** reuses the existing TCC grants without re-prompting.
- There is no workaround short of a Developer ID certificate.

### Re-grant on rebuild looks like a bug but isn't

After every rebuild, run the canonical TCC reset to clear stale entries before re-granting. Sometimes System Settings shows toggles as "on" but TCC has the old cdhash internally — symptom: "I granted them but the app still says denied". The reset fixes this:

```bash
tccutil reset Accessibility   co.wispralt.WisprAlt
tccutil reset ListenEvent     co.wispralt.WisprAlt
tccutil reset ScreenCapture   co.wispralt.WisprAlt
tccutil reset Microphone      co.wispralt.WisprAlt
```

Then reopen the app and re-grant all four permissions.

**Practical mitigation when iterating:** batch code changes and rebuild once rather than after each edit.

---

## Cloudflared LaunchAgent (user-level)

The cloudflared tunnel runs as a **user-level LaunchAgent** — not as a system LaunchDaemon. This is the only reliable path on macOS 14/15+. See the "Why not cloudflared service install?" section above for why the system path is broken.

**Install location:** `~/Library/LaunchAgents/co.wispralt.cloudflared.plist`

**Log paths:**
- stdout: `~/Library/Logs/WisprAlt/cloudflared.log`
- stderr: `~/Library/Logs/WisprAlt/cloudflared.err.log`

The plist is generated by `scripts/setup-cloudflared.sh`. It sets `RunAtLoad: true` and `KeepAlive: {SuccessfulExit: false, NetworkState: true}` with `ThrottleInterval: 10` so launchd restarts cloudflared at most every 10 seconds if it crashes. `EnvironmentVariables/PATH` is set explicitly so cloudflared can find Homebrew binaries even in the minimal launchd environment.

**Token storage:** the token is stored at `~/.config/wispralt/cloudflare-token` (mode 0600). On cloudflared ≥ 2025.4.0, the plist references it via `--token-file`; older versions inline the token in the plist (also mode 0600).

### Token rotation

Check whether your cloudflared version supports `--token-file`:

```bash
if cloudflared tunnel run --help 2>&1 | grep -q -- '--token-file'; then
    echo "modern path — use procedure A"
else
    echo "legacy path — use procedure B"
fi
```

**Procedure A (modern — cloudflared ≥ 2025.4.0, `--token-file` supported):**

```bash
# 1. Read the new token silently
read -r -s -p "New Cloudflare Tunnel token: " NEW_TOKEN
echo

# 2. Atomically replace the token file
TMP="$(mktemp)"
chmod 0600 "$TMP"
printf '%s' "$NEW_TOKEN" > "$TMP"
mv "$TMP" ~/.config/wispralt/cloudflare-token
unset NEW_TOKEN

# 3. Restart cloudflared so it picks up the new token file
launchctl bootout gui/$UID/co.wispralt.cloudflared
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/co.wispralt.cloudflared.plist
```

**Procedure B (legacy — cloudflared < 2025.4.0, token is baked into the plist):**

The token is inlined in `ProgramArguments`. Simply updating the token file won't help — the plist still has the old token. Re-run `setup-cloudflared.sh` entirely to regenerate the plist with the new token:

```bash
bash scripts/setup-cloudflared.sh
```

This tears down the old LaunchAgent, prompts for the new token, regenerates the plist, and reloads.

---

## Client login-launch via SMAppService

WisprAlt registers itself as a login item using `SMAppService.mainApp.register()` on first launch. This creates a standard System Settings → General → Login Items & Extensions entry so the menubar app relaunches automatically after every login.

**How it works:** `AppDelegate.applicationDidFinishLaunching` calls `SMAppService.mainApp.register()`. The call is idempotent — subsequent launches do nothing if already registered. The registration is tied to the app's code-signing Designated Requirement (DR), not the path, so moving the app does not break it.

**How to disable:** toggle off in System Settings → General → Login Items & Extensions, or use the **Launch at login** toggle in the WisprAlt settings popover.

**What happens on rebuild:** `SMAppService` binds the login item to the app's DR. A new build with a new Apple Development cert (e.g. after annual renewal) will have a new DR. The old login item entry becomes stale. The new build re-registers on first launch, and the old entry clears automatically. In practice this means the login item survives most rebuilds as long as the same cert identity is used; it re-registers transparently when the cert changes.

---

## Quarantine on first download

macOS Gatekeeper quarantines any app downloaded from the internet (Safari, curl, a browser, AirDrop from an unknown sender). Apple Development-signed builds are not notarized, so Gatekeeper shows "developer cannot be verified" on first open.

**Fix for the app owner or friend installing manually:**
```bash
xattr -dr com.apple.quarantine /path/to/WisprAlt.app
open /path/to/WisprAlt.app
```

Or: right-click the `.app` in Finder → **Open** → click **Open Anyway** in the dialog. The quarantine warning appears only once per version; after approval the app opens normally on all subsequent launches.

The `/setup-client` slash command runs `xattr -dr` automatically. Friends installing the DMG by hand need to do this step themselves.

---

## Annual cert renewal

Free Apple Development certificates auto-renew once per year. Xcode handles renewal silently while you stay signed into your Apple ID in Xcode → Settings → Accounts.

**Why this matters:** the renewed cert has a new SHA-1 → new Designated Requirement → new cdhash. On the next rebuild after renewal, TCC sees the app as a new entity and re-prompts for all four permissions, identical to any other rebuild. Run the standard `tccutil reset` recovery and re-grant.

This happens roughly once a year. It is expected behavior, not a bug. If you see unexpected TCC re-prompts without rebuilding, check for a cert renewal:

```bash
security find-certificate -c "Apple Development:" -p login.keychain | \
  openssl x509 -noout -dates
```

The "Not Before" date should be within the last year if a renewal just occurred.

---

## What's NOT documented elsewhere

- **The user-level LaunchAgent for cloudflared** is the only reliable persistence path on Mac. The system LaunchDaemon path is broken on at least the `cloudflared service install` versions we tested. Document this prominently in any production fork.
- **Don't run `sudo cloudflared service install <TOKEN>` and `cloudflared tunnel run --token <T>` simultaneously** — they fight over the tunnel registration. The dashboard shows whichever one Cloudflare last heard from. Always pick one method.
- **Cloudflare's "INACTIVE" status takes ~30s to update** even after the connector starts emitting traffic. Wait before assuming it's broken.
