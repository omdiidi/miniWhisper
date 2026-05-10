-- =============================================================================
-- v3 — Fallback events + dedicated worker role for the Cloudflare Worker
--      that proxies dictation to OpenRouter when the Mac mini is offline.
-- =============================================================================
-- Apply via Supabase Studio SQL editor against project lmaffmygjrfgkwrapfax.
-- Single-operator: do not run concurrently with any other migration.
--
-- Pre-flight (run first, separately, to confirm next free version):
--     SELECT max(version) FROM wispralt.schema_version;     -- expects 2
-- =============================================================================
BEGIN;

-- 1. Telemetry table — mirrors usage_events shape with provider columns.
CREATE TABLE IF NOT EXISTS wispralt.fallback_events (
    id                  BIGSERIAL    PRIMARY KEY,
    user_id             INTEGER      NOT NULL REFERENCES wispralt.users(id) ON DELETE RESTRICT,
    ts                  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    kind                TEXT         NOT NULL,          -- 'dictate' (more later if scope grows)
    status              INTEGER      NOT NULL,          -- HTTP status code emitted by the Worker
    bytes_in            INTEGER,
    duration_ms         REAL,
    error_class         TEXT,
    request_id          TEXT,
    provider            TEXT         NOT NULL,          -- 'openrouter'
    provider_request_id TEXT,                           -- X-Generation-Id from OpenRouter
    cost_micro_usd      BIGINT                           -- $0.04/audio-hour ≈ 11.11 µ$/sec
);
CREATE INDEX IF NOT EXISTS fallback_events_idx_user_ts ON wispralt.fallback_events (user_id, ts DESC);
CREATE INDEX IF NOT EXISTS fallback_events_idx_ts      ON wispralt.fallback_events (ts DESC);

-- 2. SECURITY DEFINER RPCs.
--    Owner is whoever runs this migration (typically `postgres` on Supabase).
--    `search_path` is restricted to wispralt only — public is intentionally
--    excluded to neutralize the classic search-path-injection vector.

CREATE OR REPLACE FUNCTION wispralt.lookup_user_by_token_hash(p_hash text)
RETURNS TABLE(id integer, role text)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = wispralt
AS $$
    SELECT u.id, u.role
      FROM wispralt.users u
     WHERE u.token_hash = p_hash
       AND u.revoked_at IS NULL
     LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION wispralt.fallback_micro_usd_this_month()
RETURNS bigint
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = wispralt
AS $$
    SELECT coalesce(sum(cost_micro_usd), 0)::bigint
      FROM wispralt.fallback_events
     WHERE status = 200
       AND ts >= date_trunc('month', now() AT TIME ZONE 'UTC');
$$;

-- 3. Dedicated worker role with the minimum surface needed.
--    NOLOGIN/NOINHERIT — switched into via JWT role claim through PostgREST.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wispralt_fallback_worker') THEN
        CREATE ROLE wispralt_fallback_worker NOLOGIN NOINHERIT;
    END IF;
END
$$;

-- Tighten in case prior runs granted broader privileges.
REVOKE ALL ON TABLE  wispralt.users                          FROM wispralt_fallback_worker;
REVOKE ALL ON TABLE  wispralt.fallback_events                FROM wispralt_fallback_worker;
REVOKE ALL ON FUNCTION wispralt.lookup_user_by_token_hash(text)        FROM wispralt_fallback_worker;
REVOKE ALL ON FUNCTION wispralt.fallback_micro_usd_this_month()        FROM wispralt_fallback_worker;

GRANT USAGE ON SCHEMA wispralt TO wispralt_fallback_worker;

GRANT INSERT (
    user_id, kind, status, bytes_in, duration_ms,
    error_class, request_id,
    provider, provider_request_id, cost_micro_usd
) ON wispralt.fallback_events TO wispralt_fallback_worker;

-- BIGSERIAL primary keys back to a sequence; INSERT must be able to nextval().
GRANT USAGE, SELECT ON SEQUENCE wispralt.fallback_events_id_seq TO wispralt_fallback_worker;

GRANT EXECUTE ON FUNCTION wispralt.lookup_user_by_token_hash(text) TO wispralt_fallback_worker;
GRANT EXECUTE ON FUNCTION wispralt.fallback_micro_usd_this_month() TO wispralt_fallback_worker;

-- 4. PostgREST role-switching contract: the gateway logs in as `authenticator`
--    and `SET ROLE` to whatever the JWT's `role` claim names. This requires
--    `authenticator` to have been granted the target role.
GRANT wispralt_fallback_worker TO authenticator;

-- 5. Schema version row.
INSERT INTO wispralt.schema_version (version, notes)
VALUES (3, 'Fallback events table + worker role + RPCs')
ON CONFLICT (version) DO NOTHING;

COMMIT;
