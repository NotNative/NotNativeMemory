-- Migration 013: RLS policy updates for admin-sentinel bypass
--
-- Extends the policies defined in migration 008 to accept a sentinel
-- value 'admin' for the app.current_user GUC. When set, the policy
-- short-circuits the owner_user_id match and returns true, letting
-- admin-scoped queries (e.g., list_users_overview, audit list_events)
-- see rows across all users.
--
-- The bypass is a trust boundary in the application layer:
-- memory_app's SQL privileges let any caller SET the GUC to 'admin',
-- so the DB cannot enforce who is allowed to escalate. The policy
-- is a last-line safety net; the first line is _require_admin on
-- every /admin/* route.
--
-- Without this update, an admin user hitting /admin/users would see
-- zero user counts under FORCE RLS because the scalar subqueries
-- filter on each row's owner_user_id, which never equals the admin's
-- own user_id except on the admin's own row.
--
-- Inert until FORCE ROW LEVEL SECURITY lands (5C.4); policies-defined
-- does not imply policies-enforced. See docs/rls-activation.md.
--
-- Rollback: config/migrations/rollback/013_rls_admin_sentinel.sql
-- restores the original policies without the admin bypass.

-- Drop the old policies and re-create with the admin bypass as the
-- first branch. Using `current_setting(..., true)` returns NULL when
-- unset rather than raising; NULL = 'admin' is NULL (false branch
-- taken), keeping the unset case fail-closed.
--
-- The owner_user_id comparison uses `::text = current_setting(...)`
-- rather than casting the GUC to uuid, so the 'admin' sentinel
-- (which is not a valid UUID) never reaches the cast path.

DROP POLICY IF EXISTS memories_owner_rls ON memories;
CREATE POLICY memories_owner_rls ON memories
    USING (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    )
    WITH CHECK (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    );

DROP POLICY IF EXISTS facts_owner_rls ON facts;
CREATE POLICY facts_owner_rls ON facts
    USING (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    )
    WITH CHECK (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    );

DROP POLICY IF EXISTS projects_owner_rls ON projects;
CREATE POLICY projects_owner_rls ON projects
    USING (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    )
    WITH CHECK (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    );

DROP POLICY IF EXISTS auth_tokens_owner_rls ON auth_tokens;
CREATE POLICY auth_tokens_owner_rls ON auth_tokens
    USING (
        current_setting('app.current_user', true) = 'admin'
        OR user_id::text = current_setting('app.current_user', true)
    )
    WITH CHECK (
        current_setting('app.current_user', true) = 'admin'
        OR user_id::text = current_setting('app.current_user', true)
    );
