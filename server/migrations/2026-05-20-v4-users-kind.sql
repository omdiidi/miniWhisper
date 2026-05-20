-- Migration v4: add `kind` column to wispralt.users
-- Apply via Supabase MCP `apply_migration` (name: v4_users_kind) or paste into Supabase Studio.
--
-- Distinguishes employee tokens (humans using the macOS client) from integration
-- tokens (third-party programs talking to /v1/audio/transcriptions). Role enum
-- stays {admin, employee}; kind is orthogonal and used for:
--   1. Admin UI grouping: /admin/users (employee list) vs /admin/keys (integration list)
--   2. Route-level guards: kind='integration' tokens are blocked from /me/* + /telemetry/*
--      (those surfaces are for humans, not programs).
--
-- Backfill: all existing rows default to 'employee' via the column DEFAULT.
-- Idempotent: ADD COLUMN IF NOT EXISTS + constraint guarded by DO $$ + ON CONFLICT.

ALTER TABLE wispralt.users
  ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'employee';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'users_kind_check'
  ) THEN
    ALTER TABLE wispralt.users
      ADD CONSTRAINT users_kind_check CHECK (kind IN ('employee', 'integration'));
  END IF;
END $$;

INSERT INTO wispralt.schema_version (version, notes)
VALUES (4, 'add kind column to wispralt.users (employee | integration)')
ON CONFLICT (version) DO NOTHING;
