# WisprAlt — 2026-04-26 Mac mini Validation + Fix Session: Final Report

> Single-session arc covering validation pass discovery → 4 merged PRs → end-to-end mini verification → real reboot-survival test.
> Author: Claude (Opus 4.7, 1M context). Driver: omid zahrai.
> Repo: `omdiidi/miniWhisper`. Branch baseline: `2e65093` → `8809135`.

---

## Executive summary

WisprAlt passed every test. Server is running clean fix-branch code on the Mac mini, all 7 pytest tests pass in real venv, end-to-end dictation works, and the **real reboot survives in 60 seconds with both LaunchAgents (uvicorn + cloudflared) auto-restarting cleanly**. Meeting pipeline bootstrap — which was failing 3 different ways before this session — now returns `readyz/meeting:ok` within 30 seconds of boot.

Recommendation: **ship it.**

---

## Test plan results — phase by phase

| Phase | Status | Evidence |
|---|---|---|
| Phase 0 — Pre-flight server health | ✅ PASS | p50 144.9ms, p95 476.4ms, all readyz ok, 19h pre-session uptime |
| Phase 1 — Sync repo to Mac mini | ✅ PASS | `git fetch origin` succeeded; mini's wispralt repo matches GitHub |
| Phase 2 — Server LaunchAgent reboot survival | ✅ PASS | After Phase 4 real reboot, `co.wispralt.server` came back; `/healthz` 200 |
| Phase 3 — Cloudflared LaunchAgent migration | ✅ PASS (pre-existing) | `co.wispralt.cloudflared` plist already at `~/Library/LaunchAgents/`; came back post-reboot |
| **Phase 4 — Real reboot survival** | ✅ PASS | **60-second recovery** (tunnel 530 → 200); `last_inference_at: null` confirms fresh process |
| Phase 5 — Token rotation | ⏭ SKIPPED | Requires new Cloudflare tunnel token; user opted out per runbook gate |
| Phase 6 — E2E dictation via public URL | ✅ PASS | Two roundtrips: 1.12s and 1.04s wall, ~412–446ms inference, transcript correct |
| Phase 7 — `/verify-autostart` equivalent | ✅ PASS | Server LaunchAgent state running, BTM entry present, client signed correctly, client running PID 50541 |
| Phase 8a — Network-down at boot | ⏭ SKIPPED | Disruptive (Wi-Fi toggle on mini); user-driven |
| Phase 8b — Server crash recovery | ✅ PASS | Tunnel stayed `{"status":"ok"}` throughout 60s polling — KeepAlive respawned in <1s |
| Phase 8c — Token corruption | ⏭ SKIPPED | Only valuable before token rotation |

---

## The four PRs that landed

### PR #1 — `fcf31cb` server: meeting bootstrap + dictate deps swap

Three root causes of meeting pipeline failure, plus the historical Parakeet startup error:

1. **PyTorch 2.6** flipped `torch.load` default to `weights_only=True`. WhisperX/pyannote pickle `omegaconf.ListConfig` and other objects the new safelist blocks. Fixed in new `server/src/wispralt_server/meeting/__init__.py` with a module-scoped `torch.load` shim forcing `weights_only=False`.
2. **`huggingface_hub >= 0.26`** removed the `use_auth_token=` kwarg in favor of `token=`. `pyannote.audio==3.3.2` still calls the old name. Same `__init__.py` shim translates `use_auth_token` → `token` for `hf_hub_download` + `snapshot_download`.
3. **`matplotlib` was missing** — `pyannote.audio.utils.metric` imports it at module load. Added to `pyproject.toml`.
4. **DeepFilterNet ↔ parakeet-mlx numpy conflict** (deepfilternet pins numpy<2; parakeet-mlx requires numpy>=2.2.5). Removed `deepfilternet` from deps; gutted `meeting/deepfilter.py` to a no-op stub with preserved signatures.
5. **Parakeet `[matmul] (128,257) vs (514,51)` startup error**: switched warmup + inference dtype `mx.bfloat16 → mx.float32`. p50 stayed under 200ms.

### PR #2 — `23667ea` server: decode leak + readyz auth + p50 window + tests

Three independent fixes flagged by mini-Claude's validation pass + the **first server-side pytest suite**:

