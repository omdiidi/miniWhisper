# Deploy: Team Distribution Operator Guide

Owner-side runbook for shipping WisprAlt releases and managing the team's
multi-tenant Postgres-backed access.

## One-time: apply the v1 Postgres schema

The plan applies migrations via the Supabase MCP `apply_migration` tool. As
of 2026-04-27 the project token used by that MCP returns `Unauthorized`
when called from this Claude Code session, so the v1 schema must be
applied **manually** via the Supabase Studio SQL editor.

Steps:

1. Open <https://supabase.com/dashboard/project/qglwmwmdoxopnubghnul/sql/new>.
2. Paste the entire contents of
   `server/migrations/2026-04-27-v1-wispralt-schema.sql`.
3. Click **Run**. Expect three `CREATE` statements + one `INSERT` to
   succeed with no errors. The `wispralt` schema, `wispralt.users`,
   `wispralt.usage_events`, and `wispralt.schema_version` tables will
   exist after this.
4. Verify with:
   ```sql
   SELECT version FROM wispralt.schema_version;
   -- expected: 1
   ```

Once the Supabase MCP token's scope is fixed, future migrations can be
applied via `apply_migration` directly; this manual step is only
required for the initial v1 cut.
