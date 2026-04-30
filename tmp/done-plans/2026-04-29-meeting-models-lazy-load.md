# Plan: Lazy-load meeting transcription models (WhisperX + Pyannote)

> **Brief:** [`./tmp/briefs/2026-04-29-meeting-models-lazy-load.md`](../briefs/2026-04-29-meeting-models-lazy-load.md) — read first for the full motivation, decisions, and rejected alternatives.
> **Review log:** Round 1 (23 findings, 18 incorporated). Round 2 (10 findings, 9 incorporated). Round 3 (9 findings, 9 incorporated).

## Goal

Stop eagerly loading WhisperX (`nyrahealth/faster_CrisperWhisper`) + Pyannote (`pyannote/speaker-diarization-3.1`) at server startup. Load them lazily on the first meeting job instead. Keep them resident once warm (no eviction).

**Net effect:** ~2–3 GB resident RAM freed on the Mac mini whenever no meeting has run yet (startup → first meeting). Once a meeting runs, models stay warm — subsequent meetings have zero added latency.

**Out of scope:** model swap (CrisperWhisper stays — see brief). Eviction policy (no eviction — see brief).

---

## Architecture Overview

### Current (eager bootstrap)

```
FastAPI lifespan (main.py):
  ├─ install_compat_shims()                  ← torch.load + HF kwarg patches
  ├─ asyncio.create_task(_bootstrap_and_reenqueue):
  │    ├─ await asyncio.to_thread(bootstrap_models, hf_token)
  │    │    ├─ whisperx_loader.load()        ← ~1.5–2 GB resident
  │    │    ├─ diarize.load(hf_token)        ← ~0.5–1 GB resident
  │    │    └─ deepfilter.get_df()           ← no-op stub
  │    ├─ app.state.meeting_models_ready = True
  │    └─ await meeting_runner.reenqueue_pending()
  └─ ...
```

### After (lazy load on first job)

```
FastAPI lifespan (main.py):
  ├─ install_compat_shims()                  ← unchanged; idempotent + cheap
  ├─ await meeting_runner.reenqueue_pending() ← runs immediately; first job pays load cost
  └─ ...

MeetingRunner._run (jobs/runner.py):
  └─ run_in_executor(meeting_pipeline.transcribe_meeting, ...)
      └─ pipeline.transcribe_meeting():
           ├─ _ensure_models_loaded()        ← NEW; first call: 5–30s, subsequent: 0ms
           │    ├─ acquire threading.Lock (single-flight defensive guard)
           │    ├─ if already loaded → return
           │    ├─ install_compat_shims()    ← idempotent re-call (closes long-window race)
           │    ├─ try: whisperx_loader.load(); diarize.load(hf_token); deepfilter.get_df()
           │    ├─ except: clear partial state on failure (avoid RAM leak), re-raise
           │    └─ on success: set _meeting_models_ready = True
           └─ ... rest of pipeline unchanged
```

### Key invariants preserved

1. **`install_compat_shims()` runs at startup AND inside `_ensure_models_loaded()`** — idempotent, ~ms cost. The startup call patches `whisperx`/`pyannote`/`torch`/`huggingface_hub` references in `sys.modules` immediately. The inner re-call closes the long-window race where any module imported *between* startup and first meeting (potentially hours later) might bind the un-shimmed `torch.load`. Reviewer F20.
2. **Single-job concurrency** — `MeetingRunner` uses `Semaphore(1)` and `ThreadPoolExecutor(max_workers=1)`. Two meeting jobs cannot race the lazy load. The defensive `threading.Lock` in `_ensure_models_loaded` is belt-and-suspenders for any future change to the runner's concurrency.
3. **Partial-load state is reset on failure** — if `_diarize_mod.load()` raises after `_wx_mod.load()` succeeded, we explicitly null `_wx_mod._model`, `_align_model`, `_align_metadata`, `_diarize_mod._pipeline` before re-raising. Otherwise the next retry would call `_wx_mod.load()` again with the previous model still referenced, double-allocating ~1.5–2 GB until GC ran. Reviewer F6.
4. **Re-enqueue still works** — pending jobs from prior runs still get re-enqueued at startup; the first one to execute pays the lazy-load cost.
5. **Parakeet stays eager** — request-blocking, sub-200ms target. Untouched by this plan.

### Readiness contract change

`/readyz/meeting` previously returned 503 until eager bootstrap completed. After this change:

