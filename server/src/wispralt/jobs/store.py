"""
jobs/store.py — ``JobStore``: file jobs, dictation transcripts and weekly insights.

Lives on the one SQLite DB (``Database``): a connection per call, one
transaction per method (``read()`` = ``BEGIN``, ``write()`` = ``BEGIN
IMMEDIATE``), so it is safe from any thread. ``user_id`` columns are real FKs
to ``users``. ``jobs.wav_path`` is stored relative to ``data_dir`` and
returned as an absolute ``Path``; outputs always live in ``outputs_dir``.

Recovery and shutdown (Locked Decision 8):

* ``recover_orphans()`` runs once at startup, BEFORE the staging sweep. A
  ``running`` row (the server died mid-job) goes back to ``pending`` when its
  input still exists, no cancel was requested and it has attempts left;
  otherwise it fails with an explicit reason. Pending rows then get today's
  checks (input missing → failed; truncated WAV → failed + unlinked).
* ``requeue_interrupted(jid)`` is the graceful-shutdown path: the row goes
  back to ``pending`` and the attempt ``set_running`` took is refunded.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wispralt._errors import DedupConflict, DictationDeleted, UploadTruncatedError
from wispralt.ops.staging import validate_wav_completeness

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

    from wispralt.database import Database

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
_DAY = 86_400.0
_CANCELLED = "cancelled by user"
_HISTORY_KINDS = ("dictation", "file")


@dataclass
class Job:
    id: str
    status: str  # pending | running | done | failed
    mode: str | None  # set on done by the pipeline ("single")
    created_at: float
    started_at: float | None
    finished_at: float | None
    error: str | None
    wav_path: Path  # absolute
    user_id: int
    attempts: int = 0
    phase: str | None = None
    phase_started_at: float | None = None
    chunk_index: int = 0
    total_chunks: int = 0
    cancel_requested: bool = False
    audio_duration_s: float | None = None
    transcript_text: str | None = None
    word_count: int | None = None
    client_app_version: str | None = None
    outputs_removed_at: float | None = None
    deleted_at: float | None = None


@dataclass(frozen=True, slots=True)
class HistoryHit:
    """One full-text hit. ``snippet`` is RAW text with matches wrapped in ``\\x02…\\x03``:
    render it only through ``web.templates_env.highlight`` (escape, then ``<mark>``)."""

    kind: str  # dictation | file
    row_id: str
    user_id: int
    created_at: float
    word_count: int | None
    snippet: str
    user_label: str | None = None  # team search only


_FTS_MAX_CHARS = 200
_FTS_MAX_TOKENS = 12
_FAR_FUTURE = 1e12


def fts_query(q: str | None) -> str | None:
    """User input → a safe FTS5 MATCH expression, or None (no search).

    Never passes input through raw: whitespace tokens, each double-quoted (``"`` → ``""``),
    implicitly ANDed, the last one a prefix match. Tokens with no letter or digit are dropped
    (an empty phrase matches nothing useful). Capped at 200 chars and 12 tokens.
    """
    if not q:
        return None
    tokens = [t for t in q[:_FTS_MAX_CHARS].split() if any(c.isalnum() for c in t)]
    tokens = tokens[:_FTS_MAX_TOKENS]
    if not tokens:
        return None
    quoted = ['"' + t.replace('"', '""') + '"' for t in tokens]
    quoted[-1] += "*"
    return " ".join(quoted)


# One leg per source table. {who} selects the owner label (team) or NULL; {scope} binds
# the owner. Both legs always join the source row, so deleted rows never surface.
_SEARCH_DICTATIONS = (
    "SELECT 'dictation' AS kind, CAST(d.id AS TEXT) AS row_id, d.user_id AS user_id,"
    " d.created_at AS created_at, d.word_count AS word_count, snippet(dictations_fts, 0, char(2), char(3), '…', 12) AS snippet, {who}"
    " FROM dictations_fts JOIN dictations d ON d.id = dictations_fts.rowid {join}"
    " WHERE dictations_fts MATCH :m AND d.deleted_at IS NULL"
    " AND d.created_at >= :s AND d.created_at < :u {scope}"
)
_SEARCH_JOBS = (
    "SELECT 'file' AS kind, j.id AS row_id, j.user_id AS user_id, j.created_at AS created_at,"
    " j.word_count AS word_count,"
    " snippet(jobs_fts, 0, char(2), char(3), '…', 12) AS snippet, {who}"
    " FROM jobs_fts JOIN jobs j ON j.id = jobs_fts.job_id {join}"
    " WHERE jobs_fts MATCH :m AND j.deleted_at IS NULL AND j.transcript_text IS NOT NULL"
    " AND j.created_at >= :s AND j.created_at < :u {scope}"
)


def _word_count(text: str | None) -> int | None:
    return len(text.split()) if text else None


def _stats(row: sqlite3.Row | None) -> dict[str, int | float]:
    total_words = int(row["total_words"] or 0) if row else 0
    return {
        "dictation_count": int(row["dictation_count"] or 0) if row else 0,
        "file_count": int(row["file_count"] or 0) if row else 0,
        "total_words": total_words,
        "total_inference_ms": int(row["total_inference_ms"] or 0) if row else 0,
        "time_saved_hours": round(total_words / 40.0 / 60.0, 2),  # 40 WPM typing baseline
    }


# Per-user activity since ?: dictations (non-deleted) + finished file jobs with text.
_ACTIVITY_SQL = """
SELECT user_id,
       SUM(kind = 'dictation') AS dictation_count,
       SUM(kind = 'file') AS file_count,
       COALESCE(SUM(word_count), 0) AS total_words,
       COALESCE(SUM(duration_ms), 0) AS total_inference_ms
  FROM (
    SELECT user_id, 'dictation' AS kind, word_count, duration_ms
      FROM dictations WHERE created_at >= ? AND deleted_at IS NULL
    UNION ALL
    SELECT user_id, 'file' AS kind, word_count, NULL
      FROM jobs WHERE created_at >= ? AND transcript_text IS NOT NULL AND deleted_at IS NULL
  )
"""


_EXPIRED_DICTATIONS = "SELECT {cols} FROM dictations WHERE created_at < ? AND source IS NULL"
_EXPIRED_JOBS = (
    "SELECT {cols} FROM jobs WHERE status IN ('done','failed') AND source IS NULL"
    " AND COALESCE(finished_at, created_at) < ?"
)


class JobStore:
    def __init__(self, db: Database, data_dir: Path) -> None:
        self.db = db
        self.data_dir = Path(data_dir)

    # ── paths ─────────────────────────────────────────────────────────────────

    def _rel(self, path: Path | str) -> str:
        """``path`` relative to ``data_dir`` when inside it (else kept absolute)."""
        p = Path(path)
        return str(p.relative_to(self.data_dir)) if p.is_relative_to(self.data_dir) else str(p)

    def _abs(self, stored: str) -> Path:
        return self.data_dir / stored

    def _job(self, r: sqlite3.Row) -> Job:
        d = dict(r)
        return Job(
            id=d["id"],
            status=d["status"],
            mode=d["mode"],
            created_at=d["created_at"],
            started_at=d["started_at"],
            finished_at=d["finished_at"],
            error=d["error"],
            wav_path=self._abs(d["wav_path"]),
            user_id=d["user_id"],
            attempts=int(d["attempts"] or 0),
            phase=d["phase"],
            phase_started_at=d["phase_started_at"],
            chunk_index=int(d["chunk_index"] or 0),
            total_chunks=int(d["total_chunks"] or 0),
            cancel_requested=bool(d["cancel_requested"]),
            audio_duration_s=d["audio_duration_s"],
            transcript_text=d["transcript_text"],
            word_count=d["word_count"],
            client_app_version=d["client_app_version"],
            outputs_removed_at=d["outputs_removed_at"],
            deleted_at=d["deleted_at"],
        )

    # ── job lifecycle ─────────────────────────────────────────────────────────

    def create(
        self, wav_path: Path | str, *, user_id: int, client_app_version: str | None = None
    ) -> str:
        """Insert a pending job; ``sqlite3.IntegrityError`` for an unknown user."""
        jid = str(uuid.uuid4())
        with self.db.write() as con:
            con.execute(
                "INSERT INTO jobs(id, user_id, status, created_at, wav_path, client_app_version)"
                " VALUES (?, ?, 'pending', ?, ?, ?)",
                (jid, user_id, time.time(), self._rel(wav_path), client_app_version),
            )
        return jid

    def set_running(self, jid: str) -> bool:
        """pending → running (+1 attempt), refused for a cancelled row. True iff it moved."""
        with self.db.write() as con:
            return (
                con.execute(
                    "UPDATE jobs SET status='running', started_at=?, attempts=attempts+1"
                    " WHERE id=? AND status='pending' AND cancel_requested=0",
                    (time.time(), jid),
                ).rowcount
                == 1
            )

    def set_done_if_not_cancelled(
        self, jid: str, mode: str, *, transcript_text: str | None = None
    ) -> bool:
        """running → done unless cancel was requested (one guarded UPDATE). True iff it moved."""
        with self.db.write() as con:
            return (
                con.execute(
                    "UPDATE jobs SET status='done', mode=?, finished_at=?, transcript_text=?,"
                    " word_count=? WHERE id=? AND status='running' AND cancel_requested=0",
                    (mode, time.time(), transcript_text, _word_count(transcript_text), jid),
                ).rowcount
                == 1
            )

    def set_failed(self, jid: str, error: str) -> None:
        with self.db.write() as con:
            con.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                (error, time.time(), jid),
            )

    def requeue_interrupted(self, jid: str) -> None:
        """Graceful shutdown: running → pending and refund the attempt ``set_running`` took."""
        with self.db.write() as con:
            con.execute(
                "UPDATE jobs SET status='pending', attempts=MAX(attempts-1, 0), phase=NULL,"
                " phase_started_at=NULL, started_at=NULL WHERE id=? AND status='running'",
                (jid,),
            )

    def mark_outputs_removed(self, jid: str) -> None:
        """DELETE after download: outputs are gone, the row and its text stay for history."""
        with self.db.write() as con:
            con.execute(
                "UPDATE jobs SET outputs_removed_at=? WHERE id=? AND outputs_removed_at IS NULL",
                (time.time(), jid),
            )

    def update_after_transcode(self, jid: str, *, wav_path: Path | str) -> None:
        """Persist the canonical WAV path (durability boundary before the source is unlinked)."""
        with self.db.write() as con:
            con.execute("UPDATE jobs SET wav_path=? WHERE id=?", (self._rel(wav_path), jid))

    def update_phase(self, jid: str, phase: str) -> None:
        with self.db.write() as con:
            con.execute(
                "UPDATE jobs SET phase=?, phase_started_at=? WHERE id=?", (phase, time.time(), jid)
            )

    def update_chunk(self, jid: str, idx: int, total: int) -> None:
        with self.db.write() as con:
            con.execute(
                "UPDATE jobs SET chunk_index=?, total_chunks=? WHERE id=?",
                (int(idx), int(total), jid),
            )

    def update_audio_duration(self, jid: str, seconds: float) -> None:
        with self.db.write() as con:
            con.execute(
                "UPDATE jobs SET audio_duration_s=? WHERE id=?", (float(seconds), jid)
            )

    def set_cancel_requested(self, jid: str) -> None:
        with self.db.write() as con:
            con.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (jid,))

    # ── job reads ─────────────────────────────────────────────────────────────

    def get(self, jid: str) -> Job | None:
        with self.db.read() as con:
            r = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return None if r is None else self._job(r)

    def get_status(self, jid: str) -> str | None:
        with self.db.read() as con:
            r = con.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
        return None if r is None else r["status"]

    def check_cancel_requested(self, jid: str) -> bool:
        with self.db.read() as con:
            r = con.execute("SELECT cancel_requested FROM jobs WHERE id=?", (jid,)).fetchone()
        return bool(r and r["cancel_requested"])

    def count_24h(self, status: str) -> int:
        """Finished jobs with *status* in the last 24 hours."""
        with self.db.read() as con:
            return con.execute(
                "SELECT COUNT(*) FROM jobs WHERE status=? AND finished_at > ?",
                (status, time.time() - _DAY),
            ).fetchone()[0]

    def queue_counts(self, now: float) -> dict[str, float | int | None]:
        """System page: pending / running now, failed in the last 24 h, oldest pending age."""
        with self.db.read() as con:
            r = con.execute(
                "SELECT COALESCE(SUM(status = 'pending'), 0) AS pending,"
                " COALESCE(SUM(status = 'running'), 0) AS running,"
                " COALESCE(SUM(status = 'failed' AND finished_at > ?), 0) AS failed_24h,"
                " MIN(CASE WHEN status = 'pending' THEN created_at END) AS oldest_pending"
                " FROM jobs",
                (now - _DAY,),
            ).fetchone()
        oldest = r["oldest_pending"]
        return {
            "pending": int(r["pending"]),
            "running": int(r["running"]),
            "failed_24h": int(r["failed_24h"]),
            "oldest_pending_age_s": None if oldest is None else max(0.0, now - oldest),
        }

    def list_active_jobs(self) -> list[Job]:
        with self.db.read() as con:
            rows = con.execute("SELECT * FROM jobs WHERE status IN ('pending','running')")
            return [self._job(r) for r in rows]

    def list_pending_jobs(self) -> list[tuple[str, Path]]:
        """``(id, absolute wav_path)`` of every pending job, oldest first."""
        with self.db.read() as con:
            rows = con.execute(
                "SELECT id, wav_path FROM jobs WHERE status='pending' ORDER BY created_at"
            )
            return [(r["id"], self._abs(r["wav_path"])) for r in rows]

    # ── retention (DB setting ``retention_days``; 0 = keep forever) ──────────
    # Imported rows (``source IS NOT NULL``) and ``usage_events`` are never purged.

    def retention_days(self) -> int:
        with self.db.read() as con:
            r = con.execute("SELECT value FROM settings WHERE key='retention_days'").fetchone()
        return int(r["value"]) if r else 0

    def set_retention_days(self, days: int) -> None:
        if days < 0:
            raise ValueError("retention_days must be >= 0")
        with self.db.write() as con:
            con.execute(
                "INSERT INTO settings(key, value) VALUES ('retention_days', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(days),),
            )

    def retention_preview(self, days: int) -> dict[str, int]:
        """Rows a purge at *days* would delete now (0 → nothing)."""
        if days <= 0:
            return {"dictations": 0, "jobs": 0}
        cutoff = time.time() - days * _DAY
        with self.db.read() as con:
            dicts = con.execute(_EXPIRED_DICTATIONS.format(cols="COUNT(*)"), (cutoff,)).fetchone()
            jobs = con.execute(_EXPIRED_JOBS.format(cols="COUNT(*)"), (cutoff,)).fetchone()
        return {"dictations": int(dicts[0]), "jobs": int(jobs[0])}

    def expired_jobs(self, days: int) -> list[str]:
        """Ids of finished (done/failed), non-imported jobs older than *days* (finish, else creation)."""
        if days <= 0:
            return []
        with self.db.read() as con:
            rows = con.execute(
                _EXPIRED_JOBS.format(cols="id"), (time.time() - days * _DAY,)
            ).fetchall()
        return [r["id"] for r in rows]

    def delete_finished_jobs(self, job_ids: list[str]) -> int:
        """Hard-delete the given jobs if still done/failed and not imported. Returns rows deleted."""
        deleted = 0
        with self.db.write() as con:
            for i in range(0, len(job_ids), 500):  # bound-variable limit
                batch = job_ids[i : i + 500]
                deleted += con.execute(
                    "DELETE FROM jobs WHERE status IN ('done','failed') AND source IS NULL"
                    f" AND id IN ({','.join('?' * len(batch))})",
                    batch,
                ).rowcount
        return deleted

    def purge_dictations(self, days: int) -> int:
        """Delete non-imported dictations older than *days* (0 → none). Returns rows deleted."""
        if days <= 0:
            return 0
        with self.db.write() as con:
            return con.execute(
                "DELETE FROM dictations WHERE created_at < ? AND source IS NULL",
                (time.time() - days * _DAY,),
            ).rowcount

    # ── startup recovery ──────────────────────────────────────────────────────

    def recover_orphans(self) -> dict[str, list[str]]:
        """Startup recovery (call before the staging sweep). Returns ``{"requeue", "failed"}``."""
        failed: list[str] = []
        requeue: list[str] = []
        now = time.time()
        with self.db.write() as con:

            def fail(jid: str, reason: str) -> None:
                con.execute(
                    "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                    (reason, now, jid),
                )
                failed.append(jid)

            for r in con.execute(
                "SELECT id, wav_path, attempts, cancel_requested, phase FROM jobs"
                " WHERE status='running'"
            ).fetchall():
                logger.warning("[%s] recover_orphans: interrupted at phase=%s", r["id"], r["phase"])
                if r["cancel_requested"]:
                    fail(r["id"], _CANCELLED)
                elif not self._abs(r["wav_path"]).exists():
                    fail(r["id"], "staging file missing after restart")
                elif r["attempts"] >= MAX_ATTEMPTS:
                    fail(r["id"], "max retries exceeded")
                else:
                    con.execute(
                        "UPDATE jobs SET status='pending', phase=NULL, phase_started_at=NULL,"
                        " started_at=NULL WHERE id=?",
                        (r["id"],),
                    )

            for r in con.execute("SELECT id, wav_path FROM jobs WHERE status='pending'").fetchall():
                wav = self._abs(r["wav_path"])
                if not wav.exists():
                    fail(r["id"], "staging file missing after restart")
                    continue
                # A pre-transcode source (non-.wav) is re-probed by the runner.
                if wav.suffix.lower() == ".wav":
                    try:
                        validate_wav_completeness(wav)
                    except UploadTruncatedError as exc:
                        fail(r["id"], f"WAV truncated; re-record ({exc})")
                        wav.unlink(missing_ok=True)
                        continue
                requeue.append(r["id"])
        return {"requeue": requeue, "failed": failed}

    # ── dictations ────────────────────────────────────────────────────────────

    @staticmethod
    def _stored_text(con: sqlite3.Connection, dedup_id: str, user_id: int) -> str | None:
        """Stored text for *dedup_id* owned by *user_id*, None if absent.

        ``DedupConflict`` when another user owns the id; ``DictationDeleted``
        when the owner soft-deleted it (re-running would resurrect it).
        """
        row = con.execute(
            "SELECT user_id, text, deleted_at FROM dictations WHERE client_dedup_id = ?",
            (dedup_id,),
        ).fetchone()
        if row is None:
            return None
        if row["user_id"] != user_id:
            raise DedupConflict(dedup_id)
        if row["deleted_at"] is not None:
            raise DictationDeleted(dedup_id)
        return row["text"]

    def find_dictation(self, dedup_id: str, user_id: int) -> str | None:
        """Replay lookup: the stored text, None, or ``DedupConflict`` / ``DictationDeleted``."""
        with self.db.read() as con:
            return self._stored_text(con, dedup_id, user_id)

    def insert_dictation(
        self,
        *,
        user_id: int,
        text: str,
        duration_ms: int | None,
        client_app_version: str | None,
        dedup_id: str | None,
        created_at: float | None = None,
    ) -> tuple[str, bool]:
        """Atomic select-or-insert in one ``BEGIN IMMEDIATE``. Returns ``(text, replayed)``.

        An existing row for *dedup_id* returns its stored text with
        ``replayed=True`` (or raises as ``find_dictation``).
        """
        with self.db.write() as con:
            if dedup_id is not None:
                stored = self._stored_text(con, dedup_id, user_id)
                if stored is not None:
                    return stored, True
            con.execute(
                "INSERT INTO dictations(user_id, created_at, duration_ms, text, word_count,"
                " client_app_version, client_dedup_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    time.time() if created_at is None else created_at,
                    duration_ms,
                    text,
                    len(text.split()),
                    client_app_version,
                    dedup_id,
                ),
            )
        return text, False

    def dictation_counts(self, now: float) -> dict[str, int]:
        """Non-deleted dictations in the last 24 h / 7 d / 30 d (portal tiles)."""
        with self.db.read() as con:
            r = con.execute(
                "SELECT COALESCE(SUM(created_at >= ?), 0), COALESCE(SUM(created_at >= ?), 0),"
                " COUNT(*) FROM dictations WHERE deleted_at IS NULL AND created_at >= ?",
                (now - _DAY, now - 7 * _DAY, now - 30 * _DAY),
            ).fetchone()
        return {"24h": int(r[0]), "7d": int(r[1]), "30d": int(r[2])}

    # ── weekly insights ───────────────────────────────────────────────────────

    def upsert_weekly_insight(
        self,
        *,
        iso_year: int,
        iso_week: int,
        user_id: int | None,
        insight: dict[str, Any],
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        model: str,
    ) -> None:
        """One row per (week, scope): ``user_id=None`` is the team row (``scope_key='team'``)."""
        scope_key = "team" if user_id is None else f"user:{user_id}"
        with self.db.write() as con:
            con.execute(
                "INSERT INTO weekly_insights(iso_year, iso_week, scope_key, user_id, insight_json,"
                " input_tokens, output_tokens, cost_usd, model, generated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(iso_year, iso_week, scope_key) DO UPDATE SET"
                " insight_json=excluded.insight_json, input_tokens=excluded.input_tokens,"
                " output_tokens=excluded.output_tokens, cost_usd=excluded.cost_usd,"
                " model=excluded.model, generated_at=excluded.generated_at",
                (
                    iso_year,
                    iso_week,
                    scope_key,
                    user_id,
                    json.dumps(insight),
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    model,
                    time.time(),
                ),
            )

    def _insight(self, iso_year: int, iso_week: int, scope_key: str) -> dict[str, Any] | None:
        with self.db.read() as con:
            r = con.execute(
                "SELECT insight_json FROM weekly_insights"
                " WHERE iso_year=? AND iso_week=? AND scope_key=?",
                (iso_year, iso_week, scope_key),
            ).fetchone()
        return json.loads(r[0]) if r else None

    def get_weekly_insight_person(
        self, user_id: int, iso_year: int, iso_week: int
    ) -> dict[str, Any] | None:
        return self._insight(iso_year, iso_week, f"user:{user_id}")

    def get_weekly_insight_team(self, iso_year: int, iso_week: int) -> dict[str, Any] | None:
        return self._insight(iso_year, iso_week, "team")

    def has_any_insight_for_week(self, iso_year: int, iso_week: int) -> bool:
        with self.db.read() as con:
            return (
                con.execute(
                    "SELECT 1 FROM weekly_insights WHERE iso_year=? AND iso_week=? LIMIT 1",
                    (iso_year, iso_week),
                ).fetchone()
                is not None
            )

    def weekly_insight(
        self, user_id: int | None, iso_year: int, iso_week: int
    ) -> dict[str, Any] | None:
        """The stored row for one scope and week (``user_id=None`` = team) with its metadata:
        ``insight`` (parsed JSON), ``model``, ``generated_at``, ``cost_usd``."""
        scope_key = "team" if user_id is None else f"user:{user_id}"
        with self.db.read() as con:
            r = con.execute(
                "SELECT insight_json, model, generated_at, cost_usd FROM weekly_insights"
                " WHERE iso_year=? AND iso_week=? AND scope_key=?",
                (iso_year, iso_week, scope_key),
            ).fetchone()
        if r is None:
            return None
        try:
            insight = json.loads(r["insight_json"])
        except ValueError:
            insight = None
        return {"insight": insight, "model": r["model"], "generated_at": r["generated_at"],
                "cost_usd": r["cost_usd"]}

    def refs_exist(self, refs: set[str], *, viewer_id: int, is_admin: bool) -> set[str]:
        """The subset of insight refs (``d:<id>`` / ``f:<job id>``) whose record still exists
        (not deleted) AND that the viewer may open (own rows; any row for an admin)."""
        dict_ids = sorted({r[2:] for r in refs if r.startswith("d:") and r[2:].isdigit()})
        job_ids = sorted({r[2:] for r in refs if r.startswith("f:") and r[2:]})
        found: set[str] = set()
        scope = "" if is_admin else " AND user_id = ?"
        extra = () if is_admin else (viewer_id,)
        with self.db.read() as con:
            for i in range(0, len(dict_ids), 500):
                batch = [int(x) for x in dict_ids[i : i + 500]]
                found.update(
                    f"d:{r[0]}"
                    for r in con.execute(
                        f"SELECT id FROM dictations WHERE deleted_at IS NULL{scope}"
                        f" AND id IN ({','.join('?' * len(batch))})",
                        (*extra, *batch),
                    )
                )
            for i in range(0, len(job_ids), 500):
                batch = job_ids[i : i + 500]
                found.update(
                    f"f:{r[0]}"
                    for r in con.execute(
                        "SELECT id FROM jobs WHERE deleted_at IS NULL AND transcript_text IS NOT NULL"
                        f"{scope} AND id IN ({','.join('?' * len(batch))})",
                        (*extra, *batch),
                    )
                )
        return found

    def count_records_after(
        self, user_id: int | None, since: float, until: float, generated_at: float
    ) -> int:
        """Records in ``[since, until)`` created after an insight was generated (stale badge)."""
        lo = max(since, generated_at)
        if lo >= until:
            return 0
        window = self.measured_window(user_id, lo, until)
        return int(window["dictation_count"]) + int(window["file_count"])

    def recent_topic_labels(
        self, user_id: int | None, iso_year: int, iso_week: int, *, weeks: int = 4
    ) -> dict[str, int]:
        """Casefolded topic/theme label → how many of the previous *weeks* stored insights
        (same scope) mention it. Computed, not asked of the LLM."""
        monday = date.fromisocalendar(iso_year, iso_week, 1)
        prior = [(monday - timedelta(weeks=k)).isocalendar()[:2] for k in range(1, weeks + 1)]
        scope_key = "team" if user_id is None else f"user:{user_id}"
        counts: dict[str, int] = {}
        with self.db.read() as con:
            for y, w in prior:
                r = con.execute(
                    "SELECT insight_json FROM weekly_insights"
                    " WHERE iso_year=? AND iso_week=? AND scope_key=?",
                    (y, w, scope_key),
                ).fetchone()
                if r is None:
                    continue
                try:
                    insight = json.loads(r[0])
                except ValueError:
                    continue
                labels = {
                    str(t.get("label", "")).casefold()
                    for key in ("topics", "themes")
                    for t in (insight.get(key) or [])
                    if isinstance(t, dict) and t.get("label")
                }
                for label in labels:
                    counts[label] = counts.get(label, 0) + 1
        return counts

    # ── insight runs (migration 0002): one row per run, progress polled by the portal ──

    def start_insight_run(
        self, *, trigger: str, iso_year: int, iso_week: int, estimated_usd: float | None,
        now: float,
    ) -> int:
        with self.db.write() as con:
            return con.execute(
                "INSERT INTO insight_runs(trigger, iso_year, iso_week, status, estimated_usd,"
                " started_at) VALUES (?, ?, ?, 'running', ?, ?) RETURNING id",
                (trigger, iso_year, iso_week, estimated_usd, now),
            ).fetchone()[0]

    def update_insight_run(
        self, run_id: int, *, people_total: int, people_done: int,
        failures: list[dict[str, Any]], cost_usd: float,
    ) -> None:
        """Progress. ``failures`` holds ``{user_id, error_class}`` only (never text)."""
        with self.db.write() as con:
            con.execute(
                "UPDATE insight_runs SET people_total=?, people_done=?, failures_json=?,"
                " cost_usd=? WHERE id=?",
                (people_total, people_done, json.dumps(failures), cost_usd, run_id),
            )

    def finish_insight_run(
        self, run_id: int, *, status: str, error_class: str | None, now: float
    ) -> None:
        with self.db.write() as con:
            con.execute(
                "UPDATE insight_runs SET status=?, error_class=?, finished_at=? WHERE id=?",
                (status, error_class, now, run_id),
            )

    def recent_insight_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.db.read() as con:
            rows = con.execute(
                "SELECT * FROM insight_runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["failures"] = json.loads(d.pop("failures_json") or "[]")
            out.append(d)
        return out

    def scheduled_run_state(self, iso_year: int, iso_week: int) -> dict[str, Any]:
        """For the scheduler: whether a scheduled run of the week finished ``done``, and
        when the latest scheduled attempt started."""
        with self.db.read() as con:
            r = con.execute(
                "SELECT COALESCE(MAX(status = 'done'), 0) AS done, MAX(started_at) AS last_started"
                " FROM insight_runs WHERE trigger = 'scheduled' AND iso_year = ? AND iso_week = ?",
                (iso_year, iso_week),
            ).fetchone()
        return {"done": bool(r["done"]), "last_started": r["last_started"]}

    def words_by_user(self, since: float, until: float) -> dict[int, dict[str, int]]:
        """Per user in ``[since, until)``: dictation count, file count and words (estimates)."""
        with self.db.read() as con:
            rows = con.execute(
                "SELECT user_id, SUM(kind = 'dictation') AS dictations, SUM(kind = 'file') AS files,"
                " COALESCE(SUM(word_count), 0) AS words FROM ("
                " SELECT user_id, 'dictation' AS kind, word_count FROM dictations"
                "  WHERE deleted_at IS NULL AND created_at >= :s AND created_at < :u"
                " UNION ALL SELECT user_id, 'file', word_count FROM jobs"
                "  WHERE deleted_at IS NULL AND transcript_text IS NOT NULL"
                "  AND created_at >= :s AND created_at < :u) GROUP BY user_id",
                {"s": since, "u": until},
            )
            return {
                r["user_id"]: {"dictations": int(r["dictations"]), "files": int(r["files"]),
                               "words": int(r["words"])}
                for r in rows
            }

    def rolling_insights_cost_usd(self, days: int = 30) -> float:
        """Insights spend in the last *days*: the larger of the stored insights' cost and the
        runs' recorded spend (a forced rerun overwrites a row's cost; runs also count
        replies that were billed but unusable)."""
        since = time.time() - days * _DAY
        with self.db.read() as con:
            stored = con.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM weekly_insights WHERE generated_at >= ?",
                (since,),
            ).fetchone()[0]
            runs = con.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM insight_runs WHERE started_at >= ?",
                (since,),
            ).fetchone()[0]
        return float(max(stored, runs))

    # ── per-user stats + transcripts ──────────────────────────────────────────

    def compute_user_stats(self, user_id: int, *, since_epoch: float) -> dict[str, int | float]:
        """``{dictation_count, file_count, total_words, total_inference_ms, time_saved_hours}``."""
        with self.db.read() as con:
            r = con.execute(
                _ACTIVITY_SQL + " WHERE user_id = ?", (since_epoch, since_epoch, user_id)
            ).fetchone()
        return _stats(r if r is not None and r["user_id"] is not None else None)

    # ── measured stats (Part 6): computed from rows, never from the LLM ───────

    @staticmethod
    def _scope(alias: str, user_id: int | None) -> str:
        return f" AND {alias}.user_id = :uid" if user_id is not None else ""

    def measured_window(
        self, user_id: int | None, since: float, until: float
    ) -> dict[str, int | float]:
        """One grouped query over ``[since, until)``: dictation/file counts, words, file audio
        seconds and dictation inference ms. ``user_id=None`` = the whole team (admin pages)."""
        sql = (
            "SELECT COALESCE(SUM(kind = 'dictation'), 0) AS dictation_count,"
            " COALESCE(SUM(kind = 'file'), 0) AS file_count,"
            " COALESCE(SUM(word_count), 0) AS words,"
            " COALESCE(SUM(audio_s), 0) AS file_audio_s,"
            " COALESCE(SUM(infer_ms), 0) AS dictation_inference_ms FROM ("
            " SELECT 'dictation' AS kind, d.word_count, NULL AS audio_s, d.duration_ms AS infer_ms"
            " FROM dictations d WHERE d.deleted_at IS NULL"
            " AND d.created_at >= :s AND d.created_at < :u" + self._scope("d", user_id) +
            " UNION ALL"
            " SELECT 'file', j.word_count, j.audio_duration_s, NULL FROM jobs j"
            " WHERE j.deleted_at IS NULL AND j.transcript_text IS NOT NULL"
            " AND j.created_at >= :s AND j.created_at < :u" + self._scope("j", user_id) + ")"
        )
        with self.db.read() as con:
            r = con.execute(sql, {"s": since, "u": until, "uid": user_id}).fetchone()
        return {k: (float(v) if k == "file_audio_s" else int(v)) for k, v in dict(r).items()}

    def activity_points(
        self, user_id: int | None, since: float, until: float
    ) -> list[tuple[float, int]]:
        """``(created_at, word_count)`` of every non-deleted record in the window (trend,
        hour-of-day histogram and active days are bucketed in Python, in a timezone)."""
        sql = (
            "SELECT d.created_at, COALESCE(d.word_count, 0) FROM dictations d"
            " WHERE d.deleted_at IS NULL AND d.created_at >= :s AND d.created_at < :u"
            + self._scope("d", user_id) +
            " UNION ALL SELECT j.created_at, COALESCE(j.word_count, 0) FROM jobs j"
            " WHERE j.deleted_at IS NULL AND j.transcript_text IS NOT NULL"
            " AND j.created_at >= :s AND j.created_at < :u" + self._scope("j", user_id)
        )
        with self.db.read() as con:
            return [(r[0], r[1]) for r in con.execute(sql, {"s": since, "u": until, "uid": user_id})]

    def iter_dictation_texts(
        self, user_id: int | None, since: float, until: float
    ) -> Iterator[tuple[int, str]]:
        """``(user_id, text)`` of non-deleted dictations in the window (filler counting).
        Consume it in one thread."""
        with self.db.read() as con:
            yield from (
                (r[0], r[1])
                for r in con.execute(
                    "SELECT d.user_id, d.text FROM dictations d WHERE d.deleted_at IS NULL"
                    " AND d.created_at >= :s AND d.created_at < :u" + self._scope("d", user_id),
                    {"s": since, "u": until, "uid": user_id},
                )
            )

    def stats_by_user(self, since_epoch: float) -> dict[int, dict[str, int | float]]:
        """``compute_user_stats`` for every user with activity, in one grouped query."""
        with self.db.read() as con:
            rows = con.execute(_ACTIVITY_SQL + " GROUP BY user_id", (since_epoch, since_epoch))
            return {r["user_id"]: _stats(r) for r in rows}

    def transcripts_in_range(
        self, user_id: int, *, since_epoch: float, until_epoch: float
    ) -> list[dict[str, Any]]:
        """``{created_at, text, kind, row_id}`` (kind ``dictation`` | ``file``), newest first."""
        with self.db.read() as con:
            rows = con.execute(
                "SELECT created_at, text, kind, row_id FROM ("
                " SELECT created_at, text, 'dictation' AS kind, CAST(id AS TEXT) AS row_id"
                "  FROM dictations"
                "  WHERE user_id = ? AND created_at >= ? AND created_at < ? AND deleted_at IS NULL"
                " UNION ALL"
                " SELECT created_at, transcript_text AS text, 'file' AS kind, id AS row_id FROM jobs"
                "  WHERE user_id = ? AND transcript_text IS NOT NULL AND deleted_at IS NULL"
                "  AND created_at >= ? AND created_at < ?"
                ") ORDER BY created_at DESC, row_id",
                (user_id, since_epoch, until_epoch, user_id, since_epoch, until_epoch),
            )
            return [dict(r) for r in rows]

    def transcripts_in_range_filtered(
        self,
        user_id: int,
        *,
        since_epoch: float,
        until_epoch: float,
        kind: str | None = None,
        dict_cursor: tuple[float, str] | None = None,
        jobs_cursor: tuple[float, str] | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], tuple[float, str] | None, tuple[float, str] | None]:
        """Personal history with per-leg cursors: ``(rows, next_dict_cursor, next_jobs_cursor)``.

        Each leg's cursor is set only when that leg returned ``limit`` rows. Text search is
        :meth:`search_history` (FTS5), not a filter here.
        """
        dict_rows: list[dict[str, Any]] = []
        job_rows: list[dict[str, Any]] = []
        with self.db.read() as con:
            if kind != "file":
                sql = (
                    "SELECT CAST(id AS TEXT) AS row_id, 'dictation' AS kind, created_at,"
                    " substr(text, 1, 200) AS preview, word_count, deleted_at,"
                    " COALESCE(source, 'server') AS source FROM dictations"
                    " WHERE user_id = ? AND created_at >= ? AND created_at < ? AND deleted_at IS NULL"
                )
                args: list[object] = [user_id, since_epoch, until_epoch]
                if dict_cursor is not None:
                    # dictations.id is INTEGER: compare as INTEGER (a text compare
                    # disagrees across digit widths).
                    sql += " AND (created_at, id) < (?, CAST(? AS INTEGER))"
                    args.extend(dict_cursor)
                sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
                dict_rows = [dict(r) for r in con.execute(sql, (*args, limit))]
            if kind != "dictation":
                sql = (
                    "SELECT id AS row_id, 'file' AS kind, created_at,"
                    " substr(transcript_text, 1, 200) AS preview, word_count, deleted_at,"
                    " 'server' AS source FROM jobs"
                    " WHERE user_id = ? AND created_at >= ? AND created_at < ?"
                    " AND transcript_text IS NOT NULL AND deleted_at IS NULL"
                )
                args = [user_id, since_epoch, until_epoch]
                if jobs_cursor is not None:
                    sql += " AND (created_at, id) < (?, ?)"
                    args.extend(jobs_cursor)
                sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
                job_rows = [dict(r) for r in con.execute(sql, (*args, limit))]

        merged = sorted(dict_rows + job_rows, key=lambda r: (-r["created_at"], r["row_id"]))[:limit]

        def _next(rows: list[dict[str, Any]]) -> tuple[float, str] | None:
            return (rows[-1]["created_at"], rows[-1]["row_id"]) if len(rows) == limit else None

        return merged, _next(dict_rows), _next(job_rows)

    # ── full-text history search (FTS5, migration 0002) ──────────────────────

    def _search(
        self,
        match: str,
        *,
        owner_id: int | None,
        team: bool,
        since: float | None,
        until: float | None,
        kind: str | None,
        limit: int,
        offset: int,
    ) -> list[HistoryHit]:
        legs = []
        for leg_kind, template, alias in (
            ("dictation", _SEARCH_DICTATIONS, "d"),
            ("file", _SEARCH_JOBS, "j"),
        ):
            if kind in (None, leg_kind):
                legs.append(
                    template.format(
                        who="COALESCE(u.display_name, u.label) AS user_label" if team else
                        "NULL AS user_label",
                        join=f"JOIN users u ON u.id = {alias}.user_id" if team else "",
                        scope=f"AND {alias}.user_id = :uid" if owner_id is not None else "",
                    )
                )
        if not legs:
            return []
        sql = (
            "SELECT * FROM (" + " UNION ALL ".join(legs) + ")"
            " ORDER BY created_at DESC, row_id LIMIT :lim OFFSET :off"
        )
        params = {
            "m": match,
            "uid": owner_id,
            "s": 0.0 if since is None else since,
            "u": _FAR_FUTURE if until is None else until,
            "lim": limit,
            "off": offset,
        }
        with self.db.read() as con:
            return [HistoryHit(**dict(r)) for r in con.execute(sql, params)]

    def search_history(
        self,
        user_id: int,
        q: str,
        *,
        since: float | None = None,
        until: float | None = None,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[HistoryHit]:
        """The viewer's OWN matching rows, newest first. ``user_id`` is always bound in SQL."""
        match = fts_query(q)
        if match is None:
            return []
        return self._search(
            match, owner_id=int(user_id), team=False, since=since, until=until,
            kind=kind, limit=limit, offset=offset,
        )

    def search_history_team(
        self,
        q: str,
        *,
        user_id: int | None = None,
        since: float | None = None,
        until: float | None = None,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[HistoryHit]:
        """Team-wide search with owner labels. Admin routes only (LD 15)."""
        match = fts_query(q)
        if match is None:
            return []
        return self._search(
            match, owner_id=user_id, team=True, since=since, until=until,
            kind=kind, limit=limit, offset=offset,
        )

    # ── /me/history rows (kind: dictation | file) ─────────────────────────────

    def get_record_scoped(
        self, kind: str, row_id: str, *, viewer_id: int, is_admin: bool
    ) -> dict[str, Any] | None:
        """One non-deleted record (``SELECT *`` + ``user_label``) the viewer may open, else None.

        The scope is in SQL: an employee matches only rows they own; an admin any row
        (LD 15: admins may read team transcripts). Unknown kind → None.
        """
        if kind == "dictation":
            sql = (
                "SELECT r.*, COALESCE(u.display_name, u.label) AS user_label FROM dictations r"
                " JOIN users u ON u.id = r.user_id"
                " WHERE r.id = CAST(? AS INTEGER) AND r.deleted_at IS NULL"
            )
        elif kind == "file":
            sql = (
                "SELECT r.*, COALESCE(u.display_name, u.label) AS user_label FROM jobs r"
                " JOIN users u ON u.id = r.user_id"
                " WHERE r.id = ? AND r.transcript_text IS NOT NULL AND r.deleted_at IS NULL"
            )
        else:
            return None
        sql += " AND (? OR r.user_id = ?)"
        with self.db.read() as con:
            r = con.execute(sql, (row_id, 1 if is_admin else 0, viewer_id)).fetchone()
        return None if r is None else dict(r)

    def get_history_row(self, user_id: int, kind: str, row_id: str) -> dict[str, Any] | None:
        """Owner-checked, non-deleted single row, or None (the /me surface)."""
        return self.get_record_scoped(kind, row_id, viewer_id=user_id, is_admin=False)

    def list_records_team(
        self,
        *,
        user_id: int | None = None,
        since: float | None = None,
        until: float | None = None,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Admin records list (no text query), newest first, with owner labels. Admin only."""
        legs = []
        if kind in (None, "dictation"):
            legs.append(
                "SELECT 'dictation' AS kind, CAST(r.id AS TEXT) AS row_id, r.user_id AS user_id,"
                " r.created_at AS created_at, r.word_count AS word_count,"
                " substr(r.text, 1, 200) AS preview FROM dictations r"
                " WHERE r.deleted_at IS NULL AND r.created_at >= :s AND r.created_at < :u"
                + (" AND r.user_id = :uid" if user_id is not None else "")
            )
        if kind in (None, "file"):
            legs.append(
                "SELECT 'file' AS kind, r.id AS row_id, r.user_id AS user_id,"
                " r.created_at AS created_at, r.word_count AS word_count,"
                " substr(r.transcript_text, 1, 200) AS preview FROM jobs r"
                " WHERE r.deleted_at IS NULL AND r.transcript_text IS NOT NULL"
                " AND r.created_at >= :s AND r.created_at < :u"
                + (" AND r.user_id = :uid" if user_id is not None else "")
            )
        if not legs:
            return []
        sql = (
            "SELECT x.*, COALESCE(u.display_name, u.label) AS user_label FROM ("
            + " UNION ALL ".join(legs)
            + ") x JOIN users u ON u.id = x.user_id"
            " ORDER BY x.created_at DESC, x.row_id LIMIT :lim OFFSET :off"
        )
        params = {
            "uid": user_id,
            "s": 0.0 if since is None else since,
            "u": _FAR_FUTURE if until is None else until,
            "lim": limit,
            "off": offset,
        }
        with self.db.read() as con:
            return [dict(r) for r in con.execute(sql, params)]

    def _set_deleted(self, user_id: int, kind: str, row_id: str, *, deleted: bool) -> bool:
        if kind not in _HISTORY_KINDS:
            return False
        table, id_expr = (
            ("dictations", "CAST(? AS INTEGER)") if kind == "dictation" else ("jobs", "?")
        )
        guard = "deleted_at IS NULL" if deleted else "deleted_at IS NOT NULL"
        with self.db.write() as con:
            return (
                con.execute(
                    f"UPDATE {table} SET deleted_at=? WHERE user_id=? AND id={id_expr} AND {guard}",
                    (time.time() if deleted else None, user_id, row_id),
                ).rowcount
                > 0
            )

    def soft_delete_history_row(self, user_id: int, kind: str, row_id: str) -> bool:
        """Hide one owned row (``deleted_at = now``). False if already hidden or not owned."""
        return self._set_deleted(user_id, kind, row_id, deleted=True)

    def restore_history_row(self, user_id: int, kind: str, row_id: str) -> bool:
        """Inverse of :meth:`soft_delete_history_row`."""
        return self._set_deleted(user_id, kind, row_id, deleted=False)
