"""
jobs/store.py — SQLite-backed job repository for WisprAlt meeting transcription.

Design decisions (v3 deltas P4#4 + P5#2):
- WAL mode + NORMAL synchronous: durability with reduced fsync latency.
- Single lock guards all writes; reads also go through the lock for simplicity
  given the single-writer, single-consumer access pattern.
- recover_orphans() is called once at startup by main.py lifespan and follows
  the P5#2 policy:
    * running → failed (server restarted mid-job)
    * pending with missing WAV → failed (staging file disappeared)
    * pending with existing WAV → requeue (runner will re-enqueue)
    * pending with truncated WAV → failed + file deleted (C14)
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from wispralt_server._errors import UploadTruncatedError
from wispralt_server.ops.staging import validate_wav_completeness

logger = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    status: str            # pending | running | done | failed
    mode: Optional[str]    # remote | in_person | None until done (set on done)
    created_at: float
    started_at: Optional[float]
    finished_at: Optional[float]
    error: Optional[str]
    output_dir: Optional[str]
    wav_path: str
    attempts: int = 0      # C9: incremented each time the job is set to running
    # File-job flag: when the source upload was mono (custom transcription),
    # the meeting pipeline runs its single-channel branch instead of the
    # stereo mic/system one. Defaulted to 0 in SQL via in-place ALTER.
    force_single_channel: bool = False
    # NEW (Phase 3) — observability + intent. All idempotent ALTERs in __init__.
    # request_mode: file | meeting (set at submit time; distinct from `mode`
    # which is set on completion to remote | in_person | None).
    request_mode: Optional[str] = None
    phase: Optional[str] = None
    phase_started_at: Optional[float] = None
    chunk_index: int = 0
    total_chunks: int = 0
    cancel_requested: bool = False
    audio_duration_s: Optional[float] = None
    # Transcript-storage foundation (Phase 1). Captured at completion via
    # set_done so the future weekly-insights cron has data. Legacy rows have
    # NULL — Phase 2 cron filters those out. api_key_id is the existing
    # per-user FK; required for per-employee filtering.
    transcript_text: Optional[str] = None
    word_count: Optional[int] = None
    client_app_version: Optional[str] = None
    api_key_id: Optional[int] = None


def _row_to_job(row: tuple, cursor: sqlite3.Cursor) -> Job:
    """Construct a Job from a SELECT * row, dict-unpacked from cursor.description.

    Order-independent: future ALTERs need not match the dataclass field order.
    """
    d = dict(zip([c[0] for c in cursor.description], row))
    return Job(
        id=d["id"],
        status=d["status"],
        mode=d.get("mode"),
        created_at=d["created_at"],
        started_at=d.get("started_at"),
        finished_at=d.get("finished_at"),
        error=d.get("error"),
        output_dir=d.get("output_dir"),
        wav_path=d["wav_path"],
        attempts=int(d.get("attempts") or 0),
        force_single_channel=bool(d.get("force_single_channel") or 0),
        request_mode=d.get("request_mode"),
        phase=d.get("phase"),
        phase_started_at=d.get("phase_started_at"),
        chunk_index=int(d.get("chunk_index") or 0),
        total_chunks=int(d.get("total_chunks") or 0),
        cancel_requested=bool(d.get("cancel_requested") or 0),
        audio_duration_s=d.get("audio_duration_s"),
        transcript_text=d.get("transcript_text"),
        word_count=d.get("word_count"),
        client_app_version=d.get("client_app_version"),
        api_key_id=d.get("api_key_id"),
    )


class JobStore:
    """Thread-safe SQLite repository for meeting transcription jobs.

    Usage::

        store = JobStore(Path("~/.wispralt/jobs.db").expanduser())
        jid = store.create("/tmp/wispralt/abc.wav")
        store.set_running(jid)
        store.set_done(jid, "remote", "/var/meetings")
        job = store.get(jid)  # -> Job
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Retain the path for the chmod-on-startup logic below and for
        # diagnostics. Sidecar files (-wal, -shm) are derived from this path.
        self._db_path = db_path
        # isolation_level=None → autocommit; we manage transactions explicitly
        # check_same_thread=False → fine because we guard every call with _lock
        self.con = sqlite3.connect(
            str(db_path), isolation_level=None, check_same_thread=False
        )
        self._lock = threading.Lock()

        # P4#4: WAL journal mode + NORMAL synchronous — better write throughput,
        # still durable for our single-writer workload.
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")

        self.con.execute(
            """CREATE TABLE IF NOT EXISTS jobs(
                id          TEXT PRIMARY KEY,
                status      TEXT NOT NULL,
                mode        TEXT,
                created_at  REAL NOT NULL,
                started_at  REAL,
                finished_at REAL,
                error       TEXT,
                output_dir  TEXT,
                wav_path    TEXT NOT NULL,
                attempts    INTEGER DEFAULT 0
            )"""
        )
        # C9: add `attempts` column to existing DBs that were created without it.
        try:
            self.con.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists

        # /transcribe/file: track whether the canonical WAV should run through
        # the pipeline's single-channel branch. NO discriminator `kind` column —
        # the file extension of `wav_path` is the only discriminator
        # (.wav → already-transcoded → _run_pipeline; non-.wav → pre-transcode
        # source → _run_source).
        try:
            self.con.execute(
                "ALTER TABLE jobs ADD COLUMN force_single_channel INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

        # Phase 3: observability + intent columns. Idempotent ALTERs that match
        # the existing pattern. `request_mode` (file | meeting) is the explicit
        # intent at submit time; do NOT confuse with the existing `mode` column
        # (remote | in_person | None) which is set on done by the pipeline.
        for _alter in (
            "ALTER TABLE jobs ADD COLUMN request_mode TEXT",
            "ALTER TABLE jobs ADD COLUMN phase TEXT",
            "ALTER TABLE jobs ADD COLUMN phase_started_at REAL",
            "ALTER TABLE jobs ADD COLUMN chunk_index INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE jobs ADD COLUMN total_chunks INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE jobs ADD COLUMN audio_duration_s REAL",
            # Transcript-storage foundation (Phase 1). api_key_id is the
            # existing per-user FK; required for per-employee filters in
            # the future weekly-insights cron. Legacy rows are NULL —
            # Phase 2 filters those out at insight-render time.
            "ALTER TABLE jobs ADD COLUMN transcript_text TEXT",
            "ALTER TABLE jobs ADD COLUMN word_count INTEGER",
            "ALTER TABLE jobs ADD COLUMN client_app_version TEXT",
            "ALTER TABLE jobs ADD COLUMN api_key_id INTEGER",
        ):
            try:
                self.con.execute(_alter)
            except sqlite3.OperationalError:
                pass  # column already exists

        # Dictation transcripts live in a separate table because dictation is
        # request-response and never produces a row in `jobs`. One INSERT per
        # successful dictation; capture is fire-and-forget from the route.
        self.con.execute(
            """
            CREATE TABLE IF NOT EXISTS dictations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                duration_ms INTEGER,
                text TEXT NOT NULL,
                word_count INTEGER,
                client_app_version TEXT,
                smart_format_applied INTEGER
            )
            """
        )
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS idx_dictations_api_key_created"
            " ON dictations(api_key_id, created_at)"
        )

        # chmod 600 on jobs.db + -wal + -shm sidecars. Applied unconditionally
        # on every startup so a restored backup (which may have wider perms
        # propagated from the source filesystem) is auto-tightened. Logs at
        # INFO when a mode actually changes so an operator who deliberately
        # broadened perms knows about the revert.
        target_mode = stat.S_IRUSR | stat.S_IWUSR  # 0o600
        for suffix in ("", "-wal", "-shm"):
            sidecar = Path(str(self._db_path) + suffix)
            try:
                current = sidecar.stat().st_mode & 0o777
            except FileNotFoundError:
                continue  # -wal / -shm may not exist yet
            if current != 0o600:
                try:
                    os.chmod(sidecar, target_mode)
                    logger.info("chmod 600 %s (was %o)", sidecar, current)
                except OSError as exc:
                    logger.warning(
                        "chmod 600 failed for %s: %s (continuing)", sidecar, exc
                    )

    # ── private ───────────────────────────────────────────────────────────────

    def _exec(self, sql: str, *params: object) -> sqlite3.Cursor:
        """Execute *sql* with positional *params* under the write lock."""
        with self._lock:
            return self.con.execute(sql, params)

    # ── write operations ──────────────────────────────────────────────────────

    def create(
        self,
        wav_path: str,
        *,
        request_mode: str = "meeting",
        client_app_version: str | None = None,
        api_key_id: int | None = None,
    ) -> str:
        """Insert a new pending job and return its UUID.

        ``request_mode`` encodes the caller's intent (``file`` or ``meeting``)
        and is the source of truth for diarization routing in
        :class:`MeetingRunner._run_source`. Defaulted to ``meeting`` so the
        legacy ``submit_or_429`` path (called only by /transcribe/meeting POST)
        keeps working without an explicit flag.

        ``client_app_version`` (Phase 1 transcript-storage) records the
        submitting client's app version at submit time. Survives worker
        crashes because it's persisted on the original INSERT, not at
        ``set_done`` time.

        ``api_key_id`` is the owning user's ``wispralt.users.id``. Required
        for per-employee filtering in the future weekly-insights cron. Pass
        ``None`` for the break-glass admin (user.id < 0); those rows are
        explicitly excluded from capture.
        """
        jid = str(uuid.uuid4())
        self._exec(
            "INSERT INTO jobs("
            "id, status, created_at, wav_path, request_mode,"
            " client_app_version, api_key_id"
            ") VALUES(?,?,?,?,?,?,?)",
            jid,
            "pending",
            time.time(),
            wav_path,
            request_mode,
            client_app_version,
            api_key_id,
        )
        return jid

    def set_running(self, jid: str) -> None:
        """Transition a job from pending → running.  Increments `attempts`."""
        self._exec(
            "UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 WHERE id=?",
            time.time(),
            jid,
        )

    def set_done(
        self,
        jid: str,
        mode: str,
        output_dir: str,
        *,
        transcript_text: str | None = None,
    ) -> None:
        """Transition a job from running → done.

        ``transcript_text`` (Phase 1 transcript-storage) is the plain-text
        transcript derived from the pipeline's segment list. When provided
        the helper also computes ``word_count`` server-side so callers don't
        have to. Pass ``None`` to leave the columns untouched (e.g. legacy
        callers that never populated transcripts).

        ``client_app_version`` is intentionally NOT a kwarg here — it is
        already persisted at :meth:`create` time so the value survives a
        worker crash mid-pipeline.
        """
        word_count = len(transcript_text.split()) if transcript_text else None
        self._exec(
            "UPDATE jobs SET status='done', mode=?, output_dir=?,"
            " finished_at=?, transcript_text=?, word_count=?"
            " WHERE id=?",
            mode,
            output_dir,
            time.time(),
            transcript_text,
            word_count,
            jid,
        )

    def insert_dictation(
        self,
        api_key_id: int,
        text: str,
        duration_ms: int | None,
        client_app_version: str | None,
        smart_format_applied: bool,
    ) -> None:
        """Persist one dictation transcript row.

        Called from a background asyncio task in ``routes/dictate.py`` after
        the user-facing response has already been returned. ``duration_ms``
        stores Parakeet inference latency (NOT audio length); the future
        weekly-insights cron uses it to derive per-user transcription-latency
        trends.
        """
        word_count = len(text.split()) if text else 0
        self._exec(
            "INSERT INTO dictations("
            "api_key_id, created_at, duration_ms, text, word_count,"
            " client_app_version, smart_format_applied"
            ") VALUES(?,?,?,?,?,?,?)",
            api_key_id,
            time.time(),
            duration_ms,
            text,
            word_count,
            client_app_version,
            1 if smart_format_applied else 0,
        )

    def sweep_transcripts(self, days: int) -> tuple[int, int]:
        """Zero ``jobs.transcript_text`` and delete ``dictations`` rows older
        than *days* days.

        TTL is measured from the moment the transcript existed:
        - jobs: ``COALESCE(finished_at, created_at) < cutoff``. Without
          COALESCE, a job created 91 days ago but finished today would be
          reaped tomorrow — wrong intent.
        - dictations: ``created_at < cutoff``. Dictations are INSERTed at
          completion, so there is no separate "finished" timestamp.

        Returns ``(jobs_rows_zeroed, dictations_rows_deleted)``.

        Note: ``jobs.word_count`` and other metadata survive the sweep — the
        row stays as audit history. Dictations are deleted entirely because
        per-row metadata on a fast-flowing stream isn't worth retaining past
        the privacy TTL.
        """
        cutoff = time.time() - days * 86400
        # `_exec` is varargs — pass `cutoff` positionally, NOT as a tuple.
        jobs_cur = self._exec(
            "UPDATE jobs SET transcript_text = NULL"
            " WHERE transcript_text IS NOT NULL"
            " AND COALESCE(finished_at, created_at) < ?",
            cutoff,
        )
        dicts_cur = self._exec(
            "DELETE FROM dictations WHERE created_at < ?",
            cutoff,
        )
        return jobs_cur.rowcount, dicts_cur.rowcount

    def set_failed(self, jid: str, error: str) -> None:
        """Transition any job status → failed."""
        self._exec(
            "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
            error,
            time.time(),
            jid,
        )

    def delete(self, jid: str) -> None:
        """Hard-delete a job row (called after client confirms download)."""
        self._exec("DELETE FROM jobs WHERE id=?", jid)

    def update_after_transcode(
        self,
        jid: str,
        *,
        wav_path: str,
        force_single_channel: bool,
    ) -> None:
        """Persist the canonical-WAV path + single-channel flag.

        Durability boundary for /transcribe/file: once committed, the original
        source upload may be safely deleted because the row now points at the
        ffmpeg-produced canonical WAV.
        """
        self._exec(
            "UPDATE jobs SET wav_path=?, force_single_channel=? WHERE id=?",
            wav_path,
            1 if force_single_channel else 0,
            jid,
        )

    # ── read operations ───────────────────────────────────────────────────────

    def get(self, jid: str) -> Optional[Job]:
        """Return a Job by id, or None if not found."""
        cur = self._exec("SELECT * FROM jobs WHERE id=?", jid)
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_job(row, cur)

    def count_24h(self, status: str) -> int:
        """Return the count of finished jobs with *status* in the last 24 hours."""
        cur = self._exec(
            "SELECT COUNT(*) FROM jobs"
            " WHERE status=? AND finished_at IS NOT NULL AND finished_at > ?",
            status,
            time.time() - 86400,
        )
        return int(cur.fetchone()[0])

    def list_active_jobs(self) -> list[Job]:
        """Return all non-terminal jobs (pending or running)."""
        cur = self._exec(
            "SELECT * FROM jobs WHERE status IN ('pending','running')"
        )
        rows = cur.fetchall()
        return [_row_to_job(row, cur) for row in rows]

    def list_pending_ids(self) -> list[str]:
        """Return the IDs of all pending jobs. Used by MeetingRunner.reenqueue_pending."""
        cur = self._exec("SELECT id FROM jobs WHERE status='pending'")
        return [row[0] for row in cur.fetchall()]

    def list_pending_ids_with_mode(self) -> list[tuple[str, str, Optional[str]]]:
        """Return ``(id, wav_path, request_mode)`` for every pending job.

        Used by :meth:`MeetingRunner.reenqueue_pending` to route on both the
        file extension AND the explicit submit-time intent. ``request_mode``
        may be ``None`` for rows created before Phase 3.
        """
        cur = self._exec(
            "SELECT id, wav_path, request_mode FROM jobs"
            " WHERE status='pending' ORDER BY created_at"
        )
        return [(row[0], row[1], row[2]) for row in cur.fetchall()]

    # ── Phase 3 observability helpers ────────────────────────────────────────

    def update_phase(self, jid: str, phase: str) -> None:
        """Persist current phase + freshen ``phase_started_at`` for the watchdog."""
        self._exec(
            "UPDATE jobs SET phase=?, phase_started_at=? WHERE id=?",
            phase,
            time.time(),
            jid,
        )

    def update_chunk(self, jid: str, idx: int, total: int) -> None:
        """Persist progress within a chunked phase (e.g. ``transcribe``)."""
        self._exec(
            "UPDATE jobs SET chunk_index=?, total_chunks=? WHERE id=?",
            int(idx),
            int(total),
            jid,
        )

    def set_cancel_requested(self, jid: str) -> None:
        """Flag the job for cooperative cancellation. Cancel mid-ffmpeg is
        honored by ``transcode_to_canonical_wav``'s ``cancel_cb`` poll;
        cancel mid-transcribe/diarize is advisory (the executor cannot be
        interrupted). The UI uses this flag to show a 'finishing on server'
        banner."""
        self._exec("UPDATE jobs SET cancel_requested=1 WHERE id=?", jid)

    def check_cancel_requested(self, jid: str) -> bool:
        """Return True iff ``set_cancel_requested`` was called for *jid*."""
        cur = self._exec(
            "SELECT cancel_requested FROM jobs WHERE id=?", jid
        )
        row = cur.fetchone()
        return bool(row[0]) if row else False

    def update_audio_duration(self, jid: str, seconds: float) -> None:
        """Persist the ffprobed audio duration so the watchdog can scale
        per-phase budgets to the actual content."""
        self._exec(
            "UPDATE jobs SET audio_duration_s=? WHERE id=?",
            float(seconds),
            jid,
        )

    def fail_running_jobs(self, reason: str) -> int:
        """Mark all currently running jobs as failed with *reason*.

        Returns the number of rows updated.  Used by the SIGTERM handler (M1).
        """
        cur = self._exec(
            "UPDATE jobs SET status='failed', error=?, finished_at=?"
            " WHERE status='running'",
            reason,
            time.time(),
        )
        return cur.rowcount

    # ── startup recovery ──────────────────────────────────────────────────────

    def recover_orphans(self) -> dict[str, list[str]]:
        """P5#2 orphan recovery — call once at server startup.

        Policy:
        - running  → always failed (server restarted mid-job; job is dead)
        - pending + wav exists → leave pending, return in "requeue" list so
          the runner can re-enqueue them.
        - pending + wav missing → failed (staging file disappeared, cannot run)

        Returns a dict::

            {"requeue": [jid, ...], "failed": [jid, ...]}

        so the caller can log the outcome.
        """
        with self._lock:
            # Log the last-known phase + chunk progress for jobs that were
            # mid-flight when the server died — helps post-mortem diagnosis.
            try:
                running_cur = self.con.execute(
                    "SELECT id, phase, chunk_index, total_chunks"
                    " FROM jobs WHERE status='running'"
                )
                for jid, phase, ci, tc in running_cur.fetchall():
                    import logging as _l  # local import to avoid header churn
                    _l.getLogger(__name__).warning(
                        "[%s] recover_orphans: crashed mid-job"
                        " phase=%s chunk_index=%s total_chunks=%s",
                        jid, phase, ci, tc,
                    )
            except sqlite3.OperationalError:
                # phase/chunk_index columns absent → very old DB; skip log.
                pass

            # All running jobs are dead after a restart
            self.con.execute(
                "UPDATE jobs SET status='failed', error='server restart'"
                " WHERE status='running'"
            )

            # Backfill request_mode for legacy rows so the runner's mode-aware
            # routing has a value to consult. Derivation rule matches the
            # previous file-extension-based routing in reenqueue_pending:
            #   .wav → meeting (legacy /transcribe/meeting POST)
            #   any other extension → file (/transcribe/file source upload)
            try:
                self.con.execute(
                    "UPDATE jobs SET request_mode='meeting'"
                    " WHERE request_mode IS NULL"
                    " AND lower(substr(wav_path, -4)) = '.wav'"
                )
                self.con.execute(
                    "UPDATE jobs SET request_mode='file'"
                    " WHERE request_mode IS NULL"
                )
            except sqlite3.OperationalError:
                pass

            # Pending jobs: check WAV still on disk
            cur = self.con.execute(
                "SELECT id, wav_path FROM jobs WHERE status='pending'"
            )
            rows = cur.fetchall()

            requeue: list[str] = []
            failed: list[str] = []
            for jid, wav_path in rows:  # type: ignore[misc]
                wav = Path(wav_path)
                if not wav.exists():
                    self.con.execute(
                        "UPDATE jobs SET status='failed',"
                        " error='staging file missing after restart'"
                        " WHERE id=?",
                        (jid,),
                    )
                    failed.append(jid)
                    continue

                # /transcribe/file: a pre-transcode source (non-.wav extension)
                # cannot be WAV-validated — skip the check and just re-queue;
                # the runner's _run_source will (re)run ffprobe + ffmpeg.
                # Without this branch, m4a/mp3/etc. orphans would always crash
                # validate_wav_completeness and be marked failed on every
                # restart.
                if wav.suffix.lower() != ".wav":
                    requeue.append(jid)
                    continue

                # C14: Validate WAV completeness before re-queuing. A truncated
                # upload would succeed submit but fail transcription; fail-fast here.
                try:
                    validate_wav_completeness(wav)
                except UploadTruncatedError as exc:
                    self.con.execute(
                        "UPDATE jobs SET status='failed', error=?"
                        " WHERE id=?",
                        (f"WAV truncated; re-record ({exc})", jid),
                    )
                    try:
                        wav.unlink(missing_ok=True)
                    except OSError:
                        pass
                    failed.append(jid)
                    continue

                requeue.append(jid)

        return {"requeue": requeue, "failed": failed}