- **Returns 200 from server start onward** when memory is sufficient — the lazy loader is wired and *will* succeed when invoked.
- **New tri-state** exposed via response body: `models_warm: bool` (True iff resident) and `models_loading: bool` (True iff a load is in-flight, derived from `_load_lock.locked()`). Reviewer F4.
- **Always emits both `models_warm` and `available_mb`** so the operator can disambiguate "RAM tight" vs "models cold" at a glance. Reviewer F23.
- This is a **breaking change for any external monitor** that relies on 503-until-warm. We accept it; the server is internal to this single-tenant setup. Two consumers in-tree need updating: `scripts/doctor.sh` and `.claude/commands/update-models.md` (see Tasks).

---

## Files Being Changed

```
server/src/wispralt_server/
├── main.py                           ← MODIFIED — drop bootstrap_models task; reenqueue runs directly; delete app.state.meeting_models_ready
├── meeting/
│   ├── pipeline.py                   ← MODIFIED — DELETE bootstrap_models(); add _ensure_models_loaded() + threading.Lock; call at top of transcribe_meeting(); is_ready() unchanged
│   ├── whisperx_loader.py            ← MODIFIED — add reset() helper; docstring (called from worker thread on demand)
│   ├── diarize.py                    ← MODIFIED — add reset() helper; docstring (same)
│   ├── deepfilter.py                 ← unchanged (no-op stub)
│   └── __init__.py                   ← MODIFIED — wrap install_compat_shims body in threading.Lock (future-safety)
├── routes/
│   ├── health.py                     ← MODIFIED — /readyz/meeting returns 200 when wired; uses pipeline.state(); docstring + comments updated
│   └── admin.py                      ← MODIFIED — /admin/metrics gains meeting.models_warm + models_loading via pipeline.state()

server/tests/
└── test_meeting_lazy_load.py         ← NEW — pytest for _ensure_models_loaded happy path, double-call no-op, partial-failure RAM-leak guard

scripts/
├── smoke-meeting.sh                  ← NEW — end-to-end manual smoke (synthetic 2-ch WAV → upload → poll → download → RSS delta check)
├── doctor.sh                         ← MODIFIED — Check 7 chains smoke-meeting.sh OR polls models_warm; preserves load-order verification
└── README.md                         ← MODIFIED — Check 7 description reflects new semantics

.claude/commands/
└── update-models.md                  ← MODIFIED — verification step uses /readyz/meeting models_warm or chains smoke-meeting.sh; remove stale meeting_models_ready reference

CLAUDE.md                             ← MODIFIED — relax "no model loading per request" rule wording

docs/
├── ARCHITECTURE.md                   ← MODIFIED — startup sequence + meeting pipeline section reflect lazy load
└── API.md                            ← MODIFIED — /readyz/meeting response shape (models_warm + models_loading); /admin/metrics meeting.models_warm
```

---

## Key Pseudocode

### `meeting/pipeline.py` — `_ensure_models_loaded()` with partial-state cleanup

```python
import threading
from wispralt_server.config import settings
from wispralt_server.meeting import install_compat_shims  # NEW import

# REPLACE the existing module-level _meeting_models_ready flag.
_meeting_models_ready: bool = False

# NEW — single-flight load lock. RLock (not Lock) so that any future re-entrance
# from the same thread (e.g., a telemetry decorator wrapping load() that itself
# routes through _ensure_models_loaded) does not deadlock. Same single-flight
# semantics for the cross-thread case (Round 3 F6).
_load_lock = threading.RLock()


def _ensure_models_loaded() -> None:
    """Load WhisperX + Pyannote on first invocation; no-op thereafter.

    Called from transcribe_meeting() inside the meeting executor thread.

    Failure handling: if any sub-load raises, we null all module-level model
    references before re-raising. This prevents a partial-load state where
    WhisperX is resident but `_meeting_models_ready` is False — which would
    cause the next retry to double-allocate WhisperX (~1.5-2 GB) before GC
    could reclaim the first instance. On a 16 GB Mac mini this matters.
    """
    global _meeting_models_ready
    if _meeting_models_ready:
        return
    with _load_lock:
        if _meeting_models_ready:  # double-checked under lock
            return
        logger.info("Lazy-loading meeting pipeline models (first meeting after start) …")
        # Idempotent — the startup install_compat_shims() may have missed any
        # module imported between then and now. Cheap insurance (one bool check
        # on the warm path).
        install_compat_shims()
        try:
            _wx_mod.load()
            _diarize_mod.load(settings.hf_token.get_secret_value())
            _df_mod.get_df()  # no-op today; preserved for parity with old bootstrap
        except Exception:
            # Reset partial Python references so the next retry's load() sees
            # nulled singletons. NOTE (Round 3 F3): this only drops PYTHON
            # references — PyTorch/CTranslate2 native handles may not free
            # immediately, and traceback frames hold locals until they unwind.
            # We call gc.collect() as a hint, but RSS may not drop until the
            # next retry's allocator reuses the slabs. The reset is best-effort.
            import gc
            _wx_mod.reset()
            _diarize_mod.reset()
            # _df_mod has no reset() (no-op stub today) — add one if/when
            # deepfilter is reintroduced (Round 3 F7).
            gc.collect()
            raise
        _meeting_models_ready = True
        logger.info("Meeting pipeline models loaded and resident.")


def is_ready() -> bool:
    """True if models have been loaded.  Drives /readyz/meeting models_warm."""
    return _meeting_models_ready


def is_loading() -> bool:
    """True iff a lazy load is currently in flight.  Drives /readyz/meeting models_loading.

    NOTE: callers that need a coherent (warm, loading) snapshot must use state()
    instead — reading is_ready() and is_loading() separately can observe
    intermediate states between the flag-set and lock-release in
    _ensure_models_loaded() (Round 2 F3).
    """
    return _load_lock.locked() and not _meeting_models_ready


def state() -> tuple[bool, bool]:
    """Best-effort coherent (warm, loading) snapshot for observability endpoints.

    Reads _meeting_models_ready first, then derives loading from the lock. The
    two reads happen between GIL release points, so a microsecond-scale racing
    write can produce (False, False) ("cold and not loading") while a load just
    completed (Round 3 F2). For an observability endpoint this is acceptable —
    the next poll will reflect reality. Do NOT use this snapshot for control
    flow (e.g., "is it safe to attempt a meeting"); use the per-call entry
    through transcribe_meeting() which is properly serialized.
    """
    warm = _meeting_models_ready
    loading = _load_lock.locked() and not warm
    return warm, loading


# DELETE the existing bootstrap_models() entirely. No out-of-tree callers exist
# (single-tenant private codebase). The only in-tree caller is main.py, which
# this plan also updates. No deprecation stub — clean delete keeps surface area
# minimal. (Reviewer F9.)


def transcribe_meeting(...) -> dict:
    _ensure_models_loaded()  # NEW — first line of body
    # ... rest unchanged ...
```

