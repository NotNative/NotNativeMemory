"""
Database layer for NotNativeMemory.

Handles all Postgres/pgvector operations: connection pooling, memory storage,
vector similarity search, thermal decay, deduplication, and cap enforcement.
Uses asyncpg for async access.

Thermal model (activity-driven, not time-based):
    Temperature represents relevance, not recency. It only changes when
    the system is actively used - never while idle.

    Memories heat up when:
        - Accessed via search (reheat by REHEAT_DELTA)
        - Merged with a duplicate (reheat by REHEAT_DELTA)

    Memories cool down when:
        - New memories are stored in the same project (displacement cooling)
        - The project approaches its cap (pressure cooling)

    Temperature never decays passively. A memory about a PowerShell gotcha
    is just as valuable whether the project was last used yesterday or
    three weeks ago. Only active use creates thermal pressure.

    Importance modifies cooling rate:
        - critical = never cools (sacred)
        - high = 0.25x cooling rate
        - normal = 1x cooling rate
        - low = 2x cooling rate

    Eviction:
        - Per-project cap (default 500) triggers coldest-first eviction
        - Importance is the primary tiebreaker (low evicted before critical)
"""

import asyncio
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg

_log = logging.getLogger("notnative.memory")

# Connection pool - created once, reused across calls.
_pool: Optional[asyncpg.Pool] = None

# Guards concurrent first-callers of get_pool() in the same process so
# only one coroutine runs the migration check + create_pool branch.
# Without this, two coroutines racing past the `_pool is None` check
# would both apply migrations and both create a pool (leaking one).
_pool_lock = asyncio.Lock()

# -- Thermal constants ------------------------------------------------------

# Initial temperature for new memories
TEMP_INITIAL = 70.0

# Max temperature (only sacred/critical entries could theoretically be 100)
TEMP_MAX = 95.0

# Reheat applied when a memory is accessed via search or merged
REHEAT_DELTA = 10.0

# Cooling applied to non-accessed memories when a new memory is stored.
# This creates thermal pressure: active projects displace stale memories.
DISPLACEMENT_COOL_DELTA = 0.5

# Additional cooling when project is above 80% of cap.
# Increases eviction pressure as the project fills up.
PRESSURE_COOL_DELTA = 0.5

# Pressure threshold: fraction of cap above which pressure cooling kicks in
PRESSURE_THRESHOLD = 0.8

# Cooling rate modifiers by importance
# critical = 0 (never cools), high = 0.25x, normal = 1x, low = 2x
_COOL_RATE = {
    "critical": 0.0,
    "high": 0.25,
    "normal": 1.0,
    "low": 2.0,
}

# Per-project memory cap
PROJECT_MEMORY_CAP = 500

# Deduplication: cosine similarity threshold above which a memory is
# considered a duplicate of an existing one
DEDUP_SIMILARITY_THRESHOLD = 0.92

# Importance weights for search result scoring
_IMPORTANCE_WEIGHT = {
    "critical": 0.15,
    "high": 0.10,
    "normal": 0.0,
    "low": -0.05,
}

# Max pool size - single user tool, keep it small.
_MAX_POOL_SIZE = 5

# Stats cleanup: keep decay_stats rows for this many days
_STATS_RETENTION_DAYS = 90

# Throttle: minimum number of store operations between displacement cycles
# within the same project. Prevents a batch of stores from over-cooling.
_MIN_STORES_BETWEEN_COOL = 3

# Per-project store counter for throttling displacement cooling
_store_counters: Dict[str, int] = {}

# Cap on store counters dict to prevent unbounded growth
_MAX_TRACKED_PROJECTS = 100


# -- Migrations -------------------------------------------------------------

# Resolved once on first call to _get_migrations_dir()
_MIGRATIONS_DIR: Optional[str] = None

# Postgres advisory-lock ID used to serialize migration runs across
# processes that share the same database. The value is arbitrary but
# stable: as long as every process that runs migrations uses the same
# int, only one holds the lock at a time. int64, picked to be distinct
# from anything a sibling tool might use.
_MIGRATION_ADVISORY_LOCK_ID = 0x4E4E4D5F4D494752  # "NNM_MIGR" in ASCII hex


def _get_migrations_dir() -> str:
    """Resolve the migrations directory relative to project root."""
    global _MIGRATIONS_DIR
    if _MIGRATIONS_DIR is None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _MIGRATIONS_DIR = os.path.join(project_root, "config", "migrations")
    return _MIGRATIONS_DIR


