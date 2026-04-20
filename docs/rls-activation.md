# RLS Activation Runbook

**Status:** Optional hardening. The Phase 7 per-user `owner_user_id`
filters in `lib/db.py` are the primary cross-user isolation
mechanism; Row-Level Security (RLS) layers a DB-level safety net on
top of them, so a future forgotten `WHERE owner_user_id = $N` returns
zero rows instead of leaking.

Activation is an **operational choice** that requires a small infra
change (creating a non-superuser DB role), because Postgres RLS is
always bypassed by superusers and by the `BYPASSRLS` role attribute.
The stock pgvector Docker image creates the default user as a
superuser, so shipping RLS as "on by default" would be a no-op.

## When to do this

- You're opening NNM to more than one user and want defense against
  your own future bugs.
- You're deploying to a shared database and want a second line of
  containment.
- You're auditing a production deployment and want to be able to
  tell a reviewer "RLS is enforced at the DB layer."

## When NOT to do this

- Single-user deployment where per-user isolation is moot.
- You haven't deployed all of the Phase 5C code changes yet (this
  doc depends on `lib/rls.py::app_conn` plus the dual-role config
  in `lib/db.py`).

## Prerequisites

1. Superuser access to the Postgres instance NNM uses (the default
   `memory` user in the Docker setup qualifies).
2. NNM code at or past the commit that lands the dual-role config
   (`MEMORY_APP_DB_USER` / `MEMORY_APP_DB_PASSWORD` env vars).
3. A maintenance window. Activation changes auth for every query;
   short downtime is expected on the switch.

## Steps

### 1. Create the non-superuser role

Connect to Postgres as the superuser (`memory`, or `postgres` if you
split those):

```sql
-- Replace with a strong password. Store it wherever you keep
-- MEMORY_DB_PASSWORD today.
CREATE ROLE memory_app LOGIN PASSWORD 'a-strong-random-password';

-- Scope privileges to the database NNM uses.
GRANT CONNECT ON DATABASE notnative_memory TO memory_app;

-- Give access to the schema and its existing objects.
GRANT USAGE ON SCHEMA public TO memory_app;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA public
    TO memory_app;
GRANT USAGE, SELECT
    ON ALL SEQUENCES IN SCHEMA public
    TO memory_app;

-- Future tables and sequences (e.g., from a new migration) should
-- also be accessible to memory_app without revisiting this step.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO memory_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO memory_app;
```

Verify the role was created and is NOT a superuser / BYPASSRLS:

```sql
SELECT rolname, rolsuper, rolbypassrls
FROM pg_roles
WHERE rolname = 'memory_app';
-- Expected: rolsuper = false, rolbypassrls = false
```

If either flag is `t`, RLS will still be bypassed. Fix with:

```sql
ALTER ROLE memory_app NOSUPERUSER NOBYPASSRLS;
```

### 2. Point the application at the new role

Edit `.env`:

```
# Keep these for the MIGRATION connection (needs superuser to run
# CREATE POLICY / ENABLE ROW LEVEL SECURITY in migration 012).
MEMORY_DB_USER=memory
MEMORY_DB_PASSWORD=<original>

# NEW: app connection uses the non-superuser role.
MEMORY_APP_DB_USER=memory_app
MEMORY_APP_DB_PASSWORD=<from step 1>
```

Restart the server. It should start cleanly; user-scoped routes
continue to work because `lib/db.py` call sites use `app_conn` which
`SET app.current_user` before each query.

### 3. Run migration 012 to FORCE RLS

Migration 012 ships with the ALTER TABLE ENABLE/FORCE block that
activates the policies defined in migration 008. The migration runs
under the superuser connection (`MEMORY_DB_*`), not the app role,
so it has permission to alter the tables.

Options:

- **Automatic (recommended)**: Normal server startup applies pending
  migrations. Just restart — 012 runs on the next `get_pool()`
  call.
- **Manual**: `psql` as the superuser and
  `\i config/migrations/012_rls_force.sql`.

### 4. Verify enforcement

From psql as `memory_app`:

```sql
-- No app.current_user set yet → RLS denies everything.
SELECT COUNT(*) FROM memories;
-- Expected: 0 (even if the DB has thousands of memories)

-- Set a user context.
SELECT set_config('app.current_user', '<some-user-uuid>', false);
SELECT COUNT(*) FROM memories;
-- Expected: only that user's memory count
```

Swap in a different user's UUID; you should see only their memories.
Cross-user access is now impossible at the DB layer regardless of
what SQL the app runs.

### 5. Confirm via the application

- Register two users via `/register`.
- Log in as each, store a memory as each.
- Confirm neither can see the other's memory via `/memories` or
  `/memories?q=...`.
- Check audit_events for any unexpected errors around the switch.

## Rolling back

If something breaks, roll back in the opposite order:

1. `\i config/migrations/rollback/012_rls_force.sql` (DISABLE RLS,
   keep policies defined).
2. Optionally revert the `.env` switch back to a superuser connection
   for the app (single-role setup).
3. If you want to also drop the policies:
   `\i config/migrations/rollback/008_rls_foundations.sql`.

## Notes

- The `memory_app` role has no access to the `audit_events` table?
  Yes it does — `GRANT ... ON ALL TABLES` covers it. The Phase 3.2
  audit writer runs from app routes, and needs INSERT.
- Migration runner: if you configure `MEMORY_APP_DB_USER`, app
  queries use it; migrations continue to use `MEMORY_DB_USER`.
  `lib/db.py::_run_migrations` is invoked on pool init; only the
  superuser pool applies migrations.
- The bootstrap-admin startup check in `server.py` runs under the
  superuser connection (same as migrations), not the app role, so
  it can count admins regardless of the RLS state.
