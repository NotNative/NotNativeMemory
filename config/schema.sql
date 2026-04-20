-- NotNativeMemory database schema
-- Requires PostgreSQL 16+ with pgvector extension

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Projects table: maps working directories to project identifiers.
-- scope controls cross-project memory sharing:
--   local  = single project (default)
--   domain = applies to any project declaring this domain
--   global = applies everywhere
-- domains[] lists which domain-scoped projects this project pulls from.
CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    directory TEXT NOT NULL,
    name TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'local'
        CHECK (scope IN ('local', 'domain', 'global')),
    domains TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);
-- Uniqueness is per-user: each user has their own _global, _domain_*,
-- and local project rows. Composite is added after users/ownership
-- tables exist lower in this file.

CREATE INDEX IF NOT EXISTS idx_projects_scope
    ON projects (scope);

CREATE INDEX IF NOT EXISTS idx_projects_domains
    ON projects USING gin (domains);

-- Memories table: vector-backed persistent memory store
CREATE TABLE IF NOT EXISTS memories (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    embedding vector(768),
    tags TEXT[] DEFAULT '{}',
    importance TEXT DEFAULT 'normal'
        CHECK (importance IN ('low', 'normal', 'high', 'critical')),
    temperature FLOAT DEFAULT 70.0,
    created_at TIMESTAMPTZ DEFAULT now(),
    last_accessed TIMESTAMPTZ DEFAULT now(),
    access_count INT DEFAULT 0
);

-- Vector similarity search (cosine distance)
CREATE INDEX IF NOT EXISTS idx_memories_embedding
    ON memories USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Tag filtering via GIN index
CREATE INDEX IF NOT EXISTS idx_memories_tags
    ON memories USING gin (tags);

-- Project scoping
CREATE INDEX IF NOT EXISTS idx_memories_project
    ON memories (project_id);

-- Access-based cleanup queries
CREATE INDEX IF NOT EXISTS idx_memories_last_accessed
    ON memories (last_accessed);

-- Importance filtering
CREATE INDEX IF NOT EXISTS idx_memories_importance
    ON memories (importance);

-- Temperature-based eviction (find coldest memories per project)
CREATE INDEX IF NOT EXISTS idx_memories_temperature
    ON memories (project_id, temperature ASC);

-- Decay stats: tracks usage patterns per project for future self-tuning.
CREATE TABLE IF NOT EXISTS decay_stats (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    recorded_at TIMESTAMPTZ DEFAULT now(),
    total_memories INT NOT NULL DEFAULT 0,
    memories_accessed_since_last INT NOT NULL DEFAULT 0,
    memories_decayed INT NOT NULL DEFAULT 0,
    memories_evicted INT NOT NULL DEFAULT 0,
    memories_deduplicated INT NOT NULL DEFAULT 0,
    avg_temperature FLOAT,
    avg_access_count FLOAT,
    median_access_interval_hours FLOAT
);

CREATE INDEX IF NOT EXISTS idx_decay_stats_project_time
    ON decay_stats (project_id, recorded_at DESC);

-- Migration tracking: records which migration files have been applied.
-- The migration runner creates this table itself if missing, so this
-- definition is only needed for fresh installs via docker-entrypoint.
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT now()
);

-- Facts table: temporal knowledge graph for assertions that change over time.
-- Stores (subject, predicate, object) triples with validity windows.
CREATE TABLE IF NOT EXISTS facts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence FLOAT DEFAULT 1.0,
    valid_from TIMESTAMPTZ DEFAULT now(),
    valid_to TIMESTAMPTZ,                    -- NULL = still true
    source_memory_id UUID REFERENCES memories(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_facts_subject
    ON facts (project_id, subject);

CREATE INDEX IF NOT EXISTS idx_facts_predicate
    ON facts (project_id, predicate);

CREATE INDEX IF NOT EXISTS idx_facts_valid
    ON facts (valid_from, valid_to);

-- Users table: identity for the Bearer-token auth layer.
-- Registration is open (any caller can create an account). There is
-- no admin concept - every user sees only their own memories.
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,          -- hashlib.scrypt digest
    -- Monotonic session-revocation counter. Tokens snapshot this at
    -- mint time and auth rejects any token whose snapshot differs from
    -- the current value. Bumping invalidates every outstanding token.
    token_generation INT NOT NULL DEFAULT 0,
    -- Admin flag. Set only by the claim-admin bootstrap flow or the
    -- reset-admin CLI. No route accepts is_admin in a payload.
    is_admin BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_username
    ON users (username);

-- auth_tokens: hashed Bearer tokens. Raw token value is shown exactly
-- once at creation; the DB only ever sees the hash. Revocation is
-- immediate (revoked_at timestamp; the active-lookup index filters).
CREATE TABLE IF NOT EXISTS auth_tokens (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT UNIQUE NOT NULL,      -- hashlib.scrypt digest
    -- Plaintext random string used for O(1) lookup on the auth path.
    -- Secrecy not required; entropy just keeps rows from colliding.
    lookup_key TEXT,
    label TEXT,
    -- Snapshot of users.token_generation at mint time. Auth rejects
    -- the token when this drifts from the user's current generation.
    issued_generation INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_auth_tokens_user
    ON auth_tokens (user_id);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_auth_tokens_lookup_key
    ON auth_tokens (lookup_key)
    WHERE lookup_key IS NOT NULL;

-- Ownership columns: every row belongs to exactly one user. NOT NULL
-- enforces "no anonymous rows"; per-user reads trust the non-null
-- invariant to skip a nullable branch in the query builder.
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS owner_user_id UUID NOT NULL
        REFERENCES users(id) ON DELETE CASCADE;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS owner_user_id UUID NOT NULL
        REFERENCES users(id) ON DELETE CASCADE;

ALTER TABLE facts
    ADD COLUMN IF NOT EXISTS owner_user_id UUID NOT NULL
        REFERENCES users(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_projects_owner
    ON projects (owner_user_id);

CREATE INDEX IF NOT EXISTS idx_memories_owner
    ON memories (owner_user_id);

CREATE INDEX IF NOT EXISTS idx_facts_owner
    ON facts (owner_user_id);

-- Per-user uniqueness on project directory. Two users can both have
-- a `_global` row, a `D:/Projects/foo` row, etc. Within a single
-- user, (directory, owner_user_id) is still unique.
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

-- Audit events: append-only trail of security-relevant actions.
-- Writers live in lib/audit.py::log_event.
CREATE TABLE IF NOT EXISTS audit_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    target_id UUID,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_events_actor_at
    ON audit_events (actor_user_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_event_at
    ON audit_events (event_type, at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_at
    ON audit_events (at DESC);

-- Row-Level Security policies with admin-sentinel bypass. Inert
-- until operators create a non-superuser DB role AND ENABLE ROW
-- LEVEL SECURITY on each table; see lib/rls.py, migration 008, and
-- migration 013.

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
