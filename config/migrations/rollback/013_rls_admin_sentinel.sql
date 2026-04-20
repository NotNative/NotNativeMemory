-- Rollback for 013_rls_admin_sentinel.sql
--
-- Restores the original policies from migration 008 (UUID-only match,
-- no admin sentinel). If RLS is currently ENABLED, admin dashboard
-- queries will return empty after this rollback until the policies
-- are updated or RLS is disabled.

BEGIN;

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

DROP POLICY IF EXISTS auth_tokens_owner_rls ON auth_tokens;
CREATE POLICY auth_tokens_owner_rls ON auth_tokens
    USING (user_id = current_setting('app.current_user', true)::uuid)
    WITH CHECK (user_id = current_setting('app.current_user', true)::uuid);

DELETE FROM schema_migrations WHERE filename = '013_rls_admin_sentinel.sql';

COMMIT;
