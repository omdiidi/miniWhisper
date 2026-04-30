# Deploy: Team Distribution Operator Guide

Owner-side runbook for shipping WisprAlt releases and managing the team's
multi-tenant Postgres-backed access.

---

## First-time deploy of the multi-tenant changes

Once on the Mac mini that already runs WisprAlt server:

1. **Apply the v1 Postgres schema** — see [Apply the v1 Postgres
   schema](#one-time-apply-the-v1-postgres-schema) below.
2. **Set `SUPABASE_DATABASE_URL` in `server/.env`** (mode 0600, owner =
   current user). The format is
   `postgresql://postgres.<ref>:<password>@<host>:6543/postgres`.
   Use the **Transaction Pooler** connection string from Supabase Dashboard
   → Project Settings → Database → Connection pooling. asyncpg connects
   over IPv4 fine via the pooler; the direct port 5432 string requires
   IPv6 which the Mac mini may not have.
3. **Restart the launchd agent** so the new env var and the Postgres pool
   come up:
   ```bash
   bash scripts/server-launchd.sh restart
   ```
4. **Verify** — the lifespan auto-seeds the first admin row from
   `WISPRALT_API_KEY` if `wispralt.users` is empty. Confirm:
   ```bash
   KEY="$(security find-generic-password -s co.wispralt -w)"
   curl -s -H "Authorization: Bearer $KEY" \
     https://transcribe.<your-domain>/admin/users | head -50
   ```
   You should see a single row labelled `break-glass-admin (seeded from env)`.
   Rotate it via `/admin/users/<id>/mint` once a real label is in place
   (see [Adding a new employee](#adding-a-new-employee)).

If `SUPABASE_DATABASE_URL` is unset or unreachable at boot, the server
logs a WARNING and continues with `db_pool = None`. The break-glass
admin path (env-var bearer) still works, but the admin UI returns 503.
Fix the URL, restart, and the admin UI comes back.

## Lifespan auto-seed

`main.py:_seed_admin_if_empty` runs once per process startup, immediately
after the asyncpg pool is up:

```text
1. SELECT count(*) FROM wispralt.users
2. If > 0 → return (idempotent; subsequent boots are no-ops).
3. Else → INSERT a row with role='admin', token_hash =
   sha256(WISPRALT_API_KEY), label='break-glass-admin (seeded from env)'.
   The INSERT uses ON CONFLICT (token_hash) DO NOTHING so a restart
   after a manual revoke + same env-var token doesn't crash.
```

Effect: once the schema is in place, the server is reachable on first
boot using the same key Omid already has in his macOS Keychain. The
break-glass path and the seeded admin row resolve to the **same**
token_hash, so usage events for break-glass requests have a valid FK
target instead of being dropped.

---

## One-time: apply the v1 Postgres schema

The plan applies migrations via the Supabase MCP `apply_migration` tool. As
of 2026-04-27 the project token used by that MCP returns `Unauthorized`
when called from this Claude Code session, so the v1 schema must be
applied **manually** via the Supabase Studio SQL editor.

Steps:

1. Open <https://supabase.com/dashboard/project/lmaffmygjrfgkwrapfax/sql/new>.
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

---

## Shipping a release

`scripts/release-client.sh` is the one-shot release tool. It runs locally
on Omid's MacBook (where the Apple Development cert is keychained) and
ships the result to GitHub Releases.

```bash
bash scripts/release-client.sh 0.2.0
```

What it does, in order:

1. **Pre-flight guards.**
   - Refuse unless on `main` (override: `ALLOW_BRANCH=1`).
   - Refuse if working tree is dirty (it commits the version bump, so
     unrelated changes would get folded in).
   - Refuse if the tag `v0.2.0` already exists locally OR on GitHub.
2. **Bump `CFBundleShortVersionString`** in `client/WisprAlt/Info.plist`
   to `0.2.0` via `plutil -replace`.
3. **Build** via `scripts/build-client-local.sh` (Apple Development
   signing, see [SETUP-CLIENT.md](SETUP-CLIENT.md)).
4. **Package as DMG** under `/tmp/wispralt-release-0.2.0/WisprAlt-0.2.0.dmg`
   via `hdiutil create -format UDZO`.
5. **Compute SHA256 sidecar** — written with the BARE filename so
   `shasum -c WisprAlt-*.dmg.sha256` works in the employee's CWD.
6. **Tag, push, create GitHub Release** via `gh release create` with
   both the DMG and the sha256 sidecar attached. The release notes
   embed the SHA256 in a fenced code block.

After the script prints `Release v0.2.0 shipped.`, employees can pick
up the new build by running `/wispralt-update` in Claude Code.

If the build fails partway through (codesign xattr race on Tahoe,
notably), the working tree remains on the bumped Info.plist commit but
no tag is pushed. Re-run; the pre-flight tag check will refuse, so
either roll back the commit (`git reset --hard HEAD~1`) or bump VERSION.

---

## Adding a new employee

1. Open `/admin/users` on the Mac mini admin UI (see
   [ADMIN.md](ADMIN.md) for login details).
2. Click **Mint** on the employee's row. Copy the 64-hex-char plaintext
   token shown — it is rendered **once** by `token_minted.html.j2` and
   is never persisted in plaintext anywhere.
3. Text the token to the employee via Signal / iMessage. **Never email**
   — keys-in-email is a hard "no" per the brief.
4. Tell them to run the `install.sh` curl one-liner from a Terminal,
   substituting their token for `sk_xxx` (full guide in
   [INSTALL.md](INSTALL.md)):

   ```bash
   WISPRALT_API_KEY=sk_xxx WISPRALT_SERVER=https://transcribe.integrateapi.ai \
     curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh | bash
   ```

   The installer downloads the latest signed DMG from GitHub Releases,
   verifies the SHA256, copies the app to `/Applications`, strips
   quarantine, seeds the API key into the Keychain, and opens the
   System Settings panes for the four required permissions.

The employee's first dictation populates `wispralt.usage_events`; they
appear on `/admin/users` with a non-null `last_seen_at`.

## Revoking an employee

1. `/admin/users` → find the row → click **Revoke**.
2. The route sets `revoked_at = now()` AND invalidates the cache entry
   via `token_cache.invalidate(revoked_hash)`.
3. **Cache window:** the in-process `TokenCache` has a 60-second TTL.
   Within 60 seconds, all in-flight requests using a previously cached
   hit will expire and re-resolve through Postgres, where the partial
   index `users_idx_token_hash WHERE revoked_at IS NULL` excludes the
   revoked row. Result: 401 on next request.
4. **For instant lockout** (no 60s wait), restart the launchd agent —
   the cache is in-process only:
   ```bash
   bash scripts/server-launchd.sh restart
   ```
