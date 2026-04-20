-- Rollback for 005_auth_users.sql
--
-- Apply this manually to undo the auth-schema migration. The forward
-- migration runner (lib/db.py::_run_migrations) only globs .sql files
-- at the top of config/migrations/ and does not descend into this
-- subdirectory, so leaving this file here is safe.
--
-- Destructive: drops users, auth_tokens, and the owner_user_id
-- columns. Any row in users / auth_tokens is lost; any row in
-- memories / projects / facts survives but loses its ownership
-- link. Run inside a transaction if you want to be sure.

BEGIN;

-- Drop indexes first (CASCADE on the tables would catch them, but
-- being explicit documents intent).
DROP INDEX IF EXISTS idx_projects_owner;
DROP INDEX IF EXISTS idx_memories_owner;
DROP INDEX IF EXISTS idx_facts_owner;

ALTER TABLE projects  DROP COLUMN IF EXISTS owner_user_id;
ALTER TABLE memories  DROP COLUMN IF EXISTS owner_user_id;
ALTER TABLE facts     DROP COLUMN IF EXISTS owner_user_id;

DROP INDEX IF EXISTS idx_auth_tokens_active;
DROP INDEX IF EXISTS idx_auth_tokens_user;
DROP INDEX IF EXISTS idx_users_username;

DROP TABLE IF EXISTS auth_tokens;
DROP TABLE IF EXISTS users;

-- Remove the migration tracking row so the forward runner re-applies
-- 005_auth_users.sql on the next server start, if desired.
DELETE FROM schema_migrations WHERE filename = '005_auth_users.sql';

COMMIT;
