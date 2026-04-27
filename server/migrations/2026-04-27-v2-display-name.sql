-- Migration v2: add display_name to wispralt.users
-- Apply via Supabase Studio (paste this file's contents) or `mcp__supabase__apply_migration`.
-- Constant MAX_DISPLAY_NAME_LEN=40 mirrored in server/src/wispralt_server/constants.py.

ALTER TABLE wispralt.users
  ADD COLUMN IF NOT EXISTS display_name TEXT NULL
    CHECK (
      display_name IS NULL
      OR (
        length(trim(display_name)) BETWEEN 1 AND 40
        AND display_name !~ '[[:cntrl:]]'  -- reject control chars (\n, \t, \r, NUL, etc.)
      )
    );

INSERT INTO wispralt.schema_version (version, notes)
VALUES (2, 'add display_name column to wispralt.users')
ON CONFLICT (version) DO NOTHING;
