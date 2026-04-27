-- =============================================================================
-- STATUS: APPLIED to project minicrew (lmaffmygjrfgkwrapfax) on 2026-04-27.
--   Verified via: SELECT * FROM wispralt.schema_version → version 1.
--   Re-applying this file would fail (tables already exist). Future
--   migrations should bump schema_version + use IF NOT EXISTS guards.
-- =============================================================================
-- =============================================================================
-- wispralt v1 schema
-- =============================================================================
-- Migration name: v1_wispralt_schema
-- Apply via Supabase MCP `apply_migration` once the project token has the right
-- scope.  Until then, the operator should paste this file's contents into
-- Supabase Studio's SQL editor and run it against project
-- `lmaffmygjrfgkwrapfax`.
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS wispralt;

-- Migration tracking. Future migrations check this table for the highest
-- version applied and run anything newer.
CREATE TABLE wispralt.schema_version (
    version       INTEGER PRIMARY KEY,
    applied_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes         TEXT
);
INSERT INTO wispralt.schema_version (version, notes)
VALUES (1, 'Initial: users + usage_events');

-- Users / bearer tokens.
-- token_hash is sha256 hex of the plaintext bearer.
-- role is text (not enum) to allow future roles without ALTER TYPE.
CREATE TABLE wispralt.users (
    id              SERIAL       PRIMARY KEY,
    label           TEXT         NOT NULL,
    token_hash      TEXT         NOT NULL UNIQUE,
    role            TEXT         NOT NULL CHECK (role IN ('admin', 'employee')),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    notes           TEXT
);
CREATE INDEX users_idx_token_hash ON wispralt.users (token_hash)
    WHERE revoked_at IS NULL;

-- Per-request event log.
-- kind is text (not enum) — future event types add without migrations.
-- chars/duration_ms/bytes are NULL when not applicable to that kind.
-- ON DELETE RESTRICT preserves audit history.  A user can be revoked
-- (revoked_at IS NOT NULL) without losing their usage rows.  If a row
-- ever needs hard-deletion, the operator must first reassign or
-- explicitly delete the audit rows — refused at the DB layer prevents
-- accidental "billing-relevant" data loss via Supabase Studio.
CREATE TABLE wispralt.usage_events (
    id              BIGSERIAL    PRIMARY KEY,
    user_id         INTEGER      NOT NULL REFERENCES wispralt.users(id) ON DELETE RESTRICT,
    ts              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    kind            TEXT         NOT NULL,
    status          INTEGER      NOT NULL,
    chars           INTEGER,
    duration_ms     REAL,
    bytes_in        INTEGER,
    bytes_out       INTEGER,
    error_class     TEXT,
    request_id      TEXT
);
CREATE INDEX usage_idx_user_ts  ON wispralt.usage_events (user_id, ts DESC);
CREATE INDEX usage_idx_kind_ts  ON wispralt.usage_events (kind, ts DESC);
CREATE INDEX usage_idx_ts       ON wispralt.usage_events (ts DESC);
