# Row-Level Security: Operations and Recovery

**Default state:** NNM ships with Postgres Row-Level Security active
on `memories`, `facts`, `projects`, and `auth_tokens`. Enforcement is
wired up end-to-end: the installer provisions a non-superuser
`memory_app` role, points the app pool at it, and every query in
`lib/db.py`, `lib/auth_db.py`, and `lib/audit.py` runs inside an
`rls.app_conn` or `rls.admin_conn` block that sets
`app.current_user` before executing.

This doc covers:
- How to verify RLS is actually enforcing.
- How to recover when the role is missing or privileges drifted.
- How to disable RLS if you need a single-role setup for some reason.

## Architecture in one paragraph

Two DB roles live inside Postgres. `MEMORY_DB_USER` (default `memory`,
typically a superuser) runs migrations and other DDL. `MEMORY_APP_DB_USER`
(default `memory_app`, deliberately `NOSUPERUSER NOBYPASSRLS`) runs
every query from the running server. Postgres bypasses RLS for
superusers, so the app role being a non-superuser is the load-bearing
part. Every per-user query wraps its body in
`async with rls.app_conn(pool, user_id):`, which `SET app.current_user`
on acquire and `RESET` on release; admin cross-user queries use
`rls.admin_conn(pool)` which sets the sentinel `'admin'` that the
policies treat as a bypass. Defined in migration 008 (base policies)
and 013 (admin sentinel); activated in migration 012 (ENABLE + FORCE).

## Verify enforcement

### 1. Role attributes

```sql
SELECT rolname, rolsuper, rolbypassrls
FROM pg_roles
WHERE rolname IN ('memory_app', 'memory');
```

Expected:

| rolname     | rolsuper | rolbypassrls |
|-------------|----------|--------------|
| memory      | t        | f            |
| memory_app  | f        | f            |

If `memory_app` is missing entirely, see **Recovery → missing role**
below.

### 2. RLS state on the tables

```sql
SELECT relname, relrowsecurity AS enabled, relforcerowsecurity AS forced
FROM pg_class
WHERE relname IN ('memories', 'facts', 'projects', 'auth_tokens')
ORDER BY relname;
```

All four should show `enabled = t, forced = t`. If not, migration 012
never applied — re-running the server with a working DB connection
will apply it on next `get_pool()` call.

### 3. Policies are installed

```sql
SELECT tablename, policyname
FROM pg_policies
WHERE tablename IN ('memories', 'facts', 'projects', 'auth_tokens')
ORDER BY tablename;
```

Expected four rows, one per table, named `<table>_owner_rls`.

### 4. End-to-end (from the app's point of view)

```bash
python tests/test_rls_enforcement.py
```

The test creates a temporary non-superuser role, downgrades to it via
`SET ROLE`, and validates that unset GUC returns zero rows, per-user
GUC returns only that user's rows, the admin sentinel sees all, and
cross-user INSERTs are blocked by `WITH CHECK`. Requires the
caller's `MEMORY_DB_USER` to have `CREATE ROLE` privilege (the test
creates a throwaway role and drops it).

## Recovery

### Missing role

Symptom: server logs show `password authentication failed for user
"memory_app"` on startup, or a Postgres log shows `role "memory_app"
does not exist`.

Cause: either the Docker init never ran (pre-existing volume) or the
role was manually dropped.

Fix: re-run the installer OR run the ensure script directly.

```bash
python docker/init/ensure_app_role.py
```

The script is idempotent. It CREATE-ROLEs if missing, refreshes the
password if the role exists but the password in `.env` changed, and
re-applies all the necessary grants.

Inside a Docker server-mode install, equivalent is:

```bash
docker compose -f docker/docker-compose.yml --profile server \
  run --rm mcp python docker/init/ensure_app_role.py
```

### Password drift

Symptom: authentication fails for the app role. `.env` says one thing
but Postgres has a different password on the role.

Fix: same as above. `ensure_app_role.py` calls `ALTER ROLE ... WITH
PASSWORD ...` when the role exists, so it self-heals.

### Privilege drift

Symptom: server logs show `permission denied for relation <table>`.

Cause: a previous admin manually revoked grants from `memory_app`.

Fix: same as above. The script re-applies `GRANT SELECT, INSERT,
UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO memory_app` (plus
sequences and default privileges) every time it runs.

### Policies dropped

Symptom: verification query #3 returns fewer than four policies, or
RLS isn't filtering rows.

Fix: re-apply the two policy migrations manually:

```bash
# As MEMORY_DB_USER:
psql -U memory -d notnative_memory -f config/migrations/008_rls_foundations.sql
psql -U memory -d notnative_memory -f config/migrations/013_rls_admin_sentinel.sql
```

These use `DROP POLICY IF EXISTS` + `CREATE POLICY`, so re-running is
safe.

## Disabling RLS

Two knobs, pick whichever matches your intent:

### Option A: runtime fallback to single-role (keeps RLS defined, just not enforced)

Blank the app-role env vars and restart:

```
MEMORY_APP_DB_USER=
MEMORY_APP_DB_PASSWORD=
```

`lib/db.py::get_pool` sees the blanks and falls back to
`MEMORY_DB_USER` for the app pool. That role is a superuser, so RLS
policies are bypassed. Policies remain defined; ENABLE + FORCE stay
on the tables. Flipping the vars back flips enforcement back.

### Option B: disable RLS at the DB layer (policies stay, enforcement off globally)

Run the rollback for migration 012:

```bash
psql -U memory -d notnative_memory -f config/migrations/rollback/012_rls_force.sql
```

This runs `NO FORCE ROW LEVEL SECURITY` + `DISABLE ROW LEVEL SECURITY`
on all four tables. Policies remain defined; re-enabling is a single
psql invocation of `012_rls_force.sql`.

### Option C: nuke the policies entirely

Rollback both policy migrations:

```bash
psql -U memory -d notnative_memory -f config/migrations/rollback/013_rls_admin_sentinel.sql
psql -U memory -d notnative_memory -f config/migrations/rollback/008_rls_foundations.sql
```

Not recommended — you lose the defense-in-depth and the schema
diverges from what fresh installs get.

## Internals reference

- Migration `008_rls_foundations.sql` — defines the base policies.
- Migration `012_rls_force.sql` — ENABLE + FORCE on the four tables.
- Migration `013_rls_admin_sentinel.sql` — policies with admin
  sentinel bypass.
- `docker/init/02-roles.sh` — creates `memory_app` at Postgres
  container init time (Docker full mode, first DB volume only).
- `docker/init/ensure_app_role.py` — idempotent role provisioner
  the installer runs in all modes. Safe to run by hand.
- `lib/rls.py::app_conn` — per-user RLS context for the app pool.
- `lib/rls.py::admin_conn` — admin-sentinel context for cross-user
  admin queries and pre-auth token resolution.
- `tests/test_rls_enforcement.py` — end-to-end enforcement test.
