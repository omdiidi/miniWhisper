-- 0003_import_runs.sql — Part 7 history import: one row per (source, run).
-- (0002 is reserved for Part 6; the runner applies files in sorted order and
-- does not require contiguous numbers.)
-- checkpoint = the last source_id committed; counts_json = running/final counts.

CREATE TABLE import_runs(
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, origin TEXT NOT NULL,
  started_at REAL NOT NULL, finished_at REAL,
  status TEXT NOT NULL CHECK (status IN ('running','done','failed','blocked')),
  dry_run INTEGER NOT NULL, checkpoint TEXT, counts_json TEXT);

CREATE INDEX import_runs_source ON import_runs(source, status);
