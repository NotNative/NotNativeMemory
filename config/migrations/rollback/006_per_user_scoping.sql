-- Rollback for 006_per_user_scoping.sql
--
-- Restores the pre-006 schema: brings back is_admin on users,
-- reverts projects' unique key from (directory, owner_user_id) back
-- to just directory, and drops the NOT NULL constraint on the
-- ownership columns. The orphan-reassignment step in 006 is NOT
-- reversed (that information wasn't recorded).
--
-- Safe to run only if the code base has been reverted to the Phase 5
-- era. Running this against current code will break reads because
-- read filters expect every row to have an owner.

BEGIN;

-- Re-add is_admin. Previously defaulted to FALSE; pick the row that
-- predates the migration and flip it on if you want that user to be
-- admin again.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;

-- Drop NOT NULL from ownership columns so legacy behavior works.
ALTER TABLE projects ALTER COLUMN owner_user_id DROP NOT NULL;
ALTER TABLE memories ALTER COLUMN owner_user_id DROP NOT NULL;
ALTER TABLE facts    ALTER COLUMN owner_user_id DROP NOT NULL;

-- Swap the unique constraint back.
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_directory_owner_key;

-- Re-adding the single-column UNIQUE will fail if two users have
-- rows with the same directory (exactly the multi-user case this
-- migration was built to support). Guard against that.
DO $$
DECLARE
    dup_count INT;
BEGIN
    SELECT COUNT(*) INTO dup_count FROM (
        SELECT directory FROM projects GROUP BY directory HAVING COUNT(*) > 1
    ) dups;
    IF dup_count > 0 THEN
        RAISE EXCEPTION
            'Cannot restore single-column UNIQUE on projects.directory: % '
            'directory values have multiple owners. Merge or delete those '
            'rows before running this rollback.', dup_count;
    END IF;
END $$;

ALTER TABLE projects
    ADD CONSTRAINT projects_directory_key UNIQUE (directory);

DELETE FROM schema_migrations WHERE filename = '006_per_user_scoping.sql';

COMMIT;
