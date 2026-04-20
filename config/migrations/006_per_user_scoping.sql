-- Migration 006: per-user scoping, drop admin, require ownership
--
-- Pulls the auth model from "first user is admin, shared scopes" to
-- "every row owned by a user, _global and _domain_<name> are per-user
-- reserved scope names." Concretely:
--
--   users.is_admin  ->  dropped. No admin concept anymore; any admin
--                       features move to CLI or go away entirely.
--   projects        ->  UNIQUE key on `directory` replaced with the
--                       composite `(directory, owner_user_id)` so each
--                       user has their own `_global` and domain rows.
--   owner_user_id   ->  NOT NULL on projects, memories, facts. Orphan
--                       rows (NULL owner) are assigned to the sole
--                       user if exactly one exists; otherwise the
--                       migration aborts with a helpful error.
--
-- Rollback: config/migrations/rollback/006_per_user_scoping.sql
-- undoes the constraint changes and restores the is_admin column.
-- Orphan reassignment is NOT reversible (the migration does not
-- track which rows were orphans).

-- Step 1: deal with orphan rows.
-- If exactly one user exists, assume single-user deployment and adopt
-- every orphan to that user. Otherwise halt with an error that tells
-- the operator exactly what to do.
DO $$
DECLARE
    user_count INT;
    orphan_memories INT;
    orphan_projects INT;
    orphan_facts INT;
    sole_user UUID;
BEGIN
    SELECT COUNT(*) INTO user_count FROM users;
    SELECT COUNT(*) INTO orphan_memories FROM memories WHERE owner_user_id IS NULL;
    SELECT COUNT(*) INTO orphan_projects FROM projects WHERE owner_user_id IS NULL;
    SELECT COUNT(*) INTO orphan_facts FROM facts WHERE owner_user_id IS NULL;

    IF orphan_memories + orphan_projects + orphan_facts = 0 THEN
        RAISE NOTICE 'No orphan rows. Skipping adoption step.';
    ELSIF user_count = 1 THEN
        SELECT id INTO sole_user FROM users LIMIT 1;
        RAISE NOTICE 'Assigning % orphan memories, % projects, % facts to sole user %',
            orphan_memories, orphan_projects, orphan_facts, sole_user;
        UPDATE memories SET owner_user_id = sole_user WHERE owner_user_id IS NULL;
        UPDATE projects SET owner_user_id = sole_user WHERE owner_user_id IS NULL;
        UPDATE facts    SET owner_user_id = sole_user WHERE owner_user_id IS NULL;
    ELSE
        RAISE EXCEPTION
            'Cannot apply 006: % orphan rows exist and % users are registered. '
            'Pre-migration, every memory / project / fact must have an owner. '
            'Either (a) assign orphans to a user via SQL and retry, or '
            '(b) DELETE them if they are disposable, or '
            '(c) register exactly one user (if you intend a single-user deploy) '
            'and retry.',
            orphan_memories + orphan_projects + orphan_facts, user_count;
    END IF;
END $$;

-- Step 2: composite unique on projects.
-- Drop the legacy single-column constraint and add the (directory,
-- owner_user_id) pair. This is what lets every user have their own
-- `_global`, `_domain_*`, and local project rows independently.
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_directory_key;

-- IF NOT EXISTS on ADD CONSTRAINT isn't portable; guard with pg_catalog.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'projects_directory_owner_key'
    ) THEN
        ALTER TABLE projects
            ADD CONSTRAINT projects_directory_owner_key
            UNIQUE (directory, owner_user_id);
    END IF;
END $$;

-- Step 3: require ownership on every write.
ALTER TABLE projects ALTER COLUMN owner_user_id SET NOT NULL;
ALTER TABLE memories ALTER COLUMN owner_user_id SET NOT NULL;
ALTER TABLE facts    ALTER COLUMN owner_user_id SET NOT NULL;

-- Step 4: drop is_admin. The new model has no admin concept.
ALTER TABLE users DROP COLUMN IF EXISTS is_admin;
