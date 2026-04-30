# Brief: Lazy-load meeting transcription models (WhisperX + Pyannote)

## Why

Currently both Parakeet (dictation) and WhisperX + Pyannote (meetings) are loaded eagerly at server startup, holding ~4–6 GB of resident RAM on the Mac mini at all times. Dictation is latency-critical (sub-200ms target) and must stay eager. Meetings are async batch jobs where a 5–30s cold-load cost is invisible to the user (they upload, poll, wait minutes for output anyway). Lazy-loading the meeting stack frees ~2–3 GB of resident RAM whenever no meeting is active, which is the vast majority of the time on this server.

Secondary motivation: this discussion surfaced that meetings have **zero E2E verification** on the live mini — code path is sound but never smoke-tested. Worth a quick sanity check as part of this work.

## Context

**Current state (verified by codebase exploration):**

- `server/src/wispralt_server/main.py:163-203` — startup lifespan eagerly calls `load_parakeet_model()` AND `bootstrap_models(hf_token)` (which loads WhisperX + Pyannote)
- `server/src/wispralt_server/meeting/pipeline.py:75-95` — `bootstrap_models()` calls `whisperx_loader.load()` + `diarize.load()` synchronously
- `server/src/wispralt_server/meeting/whisperx_loader.py:22` — model: `nyrahealth/faster_CrisperWhisper`, CPU, int8
- `server/src/wispralt_server/meeting/diarize.py:28` — model: `pyannote/speaker-diarization-3.1`, MPS-if-available
- `server/src/wispralt_server/jobs/runner.py` — `MeetingRunner` runs jobs via dedicated `ThreadPoolExecutor(max_workers=1)`, isolated from async pool
- `server/src/wispralt_server/routes/meeting.py:54-94` — `POST /transcribe/meeting` returns 202 + job_id immediately; processing is fully async
- Both meeting models are stored in module-global state inside `whisperx_loader.py` and `diarize.py` (currently set during `bootstrap_models`)

**Project rule that this changes:**

`CLAUDE.md` line 47: *"No model loading per request — all models are resident at startup."* This rule was written when the project assumed all models had to be eager. The rule will be relaxed for meeting models (which are async batch, not request-blocking) but kept absolute for dictation (request-blocking).

**Readiness endpoint impact:**

`/readyz/meeting` currently presumably returns 200 once the eager bootstrap completes. After lazy-load, "ready" semantically means "lazy-load is wired up and will succeed when invoked" — not "models are warm." The endpoint should still return 200 at startup (the lazy loader is ready to fire), but should expose a separate field indicating warm vs cold so an admin can see state.

**No model swap.** CrisperWhisper (large-v2, verbatim/disfluency-tuned) is the right model for meetings — already verified during discussion. Alternatives like Canary-1B would require rewriting the WhisperX alignment layer for marginal WER gain at the cost of losing disfluency capture. Out of scope.

## Decisions

- **Lazy-load WhisperX + Pyannote on first meeting job, never on startup** — saves ~2–3 GB resident RAM until first use; load cost (~5–30s) is invisible because meeting jobs are async batch
- **Keep loaded once warm, no eviction** — meetings happen in clusters; repeated unload/reload would thrash and add unpredictable latency to the 2nd, 3rd, etc. meetings of a cluster
- **Parakeet stays eager** — dictation is request-blocking with a sub-200ms target; lazy-loading would tank UX
- **Single-flight load with asyncio.Lock (or threading.Lock matching the runner's executor)** — if two meeting jobs land back-to-back before the first load completes, only one load runs; the second waits on the lock
- **Load happens inside the meeting job runner thread, not the request handler** — request handler still returns 202 immediately; load cost is paid during job processing, surfaces only as longer "pending → done" wall time on the very first meeting after server start
- **Update CLAUDE.md rule** — relax "no model loading per request" to "no model loading per dictation request; meeting models load lazily on first use"
- **Update `/readyz/meeting`** — return 200 at startup (lazy loader is wired), expose `models_warm: bool` field so admin can distinguish cold vs warm
- **Add an E2E meeting smoke test** — there's currently zero verification meetings work on the live mini; add one as part of this work (not a separate task)
- **No model swap** — CrisperWhisper is already the right meeting model

## Rejected Alternatives

- **WhisperX-only setup (drop Parakeet)** — would push dictation latency from ~200ms to 1–3s. Kills core UX.
- **Time-based eviction (unload after N minutes idle)** — adds complexity and unpredictable cold-load latency mid-meeting-cluster for marginal RAM savings. The mini is dedicated; idle RAM is not contested.
- **Per-job load (load + unload per meeting)** — pathological RAM thrashing, ~10–60s wasted per job on load+unload cycles. Worst of both worlds.
- **Switch to whisper-large-v3** — newer base model but loses CrisperWhisper's verbatim/disfluency tuning, which is more valuable on meeting content than the marginal accuracy gain.
- **Switch to large-v3-turbo or distil-large-v3** — faster but loses disfluency capture; user explicitly prefers accuracy over speed for meetings.
- **Switch to NVIDIA Canary-1B** — SOTA WER on English ASR leaderboards but requires rewriting the WhisperX alignment + Pyannote merge layer. Multi-day refactor for ~1–2% WER gain. Out of scope.
- **Use online API (Deepgram/AssemblyAI/OpenAI Whisper API)** — user explicitly rejected; current self-hosted setup is free and works.
- **Eager-preload during idle background task after startup** — defeats the entire purpose (RAM stays held); only saves the cold-load cost, which is invisible anyway.

## Direction

Refactor `meeting/pipeline.py`, `meeting/whisperx_loader.py`, and `meeting/diarize.py` so the models load on first invocation of `transcribe_meeting()` rather than during startup `bootstrap_models()`. Use a single-flight lock to prevent concurrent loads if multiple jobs land before the first load completes. Remove WhisperX + Pyannote from the startup lifespan in `main.py`; keep Parakeet eager. Update `/readyz/meeting` to expose warm-vs-cold state. Update CLAUDE.md rule wording. Add an E2E smoke test that records → uploads → polls → downloads a meeting transcript end-to-end (covers the gap that meetings currently have zero automated verification).
