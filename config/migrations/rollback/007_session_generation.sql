-- Rollback for 007_session_generation.sql
--
-- Destructive for the revocation state only: drops the counters.
-- Existing users and tokens are preserved; after rollback, tokens can
-- no longer be mass-invalidated by the generation mechanism.
--
-- Apply manually. The forward migration runner does not descend into
-- this directory.

BEGIN;

ALTER TABLE auth_tokens DROP COLUMN IF EXISTS issued_generation;
ALTER TABLE users       DROP COLUMN IF EXISTS token_generation;

DELETE FROM schema_migrations WHERE filename = '007_session_generation.sql';

COMMIT;
