-- Migration 017: switch embedding indexes from IVFFlat to HNSW
--
-- Why: IVFFlat clusters vectors into ``lists`` and a query only scans
-- a small subset of those lists by default (ivfflat.probes = 1). On
-- small or medium tables the row a caller is looking for can sit in a
-- list the probe misses entirely, and the index scan returns zero
-- rows. The behavior is correctness-affecting, not just a recall
-- shortfall: search_memories / search_docs / compose_recall all start
-- returning empty results despite the data being present and the
-- WHERE clause matching it. Increasing ivfflat.probes does not help
-- on its own at very small list-occupancy.
--
-- HNSW (Hierarchical Navigable Small World) graphs do not have the
-- list-miss failure mode. They return correct nearest neighbors at
-- any table size. HNSW is the modern recommended pgvector index
-- since 0.5.0; IVFFlat is kept primarily for backward compat.
--
-- Cost: HNSW indexes are ~2x slower to build and ~2x larger on disk
-- than IVFFlat at the same recall. For a personal-scale memory store
-- that's negligible. Query time is comparable or better.
--
-- Rollback: config/migrations/rollback/017_hnsw_indexes.sql restores
-- the IVFFlat indexes (with the same buggy small-dataset behavior).

BEGIN;

DROP INDEX IF EXISTS idx_memories_embedding;
CREATE INDEX idx_memories_embedding
    ON memories USING hnsw (embedding vector_cosine_ops);

DROP INDEX IF EXISTS idx_doc_chunks_embedding;
CREATE INDEX idx_doc_chunks_embedding
    ON doc_chunks USING hnsw (embedding vector_cosine_ops);

COMMIT;
