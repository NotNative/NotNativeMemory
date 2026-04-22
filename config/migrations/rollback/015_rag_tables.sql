-- Rollback for 015_rag_tables.sql
--
-- Drops the three RAG tables and every dependent object (indexes,
-- policies, constraints). Data in documents / doc_chunks / ingestion_jobs
-- is lost; the rollback assumes the operator is OK with that.

BEGIN;

DROP TABLE IF EXISTS ingestion_jobs CASCADE;
DROP TABLE IF EXISTS doc_chunks CASCADE;
DROP TABLE IF EXISTS documents CASCADE;

DELETE FROM schema_migrations WHERE filename = '015_rag_tables.sql';

COMMIT;
