-- Rollback for migration 022.
-- Dropping the table cascades through every index and the RLS policy.

BEGIN;

DROP TABLE IF EXISTS verbatim_chunks CASCADE;

COMMIT;