### `main.py` — lifespan changes

```python
# REMOVE: app.state.meeting_models_ready = False  (the flag is now vestigial;
#   pipeline.is_ready() is the single source of truth — Reviewer F8).
# REMOVE: from wispralt_server.meeting.pipeline import bootstrap_models
# REMOVE: the entire `_bootstrap_and_reenqueue` inner async function.
# REMOVE: app.state.bootstrap_task = asyncio.create_task(_bootstrap_and_reenqueue()).
# REMOVE: shutdown branch (lines ~287-290) that cancels app.state.bootstrap_task.

# Replace the section currently at main.py:195-215 with:

# 7. Install compat shims at startup so the deep-patch over sys.modules hits
#    whisperx/pyannote/torch references before any user code runs. The shim is
#    idempotent and re-invoked from _ensure_models_loaded() to close the long
#    window between startup and first meeting (Reviewer F20).
install_compat_shims()

# 7b. Re-enqueue any pending jobs from prior runs. The first one to execute
#     will lazy-load WhisperX + Pyannote inside the executor thread.
try:
    await meeting_runner.reenqueue_pending()
except Exception:  # noqa: BLE001 — never let re-enqueue crash startup
    logger.exception("Meeting reenqueue_pending failed; continuing startup")
```

### `routes/health.py` — `/readyz/meeting`

```python
from wispralt_server.meeting import pipeline as meeting_pipeline

@router.get("/readyz/meeting", ...)
async def readyz_meeting(request: Request) -> JSONResponse:
    available_bytes: int = psutil.virtual_memory().available
    models_warm, models_loading = meeting_pipeline.state()  # atomic snapshot (Round 2 F3)

    common_body = {
        "available_mb": available_bytes // (1024 * 1024),
        "models_warm": models_warm,
        "models_loading": models_loading,
    }

    if available_bytes < _2GiB:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "detail": "Insufficient available memory",
                "required_mb": _2GiB // (1024 * 1024),
                **common_body,
            },
        )
    return JSONResponse(content={"status": "ok", **common_body})
```

### `routes/admin.py` — `/admin/metrics` adds `meeting.models_warm`

```python
# In the metrics handler, the existing `meeting` dict already has fields like
# `active`, `active_job_id`, `completed_24h`. Add:
from wispralt_server.meeting import pipeline as meeting_pipeline

_warm, _loading = meeting_pipeline.state()
meeting_block["models_warm"] = _warm
meeting_block["models_loading"] = _loading
```

### `meeting/whisperx_loader.py` and `meeting/diarize.py` — new `reset()` helpers

Each loader module exposes a `reset()` function that nulls its module-level
singletons. Avoids `pipeline.py` and tests reaching into private attributes
(Round 2 F2).