async def _run_migrations_on_conn(conn: asyncpg.Connection) -> int:
    """
    Apply any pending migrations from config/migrations/ using the
    given connection. The caller is responsible for opening/closing
    the connection; we just run SQL over it.

    Under default single-role config this connection comes from the
    app pool. Under dual-role config, migrations run over a one-shot
    superuser connection so DDL (CREATE POLICY, ALTER TABLE ENABLE
    RLS, etc.) is always authorized, even when the app role is a
    non-superuser that cannot ALTER tables.

    Creates the schema_migrations tracking table if missing, scans
    *.sql files sorted by name, skips already-applied, applies the
    rest in order. Returns the number applied.

    Concurrency: acquires a Postgres advisory lock at the start so
    that two processes racing on a cold-start database will serialize
    behind each other instead of both trying to apply the same
    migrations and tripping DDL conflicts. The lock is session-scoped
    and released before return. An in-process asyncio lock in
    get_pool() prevents the same race between coroutines in one
    process.
    """
    # Advisory lock: blocks until the other holder releases. Safe to
    # call even when nobody else wants the lock — we just acquire and
    # release it around the migration check.
    await conn.execute(
        "SELECT pg_advisory_lock($1)", _MIGRATION_ADVISORY_LOCK_ID,
    )
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT now()
            )
        """)

        migrations_dir = _get_migrations_dir()
        if not os.path.isdir(migrations_dir):
            _log.debug("No migrations directory at %s", migrations_dir)
            return 0

        migration_files = sorted(
            f for f in os.listdir(migrations_dir)
            if f.endswith(".sql")
        )
        if not migration_files:
            return 0

        applied_rows = await conn.fetch(
            "SELECT filename FROM schema_migrations"
        )
        applied = {row["filename"] for row in applied_rows}

        pending = [f for f in migration_files if f not in applied]
        if not pending:
            return 0

        applied_count = 0
        for filename in pending:
            filepath = os.path.join(migrations_dir, filename)
            with open(filepath, "r", encoding="utf-8") as f:
                sql = f.read()

            try:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES ($1)",
                        filename,
                    )
                applied_count += 1
                _log.info("Applied migration: %s", filename)
            except Exception as exc:
                _log.error("Migration %s failed: %s", filename, exc)
                raise

        return applied_count
    finally:
        await conn.execute(
            "SELECT pg_advisory_unlock($1)", _MIGRATION_ADVISORY_LOCK_ID,
        )


# -- Connection pool --------------------------------------------------------

async def get_pool() -> asyncpg.Pool:
    """
    Get or create the asyncpg connection pool.

    Reads connection details from environment variables. Pool is
    created once and cached for the process lifetime.

    Dual-role support:
        MEMORY_DB_USER / MEMORY_DB_PASSWORD always point at the
        migration/admin role (typically the DB owner / superuser).
        When MEMORY_APP_DB_USER / MEMORY_APP_DB_PASSWORD are set,
        the app pool uses THAT role, and migrations run over a
        separate one-shot connection as the migration role. When
        the app vars are absent, the app pool uses the migration
        role (backward-compatible single-role setup).

        The split lets operators enable Postgres RLS (see
        docs/rls-activation.md): RLS is always bypassed by
        superusers, so the app needs a non-superuser role for the
        policies to bite — but migrations still need superuser
        privilege to ALTER TABLE etc.

    On first call, migrations run BEFORE the app pool is created,
    so the app pool is guaranteed to see a fully-migrated schema.

    Concurrency: the fast path (pool already built) is lock-free.
    The slow path acquires _pool_lock and re-checks _pool inside it
    (double-checked locking) so concurrent first-callers in the same
    process only run migrations and create_pool once. Cross-process
    safety is handled by the pg_advisory_lock in
    _run_migrations_on_conn.
    """
    global _pool
    if _pool is not None:
        return _pool

    async with _pool_lock:
        # Re-check under the lock: another coroutine that was blocked
        # here may have already built the pool.
        if _pool is not None:
            return _pool

        from dotenv import load_dotenv
        load_dotenv()

        host = os.environ.get("MEMORY_DB_HOST", "localhost")
        port = int(os.environ.get("MEMORY_DB_PORT", "5433"))
        database = os.environ.get("MEMORY_DB_NAME", "notnative_memory")

        mig_user = os.environ.get("MEMORY_DB_USER", "memory")
        mig_password = os.environ.get("MEMORY_DB_PASSWORD", "")
        if not mig_password:
            raise ValueError(
                "MEMORY_DB_PASSWORD not set. Run the install script or check .env"
            )

        # App-role credentials default to the migration role so existing
        # installs keep working without touching .env.
        app_user = os.environ.get("MEMORY_APP_DB_USER", "").strip() or mig_user
        app_password = (
            os.environ.get("MEMORY_APP_DB_PASSWORD", "").strip()
            or mig_password
        )
        dual_role = app_user != mig_user

        # Run migrations over a one-shot connection as the migration role.
        # Any failure here (bad SQL in a migration file, DB unreachable,
        # auth, insufficient privilege) is fatal: starting the server
        # with a half-applied or missing schema only produces cryptic
        # runtime errors on the first user-facing tool call. Refusing
        # to start surfaces the real cause at boot where an operator
        # will see it. The cross-process advisory lock inside
        # _run_migrations_on_conn means concurrent cold starts no longer
        # trip DDL-conflict errors that previously motivated the
        # swallow, so real errors are the only errors that land here.
        mig_conn = await asyncpg.connect(
            host=host, port=port, database=database,
            user=mig_user, password=mig_password,
        )
        try:
            applied = await _run_migrations_on_conn(mig_conn)
            if applied:
                _log.info("Applied %d pending migration(s)", applied)
        finally:
            await mig_conn.close()

        # Create the app pool. In single-role setups this is the same
        # role that ran migrations. In dual-role setups it's the
        # non-superuser app role.
        _pool = await asyncpg.create_pool(
            host=host, port=port, database=database,
            user=app_user, password=app_password,
            min_size=1, max_size=_MAX_POOL_SIZE,
            # Fail fast instead of hanging on a saturated pool or a
            # DB that vanished mid-flight. asyncpg's default of 60s is
            # too long for an interactive coding-agent tool call.
            timeout=10.0,
        )
        if dual_role:
            _log.info(
                "DB dual-role: migrations ran as %r, app pool as %r",
                mig_user, app_user,
            )

        return _pool


async def close_pool() -> None:
    """Close the connection pool. Called on server shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# -- Project management -----------------------------------------------------

# Reserved project name conventions for the scope hierarchy.
# "_global" creates a global-scope project (memories apply everywhere).
# "_domain_<name>" creates a domain-scope project (applies to any local
# project that declares <name> in its domains[] array).
_GLOBAL_NAME = "_global"
_DOMAIN_PREFIX = "_domain_"


def _resolve_scope(directory: str) -> tuple:
    """
    Inspect a project identifier and determine its scope + display name.

    Reserved names:
      "_global"         -> scope='global', name='global'
      "_domain_<name>"  -> scope='domain', name='<name>'
      anything else     -> scope='local',  name=basename(directory)

    Returns:
        (scope, name) tuple.
    """
    if directory == _GLOBAL_NAME:
        return "global", "global"
    if directory.startswith(_DOMAIN_PREFIX):
        domain_name = directory[len(_DOMAIN_PREFIX):]
        if not domain_name:
            raise ValueError(
                f"Invalid domain name: {directory} "
                f"(expected format: {_DOMAIN_PREFIX}<name>)"
            )
        return "domain", domain_name
    return "local", os.path.basename(directory.rstrip("/\\"))


async def get_or_create_project(
    directory: str,
    owner_user_id: UUID,
    name: Optional[str] = None,
) -> UUID:
    """
    Get existing project by (directory, owner_user_id), or create one.

    Reserved names auto-set the project's scope:
      "_global"        -> scope='global'
      "_domain_<name>" -> scope='domain'
      real paths       -> scope='local' (default)

    Each user has their own _global, _domain_*, and local project rows.
    Lookup and insert both scope on owner_user_id so two users can hold
    distinct rows with the same `directory` value without colliding.

    Args:
        directory: Project identifier. Usually an absolute path for
            local projects, or a reserved name for global/domain scopes.
        owner_user_id: User the project belongs to. Required.
        name: Override the auto-detected display name.

    Returns:
        Project UUID.
    """
    if not directory:
        raise ValueError("Project directory is required")
    if owner_user_id is None:
        raise ValueError("owner_user_id is required")

    scope, auto_name = _resolve_scope(directory)
    final_name = name or auto_name

    from lib import rls
    pool = await get_pool()
    async with rls.app_conn(pool, owner_user_id) as conn:
        row = await conn.fetchrow(
            "SELECT id FROM projects WHERE directory = $1 AND owner_user_id = $2",
            directory, owner_user_id,
        )
        if row:
            return row["id"]

        row = await conn.fetchrow(
            "INSERT INTO projects (directory, name, scope, owner_user_id) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (directory, owner_user_id) DO UPDATE SET name = $2 "
            "RETURNING id",
            directory, final_name, scope, owner_user_id,
        )
    return row["id"]


async def get_project_info(
    project_id: UUID, owner_user_id: UUID,
) -> Optional[Dict[str, Any]]:
    """Return a project's directory, name, scope, and domains.

    Requires owner_user_id so lookups can't peek at other users'
    project metadata.
    """
    from lib import rls
    pool = await get_pool()
    async with rls.app_conn(pool, owner_user_id) as conn:
        row = await conn.fetchrow(
            "SELECT id, directory, name, scope, domains, created_at "
            "FROM projects WHERE id = $1 AND owner_user_id = $2",
            project_id, owner_user_id,
        )
    if not row:
        return None
    return {
        "id": str(row["id"]),
        "directory": row["directory"],
        "name": row["name"],
        "scope": row["scope"],
        "domains": list(row["domains"]),
        "created_at": row["created_at"].isoformat(),
    }


