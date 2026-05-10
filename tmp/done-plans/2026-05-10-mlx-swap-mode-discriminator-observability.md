# Plan: MLX-Whisper swap + explicit request-mode discriminator + observability beef-up

> Source brief: `./tmp/briefs/2026-05-10-mlx-swap-mode-discriminator-observability.md`.
> **Revision history:**
> - v2 (2026-05-10): folded round-1 reviewer fixes (dropped chunked-transcribe; Phase 4 one-liner; disk gate; parameterized matrix; kept WhisperX through Phase 8).
> - v3 (2026-05-10): folded round-2 reviewer fixes — renamed column `request_mode` to avoid collision with existing `mode`; honest semaphore semantics; dropped `_USE_MLX` flag; real Job-dataclass field names; tqdm fallback; word_timestamps=True for meeting only; reenqueue_pending threads request_mode; spike measures phases separately; ProcessingMode as Enum Form param; Phase 10 manual gate; friendly phase labels.
> - **v4 (2026-05-10): folded round-3 reviewer fixes** — explicit `staging.CancelledError` class + `ffprobe_duration` helper + `cancel_cb` kwarg on `transcode_to_canonical_wav` (Popen-based); `_on_progress` NPE guard; tightened watchdog clamp; `progress_cb` signature unified to `(phase, idx, total)`; pipeline emits explicit `store.update_phase` at each in-pipeline phase boundary; prefetch script moved to T0.3 (Phase 0 needs it); Phase 2+3 documented as single combined commit; Phase 10 gate tightened (zero fails on rows 10-12, RSS < 14 GB criterion added).

## Confidence: 7/10
Knowns: complete callsite map and real schema. Spike-gated unknowns: M4 mlx-whisper realtime ratio (T0.4.1), pyannote-on-mono-at-105-min (T0.4.2), ffmpeg-decode vs transcribe time-share (T0.4.3), peak RSS (T0.4.4). **Honest limitations:** semaphore is held by the await on the executor; watchdog only marks rows failed but does NOT free the semaphore. New submissions block on a real 429 until the executor returns naturally. Client UI surfaces this as "Previous transcription still finishing on server" rather than mysterious 429.

---

## Files Being Changed

```
server/
├── pyproject.toml                                    ← MODIFIED (add mlx-whisper==0.4.2 pinned + huggingface-hub; keep whisperx through Phase 8 for rollback)
├── src/wispralt_server/
│   ├── main.py                                       ← MODIFIED (mount /admin/active + /admin/server-log; prefetch hook)
│   ├── meeting/
│   │   ├── whisperx_loader.py                        ← KEPT through Phase 8 (deleted in Phase 10 after green matrix)
│   │   ├── mlx_whisper_loader.py                     ← NEW (one-shot transcribe; tqdm patch w/ wall-clock fallback; word_timestamps gated on meeting mode)
│   │   ├── pipeline.py                               ← MODIFIED (unconditional swap to MLX loader; remove word-level speaker assign; per-phase logs+timings; thread cancel_cb/progress_cb)
│   │   ├── diarize.py                                ← MODIFIED (per-phase phase_start/done logs only)
│   │   ├── merge.py                                  ← MODIFIED (new assign_speakers_segments helper; relabel_in_person KEPT)
│   │   └── output.py                                 ← MODIFIED (handle absent `words[]` array on segments)
│   ├── jobs/
│   │   ├── runner.py                                 ← MODIFIED (ProcessingMode enum; thread request_mode through submit_*/run_*/reenqueue_pending; phase markers; advisory watchdog with honest semaphore docs)
│   │   └── store.py                                  ← MODIFIED (new columns: request_mode, phase, phase_started_at, chunk_index, total_chunks, cancel_requested, audio_duration_s; dict-based _row_to_job; helpers)
│   ├── routes/
│   │   ├── transcribe_file.py                        ← MODIFIED (mode: ProcessingMode = Form(ProcessingMode.FILE) enum-typed; disk-space gate)
│   │   ├── meeting.py                                ← MODIFIED (legacy POST persists request_mode=MEETING; GET returns progress block)
│   │   └── admin.py                                  ← MODIFIED (GET /admin/active with rich projection; GET /admin/server-log/<job_id> bracketing the job's first/last appearance)
│   └── ops/
│       └── staging.py                                ← MODIFIED (disk-space helper; ffmpeg subprocess polls cancel flag and cleans .partial on cancel)
│
├── scripts/
│   ├── server-launchd.sh                             ← UNCHANGED
│   ├── deploy-server.sh                              ← NEW (versioned, fixes `set -e` polling-bug from prior session)
│   ├── prefetch-mlx-whisper.sh                       ← NEW (huggingface_hub.snapshot_download via python -c; pinned revision; resume_download; post-download size check)
│   └── benchmark-mlx-whisper.py                      ← NEW (Phase 0 spike — separately times ffmpeg / transcribe / pyannote; samples RSS)
│
└── tests/
    └── test_whisperx_no_speech.py                    ← DELETED in Phase 10

client/WisprAlt/
├── App/
│   └── MenuBarController.swift                       ← MODIFIED (RecordingState extended INLINE at :21; activeJobID + Task handle; thread request_mode; cancel; UserDefaults activeJobID persistence; "Previous job finishing" block on new submissions when activeJobID set)
├── Server/
│   └── MeetingAPI.swift                              ← MODIFIED (submitFile mode param; PollResponse progress; cancel(_:) and fetchServerLog(_:))
├── Storage/
│   └── PendingUploadsQueue.swift                     ← MODIFIED (replay sends mode="meeting" explicitly)
├── UI/
│   ├── SettingsView.swift                            ← MODIFIED (embed RecordingIndicatorView; Cancel; View server log; "Previous job finishing" banner)
│   └── RecordingIndicatorView.swift                  ← MODIFIED (phase-name friendly map; chunk progress)

docs/
├── ARCHITECTURE.md                                   ← MODIFIED (catch up drift + add Modes/MLX/Observability sections)
├── OVERVIEW.md                                       ← MODIFIED (file→doc map updates)
├── DEPLOYMENT-NOTES.md                               ← MODIFIED (prefetch, HF cache, deploy script, model recovery)
└── TESTING.md                                        ← MODIFIED (12-run matrix parameterized on spike ratio)
```

---

## Architecture Overview

Server stops conflating channel count with intent. A new `request_mode` field (`file` | `meeting`) on `/transcribe/file` and on the existing legacy `/transcribe/meeting` route encodes intent at submission. The fix for the "all Speaker 1" bug from yesterday is one line in `_run_source`: `force_single = (request_mode == ProcessingMode.FILE)`. Existing pipeline branches handle mono+meeting correctly via the in-person path. WhisperX is swapped wholesale for `mlx-whisper` (large-v3-turbo by default; large-v3-fp16 fallback if spike shows accuracy regression). The wav2vec2 alignment pass is dropped for `request_mode=file`; for `request_mode=meeting` we keep `word_timestamps=True` in mlx-whisper so speaker-boundary segment splits can land on word boundaries (~20% perf cost — explicit trade). Per-phase structured logging + timings. SQLite checkpoints (phase, chunk progress, cancel_requested) the client polls. Menubar UI shows phase + chunks + Cancel + View server log. Pyannote diarization unchanged structurally — its existing in-person branch handles mono today. Legacy `/transcribe/meeting` POST keeps working with implicit `request_mode=MEETING`. WhisperX stays in deps through Phase 8 for revert-by-git, deleted in Phase 10 only after a clean matrix AND explicit user approval (manual gate).

