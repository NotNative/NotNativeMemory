-- Migration 014: resize memories.embedding from vector(768) to vector(1024)
--
-- Switches the embedding column to accommodate gte-large-en-v1.5 (1024
-- dim) in place of gte-base-en-v1.5 (768 dim). Existing 768-dim vectors
-- are invalid for the new model and are not convertible, so every row's
-- embedding is set to NULL by this migration. Any memories present at
-- migration time will need to be re-embedded by the application before
-- they surface in semantic search again.
--
-- Expected operational flow:
--   1. Stop the server (or let it idle).
--   2. Apply this migration.
--   3. Swap the install's models/ directory to the gte-large model.
--   4. Restart the server.
--   5. Optionally run a bulk re-embed over rows with embedding IS NULL.
--
-- The ivfflat index is dimensionality-bound, so it is dropped before
-- the ALTER and recreated after. The new index covers NULL-embedding
-- rows (pgvector ignores them); no extra filter is needed.
--
-- Rollback: config/migrations/rollback/014_embedding_to_1024.sql

BEGIN;

DROP INDEX IF EXISTS idx_memories_embedding;

ALTER TABLE memories
    ALTER COLUMN embedding TYPE vector(1024) USING NULL::vector(1024);

CREATE INDEX idx_memories_embedding
    ON memories USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

COMMIT;
