-- Rollback for 016_tsv_columns.sql
--
-- Drops the generated tsv columns and their GIN indexes. Any query
-- code branched on hybrid=True will need to be reverted in lockstep
-- or the query will fail with "column tsv does not exist".

BEGIN;

DROP INDEX IF EXISTS idx_memories_tsv;
DROP INDEX IF EXISTS idx_doc_chunks_tsv;

ALTER TABLE memories   DROP COLUMN IF EXISTS tsv;
ALTER TABLE doc_chunks DROP COLUMN IF EXISTS tsv;

DELETE FROM schema_migrations WHERE filename = '016_tsv_columns.sql';

COMMIT;