```python
# whisperx_loader.py
def reset() -> None:
    """Drop singletons so the next load() starts clean. Used on partial-load failure."""
    global _model, _align_model, _align_metadata
    _model = None
    _align_model = None
    _align_metadata = None

# diarize.py
def reset() -> None:
    """Drop the pipeline singleton so the next load() starts clean."""
    global _pipeline
    _pipeline = None
```

### `server/tests/test_meeting_lazy_load.py` — new regression coverage

```python
"""Regression tests for the lazy-load model lifecycle.

Mocks _wx_mod.load, _diarize_mod.load, _df_mod.get_df so we can exercise the
control flow without spending GB-minutes loading real weights.

Three cases (mirrors mercury_safety.py style — focused, no fixtures):
1. Happy path: first call invokes all three; second call invokes none.
2. Partial failure: _diarize_mod.load() raises → flag stays False, reset() called,
   next call retries cleanly.
3. Single-flight: thread B blocks on the lock while thread A is loading;
   when A finishes, B observes the warm flag under the double-checked lock and
   skips the load. Verifies the lock actually serialized them.
"""
from unittest.mock import patch
import threading
import time
import pytest
from wispralt_server.meeting import pipeline as p


def _reset_state() -> None:
    p._meeting_models_ready = False
    p._wx_mod.reset()
    p._diarize_mod.reset()


def test_lazy_load_happy_path():
    _reset_state()
    with patch.object(p, "install_compat_shims"), \
         patch.object(p._wx_mod, "load") as wx, \
         patch.object(p._diarize_mod, "load") as di, \
         patch.object(p._df_mod, "get_df") as df:
        p._ensure_models_loaded()
        p._ensure_models_loaded()  # second call: no-op
    assert wx.call_count == 1
    assert di.call_count == 1
    assert df.call_count == 1
    assert p.is_ready() is True


def test_lazy_load_partial_failure_calls_reset_and_retries_clean():
    _reset_state()
    wx_reset = []
    di_reset = []
    def fake_wx_reset(): wx_reset.append(1)
    def fake_di_reset(): di_reset.append(1)
    with patch.object(p, "install_compat_shims"), \
         patch.object(p._wx_mod, "load"), \
         patch.object(p._wx_mod, "reset", side_effect=fake_wx_reset), \
         patch.object(p._diarize_mod, "load", side_effect=RuntimeError("boom")), \
         patch.object(p._diarize_mod, "reset", side_effect=fake_di_reset), \
         patch.object(p._df_mod, "get_df"):
        with pytest.raises(RuntimeError, match="boom"):
            p._ensure_models_loaded()
    assert p.is_ready() is False
    assert wx_reset == [1]  # reset was called on failure
    assert di_reset == [1]


def test_lazy_load_single_flight_under_concurrency():
    """Thread A enters the load and blocks. Thread B is *proven* to have entered
    _ensure_models_loaded (and thus is blocked on the lock) before A completes.

    Round 3 F1: a plain `t2.is_alive()` check + sleep is insufficient — t2 might
    not yet have reached the function on a loaded CI box. We instead patch the
    function entry to set a `b_entered` Event from t2's frame, and assert it
    fires before releasing thread A.
    """
    _reset_state()
    proceed = threading.Event()
    b_entered = threading.Event()
    call_count = {"n": 0}

    def slow_load():
        call_count["n"] += 1
        assert proceed.wait(timeout=5.0), "test timed out waiting for proceed"

    # Wrapper for thread B: signals entry BEFORE calling _ensure_models_loaded so
    # we can be certain it reached the lock.acquire() call site.
    def b_target():
        b_entered.set()
        p._ensure_models_loaded()

    with patch.object(p, "install_compat_shims"), \
         patch.object(p._wx_mod, "load", side_effect=slow_load), \
         patch.object(p._diarize_mod, "load"), \
         patch.object(p._df_mod, "get_df"):
        t1 = threading.Thread(target=p._ensure_models_loaded)
        t1.start()
        for _ in range(50):
            if p._load_lock.locked():
                break
            time.sleep(0.01)
        assert p._load_lock.locked(), "thread A never acquired the lock"

        t2 = threading.Thread(target=b_target)
        t2.start()
        # Wait until t2 has provably entered its target (and thus is about to
        # call _ensure_models_loaded → block on _load_lock.acquire).
        assert b_entered.wait(timeout=2.0), "thread B never entered target"
        # Give t2 a few ms to reach the lock.acquire() call (no way to observe
        # this directly without instrumenting the lock). Then verify still alive.
        time.sleep(0.05)
        assert t2.is_alive(), "thread B should be blocked on the lock"

        proceed.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

    assert call_count["n"] == 1  # single-flight: load() called once
    assert p.is_ready() is True
```

