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

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg

_log = logging.getLogger("notnative.memory")

# Connection pool - created once, reused across calls.
_pool: Optional[asyncpg.Pool] = None

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


def _get_migrations_dir() -> str:
    """Resolve the migrations directory relative to project root."""
    global _MIGRATIONS_DIR
    if _MIGRATIONS_DIR is None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _MIGRATIONS_DIR = os.path.join(project_root, "config", "migrations")
    return _MIGRATIONS_DIR


async def _run_migrations(pool: asyncpg.Pool) -> int:
    """
    Apply any pending migrations from config/migrations/.

    Creates the schema_migrations tracking table if it doesn't exist
    (self-bootstrapping). Scans for *.sql files sorted by name,
    skips any already recorded, and applies the rest in order.

    Returns the number of migrations applied.
    """
    # Bootstrap: ensure the tracking table exists
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ DEFAULT now()
        )
    """)

    migrations_dir = _get_migrations_dir()
    if not os.path.isdir(migrations_dir):
        _log.debug("No migrations directory at %s", migrations_dir)
        return 0

    # List migration files sorted by name (numeric prefix = order)
    migration_files = sorted(
        f for f in os.listdir(migrations_dir)
        if f.endswith(".sql")
    )
    if not migration_files:
        return 0

    # Get already-applied migrations
    applied_rows = await pool.fetch(
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
            async with pool.acquire() as conn:
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


# -- Connection pool --------------------------------------------------------

async def get_pool() -> asyncpg.Pool:
    """
    Get or create the asyncpg connection pool.

    Reads connection details from environment variables.
    Pool is created once and cached for the process lifetime.
    On first creation, applies any pending database migrations.
    """
    global _pool
    if _pool is not None:
        return _pool

    from dotenv import load_dotenv
    load_dotenv()

    host = os.environ.get("MEMORY_DB_HOST", "localhost")
    port = int(os.environ.get("MEMORY_DB_PORT", "5433"))
    database = os.environ.get("MEMORY_DB_NAME", "notnative_memory")
    user = os.environ.get("MEMORY_DB_USER", "memory")
    password = os.environ.get("MEMORY_DB_PASSWORD", "")

    if not password:
        raise ValueError(
            "MEMORY_DB_PASSWORD not set. Run the install script or check .env"
        )

    _pool = await asyncpg.create_pool(
        host=host, port=port, database=database,
        user=user, password=password,
        min_size=1, max_size=_MAX_POOL_SIZE,
    )

    # Apply any pending migrations on first connect
    try:
        applied = await _run_migrations(_pool)
        if applied:
            _log.info("Applied %d pending migration(s)", applied)
    except Exception as exc:
        _log.error("Migration check failed: %s", exc)
        # Don't prevent startup — the server can still work with
        # existing schema. Missing tables will error on first use.

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
    name: Optional[str] = None,
    owner_user_id: Optional[UUID] = None,
) -> UUID:
    """
    Get existing project by directory, or create a new one.

    Reserved names auto-set the project's scope:
      "_global"        -> scope='global'
      "_domain_<name>" -> scope='domain'
      real paths       -> scope='local' (default)

    Args:
        directory: Project identifier. Usually an absolute path for local
            projects, or a reserved name for global/domain scopes.
        name: Override the auto-detected display name.
        owner_user_id: User who owns this project (for new rows). When
            None, the row is "unowned" (nullable FK, legacy stdio
            behavior). Existing rows keep whatever owner they have —
            lookup never overwrites.

    Returns:
        Project UUID.
    """
    if not directory:
        raise ValueError("Project directory is required")

    scope, auto_name = _resolve_scope(directory)
    final_name = name or auto_name

    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id FROM projects WHERE directory = $1", directory,
    )
    if row:
        return row["id"]

    row = await pool.fetchrow(
        "INSERT INTO projects (directory, name, scope, owner_user_id) "
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (directory) DO UPDATE SET name = $2 "
        "RETURNING id",
        directory, final_name, scope, owner_user_id,
    )
    return row["id"]


async def get_project_info(project_id: UUID) -> Optional[Dict[str, Any]]:
    """Return a project's directory, name, scope, and domains."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, directory, name, scope, domains, created_at "
        "FROM projects WHERE id = $1",
        project_id,
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
    project_id: UUID, domains: List[str],
) -> List[str]:
    """
    Set the list of domain names this project pulls from.

    Only meaningful for local-scope projects — globals apply universally
    and domains don't pull from other domains.

    Args:
        project_id: The local project to configure.
        domains: List of domain names (e.g. ["python", "docker"]) matching
            the names of existing _domain_<name> projects.

    Returns:
        The updated domains list.
    """
    # Deduplicate, strip, drop empties
    clean = []
    seen = set()
    for d in domains:
        d = d.strip()
        if d and d not in seen:
            clean.append(d)
            seen.add(d)

    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE projects SET domains = $1 WHERE id = $2 "
        "RETURNING domains",
        clean, project_id,
    )
    if not row:
        raise ValueError(f"Project {project_id} not found")
    return list(row["domains"])


