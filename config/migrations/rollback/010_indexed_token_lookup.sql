-- Rollback for 010_indexed_token_lookup.sql
--
-- Drops the lookup_key column and its unique index. The tokens
-- revoked by the forward migration stay revoked (we have no record
-- of which rows were pre-migration active), consistent with the
-- security model: any rollback is assumed to be a deliberate
-- "roll back the feature and let users re-login again" operation.

BEGIN;

DROP INDEX IF EXISTS uniq_auth_tokens_lookup_key;
ALTER TABLE auth_tokens DROP COLUMN IF EXISTS lookup_key;

DELETE FROM schema_migrations WHERE filename = '010_indexed_token_lookup.sql';

COMMIT;
