-- 0004_insight_run_heartbeat.sql — the insights run lock is a heartbeat, not a start time.
-- A 'running' insight_runs row holds the cross-process run lock while its heartbeat_at is
-- fresh; the run touches it on every progress step. Old rows get their start time.

ALTER TABLE insight_runs ADD COLUMN heartbeat_at REAL;

UPDATE insight_runs SET heartbeat_at = started_at;