async def set_project_domains(
    project_id: UUID, owner_user_id: UUID, domains: List[str],
) -> List[str]:
    """
    Set the list of domain names this project pulls from.

    Only meaningful for local-scope projects; globals apply
    per-user-universally and domains don't pull from other domains.

    Args:
        project_id: The local project to configure.
        owner_user_id: Caller identity. Required. Guards against
            configuring someone else's project.
        domains: List of domain names (e.g. ["python", "docker"])
            matching the names of existing _domain_<name> projects
            owned by this user.

    Returns:
        The updated domains list.
    """
    clean = []
    seen = set()
    for d in domains:
        d = d.strip()
        if d and d not in seen:
            clean.append(d)
            seen.add(d)

    from lib import rls
    pool = await get_pool()
    async with rls.app_conn(pool, owner_user_id) as conn:
        row = await conn.fetchrow(
            "UPDATE projects SET domains = $1 "
            "WHERE id = $2 AND owner_user_id = $3 "
            "RETURNING domains",
            clean, project_id, owner_user_id,
        )
    if not row:
        raise ValueError(f"Project {project_id} not found")
    return list(row["domains"])


async def get_visible_project_ids(
    primary_id: UUID, owner_user_id: UUID,
) -> List[UUID]:
    """
    Return the set of project IDs whose memories should be visible when
    searching from a given primary project.

    Composition:
      - Always includes the primary project itself.
      - Always includes every global-scope project OWNED BY the same user.
      - If primary is local-scope with domains[], includes any domain-scope
        project owned by the same user whose name is in that list.
      - If primary is itself global or domain, returns just itself.

    The owner filter is load-bearing: user A's search from their local
    project must not pull user B's `_global` memories even though both
    users may have a row called `_global`.
    """
    from lib import rls
    pool = await get_pool()

    async with rls.app_conn(pool, owner_user_id) as conn:
        primary = await conn.fetchrow(
            "SELECT scope, domains FROM projects WHERE id = $1 AND owner_user_id = $2",
            primary_id, owner_user_id,
        )
        if not primary:
            return [primary_id]

        # Global or domain projects don't expand — search is scoped to them.
        if primary["scope"] in ("global", "domain"):
            return [primary_id]

        # Local: include self + user's globals + user's matching domains
        domains = list(primary["domains"])

        rows = await conn.fetch(
            """SELECT id FROM projects
               WHERE owner_user_id = $3
                 AND (id = $1
                      OR scope = 'global'
                      OR (scope = 'domain' AND name = ANY($2)))""",
            primary_id, domains, owner_user_id,
        )
    return [r["id"] for r in rows]


# -- Deduplication ----------------------------------------------------------

async def _find_duplicate(
    conn: asyncpg.Connection,
    embedding: List[float],
    project_id: UUID,
) -> Optional[Dict[str, Any]]:
    """
    Check if a semantically similar memory already exists in this project.

    Takes a pre-acquired connection so the caller can establish an
    RLS context (via `rls.app_conn`) once and reuse it for the whole
    store_memory flow. Returns the existing memory row if similarity
    exceeds the dedup threshold, or None if no duplicate found.
    """
    row = await conn.fetchrow(
        """SELECT id, content, tags, importance, temperature,
                  1 - (embedding <=> $1::vector) AS similarity
           FROM memories
           WHERE project_id = $2
           ORDER BY embedding <=> $1::vector ASC
           LIMIT 1""",
        str(embedding), project_id,
    )
    if row and float(row["similarity"]) >= DEDUP_SIMILARITY_THRESHOLD:
        return dict(row)
    return None


async def _merge_duplicate(
    conn: asyncpg.Connection,
    existing_id: UUID,
    new_content: str,
    new_embedding: List[float],
    new_tags: List[str],
    new_importance: str,
) -> UUID:
    """
    Merge new memory into an existing duplicate.

    Updates content, embedding, and tags. Importance is upgraded if the
    new one is higher. Temperature is reheated. Access count is preserved.
    """
    await conn.execute(
        """UPDATE memories SET
               content = $2,
               embedding = $3::vector,
               tags = (
                   SELECT array_agg(DISTINCT t)
                   FROM unnest(tags || $4) AS t
               ),
               importance = CASE
                   WHEN array_position(
                       ARRAY['low','normal','high','critical'], $5
                   ) > array_position(
                       ARRAY['low','normal','high','critical'], importance
                   ) THEN $5
                   ELSE importance
               END,
               temperature = LEAST(temperature + $6, $7),
               last_accessed = now()
           WHERE id = $1""",
        existing_id,
        new_content,
        str(new_embedding),
        new_tags,
        new_importance,
        REHEAT_DELTA,
        TEMP_MAX,
    )
    return existing_id


# -- Displacement cooling ---------------------------------------------------

async def _apply_displacement_cooling(
    conn: asyncpg.Connection, project_id: UUID,
) -> int:
    """
    Cool non-critical memories in a project after a new memory is stored.

    This is the core thermal mechanism: storing new knowledge creates
    pressure on existing memories. Memories that are frequently accessed
    stay warm (reheated on search). Memories that are never accessed
    gradually cool and become eviction candidates.

    Only runs every _MIN_STORES_BETWEEN_COOL stores to prevent
    rapid-fire stores from over-cooling.

    Returns number of memories cooled.
    """
    project_key = str(project_id)

    # Throttle: only cool every N stores
    count = _store_counters.get(project_key, 0) + 1
    _store_counters[project_key] = count

    # Bound the tracking dict
    if len(_store_counters) > _MAX_TRACKED_PROJECTS:
        _store_counters.clear()

    if count < _MIN_STORES_BETWEEN_COOL:
        return 0
    _store_counters[project_key] = 0

    # Check if we're under pressure (above 80% cap)
    total = await conn.fetchval(
        "SELECT count(*) FROM memories WHERE project_id = $1",
        project_id,
    )
    pressure_ratio = total / PROJECT_MEMORY_CAP if PROJECT_MEMORY_CAP > 0 else 0
    extra_cool = PRESSURE_COOL_DELTA if pressure_ratio >= PRESSURE_THRESHOLD else 0
    total_cool = DISPLACEMENT_COOL_DELTA + extra_cool

    cooled = 0
    for importance, rate in _COOL_RATE.items():
        if rate == 0.0:
            continue  # critical = sacred, never cool
        delta = total_cool * rate
        if delta <= 0:
            continue
        result = await conn.execute(
            """UPDATE memories SET
                   temperature = GREATEST(temperature - $1, 0.0)
               WHERE project_id = $2
                 AND importance = $3
                 AND temperature > 0.0
                 AND (class IS DISTINCT FROM 'rule')""",
            delta, project_id, importance,
        )
        cooled += int(result.split()[-1]) if result else 0

    if cooled > 0:
        _log.info(
            "Displacement cooling: cooled %d memories in project %s "
            "(delta=%.2f, pressure=%.0f%%)",
            cooled, project_id, total_cool, pressure_ratio * 100,
        )

    return cooled


# -- Cap enforcement --------------------------------------------------------

