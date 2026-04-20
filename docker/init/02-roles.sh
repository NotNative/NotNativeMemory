#!/bin/bash
# Create the non-superuser application role used by lib/db.py's app pool.
# Runs AFTER 01-schema.sql (alphabetical order in
# /docker-entrypoint-initdb.d/) so the tables exist when we GRANT on
# them.
#
# Postgres always bypasses RLS for superusers, so if the app connects
# as POSTGRES_USER (which is always a superuser), RLS policies are
# inert. This script creates a second role that explicitly lacks
# SUPERUSER and BYPASSRLS attributes — connecting the app as this role
# makes RLS actively enforce across every memories / facts / projects /
# auth_tokens query.
#
# Skip conditions:
#   - MEMORY_APP_DB_PASSWORD unset: operator has not opted into RLS
#     enforcement. The script no-ops; app stays in single-role mode.
#   - Role already exists: installer or a prior init already created
#     it; re-running is safe (we just confirm privileges).

set -e

if [ -z "$MEMORY_APP_DB_PASSWORD" ]; then
  echo "[02-roles] MEMORY_APP_DB_PASSWORD not set; skipping memory_app role creation."
  echo "[02-roles] App will connect as the superuser; RLS policies will be inert."
  exit 0
fi

APP_USER="${MEMORY_APP_DB_USER:-memory_app}"

# The password arrives as an env var; interpolating it into SQL with
# a single-quote literal is safe because secrets.token_urlsafe (what
# the installer uses) never produces a quote character. But we double
# any embedded single quotes defensively so a non-urlsafe password
# cannot break out of the literal.
ESCAPED_PW="${MEMORY_APP_DB_PASSWORD//\'/\'\'}"

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" <<-EOSQL
  DO \$\$
  BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$APP_USER') THEN
          EXECUTE format(
              'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS',
              '$APP_USER', '$ESCAPED_PW'
          );
          RAISE NOTICE 'Created role %', '$APP_USER';
      ELSE
          RAISE NOTICE 'Role % already exists; skipping CREATE', '$APP_USER';
      END IF;
  END
  \$\$;

  -- Grants are idempotent and cheap; re-run every init to heal a
  -- partial state where the role exists but privileges drifted.
  GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO "$APP_USER";
  GRANT USAGE ON SCHEMA public TO "$APP_USER";
  GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "$APP_USER";
  GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "$APP_USER";

  -- Future tables (from new migrations) should also be accessible
  -- without revisiting this script.
  ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "$APP_USER";
  ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT USAGE, SELECT ON SEQUENCES TO "$APP_USER";
EOSQL

echo "[02-roles] memory_app role ready; RLS will enforce against app connections."