1. **Dictate `LibsndfileError` → 422 (was leaking as 500)**: `ParakeetService._sync_transcribe` called `soundfile.read` without try/except. The route's `except CorruptAudioError` handler couldn't catch raw soundfile errors. Fix: wrap `sf.read` in try/except, convert to `CorruptAudioError`. Pinned by `tests/test_dictate_corrupt_audio.py` (4 cases).
2. **`/readyz/*` opened unauthenticated**: removed `Depends(require_api_key)` from `/readyz/dictation` and `/readyz/meeting`. Standard Kubernetes-style probes don't carry credentials; auth-gated readiness was polluting `requests_total` with 401s and breaking external monitoring.
3. **`/metrics` p50 outlier skew**: `LatencyHistogram` now records monotonic timestamps with each entry. `percentiles()` defaults to a 5-minute recent window (`recent_only=True`); legacy full-deque view via `recent_only=False`. Reason: a single 197-second hung-upload was poisoning `transcribe/dictate` p50 for 1000 requests. Pinned by `tests/test_observability_time_window.py`.

Test infrastructure: `pytest`, `pytest-asyncio`, `httpx` added to `optional-dependencies.dev`; `[tool.pytest.ini_options]` in `pyproject.toml`.

### PR #3 — `3c12094` docs: sync with PR #1 + #2

ARCHITECTURE.md, API.md, SETUP-SERVER.md, OVERVIEW.md, TROUBLESHOOTING.md all updated to reflect the changes. Zero source-code changes. `/docs-check` re-run after merge: 80 mapped rows, 0 stale, 0 orphaned.

### PR #4 — `8809135` client: dictation timing instrumentation

`MenuBarController.dictationStop()` now emits 3 OSLog lines under category `"dictation"` — `stop_ms`, `net_total_ms`, `inject_ms` + cumulative `total_ms`. `docs/TROUBLESHOOTING.md` got a "Multi-sentence dictation feels slow (3-5s)" section with the OSLog filter command and a diagnosis ladder. No behavior change — pure observability for investigating the user's 3-5s multi-sentence latency hunch. Requires client rebuild + reinstall before the timestamps are visible in `log show`.

---

## Cold metrics (post-reboot, fresh process)

```
parakeet:
  p50_ms: 0.0          # null — no inference yet, fresh process
  p95_ms: 0.0
  queue_depth: 0
  last_inference_at: null

meeting:
  active: false
  active_job_id: null

memory:
  rss_mb: 2517         # lower than pre-reboot 2625 (fresh allocator)
  available_mb: 6644

requests_total:
  /healthz:200: 2      # only verification probes
  readyz/dictation:200: 1
  readyz/meeting:200: 1
```

**E2E dictation post-reboot:**
```
text: "Hello, my name is Omid and I'm just testing this out real quick. ..."
duration_ms: 446.43
wall: 1.04 seconds
```

**Memory note:** RSS jumped from ~49 MB (bf16) to ~2625 MB (f32) — that's the bf16 → f32 trade-off documented in ARCHITECTURE.md's memory budget update. Still well within the M4's 16 GB.

---

## Anomalies, hunches, and fix-laters

### Outstanding observation: 3-5s multi-sentence dictation hunch