async def _enforce_cap(conn: asyncpg.Connection, project_id: UUID) -> int:
    """
    Evict coldest memories if project exceeds the cap.

    Critical memories are evicted last (sorted by importance then
    temperature ascending). Returns number of memories evicted.
    """
    count_row = await conn.fetchrow(
        "SELECT count(*) AS cnt FROM memories WHERE project_id = $1",
        project_id,
    )
    total = count_row["cnt"]
    if total <= PROJECT_MEMORY_CAP:
        return 0

    excess = total - PROJECT_MEMORY_CAP
    result = await conn.execute(
        f"""DELETE FROM memories WHERE id IN (
               SELECT id FROM memories
               WHERE project_id = $1
                 AND (class IS DISTINCT FROM 'rule')
               ORDER BY
                   {_importance_rank_sql()} ASC,
                   temperature ASC,
                   last_accessed ASC
               LIMIT $2
           )""",
        project_id, excess,
    )
    evicted = int(result.split()[-1]) if result else 0
    if evicted > 0:
        _log.info("Cap enforcement: evicted %d memories from project %s",
                  evicted, project_id)
    return evicted


# -- Memory storage ---------------------------------------------------------

_VALID_CLASSES = {"rule", "preference", "memory"}


async def store_memory(
    content: str,
    embedding: List[float],
    project_id: UUID,
    owner_user_id: UUID,
    tags: Optional[List[str]] = None,
    importance: str = "normal",
    memory_class: Optional[str] = None,
) -> UUID:
    """
    Store a new memory with deduplication, displacement cooling, and cap
    enforcement.

    If a semantically similar memory exists (cosine similarity
    >= DEDUP_SIMILARITY_THRESHOLD, currently 0.92), the existing memory
    is updated instead of creating a duplicate.
    Storing a new memory applies displacement cooling to the project -
    existing memories cool slightly, creating thermal pressure that
    eventually evicts irrelevant ones.

    Args:
        content: The memory text.
        embedding: 1024-dim embedding vector.
        project_id: UUID of the project this memory belongs to.
        tags: Optional categorization tags.
        importance: One of low, normal, high, critical.

    Returns:
        UUID of the stored (or merged) memory.
    """
    if not content or not content.strip():
        raise ValueError("Memory content cannot be empty")
    if importance not in _IMPORTANCE_WEIGHT:
        raise ValueError(f"Invalid importance: {importance}")
    if memory_class is not None and memory_class not in _VALID_CLASSES:
        raise ValueError(f"Invalid class: {memory_class}")

    from lib.classify import augment_tags
    from lib import rls

    pool = await get_pool()
    clean_tags = augment_tags(tags or [], content)

    # Single connection for the whole store flow. app_conn SETs
    # app.current_user to the owner so dedup SELECTs, INSERTs, cooling
    # UPDATEs, cap-enforcement DELETEs, and stats writes all execute
    # under the same RLS context — and no mid-flow pool-acquire grabs
    # a connection with stale or missing identity.
    async with rls.app_conn(pool, owner_user_id) as conn:
        duplicate = await _find_duplicate(conn, embedding, project_id)
        if duplicate:
            _log.info("Dedup: merging into existing memory %s (sim=%.3f)",
                      duplicate["id"], duplicate["similarity"])
            return await _merge_duplicate(
                conn, duplicate["id"],
                content.strip(), embedding, clean_tags, importance,
            )

        row = await conn.fetchrow(
            """INSERT INTO memories
                   (project_id, content, embedding, tags, importance,
                    class, temperature, owner_user_id)
               VALUES ($1, $2, $3::vector, $4, $5, $6, $7, $8)
               RETURNING id""",
            project_id, content.strip(), str(embedding),
            clean_tags, importance, memory_class, TEMP_INITIAL,
            owner_user_id,
        )

        # Displacement cooling: storing new knowledge pressures existing
        # memories in the same project (same owner, so same RLS scope).
        await _apply_displacement_cooling(conn, project_id)
        await _enforce_cap(conn, project_id)
        await _record_store_stats(conn, project_id)

        return row["id"]


# -- Search -----------------------------------------------------------------

def _build_search_query(
    query_embedding: List[float],
    project_ids: Optional[List[UUID]] = None,
    tags: Optional[List[str]] = None,
    min_importance: Optional[str] = None,
    limit: int = 10,
    owner_user_id: Optional[UUID] = None,
) -> tuple:
    """
    Build the SQL query and params for vector similarity search.

    Joins against projects to surface scope/name so results from global
    or domain scopes can be distinguished from local project hits.
    Owner filter is added when `owner_user_id` is provided; in the
    current codebase every read path requires identity so this should
    always be set.

    Returns:
        (sql_string, params_list) ready for pool.fetch().
    """
    conditions = []
    params: List[Any] = [str(query_embedding)]
    param_idx = 2

    if project_ids:
        conditions.append(f"m.project_id = ANY(${param_idx})")
        params.append(project_ids)
        param_idx += 1

    if owner_user_id is not None:
        conditions.append(f"m.owner_user_id = ${param_idx}")
        params.append(owner_user_id)
        param_idx += 1

    if tags:
        conditions.append(f"m.tags && ${param_idx}")
        params.append(tags)
        param_idx += 1

    if min_importance:
        importance_order = ["low", "normal", "high", "critical"]
        if min_importance in importance_order:
            idx = importance_order.index(min_importance)
            allowed = importance_order[idx:]
            conditions.append(f"m.importance = ANY(${param_idx})")
            params.append(allowed)
            param_idx += 1

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)

    sql = f"""
        SELECT m.id, m.content, m.tags, m.importance, m.class,
               m.temperature,
               m.created_at, m.last_accessed, m.access_count,
               m.project_id,
               p.scope AS project_scope,
               p.name AS project_name,
               1 - (m.embedding <=> $1::vector) AS similarity
        FROM memories m
        JOIN projects p ON p.id = m.project_id
        {where}
        ORDER BY (1 - (m.embedding <=> $1::vector)) +
                 CASE m.importance
                     WHEN 'critical' THEN 0.15
                     WHEN 'high' THEN 0.10
                     WHEN 'normal' THEN 0.0
                     WHEN 'low' THEN -0.05
                     ELSE 0.0
                 END DESC,
                 m.created_at DESC,
                 m.id ASC
        LIMIT ${param_idx}
    """
    return sql, params


_RRF_K = 60  # Standard k for Reciprocal Rank Fusion; rarely worth tuning.
_HYBRID_CANDIDATE_LIMIT = 100  # Per-signal top-K before RRF fusion.