### `scripts/smoke-meeting.sh` — pseudocode (with RSS delta check)

```bash
#!/usr/bin/env bash
# Smoke test: synthetic 2-ch WAV → upload → poll → download → assert RSS grew.
# Run against a live server (default: $WISPRALT_BASE_URL or transcribe.integrateapi.ai).

set -euo pipefail

BASE_URL="${WISPRALT_BASE_URL:-https://transcribe.integrateapi.ai}"
KEY="$(security find-generic-password -s co.wispralt -w 2>/dev/null \
       || echo "${WISPRALT_API_KEY:-}")"
[ -z "$KEY" ] && { echo "No API key (Keychain or WISPRALT_API_KEY)"; exit 1; }

# 0. Capture pre-meeting RSS via /admin/metrics (if reachable; skip if not).
RSS_BEFORE="$(curl -fsS --max-time 5 -H "Authorization: Bearer $KEY" \
  "$BASE_URL/admin/metrics" 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["memory"]["rss_mb"])' \
  2>/dev/null || echo 0)"

# 1. Generate a 5-second 2-channel 16kHz WAV with sine on ch1 (mic), silence on ch2.
WAV="$(mktemp -t wispralt-smoke-XXXXXX).wav"
python3 -c "
import numpy as np, soundfile as sf, sys
sr, dur = 16000, 5.0
t = np.arange(int(sr*dur)) / sr
ch1 = (0.3 * np.sin(2*np.pi*440*t)).astype('float32')
ch2 = np.zeros_like(ch1)
sf.write(sys.argv[1], np.stack([ch1, ch2], axis=1), sr, subtype='FLOAT')
" "$WAV"

# 2. Submit.
RESP="$(curl -fsS --max-time 30 -H "Authorization: Bearer $KEY" \
  -F "file=@$WAV;type=audio/wav" "$BASE_URL/transcribe/meeting")"
JOB_ID="$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')"
echo "submitted: $JOB_ID"

# 3. Poll up to 5 minutes.
DEADLINE=$(( $(date +%s) + 300 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  STATUS="$(curl -fsS --max-time 10 -H "Authorization: Bearer $KEY" \
    "$BASE_URL/transcribe/meeting/$JOB_ID" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
  echo "  status=$STATUS"
  case "$STATUS" in
    done)   break ;;
    failed) echo "FAILED"; exit 1 ;;
  esac
  sleep 5
done

# 4. Download JSON output and assert it parses + has segments.
#    Pipe via stdin, NOT shell interpolation — prevents JSON-content injection
#    into the python literal string (Round 3 F8).
curl -fsS --max-time 30 -H "Authorization: Bearer $KEY" \
  "$BASE_URL/transcribe/meeting/$JOB_ID/download/json" \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert "segments" in d and "speakers" in d, "missing fields"
print(f"OK: mode={d[\"mode\"]} segments={len(d[\"segments\"])} duration={d[\"duration_s\"]}s")
'

# 5. RSS-delta check (optional — only if metrics were reachable in step 0).
if [ "$RSS_BEFORE" != "0" ]; then
  RSS_AFTER="$(curl -fsS --max-time 5 -H "Authorization: Bearer $KEY" \
    "$BASE_URL/admin/metrics" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["memory"]["rss_mb"])')"
  DELTA=$(( RSS_AFTER - RSS_BEFORE ))
  echo "RSS: ${RSS_BEFORE} → ${RSS_AFTER} MB (Δ ${DELTA} MB)"
  # LOG ONLY — never gate on this value (Round 2 F5).
  # glibc/jemalloc/macOS allocator can hold/release RSS lazily; deltas vary.
  # Cold-start typical: +1500-2500 MB. Warm-run typical: +0-500 MB transient.
  # Operator interprets directionally, not as a pass/fail threshold.
fi

rm -f "$WAV"
```

### `scripts/doctor.sh` — Check 7 update

Replace the current 180s polling loop. New behavior:

- Poll `/readyz/meeting` once. Expect 200.
- Parse `models_warm`. If `true`, pass with "models warm".
- If `false`, that's still a pass for readiness, but emit:

      info "models cold — /readyz/meeting wired but lazy. First meeting will load (5–30s)."
      info "For deploy verification, re-run with --with-warmup to chain scripts/smoke-meeting.sh."

  Stronger language is needed here (Round 2 F6) — without warmup, doctor will not
  catch broken HF tokens, corrupt weights, or wrong-account configs until a real
  user fires the first meeting. The `--with-warmup` flag (parsed at the top of
  doctor.sh) chains `scripts/smoke-meeting.sh` automatically and propagates its
  exit code so deploy automation can hard-fail on a broken weights install.