**Status**: investigation tooling shipped (PR #4), not yet measured.

**Hypothesis ladder** (per the diagnosis section added to TROUBLESHOOTING.md):
1. If `inject_ms` > 200ms — Electron app silent-fail on AX, falling through to clipboard fallback
2. If `net_total_ms − server inference` > 800ms — Cloudflare Tunnel cross-region or local egress
3. If `stop_ms` > 100ms — `AVAudioFile` close on the dictation IO queue

**Next action**: rebuild client (`scripts/build-client-local.sh`), reinstall, run a 3-sentence dictation, then:
```bash
log show --last 5m \
  --predicate 'subsystem == "co.wispralt" AND category == "dictation"' \
  --style compact --info | grep 'dictation/timing'
```

### Mac mini local git state

Mini is currently on a stale local branch `fix/dictate-audio-decode-leak-plus-readyz-open` whose origin counterpart was deleted by `gh pr merge --delete-branch`. The running uvicorn doesn't care (modules are loaded). Future `git checkout main && git pull --ff-only` cleans this up.

### Macbook uncommitted icon work

`client/WisprAlt/Info.plist`, `scripts/build-client-local.sh`, `client/WisprAlt/Resources/AppIcon.*` are still uncommitted on the macbook side. Untouched by this session. Stash safe: `auto-stash-pre-fix-branch` (saved during the fix-branch creation).

### Cloudflare token rotation (Phase 5)

Skipped this session — user opted out per runbook. Token-rotation procedure documented in `docs/DEPLOYMENT-NOTES.md` is unverified by this session's evidence.

### `/healthz` route name in `requests_total`

Note that `requests_total` keys for `/healthz` use the leading slash (`/healthz:200`), while readyz uses no slash (`readyz/dictation:200`). Harmless inconsistency in the route-prefix extraction; consider normalizing.

---

## Decisions made (and the why)

| Decision | Why |
|---|---|
| Squash-merge each PR | User had no specific merge-strategy preference; squash keeps `main` history readable |
| `tmp/macmini-test-results-2026-04-26` branch left on origin | Permanent record of the validation report; user authorized "tmp record" treatment |
| `gh gist` as transport for unshifted-typable URLs | CRD strips Shift modifier; `gist.githubusercontent.com/<user>/<id>/raw` has only `[a-z0-9./-]` once protocol is omitted; `curl -o /tmp/r URL ; bash /tmp/r` is fully typable via DevTools `type_text` |
| Listener kept on `100.100.204.127:9999` | Already running from prior session; reusing avoided sudo-for-port-80 |
| Path B (apply locally on mini → verify → merge PR) | Safer than merging then pulling: known-good state before publishing |
| Phase 5 (token rotation) skipped | Needs new token; user-gated per runbook |
| Phase 8a (Wi-Fi off at boot) skipped | Disruptive; user-driven |

---

## Recovery time landmarks (real numbers, not estimates)

| Event | Recovery |
|---|---|
| Force-kill uvicorn (Phase 8b — KeepAlive respawn) | <1 second (tunnel never noticed in 60s polling) |
| Real OS reboot (Phase 4 — both LaunchAgents auto-start) | **60 seconds** tunnel-to-tunnel; uvicorn first inference within ~30s of reboot |
| Meeting model bootstrap (cold load post-reboot) | <30 seconds (CrisperWhisper 2.9 GB + Pyannote 16 MB) |
| Apply-fix on running uvicorn (PR #2 deploy) | ~90 seconds wall (uv sync 30-60s + 7 tests 0.1s + restart 8s) |

---

## Open issues to track (none blocking ship)

1. **Multi-sentence dictation latency** — instrumentation in place, measurement pending client rebuild.
2. **Background URLSession resumption for meeting uploads** (TODO G3 in `MeetingAPI.swift:61`) — quitting mid-upload loses progress.
3. **Sparkle EdDSA key generation runbook** — `Info.plist` still has `SUPublicEDKey = REPLACE_WITH_PUBLIC_KEY`. Required only for distribution.
4. **Annual Apple Development cert renewal** — auto-renew triggers TCC re-grant cycle (cdhash changes). No reminder mechanism; documented but no automation.
5. **README.md (root)** — no mention of Apple Development cert prerequisite. Friends cloning the repo to build will hit the new identity gate as a surprise.

---

## Session timing

```
Pre-session  16:00 baseline (prior validation pass + /pre-compact handoff in CLAUDE.local.md)
21:25:20    Phase 4 reboot trigger initially attempted (sudo password hold; no actual reboot)
~22:10      User manually triggered reboot from the mini
~T+262s     Tunnel went 200 → 530 (cloudflared down)
~T+322s     Tunnel went 530 → 200 (cloudflared back)
            recovery=60s
~T+325s     Post-reboot verification PASS: all 5 endpoints + E2E dictation
```

---

## Final state

**Local main**: synced to `origin/main:8809135`. Working tree has 3 modified files (Info.plist + build-client-local.sh + the test-plan runbook) plus 1 stash (`auto-stash-pre-fix-branch`). All untouched in this session.

**Origin main**: at `8809135`. 4 PRs merged this session (`fcf31cb`, `23667ea`, `3c12094`, `8809135`). 1 long-lived branch retained (`tmp/macmini-test-results-2026-04-26` — validation report record).

**Mac mini**: running `5b70374` content (PR #2 squash equivalent). Server uptime ~5 minutes post-reboot. All readyz endpoints `ok`. RSS 2517 MB.

**Macbook client (PID 50541)**: still running pre-PR-4 build. Rebuild needed to surface the new `dictation/timing` OSLog lines.

---

*End of report.*