def _build_hybrid_memory_query(
    query_embedding: List[float],
    query_text: str,
    project_ids: List[UUID],
    owner_user_id: UUID,
    tags: Optional[List[str]] = None,
    min_importance: Optional[str] = None,
    limit: int = 10,
) -> tuple:
    """
    Build the RRF-fused hybrid query for memories.

    Runs a vector ranking (cosine distance, ascending) and a text
    ranking (ts_rank_cd, descending) in separate CTEs, each capped at
    _HYBRID_CANDIDATE_LIMIT, then fuses with Reciprocal Rank Fusion:

        rrf_score(d) = sum over rankings R of 1 / (k + rank_R(d))

    Rows that hit only one signal still surface; rows that hit both
    accumulate the contributions.

    Tiebreakers after rrf_score: importance rank DESC (so a critical
    memory beats a normal memory at the same fused rank), then
    created_at DESC, then id ASC.

    Returns (sql, params).
    """
    # Shared filter fragment used by both CTEs so the candidate pools
    # are drawn from the same scope. Param slots: $1 = embedding,
    # $2 = project_ids, $3 = owner_user_id, $4 = query_text.
    filters = [
        "m.project_id = ANY($2)",
        "m.owner_user_id = $3",
    ]
    params: List[Any] = [
        str(query_embedding),
        project_ids,
        owner_user_id,
        query_text,
    ]
    param_idx = 5

    if tags:
        filters.append(f"m.tags && ${param_idx}")
        params.append(tags)
        param_idx += 1

    if min_importance:
        importance_order = ["low", "normal", "high", "critical"]
        if min_importance in importance_order:
            allowed = importance_order[importance_order.index(min_importance):]
            filters.append(f"m.importance = ANY(${param_idx})")
            params.append(allowed)
            param_idx += 1

    filter_sql = " AND ".join(filters)
    params.append(limit)
    limit_idx = param_idx

    importance_rank = _importance_rank_sql("m.importance")

    sql = f"""
        WITH vec_ranked AS (
            SELECT m.id,
                   m.embedding <=> $1::vector AS vec_dist,
                   ROW_NUMBER() OVER (ORDER BY m.embedding <=> $1::vector ASC) AS rnk
              FROM memories m
             WHERE {filter_sql}
               AND m.embedding IS NOT NULL
             ORDER BY m.embedding <=> $1::vector ASC
             LIMIT {_HYBRID_CANDIDATE_LIMIT}
        ),
        text_ranked AS (
            SELECT m.id,
                   ts_rank_cd(m.tsv, plainto_tsquery('english', $4)) AS text_score,
                   ROW_NUMBER() OVER (ORDER BY ts_rank_cd(m.tsv, plainto_tsquery('english', $4)) DESC) AS rnk
              FROM memories m
             WHERE {filter_sql}
               AND m.tsv @@ plainto_tsquery('english', $4)
             ORDER BY text_score DESC
             LIMIT {_HYBRID_CANDIDATE_LIMIT}
        ),
        fused AS (
            SELECT COALESCE(v.id, t.id) AS id,
                   COALESCE(1.0 / ({_RRF_K} + v.rnk), 0)
                 + COALESCE(1.0 / ({_RRF_K} + t.rnk), 0) AS rrf_score,
                   v.vec_dist,
                   t.text_score
              FROM vec_ranked v
              FULL OUTER JOIN text_ranked t USING (id)
        )
        SELECT m.id, m.content, m.tags, m.importance, m.class,
               m.temperature,
               m.created_at, m.last_accessed, m.access_count,
               m.project_id,
               p.scope AS project_scope,
               p.name  AS project_name,
               CASE WHEN f.vec_dist IS NULL THEN NULL
                    ELSE 1 - f.vec_dist
               END AS similarity,
               f.text_score,
               f.rrf_score
          FROM fused f
          JOIN memories m ON m.id = f.id
          JOIN projects p ON p.id = m.project_id
         ORDER BY f.rrf_score DESC,
                  {importance_rank} DESC,
                  m.created_at DESC,
                  m.id ASC
         LIMIT ${limit_idx}
    """
    return sql, params


def _importance_rank_sql(col: str = "importance") -> str:
    """
    SQL fragment mapping ``importance`` to a 0..3 integer rank, suitable
    for ORDER BY. Pair with ``DESC`` for critical-first, ``ASC`` to pick
    the least-important rows first (e.g., cap-enforcement eviction).

    ``col`` lets callers pass the correct table alias (e.g. ``m.importance``).
    """
    return (
        f"CASE {col} "
        "WHEN 'critical' THEN 3 "
        "WHEN 'high' THEN 2 "
        "WHEN 'normal' THEN 1 "
        "WHEN 'low' THEN 0 "
        "END"
    )


def _format_memory_row(row: Any) -> Dict[str, Any]:
    """Format a database row into a memory result dict."""
    result = {
        "id": str(row["id"]),
        "content": row["content"],
        "tags": row["tags"],
        "importance": row["importance"],
        "class": row["class"] if "class" in row.keys() else None,
        "created_at": row["created_at"].isoformat(),
        "last_accessed": row["last_accessed"].isoformat(),
        "access_count": row["access_count"],
    }
    if "temperature" in row.keys():
        result["temperature"] = round(float(row["temperature"]), 1)
    if "similarity" in row.keys() and row["similarity"] is not None:
        # Hybrid rows where only the text signal matched carry NULL
        # similarity; omit the key in that case rather than surfacing
        # a misleading 0.0.
        result["similarity"] = round(float(row["similarity"]), 4)
    if "text_score" in row.keys() and row["text_score"] is not None:
        result["text_score"] = round(float(row["text_score"]), 4)
    if "rrf_score" in row.keys() and row["rrf_score"] is not None:
        result["rrf_score"] = round(float(row["rrf_score"]), 6)
    # Surface project scope so callers can distinguish local/domain/global hits
    if "project_scope" in row.keys():
        result["scope"] = row["project_scope"]
    if "project_name" in row.keys():
        result["project"] = row["project_name"]
    return result