This preserves the load-order verification operators expect (server is responsive, RAM is OK, first meeting will work) without falsely claiming "models loaded" when they haven't been.

### `.claude/commands/update-models.md` — verification step

Replace lines 23-31 verification block with:

```markdown
3. **Wait for server health** — poll `/readyz/meeting` once (expect 200 immediately):

       curl -fsS -H "Authorization: Bearer $WISPRALT_API_KEY" "$SERVER_URL/readyz/meeting"

   Note: this returns 200 even when models are cold. To verify the new weights actually
   load, run `scripts/smoke-meeting.sh` after the curl. Smoke uploads a 5s test WAV,
   waits for transcription, and prints RSS delta — confirming the lazy load fires
   against the new weights.

4. **Confirm**: print the result of `curl /admin/metrics` showing `meeting.models_warm: true`
   (set by smoke-meeting.sh) and the `last_inference_at` field freshly populated.
```

Replace line 43 (Round 2 F10 — preserve diagnostic value):

```markdown
- If `scripts/smoke-meeting.sh` fails (job stays "running" past 5 min, or "failed"):
  the lazy load likely couldn't fetch/decode the new weights. Check `tail -200
  ~/wispralt/logs/server.error.log` for HF token errors, missing model files, or
  out-of-memory. Verify `HF_TOKEN` is set in `~/wispralt/server/.env` and that you
  have ≥4 GB free RAM (`/admin/metrics` → `memory.available_mb`).
```

---

## Tasks (in implementation order)

> **Order matters (Round 3 F5):** loader `reset()` helpers are created BEFORE pipeline.py references them. Otherwise an implementer landing tasks top-down hits a broken intermediate state.

1. **`server/src/wispralt_server/meeting/whisperx_loader.py`** — add `reset()` helper that nulls `_model`, `_align_model`, `_align_metadata` via `global`. Update `load()` docstring: replace "Must be called exactly once, from the main thread (FastAPI lifespan)" with "Called from the meeting executor thread on first meeting job via `pipeline._ensure_models_loaded()`. Thread-safe (whisperx.load_model + load_align_model are CPU-bound and safe to call off the event loop)."
2. **`server/src/wispralt_server/meeting/diarize.py`** — add `reset()` helper that nulls `_pipeline` via `global`. Update `load(hf_token)` docstring same way.
3. **`server/src/wispralt_server/meeting/__init__.py`** — wrap the body of `install_compat_shims()` in a module-level `threading.Lock` so the deep-patch over `sys.modules` is safe under future multi-threaded callers (Round 2 F8, Round 3 F4). The early `_compat_installed` return stays; only the body needs the lock. **Known residual:** the lock does NOT prevent another thread from importing a new module *between* the snapshot and the patch loop — newly-imported modules will hold un-shimmed references. This is the same long-window race we accept by also re-calling install_compat_shims() inside `_ensure_models_loaded()`. Document this in the docstring.
4. **`server/src/wispralt_server/meeting/pipeline.py`** — add `import threading`, `from wispralt_server.config import settings`, `from wispralt_server.meeting import install_compat_shims`. Add `_load_lock = threading.RLock()`, `_ensure_models_loaded()`, `is_loading()`, `state()`. Update `is_ready()` docstring. Delete `bootstrap_models()` entirely (no stub). Call `_ensure_models_loaded()` as the first line of `transcribe_meeting()`.
5. **`server/src/wispralt_server/main.py`** — in `lifespan`:
   - Delete `from wispralt_server.meeting.pipeline import bootstrap_models` import.
   - Move `install_compat_shims()` call out of the `_bootstrap_and_reenqueue` inner function and call it directly at the same lifespan step.
   - Delete the entire `_bootstrap_and_reenqueue` inner function.
   - Replace with a direct `try/except` around `await meeting_runner.reenqueue_pending()`.
   - Delete `app.state.bootstrap_task` assignment.
   - Delete the shutdown branch (lines ~287-290) that cancels `bootstrap_task`.
   - Delete `app.state.meeting_models_ready = False` initialization (vestigial — `pipeline.is_ready()` is canonical).
