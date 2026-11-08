-- 0002_portal.sql — Part 6 portal: full-text history search, insight runs, per-key usage,
-- settings seeds. Applied by Database.migrate() statement by statement inside BEGIN IMMEDIATE
-- (trigger bodies split correctly: sqlite3.complete_statement waits for their END;).
-- Never edit once released: add 0004_*.sql instead.

-- Full-text search. dictations_fts is external-content (rowid = dictations.id, a stable
-- INTEGER PRIMARY KEY). jobs.id is TEXT, and implicit rowids can change under VACUUM /
-- VACUUM INTO (backup), so jobs_fts is a regular table keyed by an UNINDEXED job_id.
-- Soft delete keeps the index entry: queries join back to the source row and filter
-- deleted_at IS NULL, so restore needs no reindex.
CREATE VIRTUAL TABLE dictations_fts USING fts5(
  text, content='dictations', content_rowid='id', tokenize='unicode61 remove_diacritics 2');

CREATE VIRTUAL TABLE jobs_fts USING fts5(
  text, job_id UNINDEXED, tokenize='unicode61 remove_diacritics 2');

CREATE TRIGGER dictations_fts_ai AFTER INSERT ON dictations BEGIN
  INSERT INTO dictations_fts(rowid, text) VALUES (new.id, new.text);
END;

-- An external-content delete must pass the OLD text, or the index is corrupted.
CREATE TRIGGER dictations_fts_ad AFTER DELETE ON dictations BEGIN
  INSERT INTO dictations_fts(dictations_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER dictations_fts_au AFTER UPDATE OF text ON dictations
  WHEN old.text IS NOT new.text BEGIN
  INSERT INTO dictations_fts(dictations_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO dictations_fts(rowid, text) VALUES (new.id, new.text);
END;

-- jobs get transcript_text late (NULL -> text), so the insert trigger only fires with text.
CREATE TRIGGER jobs_fts_ai AFTER INSERT ON jobs WHEN new.transcript_text IS NOT NULL BEGIN
  INSERT INTO jobs_fts(text, job_id) VALUES (new.transcript_text, new.id);
END;

-- Deleting by the UNINDEXED job_id scans jobs_fts: fine at file-job volume.
CREATE TRIGGER jobs_fts_ad AFTER DELETE ON jobs BEGIN
  DELETE FROM jobs_fts WHERE job_id = old.id;
END;

CREATE TRIGGER jobs_fts_au AFTER UPDATE OF transcript_text ON jobs
  WHEN old.transcript_text IS NOT new.transcript_text BEGIN
  DELETE FROM jobs_fts WHERE job_id = old.id;
  INSERT INTO jobs_fts(text, job_id)
    SELECT new.transcript_text, new.id WHERE new.transcript_text IS NOT NULL;
END;

-- Backfill rows written before this migration.
INSERT INTO dictations_fts(dictations_fts) VALUES ('rebuild');

INSERT INTO jobs_fts(text, job_id) SELECT transcript_text, id FROM jobs WHERE transcript_text IS NOT NULL;

-- One row per weekly-insights run (scheduled, manual rerun, backfill). failures_json holds
-- [{user_id, error_class}] only: never response or transcript text.
CREATE TABLE insight_runs(
  id INTEGER PRIMARY KEY,
  trigger TEXT NOT NULL CHECK (trigger IN ('scheduled','manual','backfill')),
  iso_year INTEGER NOT NULL, iso_week INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running','done','failed','aborted','skipped')),
  people_total INTEGER NOT NULL DEFAULT 0, people_done INTEGER NOT NULL DEFAULT 0,
  failures_json TEXT NOT NULL DEFAULT '[]',
  estimated_usd REAL, cost_usd REAL NOT NULL DEFAULT 0, error_class TEXT,
  started_at REAL NOT NULL, finished_at REAL);

CREATE INDEX insight_runs_week ON insight_runs(iso_year, iso_week, status);

-- The key that authenticated each request, so the portal can show per-device last seen.
-- NULL for rows written before 0002 and for imported rows.
ALTER TABLE usage_events ADD COLUMN key_id INTEGER REFERENCES api_keys(id);

CREATE INDEX usage_key_ts ON usage_events(key_id, ts) WHERE key_id IS NOT NULL;

-- AI insights are off until an admin enables them after reading the disclosure.
INSERT OR IGNORE INTO settings(key, value) VALUES
  ('insights_enabled', '0'), ('insights_cap_30d_usd', '8'), ('server_starts', '0');
