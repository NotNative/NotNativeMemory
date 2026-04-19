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
    directory TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'local'
        CHECK (scope IN ('local', 'domain', 'global')),
    domains TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);

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

-- Users table: identity for the Bearer-token auth layer. The first
-- row registered on a fresh MCP gets is_admin=TRUE; subsequent
-- registrations require an invite from an admin.
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,          -- hashlib.scrypt digest
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
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
    label TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_auth_tokens_user
    ON auth_tokens (user_id);

CREATE INDEX IF NOT EXISTS idx_auth_tokens_active
    ON auth_tokens (token_hash)
    WHERE revoked_at IS NULL;

-- Ownership columns. Nullable so pre-auth rows remain accessible;
-- per-user isolation in the tool layer enforces scoping.
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
