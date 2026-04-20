-- Migration 007: Session revocation via token generation counter
--
-- Adds:
--   users.token_generation           -- monotonic counter per user
--   auth_tokens.issued_generation    -- generation snapshot at mint
--
-- Model: a token is valid iff its issued_generation == the owning
-- user's current token_generation. Bumping the user's generation
-- therefore invalidates every outstanding token in a single UPDATE,
-- without having to enumerate or delete rows. Used for:
--   - "force log out this user" (admin off-board, password change)
--   - "force log out everyone" (incident response; UPDATE users SET
--     token_generation = token_generation + 1)
--
-- The generation check is added to lib/auth_db.py::resolve_token's
-- existing JOIN query, so stale tokens are filtered at SQL time
-- without an extra round trip or a second per-request query.
--
-- Compat: DEFAULT 0 on both columns means existing users stay logged
-- in across the deploy. No mass-logout side effect.
--
-- Rollback: config/migrations/rollback/007_session_generation.sql

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS token_generation INT NOT NULL DEFAULT 0;

ALTER TABLE auth_tokens
    ADD COLUMN IF NOT EXISTS issued_generation INT NOT NULL DEFAULT 0;