**Three request modes:**

| Mode | Endpoint | Channel | Diarization | Model | word_timestamps |
|---|---|---|---|---|---|
| Dictation | `/transcribe/dictate` | mono | n/a | Parakeet MLX (resident) | n/a |
| Meeting | `/transcribe/file?mode=meeting` (or legacy POST) | mono or stereo | yes | mlx-whisper-turbo + pyannote | **True** (for speaker-boundary splits) |
| File | `/transcribe/file?mode=file` (default) | any | **no** | mlx-whisper-turbo | False |

**Per-phase observability flow:**

```
[queued] → [starting] → [ffprobe] → [ffmpeg_decode] → [transcribe_load] → [transcribe] → [diarize_load*] → [diarize*] → [merge] → [output_write] → [done | failed]
                                                            │
                                                            └→ tqdm patch (or wall-clock fallback) → store.update_chunk(jid, idx, total)
                                                               → client polls /transcribe/meeting/<id> → reads progress block → UI renders friendly phase label + chunks
* skipped in file mode
```

**Honest limitations (v3 — no false claims):**

- **Per-phase timeout for `ffprobe` + `ffmpeg_decode`** is enforceable via `asyncio.wait_for` (each is its own `run_in_executor` call). Real timeout, real abort.
- **Per-phase timeout for in-pipeline phases (`transcribe`, `diarize`, etc.)** is *advisory only*. A separate watchdog task reads `phase_started_at` every 5s and calls `set_failed` if elapsed > 2× budget. **The semaphore is NOT released** — `_run_source`'s `async with self._semaphore:` is awaiting the executor and will not exit until the executor thread returns naturally. The user sees "failed" in the UI but new submissions return 429 with `Retry-After`. Client UI surfaces this as a "Previous transcription still finishing on server (~Nm remaining)" banner so the user doesn't get mysterious errors.
- **Cancel mid-upload** works (URLSession.invalidateAndCancel — clean).
- **Cancel mid-`ffmpeg_decode`** works — the subprocess wrapper polls `jobs.cancel_requested` every 500ms and SIGTERMs ffmpeg on True, cleans the `.partial` file, raises `CancelledError`. `_run_source` catches it, calls `set_failed(jid, "cancelled")`, exits `async with` → semaphore released.
- **Cancel mid-`transcribe`/`diarize`** is advisory. `cancel_requested=1` is set in the row; UI hides the job; the executor thread keeps running until done. Same constraint as the advisory watchdog. The "Previous job finishing" banner appears.

---

## Key Pseudocode

### 1. `meeting/mlx_whisper_loader.py` (one-shot with tqdm-patch + wall-clock fallback)

```python
"""mlx-whisper loader. One-shot transcribe (preserves Whisper's cross-window
prompting). Progress reporting via tqdm.auto.tqdm.update monkey-patch with a
wall-clock fallback if no tqdm activity is observed within 60s."""
import logging, math, threading, time
import numpy as np

_MODEL_REPO = "mlx-community/whisper-large-v3-turbo"  # large-v3-fp16 fallback per T0.5
_loaded = False
_patch_lock = threading.Lock()
logger = logging.getLogger(__name__)

def load() -> None:
    global _loaded
    if _loaded: return
    import mlx_whisper
    silence = np.zeros(16000, dtype=np.float32)
    _ = mlx_whisper.transcribe(silence, path_or_hf_repo=_MODEL_REPO,
                               word_timestamps=False, language="en", verbose=False)
    _loaded = True

def reset() -> None:
    global _loaded
    _loaded = False

def transcribe_channel(audio_16k, *,
                       word_timestamps: bool = False,
                       progress_cb=None, cancel_cb=None,
                       duration_s_override=None) -> dict:
    import mlx_whisper, tqdm.auto as _tqdm_mod
    duration_s = duration_s_override or (len(audio_16k) / 16000)
    total_windows = max(1, math.ceil(duration_s / 30.0))

    # Synthetic-progress fallback: emit an estimate every 5s if tqdm.update never fires.
    fallback_state = {"saw_tqdm": False, "fallback_thread": None, "stop": False}
    def fallback_emitter():
        t_start = time.monotonic()
        while not fallback_state["stop"]:
            time.sleep(5)
            if fallback_state["stop"]: return
            if fallback_state["saw_tqdm"]: return  # real progress is firing; we're not needed
            elapsed = time.monotonic() - t_start
            if elapsed < 60: continue  # give tqdm a chance to fire first
            if not progress_cb: continue
            # synthetic estimate: assume 5× realtime; clamp to total
            est_done = min(total_windows, int(elapsed / max(6.0, duration_s/total_windows/5)))
            try: progress_cb("transcribe", est_done, total_windows)
            except Exception: logger.exception("synthetic progress_cb raised")

    chunk_counter = {"n": 0}
    with _patch_lock:
        original_update = _tqdm_mod.tqdm.update
        def patched_update(self, n=1):
            chunk_counter["n"] += n
            fallback_state["saw_tqdm"] = True
            if progress_cb:
                try: progress_cb("transcribe", min(chunk_counter["n"], total_windows), total_windows)
                except Exception: logger.exception("progress_cb raised — continuing transcribe")
            if cancel_cb and cancel_cb():
                logger.warning("cancel requested but mlx-whisper cannot be interrupted "
                               "mid-decode; setting failure flag for UI only")
            return original_update(self, n)
        _tqdm_mod.tqdm.update = patched_update
        fb_thread = threading.Thread(target=fallback_emitter, daemon=True)
        fb_thread.start()
        try:
            result = mlx_whisper.transcribe(
                audio_16k, path_or_hf_repo=_MODEL_REPO,
                word_timestamps=word_timestamps, language="en",
                hallucination_silence_threshold=2.0, verbose=False,
            )
        finally:
            fallback_state["stop"] = True
            _tqdm_mod.tqdm.update = original_update
    return result
```

### 2. `jobs/runner.py` — ProcessingMode, request_mode threading, honest watchdog

