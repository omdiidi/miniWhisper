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

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from wispralt_server._errors import UploadTruncatedError
from wispralt_server.ops.staging import validate_wav_completeness


@dataclass
class Job:
    id: str
    status: str            # pending | running | done | failed
    mode: Optional[str]    # remote | in_person | None until done
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
    # APPENDED LAST so positional `Job(*row)` unpacks still work.
    force_single_channel: bool = False


def _row_to_job(row: tuple) -> Job:
    """Construct a Job from a SELECT row, coercing the SQLite int → bool."""
    (
        jid, status, mode, created_at, started_at, finished_at,
        error, output_dir, wav_path, attempts, force_single_channel,
    ) = row
    return Job(
        id=jid,
        status=status,
        mode=mode,
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        error=error,
        output_dir=output_dir,
        wav_path=wav_path,
        attempts=attempts or 0,
        force_single_channel=bool(force_single_channel),
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

    # ── private ───────────────────────────────────────────────────────────────

    def _exec(self, sql: str, *params: object) -> sqlite3.Cursor:
        """Execute *sql* with positional *params* under the write lock."""
        with self._lock:
            return self.con.execute(sql, params)

    # ── write operations ──────────────────────────────────────────────────────

    def create(self, wav_path: str) -> str:
        """Insert a new pending job and return its UUID."""
        jid = str(uuid.uuid4())
        self._exec(
            "INSERT INTO jobs(id, status, created_at, wav_path) VALUES(?,?,?,?)",
            jid,
            "pending",
            time.time(),
            wav_path,
        )
        return jid

    def set_running(self, jid: str) -> None:
        """Transition a job from pending → running.  Increments `attempts`."""
        self._exec(
            "UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 WHERE id=?",
            time.time(),
            jid,
        )

    def set_done(self, jid: str, mode: str, output_dir: str) -> None:
        """Transition a job from running → done."""
        self._exec(
            "UPDATE jobs SET status='done', mode=?, output_dir=?, finished_at=? WHERE id=?",
            mode,
            output_dir,
            time.time(),
            jid,
        )

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
        cur = self._exec(
            "SELECT id, status, mode, created_at, started_at, finished_at,"
            " error, output_dir, wav_path, attempts, force_single_channel"
            " FROM jobs WHERE id=?",
            jid,
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_job(row)

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
            "SELECT id, status, mode, created_at, started_at, finished_at,"
            " error, output_dir, wav_path, attempts, force_single_channel"
            " FROM jobs WHERE status IN ('pending','running')"
        )
        return [_row_to_job(row) for row in cur.fetchall()]

    def list_pending_ids(self) -> list[str]:
        """Return the IDs of all pending jobs. Used by MeetingRunner.reenqueue_pending."""
        cur = self._exec("SELECT id FROM jobs WHERE status='pending'")
        return [row[0] for row in cur.fetchall()]

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
            # All running jobs are dead after a restart
            self.con.execute(
                "UPDATE jobs SET status='failed', error='server restart'"
                " WHERE status='running'"
            )

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
