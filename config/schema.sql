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