```python
from enum import Enum

class ProcessingMode(str, Enum):
    FILE = "file"
    MEETING = "meeting"

# Phase budgets — scalars and callables intermixed; dispatcher handles both
PHASE_BUDGETS = {
    "ffprobe": 30,
    "ffmpeg_decode": 600,
    "transcribe_load": 120,    # bumped to cover MLX + pyannote cold-start on first run
    "transcribe": lambda d: d * 4 + 120,
    "diarize_load": 120,
    "diarize": lambda d: d * 2.0 + 60,
    "merge": 60,
    "output_write": 30,
}

# Friendly phase labels for the client UI
PHASE_LABELS = {
    "queued": "Waiting in queue",
    "starting": "Starting",
    "ffprobe": "Inspecting audio",
    "ffmpeg_decode": "Decoding audio",
    "transcribe_load": "Loading transcription model",
    "transcribe": "Transcribing",
    "diarize_load": "Loading speaker model",
    "diarize": "Identifying speakers",
    "merge": "Finalizing transcript",
    "output_write": "Writing outputs",
}

# Legacy /transcribe/meeting POST path. Existing signature unchanged externally.
async def submit_or_429(self, wav_path: Path) -> str:
    """Legacy stereo-WAV submission path. Persists request_mode=MEETING."""
    async with self._submit_lock:
        if self._semaphore.locked():
            raise MeetingInProgressError("...")
        jid = self.store.create(wav_path=str(wav_path),
                                request_mode=ProcessingMode.MEETING.value)
        asyncio.create_task(self._run_pipeline(jid, Path(wav_path)))
    return jid

# New /transcribe/file path
async def submit_source_or_429(self, src_path: Path,
                                request_mode: ProcessingMode) -> str:
    async with self._submit_lock:
        if self._semaphore.locked():
            raise MeetingInProgressError("...")
        jid = self.store.create(wav_path=str(src_path),  # src_path reuses wav_path col
                                request_mode=request_mode.value)
        asyncio.create_task(self._run_source(jid, src_path, request_mode))
    return jid

# Reenqueue path must preserve request_mode
def reenqueue_pending(self):
    pending = self.store.list_pending_ids_with_mode()  # new helper returns (id, wav_path, request_mode)
    for jid, wav_path, req_mode_str in pending:
        req_mode = ProcessingMode(req_mode_str or "meeting")
        path = Path(wav_path)
        if path.suffix.lower() in (".wav",):  # legacy
            asyncio.create_task(self._run_pipeline(jid, path))
        else:
            asyncio.create_task(self._run_source(jid, path, req_mode))

# Per-phase wrapper for the seams that ARE wrappable
async def _phase(self, jid: str, name: str, budget_s: float, fn, *args):
    self.store.update_phase(jid, name)
    logger.info("[%s] phase_start name=%s", jid, name)
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(self._executor, fn, *args),
            timeout=budget_s)
    except asyncio.TimeoutError:
        logger.error("[%s] phase_timeout name=%s budget_s=%.1f", jid, name, budget_s)
        self.store.set_failed(jid, f"Phase '{name}' exceeded timeout {budget_s:.0f}s")
        raise
    logger.info("[%s] phase_done name=%s duration_ms=%d", jid, name,
                int((time.monotonic()-t0)*1000))
    return result

# Watchdog — HONEST: marks failed for UI but does NOT free semaphore
async def _phase_watchdog(self, jid: str, audio_duration_s: float):
    """Runs as a separate task. Marks the job failed if a phase exceeds 2× budget.
    Does NOT abort the executor thread; does NOT release the semaphore.
    The user-visible result: UI shows 'failed', new submissions still 429 until
    the executor returns naturally."""
    while True:
        await asyncio.sleep(5)
        job = self.store.get(jid)
        if not job or job.status in ("done", "failed"): return
        if not job.phase or not job.phase_started_at: continue
        budget = PHASE_BUDGETS.get(job.phase, 600)
        budget_s = budget(audio_duration_s) if callable(budget) else budget
        elapsed = time.time() - job.phase_started_at
        if elapsed > budget_s * 2:
            logger.error("[%s] phase_watchdog FIRING: phase=%s elapsed=%.0fs "
                         "budget=%.0fs (advisory; executor still running)",
                         jid, job.phase, elapsed, budget_s)
            self.store.set_failed(jid, f"Phase '{job.phase}' watchdog timeout (advisory)")
            return

# _run_source — semaphore held across executor await (documented constraint)
async def _run_source(self, jid: str, src_path: Path, request_mode: ProcessingMode):
    async with self._semaphore:
        watchdog_task = None
        try:
            self._active_job_id = jid
            self.store.set_running(jid)
            self.store.update_phase(jid, "starting")

            # Phase: ffprobe — REAL timeout, REAL abort
            channels = await self._phase(jid, "ffprobe", PHASE_BUDGETS["ffprobe"],
                                          staging.ffprobe_channel_count, src_path)
            audio_duration_s = await asyncio.get_event_loop().run_in_executor(
                None, staging.ffprobe_duration, src_path)
            self.store.update_audio_duration(jid, audio_duration_s)

            # Phase: ffmpeg_decode — REAL timeout, REAL abort, cancel-aware
            cancel_cb = lambda: self.store.check_cancel_requested(jid)
            wav_path = await self._phase(jid, "ffmpeg_decode",
                                          PHASE_BUDGETS["ffmpeg_decode"],
                                          functools.partial(
                                              staging.transcode_to_canonical_wav,
                                              src_path,
                                              target_channels=2,
                                              cancel_cb=cancel_cb))

            # The actual fix for "all Speaker 1": derive from request_mode, not channel count
            force_single = (request_mode == ProcessingMode.FILE)
            self.store.update_after_transcode(jid, wav_path=str(wav_path),
                                               force_single_channel=force_single)
            try: src_path.unlink()
            except OSError: pass

            # Spawn watchdog before the big executor call
            watchdog_task = asyncio.create_task(
                self._phase_watchdog(jid, audio_duration_s))

            await self._run_pipeline_inner(jid, wav_path,
                                            force_single_channel=force_single,
                                            request_mode=request_mode,
                                            audio_duration_s=audio_duration_s)
        except staging.StagingCancelled:
            logger.info("[%s] cancelled", jid)
            self.store.set_failed(jid, "cancelled by user")
        except asyncio.TimeoutError:
            pass  # already set_failed in _phase
        except Exception:
            logger.exception("[%s] _run_source failed", jid)
            current = self.store.get(jid)
            if current and current.status not in ("done", "failed"):
                self.store.set_failed(jid, "internal error")
        finally:
            if watchdog_task: watchdog_task.cancel()
            self._active_job_id = None
            staging.cleanup(src_path)

# _run_pipeline_inner — guard against executor returning after watchdog set_failed
async def _run_pipeline_inner(self, jid, wav_path, *, force_single_channel,
                                request_mode, audio_duration_s):
    self.store.update_phase(jid, "transcribe_load")
    progress_cb = lambda phase, idx, total: self._on_progress(jid, phase, idx, total)
    cancel_cb = lambda: self.store.check_cancel_requested(jid)
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            self._executor,
            functools.partial(meeting_pipeline.transcribe_meeting,
                              wav_path, self.output_dir, jid,
                              force_single_channel=force_single_channel,
                              request_mode=request_mode.value,
                              progress_cb=progress_cb, cancel_cb=cancel_cb))
    except Exception:
        logger.exception("[%s] pipeline raised", jid)
        current = self.store.get(jid)
        if current and current.status not in ("done", "failed"):
            self.store.set_failed(jid, "pipeline error")
        return

    # Guard: watchdog or cancel may have already set_failed
    current = self.store.get(jid)
    if current and current.status == "failed":
        logger.warning("[%s] executor returned post-failure; discarding result", jid)
        return
    self.store.set_done(jid, result["pipeline_mode"], result["output_dir"])

def _on_progress(self, jid: str, phase: str, idx: int, total: int):
    job = self.store.get(jid)
    if not job: return  # row deleted concurrently
    if phase != job.phase:
        self.store.update_phase(jid, phase)
    if total > 0:
        self.store.update_chunk(jid, idx, total)
```

