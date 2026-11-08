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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wispralt._errors import DedupConflict, DictationDeleted, UploadTruncatedError
from wispralt.ops.staging import validate_wav_completeness

if TYPE_CHECKING:
    import sqlite3

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

    def delete(self, jid: str) -> None:
        """Hard-delete a job row (the DELETE route, after its outputs are removed)."""
        with self.db.write() as con:
            con.execute("DELETE FROM jobs WHERE id=?", (jid,))

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

    # ── retention (Task 7 replaces these with the retention setting) ─────────

    def list_expired_finished_jobs(self, days: int) -> list[str]:
        """Ids of finished (done/failed) jobs older than *days* (by finish, else creation)."""
        with self.db.read() as con:
            rows = con.execute(
                "SELECT id FROM jobs WHERE status IN ('done','failed')"
                " AND COALESCE(finished_at, created_at) < ?",
                (time.time() - days * _DAY,),
            )
            return [r["id"] for r in rows]

    def delete_finished_jobs(self, job_ids: list[str]) -> int:
        """Hard-delete the given jobs if still done/failed. Returns rows deleted."""
        deleted = 0
        with self.db.write() as con:
            for i in range(0, len(job_ids), 500):  # bound-variable limit
                batch = job_ids[i : i + 500]
                deleted += con.execute(
                    "DELETE FROM jobs WHERE status IN ('done','failed')"
                    f" AND id IN ({','.join('?' * len(batch))})",
                    batch,
                ).rowcount
        return deleted

    def sweep_transcripts(self, days: int) -> tuple[int, int]:
        """Zero ``jobs.transcript_text`` and delete ``dictations`` older than *days*.

        Returns ``(jobs_rows_zeroed, dictations_rows_deleted)``.
        """
        cutoff = time.time() - days * _DAY
        with self.db.write() as con:
            jobs = con.execute(
                "UPDATE jobs SET transcript_text = NULL WHERE transcript_text IS NOT NULL"
                " AND COALESCE(finished_at, created_at) < ?",
                (cutoff,),
            ).rowcount
            dicts = con.execute("DELETE FROM dictations WHERE created_at < ?", (cutoff,)).rowcount
        return jobs, dicts

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

    def list_filler_counts_for_week(self, iso_year: int, iso_week: int) -> list[tuple[int, int]]:
        """``[(user_id, filler_word_count)]`` of the week's person rows, highest first."""
        out: list[tuple[int, int]] = []
        with self.db.read() as con:
            rows = con.execute(
                "SELECT user_id, insight_json FROM weekly_insights"
                " WHERE user_id IS NOT NULL AND iso_year=? AND iso_week=?",
                (iso_year, iso_week),
            ).fetchall()
        for r in rows:
            try:
                n = json.loads(r["insight_json"]).get("filler_word_count")
            except (TypeError, ValueError, AttributeError):
                logger.warning("malformed insight_json for user_id=%s W%02d", r["user_id"], iso_week)
                continue
            if isinstance(n, int):
                out.append((int(r["user_id"]), n))
        out.sort(key=lambda t: t[1], reverse=True)
        return out

    def rolling_insights_cost_usd(self, days: int = 30) -> float:
        with self.db.read() as con:
            return float(
                con.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0.0) FROM weekly_insights WHERE generated_at >= ?",
                    (time.time() - days * _DAY,),
                ).fetchone()[0]
            )

    # ── per-user stats + transcripts ──────────────────────────────────────────

    def compute_user_stats(self, user_id: int, *, since_epoch: float) -> dict[str, int | float]:
        """``{dictation_count, file_count, total_words, total_inference_ms, time_saved_hours}``."""
        with self.db.read() as con:
            r = con.execute(
                _ACTIVITY_SQL + " WHERE user_id = ?", (since_epoch, since_epoch, user_id)
            ).fetchone()
        return _stats(r if r is not None and r["user_id"] is not None else None)

    def stats_by_user(self, since_epoch: float) -> dict[int, dict[str, int | float]]:
        """``compute_user_stats`` for every user with activity, in one grouped query."""
        with self.db.read() as con:
            rows = con.execute(_ACTIVITY_SQL + " GROUP BY user_id", (since_epoch, since_epoch))
            return {r["user_id"]: _stats(r) for r in rows}

    def transcripts_in_range(
        self, user_id: int, *, since_epoch: float, until_epoch: float
    ) -> list[dict[str, Any]]:
        """``{created_at, text, kind}`` (kind ``dictation`` | ``file``), newest first."""
        with self.db.read() as con:
            rows = con.execute(
                "SELECT created_at, text, kind FROM ("
                " SELECT created_at, text, 'dictation' AS kind FROM dictations"
                "  WHERE user_id = ? AND created_at >= ? AND created_at < ? AND deleted_at IS NULL"
                " UNION ALL"
                " SELECT created_at, transcript_text AS text, 'file' AS kind FROM jobs"
                "  WHERE user_id = ? AND transcript_text IS NOT NULL AND deleted_at IS NULL"
                "  AND created_at >= ? AND created_at < ?"
                ") ORDER BY created_at DESC",
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
        search: str | None = None,
        dict_cursor: tuple[float, str] | None = None,
        jobs_cursor: tuple[float, str] | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], tuple[float, str] | None, tuple[float, str] | None]:
        """Personal history with per-leg cursors: ``(rows, next_dict_cursor, next_jobs_cursor)``.

        Each leg's cursor is set only when that leg returned ``limit`` rows.
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
                if search:
                    sql += " AND text LIKE ?"
                    args.append(f"%{search}%")
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
                if search:
                    sql += " AND transcript_text LIKE ?"
                    args.append(f"%{search}%")
                if jobs_cursor is not None:
                    sql += " AND (created_at, id) < (?, ?)"
                    args.extend(jobs_cursor)
                sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
                job_rows = [dict(r) for r in con.execute(sql, (*args, limit))]

        merged = sorted(dict_rows + job_rows, key=lambda r: (-r["created_at"], r["row_id"]))[:limit]

        def _next(rows: list[dict[str, Any]]) -> tuple[float, str] | None:
            return (rows[-1]["created_at"], rows[-1]["row_id"]) if len(rows) == limit else None

        return merged, _next(dict_rows), _next(job_rows)

    # ── /me/history rows (kind: dictation | file) ─────────────────────────────

    def get_history_row(self, user_id: int, kind: str, row_id: str) -> dict[str, Any] | None:
        """Owner-checked, non-deleted single row (``SELECT *``), or None."""
        if kind == "dictation":
            sql = (
                "SELECT * FROM dictations WHERE user_id=? AND id=CAST(? AS INTEGER)"
                " AND deleted_at IS NULL"
            )
        elif kind == "file":
            sql = (
                "SELECT * FROM jobs WHERE user_id=? AND id=?"
                " AND transcript_text IS NOT NULL AND deleted_at IS NULL"
            )
        else:
            return None
        with self.db.read() as con:
            r = con.execute(sql, (user_id, row_id)).fetchone()
        return None if r is None else dict(r)

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
