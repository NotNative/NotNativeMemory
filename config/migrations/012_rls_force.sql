-- Migration 012: enable and FORCE Row-Level Security
--
-- Activates the policies defined in migrations 008 (base) and 013
-- (admin sentinel). After this migration:
--
--   - Every query against memories / facts / projects / auth_tokens
--     from a non-superuser, non-BYPASSRLS role is filtered by the
--     policy WHERE clause.
--   - FORCE means even the table's OWNER is subject (without FORCE,
--     the owner role bypasses policies).
--
-- Prerequisites for RLS to actually *enforce* (not just be "on"):
--   1. Dual-role DB config: MEMORY_APP_DB_USER / MEMORY_APP_DB_PASSWORD
--      point at a non-superuser role without BYPASSRLS (Phase 5C.2).
--   2. Every lib/db.py, lib/auth_db.py, and lib/audit.py call site
--      goes through rls.app_conn or rls.admin_conn so
--      app.current_user is set before queries run (Phase 5C.3 Pass 2).
--
-- Both prerequisites land in this commit series. If an operator is
-- still running a single-role setup (app connects as superuser),
-- this migration is a no-op behavior-wise: superusers ALWAYS bypass
-- RLS regardless of ENABLE/FORCE state, so the policies are inert
-- until a non-superuser role takes over.
--
-- Rollback: config/migrations/rollback/012_rls_force.sql disables
-- RLS but leaves the policies in place (so a future re-enable
-- doesn't need to re-create them).

ALTER TABLE memories    ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories    FORCE ROW LEVEL SECURITY;

ALTER TABLE facts       ENABLE ROW LEVEL SECURITY;
ALTER TABLE facts       FORCE ROW LEVEL SECURITY;

ALTER TABLE projects    ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects    FORCE ROW LEVEL SECURITY;

ALTER TABLE auth_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth_tokens FORCE ROW LEVEL SECURITY;
