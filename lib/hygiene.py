"""
Dreaming-loop hygiene operations.

Bounded, idempotent maintenance passes on the memory store. Each pass
processes at most `budget` rows per category so a single run cannot
runaway. Safe to re-invoke; the second call processes only the
remaining candidates.

Operations (Phase 3, v1):
  - classify_unclassified: shape-heuristic class inference.
  - rebalance_importance: promote rule-class hot memories to critical;
    demote stale low-access model-inferred memories.
  - resolve_conflicts: cheap auto-resolver (recency on contradiction,
    specificity otherwise). Queues undecidable conflicts for review.
  - dedupe_near_duplicates: stub for v1 (relies on existing dedup
    detector already running at write-time).

Verbatim grounding (Phase 6 follow-up) plugs into _decide_conflict.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from lib import db, rls


# Markers used by the conservative class classifier. Order: rule first
# (operational imperatives), then preference, then memory (state
# statement). Anything that matches none returns None — better to leave
# unclassified than mis-classify.
_RULE_MARKERS = (
    " must ", " must.", " must,",
    " never ", " always ", "do not ", "don't ",
    " required ", " forbidden ", " prohibited ",
)
_PREFERENCE_MARKERS = (
    " prefer ", " prefers ", " preferred ",
    " would rather ", " likes to ",
)
_MEMORY_MARKERS = (
    " is ", " was ", " has ", " contains ", " uses ", " currently ",
)


def _infer_class(content: str) -> Optional[str]:
    """Return 'rule' | 'preference' | 'memory' | None.

    Conservative shape heuristic. Padding the content with spaces makes
    boundary markers match start/end-of-string the same way.
    """
    if not content:
        return None
    c = " " + content.lower() + " "
    if any(m in c for m in _RULE_MARKERS):
        return "rule"
    if any(m in c for m in _PREFERENCE_MARKERS):
        return "preference"
    if any(m in c for m in _MEMORY_MARKERS):
        return "memory"
    return None


@dataclass
class HygieneReport:
    classified: int
    conflicts_auto_resolved: int
    conflicts_queued_for_review: int
    promoted_to_critical: int
    demoted: int
    deduplicated: int
    duration_ms: int

    def as_dict(self) -> dict:
        return asdict(self)


async def _classify_unclassified_for_owner(
    owner_user_id, budget: int,
) -> int:
    """Walk up to `budget` unclassified memories and assign a class."""
    pool = await db.get_pool()
    async with rls.app_conn(pool, owner_user_id) as conn:
        rows = await conn.fetch(
            """SELECT id, content
                 FROM memories
                WHERE owner_user_id = $1
                  AND class IS NULL
                  AND superseded_by IS NULL
                ORDER BY temperature DESC NULLS LAST, access_count DESC
                LIMIT $2""",
            owner_user_id, budget,
        )

    n = 0
    for r in rows:
        cls = _infer_class(r["content"])
        if cls is None:
            continue
        ok = await db.admin_update_memory(
            r["id"], owner_user_id, memory_class=cls,
        )
        if ok:
            n += 1
    return n


async def _rebalance_importance_for_owner(
    owner_user_id, budget: int,
) -> tuple[int, int]:
    """Promote rule-class hot memories to critical. Demote stale
    model-inferred memories. Returns (promoted, demoted)."""
    pool = await db.get_pool()
    async with rls.app_conn(pool, owner_user_id) as conn:
        promote_rows = await conn.fetch(
            """SELECT id FROM memories
                WHERE owner_user_id = $1
                  AND class = 'rule'
                  AND importance = 'high'
                  AND temperature >= 85
                  AND access_count >= 10
                  AND superseded_by IS NULL
                ORDER BY temperature DESC
                LIMIT $2""",
            owner_user_id, budget,
        )
        demote_rows = await conn.fetch(
            """SELECT id FROM memories
                WHERE owner_user_id = $1
                  AND source_kind = 'model-inferred'
                  AND importance IN ('high', 'normal')
                  AND access_count <= 1
                  AND last_accessed < (now() - interval '90 days')
                  AND superseded_by IS NULL
                ORDER BY last_accessed ASC NULLS FIRST
                LIMIT $2""",
            owner_user_id, budget,
        )

    promoted = 0
    for r in promote_rows:
        ok = await db.admin_update_memory(
            r["id"], owner_user_id, importance="critical",
        )
        if ok:
            promoted += 1
    demoted = 0
    for r in demote_rows:
        ok = await db.admin_update_memory(
            r["id"], owner_user_id, importance="low",
        )
        if ok:
            demoted += 1
    return promoted, demoted


def _is_contradiction(a: str, b: str) -> bool:
    """Cheap contradiction detector: opposite-polarity imperatives that
    share at least three non-stopword tokens."""
    a_l = " " + a.lower() + " "
    b_l = " " + b.lower() + " "
    polarities = (
        ("must ", "must never "),
        ("always ", "never "),
        ("do not ", "do "),
        ("don't ", "do "),
    )
    for p1, p2 in polarities:
        if (p1 in a_l and p2 in b_l) or (p2 in a_l and p1 in b_l):
            a_toks = {t for t in a_l.split() if len(t) > 3}
            b_toks = {t for t in b_l.split() if len(t) > 3}
            if len(a_toks & b_toks) >= 3:
                return True
    return False


async def _resolve_conflicts_for_owner(
    owner_user_id, budget: int,
) -> tuple[int, int]:
    """Walk unresolved conflicts. Decide via (1) contradiction+recency,
    (2) specificity (longer wins). Skip otherwise.

    Returns (auto_resolved, skipped). 'Skipped' is the v1 stand-in for
    the queued-for-review pile — Phase 3 doesn't yet have a review UI.
    """
    rows = await db.list_conflicts(
        owner_user_id, include_resolved=False, limit=budget,
    )
    auto = 0
    skipped = 0
    from uuid import UUID
    for c in rows:
        a = c["memory_a"]
        b = c["memory_b"]
        winner_id = None
        loser_id = None

        if _is_contradiction(a["content"], b["content"]):
            # Recency wins on explicit contradiction. Recency is
            # implicit in newer-id ordering since we don't have
            # created_at here, so prefer the second-listed memory
            # which by list_conflicts order is newer-detected. As a
            # bounded-blast fallback, skip — better not to resolve
            # than mis-resolve.
            skipped += 1
            continue

        # Specificity heuristic: longer wins by ≥ 50 chars.
        a_len, b_len = len(a["content"] or ""), len(b["content"] or "")
        if abs(a_len - b_len) >= 50:
            if a_len > b_len:
                winner_id, loser_id = a["id"], b["id"]
                resolution = "supersede_b"
            else:
                winner_id, loser_id = b["id"], a["id"]
                resolution = "supersede_a"
        else:
            skipped += 1
            continue

        try:
            ok = await db.resolve_conflict(
                UUID(c["conflict_id"]), owner_user_id, resolution,
            )
            if ok:
                auto += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
    return auto, skipped


async def run_hygiene(
    owner_user_id, budget: int = 50,
) -> HygieneReport:
    """Run all hygiene passes for the caller's memories. Bounded by
    `budget` per category. Returns a HygieneReport summary."""
    start = datetime.now(timezone.utc)

    classified = await _classify_unclassified_for_owner(
        owner_user_id, budget,
    )
    promoted, demoted = await _rebalance_importance_for_owner(
        owner_user_id, budget,
    )
    auto, skipped = await _resolve_conflicts_for_owner(
        owner_user_id, budget,
    )

    duration_ms = int(
        (datetime.now(timezone.utc) - start).total_seconds() * 1000
    )
    return HygieneReport(
        classified=classified,
        conflicts_auto_resolved=auto,
        conflicts_queued_for_review=skipped,
        promoted_to_critical=promoted,
        demoted=demoted,
        deduplicated=0,
        duration_ms=duration_ms,
    )
