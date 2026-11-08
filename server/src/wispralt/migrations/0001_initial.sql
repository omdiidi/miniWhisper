-- 0001_initial.sql — the whole Local-mode schema in one file (data_dir/wispralt.db).
-- Timestamps are REAL unix seconds; paths are relative to data_dir.
-- source/source_id + their partial unique indexes are reserved for the Part 7 importer.
-- Applied by wispralt.database.Database.migrate(), one statement at a time inside
-- BEGIN IMMEDIATE. Never edit this file once released: add 0002_*.sql instead.

CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- 0 = keep forever
INSERT INTO settings VALUES ('retention_days', '0');

CREATE TABLE users(
  id INTEGER PRIMARY KEY, label TEXT NOT NULL, display_name TEXT, notes TEXT,
  role TEXT NOT NULL DEFAULT 'employee' CHECK (role IN ('admin','employee')),
  kind TEXT NOT NULL DEFAULT 'employee' CHECK (kind IN ('employee','integration')),
  created_at REAL NOT NULL, disabled_at REAL, source TEXT, source_id TEXT);

CREATE UNIQUE INDEX users_source ON users(source, source_id) WHERE source IS NOT NULL;

CREATE TABLE api_keys(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  token_hash TEXT NOT NULL UNIQUE, label TEXT, created_at REAL NOT NULL, revoked_at REAL);

CREATE INDEX api_keys_user ON api_keys(user_id) WHERE revoked_at IS NULL;

CREATE TABLE invites(
  id INTEGER PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
  user_id INTEGER REFERENCES users(id),  -- NULL: redeem creates a new user
  label TEXT, role TEXT NOT NULL DEFAULT 'employee' CHECK (role IN ('admin','employee')),
  created_by INTEGER NOT NULL REFERENCES users(id), created_at REAL NOT NULL,
  expires_at REAL NOT NULL, redeemed_at REAL, api_key_id INTEGER REFERENCES api_keys(id),
  CHECK (user_id IS NOT NULL OR label IS NOT NULL));

CREATE TABLE usage_events(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), ts REAL NOT NULL,
  kind TEXT NOT NULL, status INTEGER NOT NULL, chars INTEGER, duration_ms REAL, bytes_in INTEGER,
  bytes_out INTEGER, error_class TEXT, request_id TEXT, source TEXT, source_id TEXT);

CREATE INDEX usage_user_ts ON usage_events(user_id, ts);

CREATE INDEX usage_ts ON usage_events(ts);

CREATE UNIQUE INDEX usage_source ON usage_events(source, source_id) WHERE source IS NOT NULL;

CREATE TABLE dictations(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), created_at REAL NOT NULL,
  duration_ms INTEGER, text TEXT NOT NULL, word_count INTEGER, client_app_version TEXT,
  client_dedup_id TEXT UNIQUE, deleted_at REAL, source TEXT, source_id TEXT);

CREATE INDEX dictations_user_created ON dictations(user_id, created_at) WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX dictations_source ON dictations(source, source_id) WHERE source IS NOT NULL;

CREATE TABLE jobs(
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL CHECK (status IN ('pending','running','done','failed')), mode TEXT,
  created_at REAL NOT NULL, started_at REAL, finished_at REAL, error TEXT, wav_path TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, phase TEXT, phase_started_at REAL,
  chunk_index INTEGER NOT NULL DEFAULT 0, total_chunks INTEGER NOT NULL DEFAULT 0,
  cancel_requested INTEGER NOT NULL DEFAULT 0, audio_duration_s REAL, transcript_text TEXT,
  word_count INTEGER, client_app_version TEXT, outputs_removed_at REAL, deleted_at REAL,
  source TEXT, source_id TEXT, estimated INTEGER NOT NULL DEFAULT 0);

CREATE INDEX jobs_user_active ON jobs(user_id, created_at)
  WHERE deleted_at IS NULL AND transcript_text IS NOT NULL;

CREATE INDEX jobs_status ON jobs(status);

CREATE UNIQUE INDEX jobs_source ON jobs(source, source_id) WHERE source IS NOT NULL;

CREATE TABLE weekly_insights(
  iso_year INTEGER NOT NULL, iso_week INTEGER NOT NULL, scope_key TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id), insight_json TEXT NOT NULL, input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL, model TEXT NOT NULL, generated_at REAL NOT NULL,
  PRIMARY KEY (iso_year, iso_week, scope_key),
  CHECK (scope_key = CASE WHEN user_id IS NULL THEN 'team' ELSE 'user:' || user_id END));

CREATE INDEX weekly_insights_generated ON weekly_insights(generated_at);
