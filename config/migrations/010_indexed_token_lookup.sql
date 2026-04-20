-- Migration 010: indexed token lookup
--
-- Replaces the LIMIT 500 + scrypt-every-row resolve_token strategy
-- with an O(1) WHERE lookup_key = $1 SELECT + a single scrypt
-- verification of the raw token's secret half. Removes the stale-
-- token-ages-out-of-the-window availability concern and turns
-- authentication into a constant-time DB lookup.
--
-- Token format change:
--   old:  nnm_<secret>                    (whole secret stored as hash)
--   new:  nnm_<lookup_key>.<secret>       (lookup_key stored plain +
--                                          indexed; secret stored as
--                                          scrypt hash)
--
-- Security properties preserved:
--   - secret is still scrypt-hashed at rest.
--   - A DB dump exposes lookup_keys (cheap-to-generate random strings
--     — secrecy is unnecessary for them) but NOT the secrets that
--     gate actual authentication.
--   - Raw token is still shown exactly once at mint time.
--
-- Compat: existing tokens (NULL lookup_key after this migration)
-- cannot be resolved by the new path. Rather than dual-parse the old
-- format, the migration revokes every outstanding token. Users re-
-- login after deploy. The auth_tokens table keeps the historical
-- rows (so audit trail and last_used_at are preserved) but they
-- immediately fail auth from the moment this migration applies.
--
-- Rollback: config/migrations/rollback/010_indexed_token_lookup.sql.
-- Un-revoking the forcibly-revoked tokens is NOT part of the rollback
-- (we don't track which were pre-migration active vs. already revoked);
-- a rollback leaves those tokens revoked forever, consistent with the
-- security model.

-- Step 1: add the column. NULLABLE so legacy rows satisfy the schema.
ALTER TABLE auth_tokens
    ADD COLUMN IF NOT EXISTS lookup_key TEXT;

-- Step 2: unique index, partial on non-NULL so legacy NULL rows
-- don't collide with each other or with new inserts.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_auth_tokens_lookup_key
    ON auth_tokens (lookup_key)
    WHERE lookup_key IS NOT NULL;

-- Step 3: revoke every outstanding pre-migration token. The Python
-- layer will stop accepting them on the next auth attempt because
-- the new resolve_token path filters on a non-NULL lookup_key, and
-- revoked_at IS NOT NULL is already a filter.
UPDATE auth_tokens
SET revoked_at = COALESCE(revoked_at, now())
WHERE lookup_key IS NULL AND revoked_at IS NULL;