6. **`server/src/wispralt_server/routes/health.py`** — import `meeting.pipeline`; in `/readyz/meeting`, return 200 when RAM is sufficient regardless of warm state; emit `models_warm` + `models_loading` + `available_mb` in all response bodies via `meeting_pipeline.state()`. Also rewrite the module docstring (lines 14-21 currently describe eager bootstrap) and the per-route comment at lines 88-89 ("set to True in main.py lifespan") to reflect lazy load (Round 2 F9).
7. **`server/src/wispralt_server/routes/admin.py`** — in `/admin/metrics` handler, import `meeting.pipeline` and add `meeting.models_warm` + `meeting.models_loading` to the existing meeting block via `meeting_pipeline.state()`.
8. **`server/tests/test_meeting_lazy_load.py`** — new pytest covering the 3 cases above (happy path, partial-failure reset, single-flight). Single-flight test uses the `b_entered` Event pattern to prove serialization (not just timing).
9. **`scripts/smoke-meeting.sh`** — new file; chmod +x; matches pseudocode (incl. RSS delta log-only and JSON-via-stdin).
10. **`scripts/doctor.sh`** — replace lines 183-206 with the new Check 7 logic (poll once, parse models_warm, never falsely claim warm). Add top-level `--with-warmup` flag that chains `scripts/smoke-meeting.sh` and propagates exit code.
11. **`scripts/README.md`** — line 70: replace "polls up to 180 s (WhisperX + Pyannote load)" with "polls once; reports `models_warm` (cold is OK — first meeting will load lazily). Pass `--with-warmup` to chain smoke-meeting.sh for full E2E verification."
12. **`.claude/commands/update-models.md`** — replace lines 23-31 verification block per pseudocode above; replace line 43 with the new troubleshooting entry (do not just delete).
13. **`CLAUDE.md`** — replace bullet *"No model loading per request — all models are resident at startup"* with: *"No model loading per dictation request; dictation models are resident at startup. Meeting models load lazily on the first meeting job (async batch — load cost is invisible)."*
14. **`docs/ARCHITECTURE.md`** — update startup sequence section + meeting pipeline section to reflect lazy load. Cross-reference `_ensure_models_loaded`, the RLock rationale, the partial-failure reset (and its best-effort RAM-reclaim caveat).
15. **`docs/API.md`** — `/readyz/meeting` response shape (`models_warm`, `models_loading` fields, contract change note); `/admin/metrics` `meeting.models_warm` + `models_loading` fields. Note that `/transcribe/meeting/{id}` returns `status: "running"` during the lazy-load window — the model load is included in the running duration on the first meeting after start (Round 2 F7). Inline the four-quadrant interpretation table (Round 3 F9):

    | `status` | `models_warm` | `available_mb` | What it means |
    |---|---|---|---|
    | 200 | true | ≥ 2048 | Steady state — submit and go. |
    | 200 | false | ≥ 2048 | Ready but cold — first meeting will pay 5–30s load. |
    | 503 | true | < 2048 | RAM tight — runner.submit_or_429 will reject with 429 until RAM frees. |
    | 503 | false | < 2048 | Cold AND tight — first meeting will likely fail OOM guard before loading. Free RAM first. |

16. **Run pytest** — `cd server && pytest tests/test_meeting_lazy_load.py -v`. All 3 cases must pass.
17. **Manual deploy verification** — push to mini, restart, then run `scripts/smoke-meeting.sh`. Confirm:
    - Server starts in <5s (no eager bootstrap blocking the lifespan).
    - `/readyz/meeting` returns 200 with `models_warm: false` immediately after start.
    - `/admin/metrics` shows pre-meeting `memory.rss_mb` value.
    - First meeting job pays the cold-load cost (~5–30s extra wall time before "done").
    - `/readyz/meeting` returns `models_warm: true` after the first meeting.
    - Second smoke run has no extra latency on the first poll cycle.
    - Smoke script's RSS delta line shows ~1500+ MB growth on cold server, ~0 on warm.

---

## Gotchas & Context

- **`install_compat_shims()` ordering — addressed two ways.** (1) Startup call patches `whisperx`/`pyannote`/`torch`/`huggingface_hub` references that are already in `sys.modules` because `pipeline.py` imports `whisperx` at module top. (2) `_ensure_models_loaded()` re-calls the shim before any actual load, closing the long-window race where any module imported between startup and first meeting could bind to un-shimmed `torch.load`. The shim is idempotent (`_compat_installed` guard at `meeting/__init__.py:62`), so the warm-path cost is one bool check.

- **`hf_token` from the executor thread.** Read via `settings.hf_token.get_secret_value()`. Pydantic `Settings` is thread-safe.

- **Partial-load RAM leak — best-effort mitigation (Reviewer F6, Round 3 F3).** Without explicit reset, a failure during `_diarize_mod.load()` after `_wx_mod.load()` succeeded would leave WhisperX resident (~1.5–2 GB) but `_meeting_models_ready` False. The next retry would call `_wx_mod.load()` *again*. Mitigation: each loader exposes `reset()` which nulls its module-level singletons; `_ensure_models_loaded`'s except block calls them and then `gc.collect()`. **Caveat:** dropping the Python reference does NOT guarantee immediate RAM reclaim — PyTorch and CTranslate2 hold C-level handles, traceback frames retain locals until they unwind, and the OS allocator can keep slabs cached. RSS may stay elevated until the next allocation reuses the freed slabs. The reset is best-effort; it prevents reference accumulation across retries but cannot promise sub-second OS-level reclaim.

