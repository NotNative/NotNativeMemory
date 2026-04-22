-- Migration 016: add tsvector columns on memories and doc_chunks
--
-- Groundwork for BM25-style hybrid retrieval. Pure cosine similarity
-- over embeddings misses exact-term matches (names, acronyms, code
-- identifiers). A generated tsvector column lets us run Postgres
-- full-text search alongside the vector query and fuse the rankings
-- via Reciprocal Rank Fusion at query time.
--
-- The tsv column is GENERATED ALWAYS AS ... STORED:
--   - Auto-updates when content changes (no triggers to maintain).
--   - Computed for existing rows during this ALTER (fast on the dev
--     and early-prod volumes we have; slower tables would need a
--     separate backfill strategy).
--   - Disk cost: one tsvector per row. Roughly 2x-3x the source text
--     size in storage for typical English prose.
--
-- Language is hard-coded to 'english' for v1. Most NNM content is
-- English. Making this per-document configurable is a later concern
-- once we actually see non-English content in the corpus.
--
-- GIN indexes on the new columns back the @@ operator at query time.
-- GIN is the standard choice for tsvector; GiST would let us update
-- faster at the cost of slower lookups, which is the wrong trade for
-- a read-heavy retrieval workload.
--
-- Rollback: config/migrations/rollback/016_tsv_columns.sql drops the
-- columns and indexes.

BEGIN;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX IF NOT EXISTS idx_memories_tsv
    ON memories USING gin (tsv);

ALTER TABLE doc_chunks
    ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX IF NOT EXISTS idx_doc_chunks_tsv
    ON doc_chunks USING gin (tsv);

COMMIT;
