-- Rollback for 011_admin_role.sql
--
-- Drops the is_admin column. Safe provided no server code has been
-- redeployed without the admin helpers (once helpers depend on
-- is_admin existing, rollback breaks login). Re-deploy an older
-- server build first, then run this.

BEGIN;

ALTER TABLE users DROP COLUMN IF EXISTS is_admin;

DELETE FROM schema_migrations WHERE filename = '011_admin_role.sql';

COMMIT;