- **`bootstrap_models()` is fully deleted, no stub.** Single-tenant private codebase, only one in-tree caller (main.py, also updated in this PR), no test exercises it. Reviewer F9 — keeping a no-op is more surface area, not less.

- **`app.state.meeting_models_ready` is fully deleted.** Reviewer F8 — vestigial after the swap to `pipeline.is_ready()`. Leaving it as a stale `False` flag creates a trap for future code paths that read it expecting truth.

- **Re-enqueue bombing risk (Reviewer F19).** A poison-pill pending job from a prior run will trigger lazy load on its first attempt. If load itself fails (HF flake), `_run`'s except handler marks the job failed — but the per-job `attempts` counter is the only retry guard, and it's bounded at 3 (runner.py:128-135). Worst case: 3 cold-load attempts back-to-back. With the partial-failure reset (above), each attempt starts clean. Acceptable.

- **`/readyz/meeting` 503-on-low-memory branch now includes `models_warm: false` to disambiguate.** A 503 with `models_warm: false` + `available_mb: 1500` tells the operator "RAM tight AND models cold — both block." The previous response was ambiguous between these two cases. Reviewer F23.

- **No client changes needed.** `MeetingAPI.swift` polls job status until `done` or `failed`; doesn't read `models_warm` or `models_loading`. The added 5–30s on the very first meeting is invisible to the user — they already see "transcribing…" for minutes.

- **Future warming triggers (Reviewer F10, F11) deferred.** Pre-warming on first `/readyz/meeting` poll, or on submit-not-execute, are interesting alternatives but add complexity. The brief explicitly accepted "first meeting pays the load cost" and the pre-warm options trade RAM-savings goalpost for marginal latency wins on the first meeting only. Documented in Rejected Alternatives below for future revisitation.

---

## Rejected Alternatives (added during review)

- **Warm on first `/readyz/meeting?warm=true` poll.** Returns 202 while loading; 200 once warm. Preserves the operator-verification surface (poll-until-warm). Rejected: adds a probe-side-effect endpoint, complicates the readiness contract, and conflicts with the brief's "load on first job" decision. May revisit if external monitoring shifts to actively gating on warm models.

- **Warm on first meeting POST (submit-not-execute).** Fires `asyncio.create_task(asyncio.to_thread(_ensure_models_loaded))` from `submit_or_429`. Saves ~5–10s of cold-load latency on the first meeting by running the load in parallel with the client's first poll cycles. Rejected: trade-off vs. the OOM check ordering is non-trivial, and the saved 5–10s on a multi-minute meeting is invisible. Worth ~30 LOC and 0% UX gain.

- **Tri-state `/readyz/meeting` body field instead of two booleans.** A single `models_state: "cold" | "loading" | "warm"` would be cleaner than two booleans. Rejected: existing API contract uses booleans elsewhere (`status: "ok" | "not_ready"`); adding a string enum breaks the consistency. Two booleans + invariant "loading implies not warm" is fine.

---

## Quality Checklist

- [x] All necessary context included (file paths + line numbers + pseudocode)
- [x] Validation gates are executable by AI (pytest unit cases + shell smoke with RSS assertions)
- [x] References existing patterns (mirrors mercury_safety.py test style; preserves install_compat_shims design)
- [x] Clear implementation path (numbered tasks 1–16)
- [x] Error handling documented (partial-state reset, re-enqueue retry cap, /readyz tri-state)
- [x] Files Being Changed tree filled in (10 src files + 1 new test + 1 new script + 4 docs)
- [x] Architecture overview proportional (current vs. after diagram + 5 invariants)
- [x] Key pseudocode covers hot spots (`_ensure_models_loaded`, lifespan, readiness, admin metrics, smoke, tests)
- [x] No unresolved `[NEEDS CLARIFICATION]` markers
- [x] Backwards compat: none (clean delete of `bootstrap_models`, `app.state.meeting_models_ready`; readiness contract change documented)

**Confidence score: 9.5/10** — Three review rounds applied. Round 1 caught architectural gaps (partial-state RAM leak, long-window shim race, missed callsites). Round 2 caught implementation flaws (broken single-flight test, private-attribute coupling, stale docstrings). Round 3 caught the last 5% (RLock vs Lock, true single-flight proof, gc.collect + soften RAM-reclaim promise, task ordering, JSON injection in smoke script, four-quadrant table). Remaining risk: the `/readyz/meeting` contract change is undocumented for any out-of-tree monitor (bounded by single-tenant deployment).
