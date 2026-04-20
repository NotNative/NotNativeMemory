-- Migration 005: Auth users and per-user ownership columns
--
-- Adds:
--   users                -- identity table (username + scrypt hash)
--   auth_tokens          -- Bearer tokens, hashed at rest, revocable
--   projects.owner_user_id, memories.owner_user_id, facts.owner_user_id
--                         -- ownership columns, nullable at first so
--                           existing rows are "unclaimed" until the
--                           first registrant adopts them.
--
-- This migration is schema-only. The auth middleware and registration
-- endpoints live in a separate server-side change (Phase 5). This
-- migration is safe to apply on a live database that has no auth
-- logic yet: existing tools keep working because owner_user_id is
-- NULL on every existing row, and no query filters on it yet.
--
-- Rollback: config/migrations/rollback/005_auth_users.sql reverses
-- this. The rollback dir is not scanned by the forward migration
-- runner (it only globs the top level of config/migrations/).

-- -----------------------------------------------------------------
-- users
-- -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,          -- hashlib.scrypt digest
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_username
    ON users (username);

-- -----------------------------------------------------------------
-- auth_tokens
-- -----------------------------------------------------------------
-- Bearer tokens are stored hashed; the raw token is shown to the user
-- exactly once at creation time. Revocation is immediate — the
-- lookup index below skips revoked rows.
CREATE TABLE IF NOT EXISTS auth_tokens (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT UNIQUE NOT NULL,      -- hashlib.scrypt digest
    label TEXT,                            -- optional user-facing name
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_auth_tokens_user
    ON auth_tokens (user_id);

-- Partial index so revoked tokens don't slow down auth lookups.
CREATE INDEX IF NOT EXISTS idx_auth_tokens_active
    ON auth_tokens (token_hash)
    WHERE revoked_at IS NULL;

-- -----------------------------------------------------------------
-- Ownership columns on existing tables
-- -----------------------------------------------------------------
-- Nullable on purpose. Existing rows stay accessible to the server
-- until the first-registrant adoption step runs in the auth phase.
-- After adoption, Phase 6 (per-user isolation) makes every query
-- filter on owner_user_id and NULL becomes "no owner, hidden to all."
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS owner_user_id UUID
        REFERENCES users(id) ON DELETE CASCADE;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS owner_user_id UUID
        REFERENCES users(id) ON DELETE CASCADE;

ALTER TABLE facts
    ADD COLUMN IF NOT EXISTS owner_user_id UUID
        REFERENCES users(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_projects_owner
    ON projects (owner_user_id);

CREATE INDEX IF NOT EXISTS idx_memories_owner
    ON memories (owner_user_id);

CREATE INDEX IF NOT EXISTS idx_facts_owner
    ON facts (owner_user_id);