async def get_visible_project_ids(primary_id: UUID) -> List[UUID]:
    """
    Return the set of project IDs whose memories should be visible when
    searching from a given primary project.

    Composition:
      - Always includes the primary project itself.
      - Always includes every global-scope project.
      - If primary is local-scope with domains[], includes any
        domain-scope project whose name is in that list.
      - If primary is itself global or domain, returns just itself
        (global/domain projects don't pull from each other).
    """
    pool = await get_pool()

    primary = await pool.fetchrow(
        "SELECT scope, domains FROM projects WHERE id = $1",
        primary_id,
    )
    if not primary:
        return [primary_id]

    # Global or domain projects don't expand — search is scoped to them.
    if primary["scope"] in ("global", "domain"):
        return [primary_id]

    # Local: include self + all globals + matching domains
    domains = list(primary["domains"])

    rows = await pool.fetch(
        """SELECT id FROM projects
           WHERE id = $1
              OR scope = 'global'
              OR (scope = 'domain' AND name = ANY($2))""",
        primary_id, domains,
    )
    return [r["id"] for r in rows]


# -- Deduplication ----------------------------------------------------------

async def _find_duplicate(
    pool: asyncpg.Pool,
    embedding: List[float],
    project_id: UUID,
) -> Optional[Dict[str, Any]]:
    """
    Check if a semantically similar memory already exists in this project.

    Returns the existing memory row if similarity exceeds the dedup
    threshold, or None if no duplicate found.
    """
    row = await pool.fetchrow(
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
    pool: asyncpg.Pool,
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
    await pool.execute(
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
    pool: asyncpg.Pool, project_id: UUID,
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
    total = await pool.fetchval(
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
        result = await pool.execute(
            """UPDATE memories SET
                   temperature = GREATEST(temperature - $1, 0.0)
               WHERE project_id = $2
                 AND importance = $3
                 AND temperature > 0.0""",
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

async def _enforce_cap(pool: asyncpg.Pool, project_id: UUID) -> int:
    """
    Evict coldest memories if project exceeds the cap.

    Critical memories are evicted last (sorted by importance then
    temperature ascending). Returns number of memories evicted.
    """
    count_row = await pool.fetchrow(
        "SELECT count(*) AS cnt FROM memories WHERE project_id = $1",
        project_id,
    )
    total = count_row["cnt"]
    if total <= PROJECT_MEMORY_CAP:
        return 0

    excess = total - PROJECT_MEMORY_CAP
    result = await pool.execute(
        """DELETE FROM memories WHERE id IN (
               SELECT id FROM memories
               WHERE project_id = $1
               ORDER BY
                   CASE importance
                       WHEN 'critical' THEN 3
                       WHEN 'high' THEN 2
                       WHEN 'normal' THEN 1
                       WHEN 'low' THEN 0
                   END ASC,
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

async def store_memory(
    content: str,
    embedding: List[float],
    project_id: UUID,
    tags: Optional[List[str]] = None,
    importance: str = "normal",
    owner_user_id: Optional[UUID] = None,
) -> UUID:
    """
    Store a new memory with deduplication, displacement cooling, and cap
    enforcement.

    If a semantically similar memory exists (cosine similarity > 0.92),
    the existing memory is updated instead of creating a duplicate.
    Storing a new memory applies displacement cooling to the project -
    existing memories cool slightly, creating thermal pressure that
    eventually evicts irrelevant ones.

    Args:
        content: The memory text.
        embedding: 768-dim embedding vector.
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

    from lib.classify import augment_tags

    pool = await get_pool()
    clean_tags = augment_tags(tags or [], content)

    # Check for duplicate
    duplicate = await _find_duplicate(pool, embedding, project_id)
    if duplicate:
        _log.info("Dedup: merging into existing memory %s (sim=%.3f)",
                  duplicate["id"], duplicate["similarity"])
        return await _merge_duplicate(
            pool, duplicate["id"],
            content.strip(), embedding, clean_tags, importance,
        )

    # Insert new memory
    row = await pool.fetchrow(
        """INSERT INTO memories
               (project_id, content, embedding, tags, importance,
                temperature, owner_user_id)
           VALUES ($1, $2, $3::vector, $4, $5, $6, $7)
           RETURNING id""",
        project_id, content.strip(), str(embedding),
        clean_tags, importance, TEMP_INITIAL, owner_user_id,
    )

    # Displacement cooling: storing new knowledge pressures existing memories
    await _apply_displacement_cooling(pool, project_id)

    # Enforce per-project cap (evict coldest if over limit)
    await _enforce_cap(pool, project_id)

    # Record stats for future self-tuning
    await _record_store_stats(pool, project_id)

    return row["id"]


# -- Search -----------------------------------------------------------------

def _build_search_query(
    query_embedding: List[float],
    project_ids: Optional[List[UUID]] = None,
    tags: Optional[List[str]] = None,
    min_importance: Optional[str] = None,
    limit: int = 10,
) -> tuple:
    """
    Build the SQL query and params for vector similarity search.

    Joins against projects to surface scope/name so results from global
    or domain scopes can be distinguished from local project hits.

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
        SELECT m.id, m.content, m.tags, m.importance, m.temperature,
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
                 END DESC
        LIMIT ${param_idx}
    """
    return sql, params


def _format_memory_row(row: Any) -> Dict[str, Any]:
    """Format a database row into a memory result dict."""
    result = {
        "id": str(row["id"]),
        "content": row["content"],
        "tags": row["tags"],
        "importance": row["importance"],
        "created_at": row["created_at"].isoformat(),
        "last_accessed": row["last_accessed"].isoformat(),
        "access_count": row["access_count"],
    }
    if "temperature" in row.keys():
        result["temperature"] = round(float(row["temperature"]), 1)
    if "similarity" in row.keys():
        result["similarity"] = round(float(row["similarity"]), 4)
    # Surface project scope so callers can distinguish local/domain/global hits
    if "project_scope" in row.keys():
        result["scope"] = row["project_scope"]
    if "project_name" in row.keys():
        result["project"] = row["project_name"]
    return result


async def search_memories(
    query_embedding: List[float],
    project_id: Optional[UUID] = None,
    tags: Optional[List[str]] = None,
    min_importance: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    Search memories by vector similarity with importance weighting.

    When a local project is provided, results automatically include
    every global-scope memory plus any domain-scope memories matching
    that project's declared domains. Global and domain projects search
    only within themselves.

    Results are ranked by: (1 - cosine_distance) + importance_bonus.
    Returned memories are reheated (temperature increases).

    Args:
        query_embedding: 768-dim query vector.
        project_id: Primary project to search. If None, searches all
            projects regardless of scope.
        tags: Optional tag filter (any match).
        min_importance: Optional floor (e.g. "high" excludes low and normal).
        limit: Max results to return.

    Returns:
        List of memory dicts with similarity, temperature, scope, and
        project name so callers can see where each hit came from.
    """
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    pool = await get_pool()

    # Expand the primary project to its visible set (self + globals +
    # matching domains). Passing None means cross-project search.
    visible_ids = None
    if project_id is not None:
        visible_ids = await get_visible_project_ids(project_id)

    sql, params = _build_search_query(
        query_embedding, visible_ids, tags, min_importance, limit,
    )
    rows = await pool.fetch(sql, *params)

    # Reheat accessed memories and update access timestamps
    if rows:
        ids = [row["id"] for row in rows]
        await pool.execute(
            """UPDATE memories SET
                   last_accessed = now(),
                   access_count = access_count + 1,
                   temperature = LEAST(temperature + $2, $3)
               WHERE id = ANY($1)""",
            ids, REHEAT_DELTA, TEMP_MAX,
        )

    return [_format_memory_row(row) for row in rows]


# -- Forget -----------------------------------------------------------------

async def forget_memory(memory_id: UUID) -> bool:
    """
    Delete a memory by ID.

    Args:
        memory_id: UUID of the memory to remove.

    Returns:
        True if a memory was deleted, False if not found.
    """
    pool = await get_pool()
    result = await pool.execute(
        "DELETE FROM memories WHERE id = $1", memory_id,
    )
    return result == "DELETE 1"


# -- List -------------------------------------------------------------------

async def list_memories(
    project_id: Optional[UUID] = None,
    tags: Optional[List[str]] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    List memories, optionally filtered by project and tags.

    Args:
        project_id: Optional project scope.
        tags: Optional tag filter.
        limit: Max results.

    Returns:
        List of memory dicts ordered by most recently created.
    """
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    pool = await get_pool()

    conditions = []
    params: List[Any] = []
    param_idx = 1

    if project_id:
        conditions.append(f"project_id = ${param_idx}")
        params.append(project_id)
        param_idx += 1

    if tags:
        conditions.append(f"tags && ${param_idx}")
        params.append(tags)
        param_idx += 1

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    params.append(limit)

    rows = await pool.fetch(
        f"""SELECT id, content, tags, importance, temperature, created_at,
                   last_accessed, access_count
            FROM memories {where}
            ORDER BY created_at DESC
            LIMIT ${param_idx}""",
        *params,
    )

    return [_format_memory_row(row) for row in rows]


# -- Context loading --------------------------------------------------------

async def get_context_memories(
    project_id: UUID,
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

    pool = await get_pool()

    # Expand to visible project set (self + globals + matching domains)
    visible_ids = await get_visible_project_ids(project_id)

    # Fetch more than we need, then trim to budget
    rows = await pool.fetch(
        """SELECT m.id, m.content, m.tags, m.importance, m.temperature,
                  m.created_at, m.last_accessed, m.access_count,
                  m.project_id,
                  p.scope AS project_scope,
                  p.name AS project_name
           FROM memories m
           JOIN projects p ON p.id = m.project_id
           WHERE m.project_id = ANY($1)
           ORDER BY
               CASE m.importance
                   WHEN 'critical' THEN 3
                   WHEN 'high' THEN 2
                   WHEN 'normal' THEN 1
                   WHEN 'low' THEN 0
               END DESC,
               m.temperature DESC
           LIMIT 50""",
        visible_ids,
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
        await pool.execute(
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
    confidence: float = 1.0,
    source_memory_id: Optional[UUID] = None,
    owner_user_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    """
    Store a fact triple. If a conflicting fact exists (same subject +
    predicate, still valid), auto-invalidate the old one by setting
    its valid_to to now.

    Returns dict with the new fact id and whether a prior fact was superseded.
    """
    pool = await get_pool()

    # Invalidate any existing valid fact with the same subject + predicate
    result = await pool.execute(
        """UPDATE facts SET valid_to = now()
           WHERE project_id = $1
             AND subject = $2
             AND predicate = $3
             AND valid_to IS NULL""",
        project_id, subject, predicate,
    )
    superseded = int(result.split()[-1]) if result else 0

    row = await pool.fetchrow(
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
    project_id: Optional[UUID],
    subject: str,
    as_of: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Query facts about a subject, optionally at a point in time.

    Args:
        project_id: Optional project scope.
        subject: The subject to query facts about.
        as_of: Optional timestamp. If provided, returns facts valid at
            that time. Defaults to now (current facts only).

    Returns:
        List of fact dicts.
    """
    pool = await get_pool()

    conditions = ["subject = $1"]
    params: List[Any] = [subject]
    param_idx = 2

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

    rows = await pool.fetch(
        f"""SELECT id, subject, predicate, object, confidence,
                   valid_from, valid_to, source_memory_id, created_at
            FROM facts
            {where}
            ORDER BY predicate, valid_from DESC""",
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

async def _record_store_stats(pool: asyncpg.Pool, project_id: UUID) -> None:
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
        row = await pool.fetchrow(
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

        await pool.execute(
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
        await pool.execute(
            "DELETE FROM decay_stats WHERE recorded_at < $1", cutoff,
        )
    except Exception as exc:
        _log.debug("Stats recording failed: %s", exc)
