-- Rollback for 008_rls_foundations.sql
--
-- Drops the policies and disables RLS on user-scoped tables (in case
-- a follow-up operation had enabled them). Safe whether ENABLE ever
-- ran or not.
--
-- Apply manually. The forward migration runner does not descend into
-- this directory.

BEGIN;

ALTER TABLE IF EXISTS memories    DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS facts       DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS projects    DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS auth_tokens DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS memories_owner_rls    ON memories;
DROP POLICY IF EXISTS facts_owner_rls       ON facts;
DROP POLICY IF EXISTS projects_owner_rls    ON projects;
DROP POLICY IF EXISTS auth_tokens_owner_rls ON auth_tokens;

DELETE FROM schema_migrations WHERE filename = '008_rls_foundations.sql';

COMMIT;
