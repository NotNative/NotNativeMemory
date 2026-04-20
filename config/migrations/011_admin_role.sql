-- Migration 011: admin role (second incarnation)
--
-- Phase 7 dropped users.is_admin because the old semantics were
-- "first user auto-becomes admin" (Phase 5), which turned out to be
-- too coupled to deploy ordering. This migration reintroduces the
-- column with different semantics:
--
--   is_admin is set ONLY by:
--     1. The claim-admin flow (lib/web_routes.py POST /claim-admin
--        and lib/auth_routes.py POST /auth/claim-admin), which
--        requires possession of a file-based bootstrap token that
--        only the operator with filesystem access can read.
--     2. The reset-admin CLI (python server.py --reset-admin), which
--        clears the admin flag and causes a fresh bootstrap file to
--        be issued on next startup.
--
-- No route lets a normal user toggle is_admin. No API accepts
-- is_admin in a payload.
--
-- Default false keeps every existing user non-admin across the
-- deploy. On a fresh install, the first startup sees no admin,
-- writes the bootstrap file, and waits for someone to claim it.
--
-- Rollback: config/migrations/rollback/011_admin_role.sql

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT false;
