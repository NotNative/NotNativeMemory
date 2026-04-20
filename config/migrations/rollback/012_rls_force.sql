-- Rollback for 012_rls_force.sql
--
-- Turns off RLS enforcement on the user-scoped tables. The policies
-- defined in migrations 008 + 013 remain; a future re-enable just
-- needs to ALTER TABLE ENABLE + FORCE again. Safe to run regardless
-- of whether 012 succeeded.

BEGIN;

ALTER TABLE memories    NO FORCE ROW LEVEL SECURITY;
ALTER TABLE memories    DISABLE ROW LEVEL SECURITY;

ALTER TABLE facts       NO FORCE ROW LEVEL SECURITY;
ALTER TABLE facts       DISABLE ROW LEVEL SECURITY;

ALTER TABLE projects    NO FORCE ROW LEVEL SECURITY;
ALTER TABLE projects    DISABLE ROW LEVEL SECURITY;

ALTER TABLE auth_tokens NO FORCE ROW LEVEL SECURITY;
ALTER TABLE auth_tokens DISABLE ROW LEVEL SECURITY;

DELETE FROM schema_migrations WHERE filename = '012_rls_force.sql';

COMMIT;
