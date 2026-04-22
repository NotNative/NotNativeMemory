-- Rollback for 017_hnsw_indexes.sql
--
-- Restores the IVFFlat indexes. Note: this re-introduces the small-
-- dataset bug where the index can return zero rows even when the
-- target memory / chunk is present. Operators rolling back should
-- be aware that search_memories / search_docs / compose_recall may
-- intermittently return empty results until the table grows past
-- the lists threshold.

BEGIN;

DROP INDEX IF EXISTS idx_memories_embedding;
CREATE INDEX idx_memories_embedding
    ON memories USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

DROP INDEX IF EXISTS idx_doc_chunks_embedding;
CREATE INDEX idx_doc_chunks_embedding
    ON doc_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

DELETE FROM schema_migrations WHERE filename = '017_hnsw_indexes.sql';

COMMIT;