### 3. `jobs/store.py` — additions matching actual schema

```python
# Append to Job dataclass (after force_single_channel). With dict-based
# _row_to_job the order no longer matters for correctness but match existing style.
@dataclass
class Job:
    id: str
    status: str
    mode: Optional[str]          # EXISTING: remote | in_person | None (set on done)
    created_at: float
    started_at: Optional[float]
    finished_at: Optional[float]
    error: Optional[str]
    output_dir: Optional[str]
    wav_path: str
    attempts: int = 0
    force_single_channel: bool = False
    # NEW columns — all idempotent ALTERs in __init__
    request_mode: Optional[str] = None      # file | meeting (set at submit)
    phase: Optional[str] = None
    phase_started_at: Optional[float] = None
    chunk_index: int = 0
    total_chunks: int = 0
    cancel_requested: bool = False
    audio_duration_s: Optional[float] = None

# ALTERs (idempotent, matching existing pattern at store.py:111-126)
for sql in [
    "ALTER TABLE jobs ADD COLUMN request_mode TEXT",
    "ALTER TABLE jobs ADD COLUMN phase TEXT",
    "ALTER TABLE jobs ADD COLUMN phase_started_at REAL",
    "ALTER TABLE jobs ADD COLUMN chunk_index INTEGER DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN total_chunks INTEGER DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN audio_duration_s REAL",
]:
    try: self.con.execute(sql)
    except sqlite3.OperationalError: pass

# Dict-based _row_to_job — order-independent (kills future ALTER footgun)
def _row_to_job(row, cursor) -> Job:
    d = dict(zip([c[0] for c in cursor.description], row))
    return Job(
        id=d["id"], status=d["status"], mode=d.get("mode"),
        created_at=d["created_at"], started_at=d.get("started_at"),
        finished_at=d.get("finished_at"), error=d.get("error"),
        output_dir=d.get("output_dir"), wav_path=d["wav_path"],
        attempts=int(d.get("attempts") or 0),
        force_single_channel=bool(d.get("force_single_channel") or 0),
        request_mode=d.get("request_mode"),
        phase=d.get("phase"),
        phase_started_at=d.get("phase_started_at"),
        chunk_index=int(d.get("chunk_index") or 0),
        total_chunks=int(d.get("total_chunks") or 0),
        cancel_requested=bool(d.get("cancel_requested") or 0),
        audio_duration_s=d.get("audio_duration_s"),
    )

# create() signature — extended with request_mode kwarg
def create(self, wav_path: str, *, request_mode: str = "meeting") -> str:
    jid = str(uuid.uuid4())
    self._exec("INSERT INTO jobs(id, status, created_at, wav_path, request_mode) "
               "VALUES(?, 'pending', ?, ?, ?)",
               (jid, time.time(), wav_path, request_mode))
    return jid

# Helpers
def update_phase(self, jid, phase):
    self._exec("UPDATE jobs SET phase=?, phase_started_at=? WHERE id=?",
               (phase, time.time(), jid))
def update_chunk(self, jid, idx, total):
    self._exec("UPDATE jobs SET chunk_index=?, total_chunks=? WHERE id=?",
               (idx, total, jid))
def set_cancel_requested(self, jid):
    self._exec("UPDATE jobs SET cancel_requested=1 WHERE id=?", (jid,))
def check_cancel_requested(self, jid) -> bool:
    row = self._exec("SELECT cancel_requested FROM jobs WHERE id=?",
                     (jid,), fetch=True)
    return bool(row[0]) if row else False
def update_audio_duration(self, jid, seconds):
    self._exec("UPDATE jobs SET audio_duration_s=? WHERE id=?", (seconds, jid))
def list_pending_ids_with_mode(self):
    return self._exec("SELECT id, wav_path, request_mode FROM jobs "
                      "WHERE status='pending' ORDER BY created_at",
                      fetch_all=True) or []
```

### 4. `merge.py` — assign_speakers_segments with word-level splits

```python
# Uses mlx-whisper's word_timestamps when available (meeting mode). Without
# word timestamps (file mode never reaches this code path), falls back to
# largest-overlap with no splitting.
def assign_speakers_segments(transcribe_result, diar_df):
    """Map each transcribed segment to a pyannote speaker label.
    Splits segments at speaker boundaries IF word_timestamps are present.
    Otherwise assigns dominant speaker (largest overlap)."""
    out_segments = []
    for seg in transcribe_result["segments"]:
        words = seg.get("words", [])
        if words:
            # Word-aware split: group consecutive words by their assigned speaker
            assigned = []  # list of (word_dict, speaker)
            for w in words:
                t = (w["start"] + w["end"]) / 2
                matched = diar_df[(diar_df["start"] <= t) & (diar_df["end"] >= t)]
                speaker = matched["speaker"].iloc[0] if len(matched) else "Unknown"
                assigned.append((w, speaker))
            # Coalesce consecutive same-speaker words
            current = [assigned[0]]
            for w, sp in assigned[1:]:
                if sp == current[-1][1]:
                    current.append((w, sp))
                else:
                    out_segments.append({
                        "start": current[0][0]["start"],
                        "end": current[-1][0]["end"],
                        "text": " ".join(w["word"] for w, _ in current).strip(),
                        "speaker": current[0][1],
                        "words": [w for w, _ in current],
                    })
                    current = [(w, sp)]
            out_segments.append({  # last group
                "start": current[0][0]["start"],
                "end": current[-1][0]["end"],
                "text": " ".join(w["word"] for w, _ in current).strip(),
                "speaker": current[0][1],
                "words": [w for w, _ in current],
            })
        else:
            # No word timestamps: largest-overlap, no splitting
            best_speaker, best_overlap = "Unknown", 0
            for _, row in diar_df.iterrows():
                ov = max(0, min(seg["end"], row["end"]) - max(seg["start"], row["start"]))
                if ov > best_overlap:
                    best_overlap, best_speaker = ov, row["speaker"]
            out_segments.append({**seg, "speaker": best_speaker})
    return out_segments
```

### 5. Route — FastAPI Enum-typed mode form param

```python
# routes/transcribe_file.py
from wispralt_server.jobs.runner import ProcessingMode

@router.post("")
async def submit_file(request: Request, file: UploadFile,
                      mode: ProcessingMode = Form(ProcessingMode.FILE),
                      content_length: int | None = Header(None, alias="Content-Length")):
    # FastAPI auto-validates the enum value; invalid → 422 structured error
    # Pre-flight: ensure staging dir exists (otherwise disk_usage raises)
    settings.staging_dir.mkdir(parents=True, exist_ok=True)
    # Disk gate
    if content_length:
        free = shutil.disk_usage(settings.staging_dir).free
        if free < content_length * 2:
            return JSONResponse({"error": "Insufficient disk space"},
                                status_code=507, headers={"Retry-After": "300"})
    # RAM gate (bump from 2 GiB → 4 GiB per brief)
    if psutil.virtual_memory().available < 4 * 1024**3:
        return JSONResponse({"error": "Server low on memory"},
                            status_code=503, headers={"Retry-After": "60"})
    # ...existing stream-to-staging-raw...
    jid = await runner.submit_source_or_429(src_path, request_mode=mode)
    return JSONResponse({"job_id": jid, "status": "pending"}, status_code=202)
```

