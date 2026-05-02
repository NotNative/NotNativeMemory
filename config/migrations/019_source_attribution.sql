-- Migration 019: Add source attribution to memories
--
-- Tracks provenance: who/what created the memory and under what context.
-- source_kind distinguishes explicit user statements from tool outputs
-- from model inferences, letting retrieval weight by reliability.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS source_kind TEXT
        CHECK (source_kind IS NULL OR source_kind IN ('user-stated', 'tool-result', 'model-inferred'));

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS source_session_id TEXT;

CREATE INDEX IF NOT EXISTS idx_memories_source_kind
    ON memories (source_kind)
    WHERE source_kind IS NOT NULL;
