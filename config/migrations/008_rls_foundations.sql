-- Migration 008: Row-Level Security foundations
--
-- Defines RLS policies for the user-scoped tables but does NOT
-- enable RLS on them. Enabling is a deliberate operational decision
-- that requires:
--
--   1. A non-superuser DB role for the application to connect as.
--      Postgres superusers (and BYPASSRLS roles) always bypass RLS,
--      so if the app connects as `memory` on the stock Docker pgvector
--      image, RLS policies are a no-op until a separate role is used.
--
--   2. The server layer to call `SET app.current_user = <uid>` before
--      touching user-scoped tables. See lib/rls.py::app_conn for the
--      async context manager that handles the session-variable lifecycle
--      around pool-acquire / pool-release.
--
--   3. A deliberate `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` step
--      (and optionally FORCE ROW LEVEL SECURITY to subject the table
--      owner role as well) once those pieces are in place.
--
-- This migration only defines the policies so the scaffold is ready.
-- Until ENABLE runs, policies are inert and the per-user owner_user_id
-- filters in lib/db.py remain the only enforcement (which is where
-- they have always been).
--
-- Rollback: config/migrations/rollback/008_rls_foundations.sql drops
-- the policies and turns off RLS if it was enabled.

-- Policies use `current_setting('app.current_user', true)::uuid`.
-- The `true` second argument returns NULL when the GUC is unset,
-- instead of raising. If the GUC is unset, the comparison yields
-- NULL, the policy excludes the row, and the caller sees zero
-- results — fail-closed by design.

DROP POLICY IF EXISTS memories_owner_rls ON memories;
CREATE POLICY memories_owner_rls ON memories
    USING (owner_user_id = current_setting('app.current_user', true)::uuid)
    WITH CHECK (owner_user_id = current_setting('app.current_user', true)::uuid);

DROP POLICY IF EXISTS facts_owner_rls ON facts;
CREATE POLICY facts_owner_rls ON facts
    USING (owner_user_id = current_setting('app.current_user', true)::uuid)
    WITH CHECK (owner_user_id = current_setting('app.current_user', true)::uuid);

DROP POLICY IF EXISTS projects_owner_rls ON projects;
CREATE POLICY projects_owner_rls ON projects
    USING (owner_user_id = current_setting('app.current_user', true)::uuid)
    WITH CHECK (owner_user_id = current_setting('app.current_user', true)::uuid);

-- auth_tokens uses user_id, not owner_user_id (it's the user's own token).
DROP POLICY IF EXISTS auth_tokens_owner_rls ON auth_tokens;
CREATE POLICY auth_tokens_owner_rls ON auth_tokens
    USING (user_id = current_setting('app.current_user', true)::uuid)
    WITH CHECK (user_id = current_setting('app.current_user', true)::uuid);

-- Enabling is deliberately OMITTED here. Uncomment the following
-- block in a future migration (or via a runbook) when the non-
-- superuser role and db.py call-site migration are in place:
--
--   ALTER TABLE memories    ENABLE ROW LEVEL SECURITY;
--   ALTER TABLE memories    FORCE ROW LEVEL SECURITY;
--   ALTER TABLE facts       ENABLE ROW LEVEL SECURITY;
--   ALTER TABLE facts       FORCE ROW LEVEL SECURITY;
--   ALTER TABLE projects    ENABLE ROW LEVEL SECURITY;
--   ALTER TABLE projects    FORCE ROW LEVEL SECURITY;
--   ALTER TABLE auth_tokens ENABLE ROW LEVEL SECURITY;
--   ALTER TABLE auth_tokens FORCE ROW LEVEL SECURITY;
