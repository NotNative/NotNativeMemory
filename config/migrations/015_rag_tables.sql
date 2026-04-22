-- Migration 015: RAG scaffolding — documents, doc_chunks, ingestion_jobs
--
-- Phase A of the RAG integration. Adds three new tables alongside the
-- existing memories stack. Separation is deliberate: memory rows are
-- curated (every row is worth keeping), doc chunks are bulk-ingested
-- (many rows per document, noisy, subject to re-ingestion). Mixing
-- them in the memories table would put doc-chunk floods in reach of
-- the thermal/eviction/dedup machinery that assumes curated scale.
--
-- Schema overview:
--
--   documents           one row per ingested file / URL / paste
--     - sha256 is the dedup key (unique per owner)
--     - scoped to (owner_user_id, project_id) like memories
--
--   doc_chunks          the retrieval unit
--     - vector(1024) embedding (matches memories after migration 014)
--     - (document_id, chunk_index) is unique per document
--     - char_start/char_end let callers cite back to the source text
--     - ivfflat index mirrors the memories embedding index
--
--   ingestion_jobs      lifecycle tracking for the embed + store pass
--     - inline ingestion in v1: rows transition queued -> running ->
--       complete (or failed) before the MCP tool returns. The table
--       exists so async ingestion in a later phase does not need a
--       schema change, only a worker.
--
-- RLS:
--   Every new table uses the same owner_user_id + admin-sentinel
--   policy defined for memories/facts/projects/auth_tokens in
--   migrations 008 + 013, enabled and forced the way migration 012
--   handles the existing tables.
--
-- Grants:
--   Not included here. The app role's ALTER DEFAULT PRIVILEGES (set
--   up by docker/init/ensure_app_role.py) covers new tables created
--   after role provisioning. Fresh installs pick the grants up
--   automatically on first connect.
--
-- Rollback: config/migrations/rollback/015_rag_tables.sql drops the
-- three tables (CASCADE) which in turn drops indexes and policies.

BEGIN;

-- documents: metadata + dedup key for each ingested source.
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    source_uri TEXT,
    content_type TEXT NOT NULL DEFAULT 'text/plain',
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-owner dedup: ingesting the same content twice into the same
-- owner updates rather than duplicates. Different owners may ingest
-- the same content independently.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_documents_owner_sha256
    ON documents (owner_user_id, sha256);

CREATE INDEX IF NOT EXISTS idx_documents_project
    ON documents (project_id);

CREATE INDEX IF NOT EXISTS idx_documents_owner
    ON documents (owner_user_id);

-- doc_chunks: retrieval units, one row per chunk of a document.
CREATE TABLE IF NOT EXISTS doc_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding vector(1024),
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_doc_chunks_document_idx
    ON doc_chunks (document_id, chunk_index);

CREATE INDEX IF NOT EXISTS idx_doc_chunks_embedding
    ON doc_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_doc_chunks_project
    ON doc_chunks (project_id);

CREATE INDEX IF NOT EXISTS idx_doc_chunks_owner
    ON doc_chunks (owner_user_id);

CREATE INDEX IF NOT EXISTS idx_doc_chunks_document
    ON doc_chunks (document_id);

-- ingestion_jobs: lifecycle of the chunk-and-embed pass per document.
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'complete', 'failed')),
    error TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_document
    ON ingestion_jobs (document_id);

CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_owner
    ON ingestion_jobs (owner_user_id);

CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_status
    ON ingestion_jobs (status)
    WHERE status IN ('queued', 'running');

-- RLS policies: admin sentinel + owner match, following migration 013.
DROP POLICY IF EXISTS documents_owner_rls ON documents;
CREATE POLICY documents_owner_rls ON documents
    USING (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    )
    WITH CHECK (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    );

DROP POLICY IF EXISTS doc_chunks_owner_rls ON doc_chunks;
CREATE POLICY doc_chunks_owner_rls ON doc_chunks
    USING (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    )
    WITH CHECK (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    );

DROP POLICY IF EXISTS ingestion_jobs_owner_rls ON ingestion_jobs;
CREATE POLICY ingestion_jobs_owner_rls ON ingestion_jobs
    USING (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    )
    WITH CHECK (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    );

-- Enable + FORCE RLS so the memory_app role hits the same enforcement
-- the memories stack already has after migration 012.
ALTER TABLE documents       ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents       FORCE ROW LEVEL SECURITY;
ALTER TABLE doc_chunks      ENABLE ROW LEVEL SECURITY;
ALTER TABLE doc_chunks      FORCE ROW LEVEL SECURITY;
ALTER TABLE ingestion_jobs  ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_jobs  FORCE ROW LEVEL SECURITY;

COMMIT;
