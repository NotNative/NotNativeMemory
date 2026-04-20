"""
Ensure the non-superuser memory_app role exists and has correct
privileges. Idempotent — safe to run on fresh installs, upgrades,
and re-installs.

The Docker full-install path has this same logic in docker/init/
02-roles.sh which runs at POSTGRES_USER init time. This script is
the equivalent for:

  - Docker server-mode (remote DB) installs, where the init script
    never runs because the Postgres container is not ours.
  - Native python installs, same reason.
  - Upgrade scenarios on full mode where the DB volume predates the
    addition of 02-roles.sh (init scripts don't run on existing
    data dirs).

Connects as MEMORY_DB_USER (expected to have CREATE ROLE privilege;
typically the DB owner / superuser). If that privilege is missing,
emits a warning and exits 0 so the install proceeds in single-role
(no-RLS-enforcement) mode. Operators can create the role manually
following docs/rls-activation.md and restart the server.

No-op when MEMORY_APP_DB_USER / MEMORY_APP_DB_PASSWORD are unset —
the operator has deliberately opted out of RLS enforcement.
"""

import asyncio
import os
import sys

try:
    import asyncpg
except ImportError:
    print("asyncpg not installed; cannot ensure memory_app role", file=sys.stderr)
    sys.exit(0)  # non-fatal — install proceeds without RLS enforcement

# Load .env so this script works equally from the host python path
# (where the installer just wrote .env and hasn't re-exec'd the shell)
# and from inside the Docker mcp container (where env_file already
# hydrated everything, so load_dotenv is a no-op).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # dotenv isn't in the container (it's a host-side requirement) but
    # env_file already populated os.environ, so skipping is fine.
    pass


async def ensure_role() -> int:
    app_user = os.environ.get("MEMORY_APP_DB_USER", "").strip()
    app_password = os.environ.get("MEMORY_APP_DB_PASSWORD", "").strip()
    if not app_user or not app_password:
        print("MEMORY_APP_DB_USER/PASSWORD unset; skipping role creation")
        print("App will connect as MEMORY_DB_USER; RLS policies will be inert.")
        return 0

    try:
        conn = await asyncpg.connect(
            host=os.environ["MEMORY_DB_HOST"],
            port=int(os.environ["MEMORY_DB_PORT"]),
            database=os.environ["MEMORY_DB_NAME"],
            user=os.environ["MEMORY_DB_USER"],
            password=os.environ["MEMORY_DB_PASSWORD"],
            timeout=10,
        )
    except Exception as exc:
        print(f"ERROR: could not connect to DB as MEMORY_DB_USER: {exc}", file=sys.stderr)
        return 1

    # Defensive quote-escape even though token_urlsafe never produces
    # single quotes. Double-single-quote is PostgreSQL's escape for
    # string literals.
    escaped_pw = app_password.replace("'", "''")
    dbname = os.environ["MEMORY_DB_NAME"]

    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = $1", app_user,
        )
        if not exists:
            await conn.execute(
                f'CREATE ROLE "{app_user}" LOGIN PASSWORD \'{escaped_pw}\' '
                f"NOSUPERUSER NOBYPASSRLS"
            )
            print(f"Created role {app_user}")
        else:
            # Refresh the password in case the installer regenerated it.
            await conn.execute(
                f'ALTER ROLE "{app_user}" WITH PASSWORD \'{escaped_pw}\''
            )
            print(f"Role {app_user} exists; refreshed password")

        # Grants are idempotent. Run every time so a manually-revoked
        # privilege gets restored by the installer without operator
        # intervention.
        await conn.execute(f'GRANT CONNECT ON DATABASE "{dbname}" TO "{app_user}"')
        await conn.execute(f'GRANT USAGE ON SCHEMA public TO "{app_user}"')
        await conn.execute(
            f'GRANT SELECT, INSERT, UPDATE, DELETE '
            f'ON ALL TABLES IN SCHEMA public TO "{app_user}"'
        )
        await conn.execute(
            f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{app_user}"'
        )
        await conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{app_user}"'
        )
        await conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
            f'GRANT USAGE, SELECT ON SEQUENCES TO "{app_user}"'
        )
        print(f"Granted privileges on public schema to {app_user}")
        print("RLS enforcement is active against app connections.")
        return 0

    except asyncpg.InsufficientPrivilegeError as exc:
        print(
            f"WARNING: MEMORY_DB_USER lacks privilege to CREATE ROLE: {exc}",
            file=sys.stderr,
        )
        print(
            "App will fall back to MEMORY_DB_USER at runtime; RLS inert.",
            file=sys.stderr,
        )
        print(
            "See docs/rls-activation.md to create the role manually.",
            file=sys.stderr,
        )
        return 0  # non-fatal; install proceeds

    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(ensure_role()))