async def search_memories(
    query_embedding: List[float],
    project_id: UUID,
    owner_user_id: UUID,
    tags: Optional[List[str]] = None,
    min_importance: Optional[str] = None,
    limit: int = 10,
    *,
    hybrid: bool = False,
    query_text: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Search memories by similarity.

    Two retrieval modes:

    - Pure vector (default): ranked by cosine similarity plus a
      calibrated importance bonus. Fast, captures semantic meaning,
      can miss exact keyword matches (names, acronyms).
    - Hybrid (``hybrid=True``, also requires ``query_text``): fuses
      the vector ranking with a Postgres full-text ranking via
      Reciprocal Rank Fusion. Often surfaces named entities that
      pure-vector misses. Slightly more expensive per query.

    Scope expansion and ownership filtering are identical in both
    modes: caller sees self + their globals + their matching domains.

    Args:
        query_embedding: 1024-dim query vector. Required.
        project_id: Primary project to search. Required.
        owner_user_id: Caller identity. Required.
        tags: Optional tag filter (any match).
        min_importance: Optional floor (e.g. "high" excludes low and
            normal).
        limit: Max results to return.
        hybrid: Enable BM25-style hybrid retrieval.
        query_text: Raw query string. Only consulted when
            ``hybrid=True``; callers pass the same text they embedded.

    Returns:
        List of memory dicts with similarity, temperature, scope, and
        project name. When hybrid=True, each dict also carries
        rrf_score and text_score so callers can inspect ranking.
    """
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    if hybrid and (not query_text or not query_text.strip()):
        # Silent fallback: caller asked for hybrid but gave nothing to
        # match on text-side. Drop back to vector-only rather than
        # raising, so the overall retrieval still functions.
        hybrid = False

    from lib import rls

    pool = await get_pool()

    # Expand the primary project to its owner-scoped visible set.
    visible_ids = await get_visible_project_ids(project_id, owner_user_id)

    if hybrid:
        sql, params = _build_hybrid_memory_query(
            query_embedding=query_embedding,
            query_text=query_text,
            project_ids=visible_ids,
            owner_user_id=owner_user_id,
            tags=tags,
            min_importance=min_importance,
            limit=limit,
        )
    else:
        sql, params = _build_search_query(
            query_embedding, visible_ids, tags, min_importance, limit,
            owner_user_id=owner_user_id,
        )
    async with rls.app_conn(pool, owner_user_id) as conn:
        rows = await conn.fetch(sql, *params)

        # Reheat accessed memories and update access timestamps
        if rows:
            ids = [row["id"] for row in rows]
            await conn.execute(
                """UPDATE memories SET
                       last_accessed = now(),
                       access_count = access_count + 1,
                       temperature = LEAST(temperature + $2, $3)
                   WHERE id = ANY($1)""",
                ids, REHEAT_DELTA, TEMP_MAX,
            )

    return [_format_memory_row(row) for row in rows]


# -- Forget -----------------------------------------------------------------

async def admin_get_memory(
    memory_id: UUID, owner_user_id: UUID,
) -> Optional[Dict[str, Any]]:
    """
    Fetch a single memory by ID, scoped to the caller's ownership.

    Returns a dict with the fields memory_store / search return plus
    the project directory so the admin UI can render a rescope form.
    None if the memory does not exist or is not owned by the caller.
    """
    from lib import rls
    pool = await get_pool()
    async with rls.app_conn(pool, owner_user_id) as conn:
        row = await conn.fetchrow(
            """SELECT m.id, m.content, m.tags, m.importance, m.class,
                      m.temperature,
                      m.created_at, m.last_accessed, m.access_count,
                      m.project_id,
                      p.scope AS project_scope,
                      p.name AS project_name,
                      p.directory AS project_directory
               FROM memories m
               JOIN projects p ON p.id = m.project_id
               WHERE m.id = $1 AND m.owner_user_id = $2""",
            memory_id, owner_user_id,
        )
    if not row:
        return None
    out = _format_memory_row(row)
    out["project_directory"] = row["project_directory"]
    return out


async def admin_update_memory(
    memory_id: UUID,
    owner_user_id: UUID,
    *,
    content: Optional[str] = None,
    embedding: Optional[List[float]] = None,
    tags: Optional[List[str]] = None,
    importance: Optional[str] = None,
    memory_class: Optional[str] = ...,
    project_id: Optional[UUID] = None,
) -> bool:
    """
    Update a memory in place. Every field is optional; only the ones
    you pass are written. Returns True if the update hit a row, False
    if the memory doesn't exist or belongs to someone else.

    Re-embedding is the caller's responsibility: when the content
    changes, pass a fresh `embedding` alongside the new `content` or
    downstream search results will drift from what the UI shows.
    """
    sets = []
    params: List[Any] = []
    idx = 1

    if content is not None:
        sets.append(f"content = ${idx}")
        params.append(content)
        idx += 1
    if embedding is not None:
        sets.append(f"embedding = ${idx}::vector")
        params.append(str(embedding))
        idx += 1
    if tags is not None:
        sets.append(f"tags = ${idx}")
        params.append(tags)
        idx += 1
    if importance is not None:
        if importance not in _IMPORTANCE_WEIGHT:
            raise ValueError(f"Invalid importance: {importance}")
        sets.append(f"importance = ${idx}")
        params.append(importance)
        idx += 1
    if memory_class is not ...:
        if memory_class is not None and memory_class not in _VALID_CLASSES:
            raise ValueError(f"Invalid class: {memory_class}")
        sets.append(f"class = ${idx}")
        params.append(memory_class)
        idx += 1
    if project_id is not None:
        sets.append(f"project_id = ${idx}")
        params.append(project_id)
        idx += 1

    if not sets:
        return False

    params.extend([memory_id, owner_user_id])
    from lib import rls
    pool = await get_pool()
    async with rls.app_conn(pool, owner_user_id) as conn:
        result = await conn.execute(
            f"""UPDATE memories SET {', '.join(sets)}
                WHERE id = ${idx} AND owner_user_id = ${idx + 1}""",
            *params,
        )
    return result == "UPDATE 1"


async def admin_list_facts(
    owner_user_id: UUID,
    *,
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    scope: Optional[str] = None,
    q: Optional[str] = None,
    include_history: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> tuple:
    """
    Facts list for the admin UI. Scoped to the caller's rows.

    Filters compose with AND. `include_history=False` (the default)
    hides superseded facts (valid_to IS NOT NULL) so the list shows
    only the currently-true assertions. Pass True to see history.

    Returns (fact_dicts, total_count_int).
    """
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    if offset < 0:
        offset = 0

    from lib import rls
    pool = await get_pool()

    conditions = ["f.owner_user_id = $1"]
    params: List[Any] = [owner_user_id]
    idx = 2

    if subject:
        conditions.append(f"f.subject ILIKE ${idx}")
        params.append(f"%{subject}%")
        idx += 1
    if predicate:
        conditions.append(f"f.predicate = ${idx}")
        params.append(predicate)
        idx += 1
    if scope in ("local", "global", "domain"):
        conditions.append(f"p.scope = ${idx}")
        params.append(scope)
        idx += 1
    if q:
        conditions.append(
            f"(f.object ILIKE ${idx} OR f.subject ILIKE ${idx})"
        )
        params.append(f"%{q}%")
        idx += 1
    if not include_history:
        conditions.append("f.valid_to IS NULL")

    where = "WHERE " + " AND ".join(conditions)

    async with rls.app_conn(pool, owner_user_id) as conn:
        count_row = await conn.fetchrow(
            f"""SELECT COUNT(*) AS n FROM facts f
                JOIN projects p ON p.id = f.project_id
                {where}""",
            *params,
        )
        total = int(count_row["n"] or 0)

        limit_placeholder = idx
        offset_placeholder = idx + 1
        params.append(limit)
        params.append(offset)

        rows = await conn.fetch(
            f"""SELECT f.id, f.subject, f.predicate, f.object, f.confidence,
                       f.valid_from, f.valid_to, f.source_memory_id,
                       f.created_at,
                       p.scope AS project_scope,
                       p.name AS project_name,
                       p.directory AS project_directory
                FROM facts f
                JOIN projects p ON p.id = f.project_id
                {where}
                ORDER BY f.valid_from DESC, f.created_at DESC, f.id ASC
                LIMIT ${limit_placeholder} OFFSET ${offset_placeholder}""",
            *params,
        )

    out = []
    for r in rows:
        out.append({
            "id": str(r["id"]),
            "subject": r["subject"],
            "predicate": r["predicate"],
            "object": r["object"],
            "confidence": float(r["confidence"]),
            "valid_from": r["valid_from"].isoformat(),
            "valid_to": r["valid_to"].isoformat() if r["valid_to"] else None,
            "created_at": r["created_at"].isoformat(),
            "scope": r["project_scope"],
            "project": r["project_name"],
            "project_directory": r["project_directory"],
            "source_memory_id": str(r["source_memory_id"]) if r["source_memory_id"] else None,
        })
    return out, total


async def forget_fact(fact_id: UUID, owner_user_id: UUID) -> bool:
    """
    Hard-delete a fact row. Only the owner can delete their own.
    """
    from lib import rls
    pool = await get_pool()
    async with rls.app_conn(pool, owner_user_id) as conn:
        result = await conn.execute(
            "DELETE FROM facts WHERE id = $1 AND owner_user_id = $2",
            fact_id, owner_user_id,
        )
    return result == "DELETE 1"


async def admin_bulk_delete(
    memory_ids: List[UUID], owner_user_id: UUID,
) -> int:
    """
    Delete several memories at once, scoped to the caller. IDs that
    don't exist or belong to another user are silently skipped.

    Returns the number of rows actually deleted.
    """
    if not memory_ids:
        return 0
    from lib import rls
    pool = await get_pool()
    async with rls.app_conn(pool, owner_user_id) as conn:
        result = await conn.execute(
            "DELETE FROM memories WHERE id = ANY($1) AND owner_user_id = $2",
            memory_ids, owner_user_id,
        )
    # asyncpg returns "DELETE N"
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError) as exc:
        _log.debug("admin_bulk_delete: unparseable result %r (%s)", result, exc)
        return 0


async def forget_memory(memory_id: UUID, owner_user_id: UUID) -> bool:
    """
    Delete a memory by ID. Only the owner can delete their own memory;
    deleting someone else's memory returns False without side effects.

    Args:
        memory_id: UUID of the memory to remove.
        owner_user_id: Caller identity. Required.

    Returns:
        True if a memory was deleted, False if not found or not owned.
    """
    from lib import rls
    pool = await get_pool()
    async with rls.app_conn(pool, owner_user_id) as conn:
        result = await conn.execute(
            "DELETE FROM memories WHERE id = $1 AND owner_user_id = $2",
            memory_id, owner_user_id,
        )
    return result == "DELETE 1"


# -- List -------------------------------------------------------------------

# Whitelist of columns the admin UI is allowed to sort by. Any column
# outside this set gets rejected; prevents SQL injection via a hostile
# `sort` query-string value.
_ADMIN_SORT_COLUMNS = {
    "created_at", "last_accessed", "temperature", "access_count",
    "importance",
}


async def admin_list_memories(
    owner_user_id: UUID,
    *,
    project: Optional[str] = None,
    scope: Optional[str] = None,
    tag: Optional[str] = None,
    min_importance: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "created_at",
    order: str = "DESC",
    offset: int = 0,
    limit: int = 20,
) -> tuple:
    """
    Rich list query for the admin UI. Scoped to the caller's rows.

    Filters all compose with AND. The return value is
    (memory_dicts, total_count_before_paging) so the caller can
    render "showing X-Y of N" + paging controls without a second
    query.

    Args:
        owner_user_id: caller identity (required).
        project: exact directory to filter to, or a reserved scope
            name ("_global", "_domain_<name>"). None means "any
            project owned by this user."
        scope: filter by project scope; 'local' | 'global' | 'domain'.
        tag: require this tag to be present on the memory.
        min_importance: 'low' | 'normal' | 'high' | 'critical'; acts
            as a floor (critical-only, high+, normal+, low+).
        q: content substring (case-insensitive ILIKE).
        sort: column from _ADMIN_SORT_COLUMNS.
        order: 'ASC' or 'DESC'.
        offset: pagination offset.
        limit: page size (clamped to 1..100).

    Returns:
        (list_of_memory_dicts, total_count_int).
    """
    if sort not in _ADMIN_SORT_COLUMNS:
        sort = "created_at"
    order = "ASC" if order.upper() == "ASC" else "DESC"
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100
    if offset < 0:
        offset = 0

    from lib import rls
    pool = await get_pool()

    conditions = ["m.owner_user_id = $1"]
    params: List[Any] = [owner_user_id]
    param_idx = 2

    if project:
        conditions.append(f"p.directory = ${param_idx}")
        params.append(project)
        param_idx += 1

    if scope in ("local", "global", "domain"):
        conditions.append(f"p.scope = ${param_idx}")
        params.append(scope)
        param_idx += 1

    if tag:
        conditions.append(f"${param_idx} = ANY(m.tags)")
        params.append(tag)
        param_idx += 1

    if min_importance:
        importance_order = ["low", "normal", "high", "critical"]
        if min_importance in importance_order:
            idx = importance_order.index(min_importance)
            allowed = importance_order[idx:]
            conditions.append(f"m.importance = ANY(${param_idx})")
            params.append(allowed)
            param_idx += 1

    if q:
        conditions.append(f"m.content ILIKE ${param_idx}")
        params.append(f"%{q}%")
        param_idx += 1

    where = "WHERE " + " AND ".join(conditions)

    async with rls.app_conn(pool, owner_user_id) as conn:
        count_row = await conn.fetchrow(
            f"""SELECT COUNT(*) AS n FROM memories m
                JOIN projects p ON p.id = m.project_id
                {where}""",
            *params,
        )
        total = int(count_row["n"] or 0)

        # Append limit + offset as the last two parameters. $N numbering
        # picks up where the filter block left off.
        limit_placeholder = param_idx
        offset_placeholder = param_idx + 1
        params.append(limit)
        params.append(offset)

        # Importance ORDER BY uses a CASE to respect low<normal<high<critical;
        # other columns sort directly.
        if sort == "importance":
            order_clause = f"{_importance_rank_sql('m.importance')} {order}"
        else:
            order_clause = f"m.{sort} {order}"

        rows = await conn.fetch(
            f"""SELECT m.id, m.content, m.tags, m.importance, m.class,
                       m.temperature,
                       m.created_at, m.last_accessed, m.access_count,
                       m.project_id,
                       p.scope AS project_scope,
                       p.name AS project_name,
                       p.directory AS project_directory
                FROM memories m
                JOIN projects p ON p.id = m.project_id
                {where}
                ORDER BY {order_clause}, m.created_at DESC, m.id ASC
                LIMIT ${limit_placeholder} OFFSET ${offset_placeholder}""",
            *params,
        )

    return [_format_memory_row(r) for r in rows], total


async def list_memories(
    owner_user_id: UUID,
    project_id: Optional[UUID] = None,
    tags: Optional[List[str]] = None,
    memory_class: Optional[str] = ...,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    List memories scoped to a specific user, optionally filtered by
    project, tags, and class.

    Args:
        owner_user_id: Caller identity. Required.
        project_id: Optional project filter. When None, lists across
            every project the caller owns.
        tags: Optional tag filter.
        memory_class: Filter by class. Ellipsis = no filter. None =
            unclassified only. String = exact match.
        limit: Max results.

    Returns:
        List of memory dicts ordered by most recently created.
    """
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    from lib import rls
    pool = await get_pool()

    conditions = ["owner_user_id = $1"]
    params: List[Any] = [owner_user_id]
    param_idx = 2

    if project_id:
        conditions.append(f"project_id = ${param_idx}")
        params.append(project_id)
        param_idx += 1

    if tags:
        conditions.append(f"tags && ${param_idx}")
        params.append(tags)
        param_idx += 1

    if memory_class is not ...:
        if memory_class is None:
            conditions.append("class IS NULL")
        else:
            conditions.append(f"class = ${param_idx}")
            params.append(memory_class)
            param_idx += 1

    where = "WHERE " + " AND ".join(conditions)
    params.append(limit)

    async with rls.app_conn(pool, owner_user_id) as conn:
        rows = await conn.fetch(
            f"""SELECT id, content, tags, importance, class, temperature,
                       created_at, last_accessed, access_count
                FROM memories {where}
                ORDER BY created_at DESC, id ASC
                LIMIT ${param_idx}""",
            *params,
        )

    return [_format_memory_row(row) for row in rows]


# -- Context loading --------------------------------------------------------

async def get_context_memories(
    project_id: UUID,
    owner_user_id: UUID,
    max_tokens: int = 500,
) -> List[Dict[str, Any]]:
    """
    Return the most important and hottest memories for a project,
    fitting within a token budget.

    Pulls from the project's visible scope: its own local memories,
    all global memories, and any domain memories matching the project's
    declared domains. Designed for lightweight context injection
    (hooks, session start) — no query needed.

    Args:
        project_id: Project to load context for.
        max_tokens: Approximate token budget. Uses ~4 chars/token estimate.

    Returns:
        List of memory dicts, ordered by importance then temperature,
        annotated with scope and project name.
    """
    if max_tokens < 50:
        max_tokens = 50
    if max_tokens > 2000:
        max_tokens = 2000

    from lib import rls
    pool = await get_pool()

    # Owner-scoped visible set: self + user's globals + user's matching
    # domains. Another user's _global row never surfaces here.
    visible_ids = await get_visible_project_ids(project_id, owner_user_id)

    async with rls.app_conn(pool, owner_user_id) as conn:
        rows = await conn.fetch(
            f"""SELECT m.id, m.content, m.tags, m.importance, m.class,
                      m.temperature,
                      m.created_at, m.last_accessed, m.access_count,
                      m.project_id,
                      p.scope AS project_scope,
                      p.name AS project_name
               FROM memories m
               JOIN projects p ON p.id = m.project_id
               WHERE m.project_id = ANY($1)
                 AND m.owner_user_id = $2
               ORDER BY
                   {_importance_rank_sql('m.importance')} DESC,
                   m.temperature DESC,
                   m.created_at DESC,
                   m.id ASC
               LIMIT 50""",
            visible_ids, owner_user_id,
        )

        # Trim to token budget (~4 chars per token)
        char_budget = max_tokens * 4
        result = []
        chars_used = 0
        for row in rows:
            content_len = len(row["content"])
            if chars_used + content_len > char_budget and result:
                break
            result.append(_format_memory_row(row))
            chars_used += content_len

        # Reheat accessed memories
        if result:
            ids = [row["id"] for row in rows[:len(result)]]
            await conn.execute(
                """UPDATE memories SET
                       last_accessed = now(),
                       access_count = access_count + 1,
                       temperature = LEAST(temperature + $2, $3)
                   WHERE id = ANY($1)""",
                ids, REHEAT_DELTA, TEMP_MAX,
            )

    return result


# -- Facts (temporal knowledge graph) ---------------------------------------

async def add_fact(
    project_id: UUID,
    subject: str,
    predicate: str,
    obj: str,
    owner_user_id: UUID,
    confidence: float = 1.0,
    source_memory_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    """
    Store a fact triple. If a conflicting fact exists (same subject +
    predicate, still valid), auto-invalidate the old one by setting
    its valid_to to now.

    Returns dict with the new fact id and whether a prior fact was superseded.
    """
    from lib import rls
    pool = await get_pool()

    async with rls.app_conn(pool, owner_user_id) as conn:
        # Invalidate any existing valid fact with the same subject +
        # predicate owned by this user. Cross-user facts aren't touched.
        result = await conn.execute(
            """UPDATE facts SET valid_to = now()
               WHERE project_id = $1
                 AND subject = $2
                 AND predicate = $3
                 AND owner_user_id = $4
                 AND valid_to IS NULL""",
            project_id, subject, predicate, owner_user_id,
        )
        superseded = int(result.split()[-1]) if result else 0

        row = await conn.fetchrow(
            """INSERT INTO facts
                   (project_id, subject, predicate, object, confidence,
                    source_memory_id, owner_user_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               RETURNING id, valid_from""",
            project_id, subject, predicate, obj, confidence,
            source_memory_id, owner_user_id,
        )

    return {
        "id": str(row["id"]),
        "valid_from": row["valid_from"].isoformat(),
        "superseded": superseded,
    }


async def query_facts(
    owner_user_id: UUID,
    subject: str,
    project_id: Optional[UUID] = None,
    as_of: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Query facts about a subject, optionally at a point in time.

    Args:
        owner_user_id: Caller identity. Required. Results are filtered
            to this user only.
        subject: The subject to query facts about.
        project_id: Optional project scope.
        as_of: Optional timestamp. If provided, returns facts valid at
            that time. Defaults to now (current facts only).

    Returns:
        List of fact dicts.
    """
    from lib import rls
    pool = await get_pool()

    conditions = ["subject = $1", "owner_user_id = $2"]
    params: List[Any] = [subject, owner_user_id]
    param_idx = 3

    if project_id:
        conditions.append(f"project_id = ${param_idx}")
        params.append(project_id)
        param_idx += 1

    if as_of:
        conditions.append(f"valid_from <= ${param_idx}")
        params.append(as_of)
        param_idx += 1
        conditions.append(
            f"(valid_to IS NULL OR valid_to > ${param_idx})"
        )
        params.append(as_of)
        param_idx += 1
    else:
        conditions.append("valid_to IS NULL")

    where = "WHERE " + " AND ".join(conditions)

    async with rls.app_conn(pool, owner_user_id) as conn:
        rows = await conn.fetch(
            f"""SELECT id, subject, predicate, object, confidence,
                       valid_from, valid_to, source_memory_id, created_at
                FROM facts
                {where}
                ORDER BY predicate, valid_from DESC, id ASC""",
            *params,
        )

    return [
        {
            "id": str(r["id"]),
            "subject": r["subject"],
            "predicate": r["predicate"],
            "object": r["object"],
            "confidence": r["confidence"],
            "valid_from": r["valid_from"].isoformat(),
            "valid_to": r["valid_to"].isoformat() if r["valid_to"] else None,
            "source_memory_id": str(r["source_memory_id"]) if r["source_memory_id"] else None,
        }
        for r in rows
    ]


# -- Stats collection (for future self-tuning) ------------------------------

async def _record_store_stats(conn: asyncpg.Connection, project_id: UUID) -> None:
    """
    Record a snapshot of project metrics after a store operation.

    Collects data for the future self-tuning loop: total count,
    temperature distribution, and access patterns. Only records
    every _MIN_STORES_BETWEEN_COOL stores (piggybacks on the
    displacement cooling throttle).
    """
    # Only record when displacement cooling runs (same throttle)
    project_key = str(project_id)
    if _store_counters.get(project_key, 0) != 0:
        return  # counter was just reset by cooling = time to record

    try:
        row = await conn.fetchrow(
            """SELECT
                   count(*) AS total,
                   avg(temperature) AS avg_temp,
                   avg(access_count) AS avg_access
               FROM memories
               WHERE project_id = $1""",
            project_id,
        )
        if not row or row["total"] == 0:
            return

        await conn.execute(
            """INSERT INTO decay_stats
                   (project_id, total_memories,
                    avg_temperature, avg_access_count)
               VALUES ($1, $2, $3, $4)""",
            project_id,
            row["total"],
            float(row["avg_temp"]) if row["avg_temp"] else 0.0,
            float(row["avg_access"]) if row["avg_access"] else 0.0,
        )

        # Prune old stats
        cutoff = datetime.now(timezone.utc) - timedelta(days=_STATS_RETENTION_DAYS)
        await conn.execute(
            "DELETE FROM decay_stats WHERE recorded_at < $1", cutoff,
        )
    except Exception as exc:
        _log.debug("Stats recording failed: %s", exc)
