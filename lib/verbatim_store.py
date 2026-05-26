"""
Verbatim chunk store — pgvector-backed primary-source transcript layer.

The "drawers" half of a mempalace-style two-layer memory stack: append-only
per-turn chunks captured by the NNA-side hook (turn:post, tool.call.post,
user.prompt.submit). Search uses the same vector(1024) + tsvector + RRF
pattern as `memories`, but the table is separate so:

  * verbatim writes never trigger memories-side dedup / displacement / cap
    enforcement;
  * memory_search rankings never include transcript noise;
  * the two layers can be retention-managed independently.

Capture is idempotent on (owner_user_id, session_id, chunk_index) — the
NNA-side hook can safely re-send a chunk it isn't sure landed.

Outcome is stamped after the fact via `stamp_outcome` once the curator /
post-turn classifier decides success vs. failure. NULL means unknown.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import asyncpg

_log = logging.getLogger("notnative.verbatim")

# Standard k for Reciprocal Rank Fusion; matches memories-side _RRF_K.
_RRF_K = 60

# Per-signal top-K before RRF fusion. Verbatim corpora are larger than the
# distilled memories table (every turn is a row), so a wider candidate pool
# keeps recall reasonable when one signal pulls weak matches.
_HYBRID_CANDIDATE_LIMIT = 200

# Valid outcome stamps. Kept small and explicit so callers can't smuggle
# free-form strings into the column.
_VALID_OUTCOMES = frozenset({"success", "failure", "aborted", "unknown"})


async def store_chunk(
    content: str,
    embedding: List[float],
    *,
    session_id: str,
    chunk_index: int,
    project_id: UUID,
    owner_user_id: UUID,
    source_event: str,
    topic: Optional[str] = None,
    agent: Optional[str] = None,
    is_error: bool = False,
    loaded_skills: Optional[List[str]] = None,
    mission_id: Optional[str] = None,
    mission_type: Optional[str] = None,
) -> Tuple[UUID, bool]:
    """
    Append a verbatim chunk. Idempotent on (owner_user_id, session_id,
    chunk_index): re-sending the same coordinates is a no-op and returns
    the existing row's id.

    Returns:
        (chunk_id, inserted) — `inserted` is False when the row already
        existed and we returned its id without writing.
    """
    if not content or not content.strip():
        raise ValueError("Verbatim content cannot be empty")
    if not session_id:
        raise ValueError("session_id is required")
    if chunk_index < 0:
        raise ValueError("chunk_index must be >= 0")
    if not source_event:
        raise ValueError("source_event is required")

    from lib import rls
    from lib.db import get_pool

    pool = await get_pool()
    async with rls.app_conn(pool, owner_user_id) as conn:
        row = await conn.fetchrow(
            """INSERT INTO verbatim_chunks
                   (project_id, owner_user_id, session_id, chunk_index,
                    content, embedding,
                    topic, agent, source_event, is_error,
                    loaded_skills, mission_id, mission_type)
               VALUES ($1, $2, $3, $4, $5, $6::vector,
                       $7, $8, $9, $10, $11, $12, $13)
               ON CONFLICT (owner_user_id, session_id, chunk_index)
               DO NOTHING
               RETURNING id""",
            project_id, owner_user_id, session_id, chunk_index,
            content.strip(), str(embedding),
            topic, agent, source_event, is_error,
            list(loaded_skills or []), mission_id, mission_type,
        )
        if row is not None:
            return row["id"], True

        # Conflict path: pull the existing row's id.
        existing = await conn.fetchrow(
            """SELECT id FROM verbatim_chunks
                WHERE owner_user_id = $1
                  AND session_id = $2
                  AND chunk_index = $3""",
            owner_user_id, session_id, chunk_index,
        )
        if existing is None:
            # Shouldn't happen — the conflict implies a row exists — but
            # if RLS hid it we treat that as "not ours" and surface the
            # condition rather than fabricating an id.
            raise RuntimeError(
                "verbatim_chunks ON CONFLICT fired but follow-up SELECT "
                "returned no row; check RLS context",
            )
        return existing["id"], False


# -- Search -----------------------------------------------------------------

def _build_verbatim_filters(
    owner_user_id: UUID,
    project_ids: List[UUID],
    *,
    session_id: Optional[str],
    topic: Optional[str],
    mission_id: Optional[str],
    is_error: Optional[bool],
    source_events: Optional[List[str]],
    outcomes: Optional[List[str]],
) -> Tuple[List[str], List[Any], int]:
    """
    Build the WHERE-clause fragments + bound params shared by both the
    vector-only and hybrid query builders.

    Returns (filter_sql_fragments, params, next_param_idx). Param slots
    $1..$4 are reserved by callers for embedding, project_ids,
    owner_user_id, and (for hybrid) query_text; this helper starts
    binding at $5 onwards.
    """
    filters: List[str] = [
        "v.project_id = ANY($2)",
        "v.owner_user_id = $3",
    ]
    params: List[Any] = []
    idx = 5

    if session_id is not None:
        filters.append(f"v.session_id = ${idx}")
        params.append(session_id)
        idx += 1
    if topic is not None:
        filters.append(f"v.topic = ${idx}")
        params.append(topic)
        idx += 1
    if mission_id is not None:
        filters.append(f"v.mission_id = ${idx}")
        params.append(mission_id)
        idx += 1
    if is_error is not None:
        filters.append(f"v.is_error = ${idx}")
        params.append(is_error)
        idx += 1
    if source_events:
        filters.append(f"v.source_event = ANY(${idx})")
        params.append(list(source_events))
        idx += 1
    if outcomes:
        filters.append(f"v.outcome = ANY(${idx})")
        params.append(list(outcomes))
        idx += 1

    return filters, params, idx


def _format_chunk_row(row: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": str(row["id"]),
        "content": row["content"],
        "session_id": row["session_id"],
        "chunk_index": row["chunk_index"],
        "source_event": row["source_event"],
        "topic": row["topic"],
        "agent": row["agent"],
        "is_error": row["is_error"],
        "loaded_skills": list(row["loaded_skills"] or []),
        "mission_id": row["mission_id"],
        "mission_type": row["mission_type"],
        "outcome": row["outcome"],
        "ts": row["ts"].isoformat(),
    }
    if "similarity" in row.keys() and row["similarity"] is not None:
        out["similarity"] = round(float(row["similarity"]), 4)
    if "text_score" in row.keys() and row["text_score"] is not None:
        out["text_score"] = round(float(row["text_score"]), 4)
    if "rrf_score" in row.keys() and row["rrf_score"] is not None:
        out["rrf_score"] = round(float(row["rrf_score"]), 6)
    return out


async def search_chunks(
    *,
    query_embedding: List[float],
    project_id: UUID,
    owner_user_id: UUID,
    query_text: Optional[str] = None,
    hybrid: bool = True,
    session_id: Optional[str] = None,
    topic: Optional[str] = None,
    mission_id: Optional[str] = None,
    is_error: Optional[bool] = None,
    source_events: Optional[List[str]] = None,
    outcomes: Optional[List[str]] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    Retrieve verbatim chunks by similarity. Hybrid (vector + Postgres
    full-text via RRF) is the default; pass `hybrid=False` for pure
    vector ranking when the caller has no useful keyword signal.

    Scope: a single project's chunks. Cross-project recall would mix
    unrelated transcripts and isn't a real use case for the dreaming
    loop today, so we don't expand to globals/domains the way
    memory_search does.

    Tiebreaking: rrf_score DESC (hybrid) or similarity DESC (vector),
    then ts DESC, then id ASC.
    """
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    if hybrid and (not query_text or not query_text.strip()):
        # No text-side signal to fuse — fall back to vector-only rather
        # than raising. Same silent fallback shape db.search_memories
        # uses.
        hybrid = False

    from lib import rls
    from lib.db import get_pool

    pool = await get_pool()

    filters, extra_params, idx = _build_verbatim_filters(
        owner_user_id, [project_id],
        session_id=session_id,
        topic=topic,
        mission_id=mission_id,
        is_error=is_error,
        source_events=source_events,
        outcomes=outcomes,
    )

    if hybrid:
        params: List[Any] = [
            str(query_embedding),
            [project_id],
            owner_user_id,
            query_text,
        ]
        params.extend(extra_params)
        params.append(limit)
        limit_idx = idx
        filter_sql = " AND ".join(filters)

        sql = f"""
            WITH vec_ranked AS (
                SELECT v.id,
                       v.embedding <=> $1::vector AS vec_dist,
                       ROW_NUMBER() OVER (
                           ORDER BY v.embedding <=> $1::vector ASC
                       ) AS rnk
                  FROM verbatim_chunks v
                 WHERE {filter_sql}
                   AND v.embedding IS NOT NULL
                 ORDER BY v.embedding <=> $1::vector ASC
                 LIMIT {_HYBRID_CANDIDATE_LIMIT}
            ),
            text_ranked AS (
                SELECT v.id,
                       ts_rank_cd(v.tsv, plainto_tsquery('english', $4))
                           AS text_score,
                       ROW_NUMBER() OVER (
                           ORDER BY ts_rank_cd(
                               v.tsv, plainto_tsquery('english', $4)
                           ) DESC
                       ) AS rnk
                  FROM verbatim_chunks v
                 WHERE {filter_sql}
                   AND v.tsv @@ plainto_tsquery('english', $4)
                 ORDER BY text_score DESC
                 LIMIT {_HYBRID_CANDIDATE_LIMIT}
            ),
            fused AS (
                SELECT COALESCE(vr.id, tr.id) AS id,
                       COALESCE(1.0 / ({_RRF_K} + vr.rnk), 0)
                     + COALESCE(1.0 / ({_RRF_K} + tr.rnk), 0) AS rrf_score,
                       vr.vec_dist,
                       tr.text_score
                  FROM vec_ranked vr
                  FULL OUTER JOIN text_ranked tr USING (id)
            )
            SELECT v.id, v.content, v.session_id, v.chunk_index,
                   v.source_event, v.topic, v.agent, v.is_error,
                   v.loaded_skills, v.mission_id, v.mission_type,
                   v.outcome, v.ts,
                   CASE WHEN f.vec_dist IS NULL THEN NULL
                        ELSE 1 - f.vec_dist
                   END AS similarity,
                   f.text_score,
                   f.rrf_score
              FROM fused f
              JOIN verbatim_chunks v ON v.id = f.id
             ORDER BY f.rrf_score DESC, v.ts DESC, v.id ASC
             LIMIT ${limit_idx}
        """
    else:
        params = [
            str(query_embedding),
            [project_id],
            owner_user_id,
        ]
        # Param slot $4 is the hybrid query_text; vector-only mode never
        # references it, but the filter builder reserved slots from $5 on
        # so the extra params still land at the right offsets. asyncpg
        # cannot infer the type of a bare None at an unreferenced slot
        # (IndeterminateDatatypeError) — pass an empty string so the
        # parameter has a concrete text type even though the query body
        # never reads it.
        params.append('')
        params.extend(extra_params)
        params.append(limit)
        limit_idx = idx
        filter_sql = " AND ".join(filters)

        sql = f"""
            SELECT v.id, v.content, v.session_id, v.chunk_index,
                   v.source_event, v.topic, v.agent, v.is_error,
                   v.loaded_skills, v.mission_id, v.mission_type,
                   v.outcome, v.ts,
                   1 - (v.embedding <=> $1::vector) AS similarity
              FROM verbatim_chunks v
             WHERE {filter_sql}
               AND v.embedding IS NOT NULL
             ORDER BY (v.embedding <=> $1::vector) ASC,
                      v.ts DESC,
                      v.id ASC
             LIMIT ${limit_idx}
        """

    async with rls.app_conn(pool, owner_user_id) as conn:
        rows = await conn.fetch(sql, *params)
    return [_format_chunk_row(r) for r in rows]