### 6. Client — RecordingState extension (lives INLINE in MenuBarController.swift:21)

```swift
// In MenuBarController.swift around line 21 — extend existing class, NOT a new file.
class RecordingState: ObservableObject {
    @Published var uploadFraction: Double = 0
    @Published var phase: String? = nil           // raw phase name
    @Published var chunkIndex: Int? = nil
    @Published var totalChunks: Int? = nil
    @Published var activeJobID: String? = nil     // persisted to UserDefaults
    @Published var serverFinishingJobID: String? = nil  // shown when cancel/error left a server-side job

    func reset() {
        uploadFraction = 0; phase = nil; chunkIndex = nil; totalChunks = nil
        activeJobID = nil
    }

    // Friendly phase label for UI
    var phaseLabel: String? {
        guard let p = phase else { return nil }
        return ["queued":"Waiting in queue","starting":"Starting",
                "ffprobe":"Inspecting audio","ffmpeg_decode":"Decoding audio",
                "transcribe_load":"Loading transcription model","transcribe":"Transcribing",
                "diarize_load":"Loading speaker model","diarize":"Identifying speakers",
                "merge":"Finalizing transcript","output_write":"Writing outputs"][p] ?? p
    }
}

// Persistence: on activeJobID set, write to UserDefaults. On app launch in
// MenuBarController.applicationDidFinishLaunching, if a job ID is present, resume polling.
```

### 7. UI guard — "Previous job finishing" banner blocks new submissions

```swift
// SettingsView.swift QuickActionsSection
if let finishingID = recordingState.serverFinishingJobID {
    Banner("Previous transcription still finishing on server. " +
           "Estimated remaining: \(estimateRemaining()). " +
           "Cancel will mark it as done locally but cannot interrupt the server.")
    Button("Show Details") { showServerLog(jobID: finishingID) }
} else if let phaseLabel = recordingState.phaseLabel {
    if let i = recordingState.chunkIndex, let n = recordingState.totalChunks, n > 0,
       recordingState.phase == "transcribe" {
        Text("\(phaseLabel) — chunk \(i)/\(n)")
    } else {
        Text(phaseLabel)
    }
}
```

---

## Tasks (in implementation order)

### Phase 0 — Benchmark spike (gates Phases 1-10)

> **CRITICAL: Ask user for CRD approval ONCE at the start of Phase 0. After approval, all mini-side work through end-of-Phase-8 runs autonomously. Phase 10 requires explicit Phase-8-summary approval before deletion of WhisperX.**

**T0.1** — Write `server/scripts/benchmark-mlx-whisper.py`. Args: `--input <path> --mode {file,meeting}`. Separately times: `ffmpeg_decode_s`, `transcribe_s` (with word_timestamps reflecting the mode), `pyannote_s` (if mode=meeting). Samples RSS every 2s via psutil. Output JSON: `{audio_duration_s, ffmpeg_decode_s, transcribe_s, pyannote_s, wall_clock_s, realtime_ratio, peak_rss_mb, segments_count, speakers_detected}`. Exit non-zero on failure.

**T0.2** — Ask user for CRD access. On approval, switch to CRD page, confirm mini Terminal is focused.

**T0.2a** — Write `server/scripts/prefetch-mlx-whisper.sh` now (Phase 0 needs it; full body in T1.7 below — same content, just written earlier in the timeline).

**T0.3** — `/macmini paste` and run `prefetch-mlx-whisper.sh`. Verify model lands at `~/.cache/huggingface/hub/` and model.safetensors is >800 MB. Install `mlx-whisper==0.4.2`, `huggingface-hub>=0.20`, `psutil` in the server's venv (path from `scripts/server-launchd.sh:66`).

**T0.4** — Verify server idle (curl /admin/metrics, `meeting.active=false`).

**T0.4.1** — Run spike: `mode=file` on `Sammamish Endodontics.m4a`. Record numbers.

**T0.4.2** — Run spike: `mode=meeting` on the same file. **Verify ≥2 speakers detected** (the actual bug fix). Spot-check 10 segments for speaker plausibility.

**T0.4.3** — Verify ffmpeg + transcribe + pyannote times sum approximately to wall_clock_s (sanity).

**T0.4.4** — Confirm peak RSS < 14 GB (16 GB mini, 2 GB headroom).

**T0.5** — Branch on `transcribe_realtime_ratio = audio_duration / transcribe_s`:
- **≥ 5×**: proceed with `large-v3-turbo`. Fill matrix wall-time pass criteria using `R = ratio`.
- **3-5×**: proceed with `large-v3-turbo`. If T0.6 reveals accuracy regression, swap to `mlx-community/whisper-large-v3-mlx` (non-turbo). Update `_MODEL_REPO` in T1.2.
- **< 3×**: STOP. Insert `[NEEDS CLARIFICATION]`. Likely ffmpeg-decode or pyannote-load is dominant; investigate.

**T0.6** — Compare spike's mode=meeting transcript text against today's WhisperX JSON (`Sammamish Endodontics.json`). 20-segment spot-check across timeline. Pass: same proper-noun rate ±2, no catastrophic hallucinations, similar segment cadence.

**T0.7** — Verify pyannote-on-mono-105-min wall ≤ 1.5× duration. If higher, lower the meeting-mode matrix pass criteria accordingly.

### Phase 1 — Server: model swap (mlx-whisper)

**T1.1** — `server/pyproject.toml`: add `mlx-whisper==0.4.2`, `huggingface-hub>=0.20`, `psutil>=5.9`. **Keep whisperx, ctranslate2, faster-whisper through Phase 8**. **Pre-flight:** before committing, run `uv add --dry-run mlx-whisper==0.4.2 huggingface-hub` on dev box to verify resolution. If conflict: document the dep tree before proceeding.

**T1.2** — Create `server/src/wispralt_server/meeting/mlx_whisper_loader.py` per pseudocode section 1. Verify the patched module name (`tqdm.auto`) by inspecting installed `mlx-whisper==0.4.2` source: `python -c "import mlx_whisper, inspect; print([s for s in inspect.getsource(mlx_whisper.transcribe).split('\\n') if 'tqdm' in s])"`. If `tqdm.auto.tqdm` is the path, patch as written. If `tqdm.tqdm` (no .auto): adjust import. Document the verified path in the module docstring.

**T1.3** — Unconditional swap in `meeting/pipeline.py`. Replace `from wispralt_server.meeting import whisperx_loader as _wx_mod` with `from wispralt_server.meeting import mlx_whisper_loader as _mlx_mod`. Update three transcribe call sites (currently at `pipeline.py:392`, `:456`, `:482` — re-anchor at implementation time): use `_mlx_mod.transcribe_channel(audio_16k, word_timestamps=(not force_single_channel), progress_cb=progress_cb, cancel_cb=cancel_cb)`. No `_USE_MLX` flag — single-path code.

**T1.4** — Remove `whisperx.assign_word_speakers(...)` calls at `pipeline.py:473` and `:488`. Add `merge.assign_speakers_segments(transcribe_result, diar_df)` per pseudocode section 4. Calls it from both in-person and remote branches.

