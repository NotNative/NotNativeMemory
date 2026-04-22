-- Rollback for 014_embedding_to_1024.sql
--
-- Returns memories.embedding to vector(768). As with the forward
-- migration, no data is preserved: every row's embedding becomes NULL.
-- Re-embedding under the prior model (gte-base-en-v1.5) is the
-- operator's responsibility.

BEGIN;

DROP INDEX IF EXISTS idx_memories_embedding;

ALTER TABLE memories
    ALTER COLUMN embedding TYPE vector(768) USING NULL::vector(768);

CREATE INDEX idx_memories_embedding
    ON memories USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

DELETE FROM schema_migrations WHERE filename = '014_embedding_to_1024.sql';

COMMIT;