# -- Outcome stamping -------------------------------------------------------

async def stamp_outcome(
    *,
    session_id: str,
    outcome: str,
    owner_user_id: UUID,
    overwrite: bool = False,
) -> int:
    """
    Mark every chunk in a session with an outcome. By default only
    updates rows whose outcome is still NULL — repeated stamps with
    different values are a curator decision (`overwrite=True`).

    Returns the number of rows updated.
    """
    if outcome not in _VALID_OUTCOMES:
        raise ValueError(
            f"Invalid outcome {outcome!r}; expected one of "
            f"{sorted(_VALID_OUTCOMES)}",
        )
    if not session_id:
        raise ValueError("session_id is required")

    from lib import rls
    from lib.db import get_pool

    pool = await get_pool()

    if overwrite:
        sql = """UPDATE verbatim_chunks
                    SET outcome = $1
                  WHERE session_id = $2
                    AND owner_user_id = $3"""
    else:
        sql = """UPDATE verbatim_chunks
                    SET outcome = $1
                  WHERE session_id = $2
                    AND owner_user_id = $3
                    AND outcome IS NULL"""

    async with rls.app_conn(pool, owner_user_id) as conn:
        result = await conn.execute(sql, outcome, session_id, owner_user_id)
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


# -- Topic listing (v2 §3 optional helper) ----------------------------------

async def list_topics(
    *,
    project_id: UUID,
    owner_user_id: UUID,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Return distinct topics seen in this project's verbatim chunks with
    a row count per topic. Cheap enough to call from the curator on
    every dreaming-loop pass.
    """
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    from lib import rls
    from lib.db import get_pool

    pool = await get_pool()
    async with rls.app_conn(pool, owner_user_id) as conn:
        rows = await conn.fetch(
            """SELECT topic, COUNT(*) AS n,
                      MAX(ts) AS last_seen
                 FROM verbatim_chunks
                WHERE project_id = $1
                  AND owner_user_id = $2
                  AND topic IS NOT NULL
                GROUP BY topic
                ORDER BY n DESC, last_seen DESC
                LIMIT $3""",
            project_id, owner_user_id, limit,
        )
    return [
        {
            "topic": r["topic"],
            "count": int(r["n"]),
            "last_seen": r["last_seen"].isoformat(),
        }
        for r in rows
    ]