**T1.5** — Update `_ensure_models_loaded` (pipeline.py:91): call `_mlx_mod.load()` (NOT `_wx_mod.load()`), drop `whisperx.load_align_model()`. Update `_state()` to read `_mlx_mod._loaded`.

**T1.6** — `evict_if_idle` (pipeline.py:169): keep eviction logic for pyannote + DeepFilterNet; `_mlx_mod.reset()` is essentially no-op (MLX unified memory) — document with a comment.

**T1.7** — Create `server/scripts/prefetch-mlx-whisper.sh`:

```bash
#!/bin/bash
set -e
REVISION="${WHISPER_REVISION:-main}"  # filled in after T0.5
python3 -c "
from huggingface_hub import snapshot_download
p = snapshot_download(
    repo_id='mlx-community/whisper-large-v3-turbo',
    revision='${REVISION}',
    resume_download=True,
)
print(f'Downloaded to: {p}')
import os
mp = os.path.join(p, 'model.safetensors')
if os.path.exists(mp):
    sz = os.path.getsize(mp)
    print(f'Model size: {sz/(1024**2):.1f} MB')
    assert sz > 800 * 1024 * 1024, f'Model size too small: {sz}'
print('OK')
"
```

**T1.8** — Audit `meeting/__init__.py install_compat_shims`. If whisperx-specific patches exist, leave them in place through Phase 8 (still imported) and remove in Phase 10.

### Phase 2 — Server: ProcessingMode + route + resource gates

**T2.1** — Add `ProcessingMode` Enum at top of `jobs/runner.py`. Add `PHASE_BUDGETS` and `PHASE_LABELS` dicts.

**T2.2** — Modify `routes/transcribe_file.py`: change signature to `mode: ProcessingMode = Form(ProcessingMode.FILE)` (Enum-typed; FastAPI auto-validates). Drop the manual `try/except ValueError → 422` block.

**T2.2.1** — Pre-flight `settings.staging_dir.mkdir(parents=True, exist_ok=True)` BEFORE `shutil.disk_usage`. Disk gate: free < content_length * 2 → 507 + Retry-After 300. RAM gate: available < 4 GiB → 503 + Retry-After 60. Per pseudocode section 5.

**T2.3** — Modify `runner.submit_source_or_429`: add `request_mode: ProcessingMode` param. Thread to `_run_source`. Persist via `store.create(wav_path=..., request_mode=request_mode.value)`.

**T2.3.1** — Modify `runner.submit_or_429` (legacy /transcribe/meeting POST): no signature change. Inside body, change `store.create(str(wav_path))` to `store.create(str(wav_path), request_mode=ProcessingMode.MEETING.value)`.

**T2.3.2** — Update `reenqueue_pending` (runner.py:177) per pseudocode: read `request_mode` from row, route by extension AND mode.

**T2.4.0** — In `ops/staging.py`: (a) define `class StagingCancelled(Exception): pass` at top of module; (b) add `def ffprobe_duration(src_path: Path) -> float` mirroring `ffprobe_channel_count`'s shape (uses `ffprobe -v error -select_streams a:0 -show_entries format=duration -of default=noprint_wrappers=1:nokey=1`); (c) extend `transcode_to_canonical_wav` signature with `cancel_cb: Callable[[], bool] | None = None`; convert from `subprocess.run` to `subprocess.Popen` with a 500ms poll loop checking `cancel_cb()`; on True → `proc.send_signal(SIGTERM); proc.wait(timeout=5); temp_target.unlink(missing_ok=True); raise StagingCancelled("cancelled mid-decode")`. Cleanup of `.partial` happens in the `finally` block uniformly for both cancel and error paths.

**T2.4** — Rewrite `_run_source` per pseudocode section 2: explicit phase markers, `_phase()` wrapper for ffprobe + ffmpeg_decode (real `asyncio.wait_for`), watchdog spawned via `asyncio.create_task` before pipeline call, watchdog cancelled in `finally`. Catches `staging.StagingCancelled` (defined in T2.4.0). Honest semaphore semantics — no false claims about release.

**T2.5** — Modify `_run_pipeline_inner` per pseudocode section 2: add `request_mode` and `audio_duration_s` params; pass `progress_cb` + `cancel_cb` to `transcribe_meeting`; **guard `set_done` with status check** — if watchdog already wrote `failed`, discard the executor's result and log a warning.

### Phase 3 — Server: store + observability

> **Phase 2 + Phase 3 land in a single commit.** Phase 2's `_run_pipeline_inner` guards (T2.5) reference columns (`cancel_requested`, `phase`) added in Phase 3 (T3.1). Implementer must complete both phases before committing — Phase 2 alone won't compile cleanly.


**T3.1** — Add 7 new columns per pseudocode section 3 (matching existing pattern). Append same fields to `Job` dataclass.

**T3.2** — Refactor `_row_to_job` to dict-based unpacking per pseudocode. Change SELECTs in `get` and `list_active_jobs` to `SELECT *`. Eliminates positional-coupling for all future ALTERs.

**T3.3** — Add `update_phase`, `update_chunk`, `set_cancel_requested`, `check_cancel_requested`, `update_audio_duration`, `list_pending_ids_with_mode` helpers.

**T3.4** — Update `recover_orphans` to log the last-known `phase` and `chunk_index` for crashed jobs. **Backfill rule:** for existing rows where `request_mode IS NULL`, derive from `wav_path` extension: `.wav` → `meeting`, else infer from file extension. Document. **Do not implement resume-from-checkpoint** — defer.

**T3.5** — Add `GET /admin/active` to `routes/admin.py`. Auth same as `/admin/metrics`. Rich projection: `id, status, request_mode, mode, phase, phase_label (PHASE_LABELS[phase]), phase_elapsed_s (now - phase_started_at), chunk_index, total_chunks, started_at, wav_path, audio_duration_s, attempts, cancel_requested, current_rss_mb`. Helps operator diagnose hangs.

**T3.6** — Add `GET /admin/server-log/{job_id}`: returns the 100 lines bracketing the job_id's first and last appearance in `settings.server_log_path` (resolved from settings, NOT hardcoded — dev box and prod-mini differ; default `Path.home() / "Library/Logs/WisprAlt/server.log"`). Includes inter-job-id lines (ffmpeg stderr, pyannote warnings) in that range. Plain text response.

**T3.7** — Add per-phase structured logs in `meeting/pipeline.py`. `phase_start name=X` at entry, `phase_done name=X duration_ms=N` at exit. Greppable timeline.

**T3.8** — Update `routes/meeting.py poll_meeting` to include `progress` block when status=running, sourced from job row's phase / phase_started_at / chunk_index / total_chunks / phase_label. Also expose `serverFinishing` boolean if `cancel_requested=1` AND status="running".

### Phase 4 — Server: pipeline mode-routing

