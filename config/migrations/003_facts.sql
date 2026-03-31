-- Migration 003: Temporal knowledge graph
-- Stores facts as (subject, predicate, object) triples with time validity.
-- Facts can be superseded without deletion, enabling time-travel queries.

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