**T4.1** — Modify `meeting/pipeline.py transcribe_meeting`: add `progress_cb: Callable[[str, int, int], None] | None`, `cancel_cb: Callable[[], bool] | None`, `request_mode: str` kwargs. Keep `force_single_channel` (now derived from request_mode in caller). **Critically:** at each in-pipeline phase boundary inside `transcribe_meeting` (transcribe_load → transcribe → diarize_load → diarize → merge → output_write), call `store.update_phase(jid, "<name>")` (via a passed-in `phase_setter_cb` closure, since pipeline.py can't import the store directly without circular imports — runner passes a closure). This ensures `phase_started_at` is fresh for the watchdog's elapsed calculation.

**T4.2** — Thread `progress_cb` (per-phase + per-chunk) and `cancel_cb` to `_mlx_mod.transcribe_channel(...)`. In meeting mode: `word_timestamps=True`. In file mode: `word_timestamps=False`.

**T4.3** — Update `output.py`: when emitting JSON output, gracefully handle segments without `words[]` (file mode). SRT/VTT generators use segment-level text only — verify no `seg["words"]` access. Add a test note in T9.4 to confirm SRT/VTT outputs are identical to pre-swap for the same input.

**T4.4** — Mono-warning probe (pipeline.py:436): only warn when `request_mode=meeting` AND ffprobe says mono (where it's unusual). File mode mono is normal — silent.

**T4.5** — Verify existing in-person branch handles meeting+mono via the ch2-silent path. The transcode in T2.4 produces 2-channel WAV (target_channels=2), silent ch2 → in-person → diarize on ch1 → `relabel_in_person` (kept). Confirmed by T0.4.2 spike result.

### Phase 5 — Client: API + state plumbing

**T5.1** — `MeetingAPI.swift`: extend `PollResponse` with optional `progress: ProgressInfo?` and optional `serverFinishing: Bool?`. Update `mapStatus`.

**T5.2** — `MeetingAPI.submitFile`: add `mode: String = "file"` parameter. Insert mode form part before file part in multipart envelope.

**T5.3** — Add `MeetingAPI.cancel(_:)` (DELETE /transcribe/meeting/<id>) and `MeetingAPI.fetchServerLog(_:)` (GET /admin/server-log/<id>).

**T5.4** — In `MenuBarController.swift:21` extend `RecordingState` per pseudocode section 6: add `phase`, `chunkIndex`, `totalChunks`, `activeJobID`, `serverFinishingJobID`, `phaseLabel` computed property. Persist `activeJobID` to UserDefaults.

**T5.5** — In `MenuBarController.swift`:
- Add `private var activeJobTask: Task<Void, Error>?` and `private var activeUploadSession: URLSession?`.
- Modify `runFileTranscriptionJob` to accept `mode: String`; capture Task handle; on each poll, update `recordingState.phase/chunkIndex/totalChunks`.
- `processMeetingUpload` passes `mode: "meeting"`.
- `processCustomTranscriptionUpload` passes `mode: "file"`.
- `cancelActiveTranscription()`: invalidates URLSession (mid-upload cancel), calls `MeetingAPI.cancel(activeJobID)` (sets server flag), Task.cancel(). On confirm, if status is still pending/running on server: set `recordingState.serverFinishingJobID = activeJobID` and `activeJobID = nil`; poll continues but only to detect the eventual completion/failure, not to block UI. Logs warning that mid-transcribe is advisory.
- On `applicationDidFinishLaunching`: if UserDefaults has `activeJobID`, resume polling (recovery from network blip).
- Retry the poll request with exponential backoff (1s, 2s, 4s) on transient network errors before surrendering.

**T5.6** — `PendingUploadsQueue.swift:182` — `.m4a` replay passes `mode: "meeting"` explicitly.

### Phase 6 — Client: UI

**T6.1** — `RecordingIndicatorView.swift:105-125`: use `recordingState.phaseLabel` (friendly map) and show chunk progress only when phase=="transcribe". Otherwise just the phase label.

**T6.2** — `SettingsView.swift:631` QuickActionsSection: embed `RecordingIndicatorView()` when `mode != .idle`. Cancel button when `mode ∈ {.uploading, .processing, .converting}`. **"Previous job finishing" banner** when `serverFinishingJobID != nil` — blocks new Transcribe-file action with a tooltip explaining wait. Estimated remaining computed as `(total_chunks - chunk_index) * avg_chunk_time` if available.

**T6.2.1** — "View server log" button in the popover; opens a sheet rendering `fetchServerLog(activeJobID ?? serverFinishingJobID)` as monospace. Refresh button polls every 5s.

**T6.3** — Build locally; resolve Swift errors.

### Phase 7 — Deployment to Mac mini (autonomous after Phase 0 approval)

**T7.0** — Commit `scripts/deploy-server.sh` to repo. Versioned. `set -e` fix: `code=$(curl ... || echo "000")`. Idempotent backup. Captures pre-deploy backup. Includes gist-transport scaffolding.

**T7.1** — Run deploy via `/macmini paste`: cp files, `uv sync`, prefetch model, verify ffmpeg/ffprobe on PATH.

**T7.2** — Pre-kickstart: query `/admin/active`. If active: abort, PushNotification user, do NOT restart. Wait for user.

**T7.3** — `launchctl kickstart -k gui/$UID/co.wispralt.server`. Poll healthz with `|| echo "000"`. Up to 60s.

**T7.4** — Smoke: curl healthz/readyz/admin/active. 30s mono m4a `mode=file` end-to-end. Verify expected output schema.

### Phase 8 — 12-run test matrix (autonomous)

Test files (prepare on dev MacBook; upload to mini):
- 30s clean clip / 5m Voice Memo / 30m clip / 105m `Sammamish Endodontics.m4a`

12 runs (4 sizes × 3 path combos: file/any, meeting/mono, meeting/stereo). Wall budgets parameterized on T0.5's `R` (transcribe_realtime_ratio), plus a `LOAD_COST = 60s` constant for cold-start (first job pays MLX + pyannote load).

| # | Size | Mode/path | Wall budget | Other pass |
|---|---|---|---|---|
| 1 | 30s | file/any | 30/R + 30s + LOAD_COST | json/srt/vtt/txt non-empty |
| 2 | 30s | meeting/mono | (30 × 1.5)/R + 60s + LOAD_COST | ≥1 speaker label |
| 3 | 30s | meeting/stereo | (30 × 1.5)/R + 60s + LOAD_COST | ≥2 speakers if applicable |
| 4 | 5m | file/any | 300/R + 60s | non-empty |
| 5 | 5m | meeting/mono | (300 × 1.5)/R + 90s | ≥1 speaker |
| 6 | 5m | meeting/stereo | (300 × 1.5)/R + 90s | ≥2 speakers if applicable |
| 7 | 30m | file/any | 1800/R + 120s | non-empty |
| 8 | 30m | meeting/mono | (1800 × 1.5)/R + 180s | ≥1 speaker |
| 9 | 30m | meeting/stereo | (1800 × 1.5)/R + 180s | ≥2 speakers if applicable |
| 10 | 105m | file/any | 6300/R + 300s | non-empty |
| 11 | 105m | meeting/mono | (6300 × 1.5)/R + 600s | **≥2 speakers — yesterday's failing case** |
| 12 | 105m | meeting/stereo | (6300 × 1.5)/R + 600s | ≥2 speakers if applicable |

LOAD_COST only on runs 1-3 (first three exercise cold-start). After warming, runs 4-12 skip the load tax.

For each: kick off via menubar UI, watch `/admin/active` + live server-log tail, capture `phase_done` lines, verify outputs.

**Cancel test (separate from matrix):**
- Run #10 (105m file).
- At chunk ~20% through, click Cancel.
- Pass: UI shows "Previous transcription still finishing on server" banner within 2s. `/admin/active` shows `cancel_requested=1`. Server log shows watchdog/cancel handling. **Expected:** new file submissions during this window return 429 OR are blocked client-side by the banner. After executor finishes naturally (additional ~Xm), banner clears, new submissions succeed.

**Failure handling:**
- Wall-budget fail → mark fail, log measured ratio, continue.
- Output-criteria fail → STOP. Investigate.
- After all 12: build summary table (measured vs budget, segments, speakers). Required for Phase 10 gate.

### Phase 9 — Docs

**T9.1** — [x] Update `docs/ARCHITECTURE.md`: caught up Custom Transcriptions + AAC drift (MeetingRecorder row rewritten for AVAssetWriter AAC m4a + back-pressure note; added Custom Transcriptions section after Cloud Fallback). Added: Processing Modes table (Dictation/Meeting/File with endpoint, channel handling, diarization, model, word_timestamps); MLX Whisper section (loader, model repo, spike `R=8.57`, eviction caveat, cancel semantics); Observability section (phase_start/done log shape, /transcribe/meeting/{id} `progress` block + `serverFinishing`, /admin/active and /admin/server-log/{id}); Honest Limitations section (2 real abort surfaces + 3 advisory). Updated memory table (mlx-whisper ~2.5–3.0 GB resident; total warm 7.5–8.5 GB; peak < 14 GB gate), eviction note, device matrix, MeetingPipeline row, concurrency model diagram, top-level dataflow box.

**T9.2** — [x] `docs/OVERVIEW.md` file→doc map updated: added `mlx_whisper_loader.py`, `prefetch-mlx-whisper.sh`, `benchmark-mlx-whisper.py`, `scripts/deploy-server.sh`, `routes/transcribe_file.py`, `CustomTranscriptionsStore.swift`, `LastTranscriptCaption.swift`, `TranscriptNotifications.swift`. Rewrote rows for `MenuBarController.swift` (extended `RecordingState`), `MeetingAPI.swift` (submitFile/cancel/fetchServerLog/ProgressInfo), `SettingsView.swift` (banner + log sheet + RecordingIndicatorView embed), `RecordingIndicatorView.swift` (`@EnvironmentObject` usage), `routes/admin.py` (active + server-log additions). `whisperx_loader.py` row annotated as deprecated-pending-Phase-10 but left in place.

**T9.3** — [x] `docs/DEPLOYMENT-NOTES.md`: added MLX Whisper section (pinned deps with rationale — `mlx-whisper==0.4.2` for tqdm-patch stability; `huggingface_hub>=1.12.0,<1.13` because pyannote-3.1 still calls `use_auth_token=`; HF cache path; ~1.6 GB disk; prefetch step), `scripts/deploy-server.sh` section (contract, idempotency, `set -e` polling-bug fix from 2026-05-09), "Recovery from corrupt mlx-whisper prefetch" section (signs: model.safetensors < 800 MB; manual recovery steps).

**T9.4** — [x] `docs/TESTING.md` created (file did not exist). Documents spike baseline (`audio_duration_s=6341.4`, `transcribe_s=740.26`, `R=8.57`); full 12-row matrix parameterized on `R` with `LOAD_COST=60s` on rows 1–3; Cancel test as separate scenario tied to the Honest Limitations doc; SRT/VTT/TXT regression pin against pre-swap golden; rollback path noting WhisperX stays in deps through Phase 8. Index of existing unit/integration tests included.

**Plan Delta (Phase 9):** TESTING.md did not exist in the repo prior to this phase — file was created fresh, not edited. All references in OVERVIEW.md to `TESTING.md` are net-new doc links.

### Phase 10 — Deprecated code removal (MANUAL GATE)

> **Before starting Phase 10: print the Phase 8 summary table. User must explicitly approve "delete WhisperX" before proceeding. Tightened approval criteria:**
> - **Zero wall-budget failures on rows 10-12** (the long-form 105m runs — the actual user pain point).
> - **≤ 1 wall-budget warning on rows 1-9** (cold-start / short-clip noise tolerated).
> - **Zero output-criteria failures** across all 12 rows.
> - **Cancel test passed.**
> - **Peak RSS observed across the matrix < 14 GB** (16 GB mini, 2 GB headroom).
> - **Row 11 measured `R` within 20%** of T0.5's spike `R` (no major prod-vs-spike drift).

**T10.1** — Delete `server/src/wispralt_server/meeting/whisperx_loader.py`.
**T10.2** — Remove `whisperx`, `ctranslate2`, `faster-whisper` from `pyproject.toml`. `uv sync`. Commit lockfile.
**T10.3** — `meeting/pipeline.py`: remove `import whisperx` and any vestigial references.
**T10.4** — Delete `server/tests/test_whisperx_no_speech.py`. (Port to `test_mlx_no_speech.py` is out of scope — TODO note.)
**T10.5** — Audit `meeting/__init__.py install_compat_shims`; remove whisperx-specific patches.
**T10.6** — Re-deploy via `scripts/deploy-server.sh`. Re-run matrix rows 1-3 to confirm clean state. Manual user confirm of green smoke.

---

## Manual Steps (require user approval)

1. **CRD access — ONE upfront approval at the start of Phase 0.** After approval, all mini-side work through end-of-Phase-8 runs autonomously.
2. **Phase 10 gate** — explicit user approval after Phase 8 summary, before deleting WhisperX.
3. **Final commit & push to origin/main** — explicit approval per CLAUDE.md push policy. Three commits:
   - End of Phase 6: `feat(server+client): mlx-whisper swap + request-mode + observability`
   - End of Phase 9: `docs: update for mlx + modes + observability`
   - End of Phase 10: `chore: remove whisperx after green matrix`

---

## Confidence: 7/10

**Why 7:** complete callsite map; real schema verified (request_mode chosen to avoid collision with existing `mode` column); reviewer round-1 + round-2 fixes folded; honest semaphore semantics (no false claims); resource gates (RAM 4 GB + disk 2× upload); the actual fix for the "all Speaker 1" bug is one line in `_run_source`; rollback preserved through Phase 8; manual gate before WhisperX deletion.

**Why not higher:** (a) spike outcome can force re-scope (3 branches in T0.5); (b) per-phase timeouts for in-pipeline transcribe/diarize are *advisory* — watchdog marks failed but cannot release semaphore (documented, UI surfaces "Previous job finishing"); (c) Cancel mid-transcribe ditto; (d) tqdm monkey-patch fragility mitigated by wall-clock fallback but still depends on mlx-whisper internals; (e) pyannote-on-mono-at-105-min and word_timestamps=True perf cost both unverified pre-spike (T0.4.2 + T0.4.4 close both).

---

## Open Questions / [NEEDS CLARIFICATION]

None for the user. All settled by brief + reviewer rounds 1 & 2. Spike outcome branching is internal.
