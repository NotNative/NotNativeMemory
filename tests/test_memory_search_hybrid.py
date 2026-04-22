"""
Integration tests for the BM25-style hybrid path in search_memories.

Covers:
- Structural invariants: hybrid results carry rrf_score; pure-vector
  results do not. Hybrid rows that matched the text side carry a
  non-None text_score.
- Behavioral: a memory whose content contains an exact-keyword match
  is surfaced under hybrid with a non-zero rrf_score. Confirms the
  full-text side of the fusion is actually contributing.
- Fallback: hybrid=True with empty query_text drops back to
  vector-only rather than raising.
- Isolation: hybrid mode still respects owner_user_id boundaries
  (bob cannot see alice's keyword-matched memory).

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)
    MEMORY_MODEL_PATH                            (loaded)

Usage:
    python tests/test_memory_search_hybrid.py
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


# Rare, invented token so the text side of the fusion has something
# unambiguous to match. Avoiding English words keeps this test stable
# even as the underlying embedder or stopword list changes.
UNIQUE_TOKEN = "zyloburn"


async def run() -> int:
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id
    from lib.embeddings import embed

    failed = 0
    total = 0

    def check(label, cond):
        nonlocal failed, total
        total += 1
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    run_id = secrets.token_hex(4)
    alice_name = f"hybrid-alice-{run_id}"
    bob_name = f"hybrid-bob-{run_id}"

    pool = await db.get_pool()
    alice = await auth_db.create_user(alice_name, "password-1234")
    alice_uid = UUID(alice["id"])
    bob = await auth_db.create_user(bob_name, "password-1234")
    bob_uid = UUID(bob["id"])

    set_current_user_id(alice_uid)
    project_dir = f"/tmp/hybrid-mem-{run_id}"
    project_id = await db.get_or_create_project(project_dir, owner_user_id=alice_uid)

    # Seed two memories. The targeted one contains UNIQUE_TOKEN; the
    # decoy is unrelated text that should lose under hybrid because
    # there is no keyword overlap.
    targeted_content = (
        f"The build system uses {UNIQUE_TOKEN} as the cache key "
        f"derivation. {UNIQUE_TOKEN} participates in every nightly run."
    )
    decoy_content = (
        "Morning standups are scheduled at 10am and cover blockers "
        "plus priorities for the day."
    )

    targeted_id = await db.store_memory(
        content=targeted_content,
        embedding=embed(targeted_content),
        project_id=project_id,
        owner_user_id=alice_uid,
        importance="normal",
    )
    decoy_id = await db.store_memory(
        content=decoy_content,
        embedding=embed(decoy_content),
        project_id=project_id,
        owner_user_id=alice_uid,
        importance="normal",
    )

    query_text = UNIQUE_TOKEN
    query_embedding = embed(query_text)

    # 1. Pure vector: no rrf_score / text_score keys on results.
    vector_results = await db.search_memories(
        query_embedding=query_embedding,
        project_id=project_id,
        owner_user_id=alice_uid,
        limit=5,
    )
    check("pure vector returns at least one row",
          len(vector_results) >= 1)
    check("pure vector rows do NOT carry rrf_score",
          all("rrf_score" not in r for r in vector_results))
    check("pure vector rows do NOT carry text_score",
          all("text_score" not in r for r in vector_results))

    # 2. Hybrid: targeted memory must surface with a non-zero
    # rrf_score, and at least that row's text_score is populated
    # (since the content contains the exact token).
    hybrid_results = await db.search_memories(
        query_embedding=query_embedding,
        project_id=project_id,
        owner_user_id=alice_uid,
        limit=5,
        hybrid=True,
        query_text=query_text,
    )
    check("hybrid returns at least one row", len(hybrid_results) >= 1)
    targeted_hit = next(
        (r for r in hybrid_results if r["id"] == str(targeted_id)),
        None,
    )
    check("hybrid surfaces the targeted memory", targeted_hit is not None)
    if targeted_hit is not None:
        check("targeted hit carries rrf_score",
              "rrf_score" in targeted_hit
              and targeted_hit["rrf_score"] > 0)
        check("targeted hit carries text_score (text side matched)",
              "text_score" in targeted_hit
              and targeted_hit["text_score"] > 0)

    # 3. Hybrid should rank the targeted memory ahead of the decoy
    # because the text side contributes rank for the keyword-matched
    # row but not for the decoy.
    ranks = {r["id"]: i for i, r in enumerate(hybrid_results)}
    if str(targeted_id) in ranks and str(decoy_id) in ranks:
        check("targeted memory ranked ahead of decoy under hybrid",
              ranks[str(targeted_id)] < ranks[str(decoy_id)])

    # 4. Fallback: hybrid=True with empty query_text silently drops
    # back to vector-only rather than raising. Results should look
    # identical to a pure-vector call (no rrf_score).
    fallback = await db.search_memories(
        query_embedding=query_embedding,
        project_id=project_id,
        owner_user_id=alice_uid,
        limit=5,
        hybrid=True,
        query_text="",
    )
    check("hybrid with empty query_text does not raise",
          isinstance(fallback, list))
    check("fallback rows do not carry rrf_score (vector path taken)",
          all("rrf_score" not in r for r in fallback))

    # 5. RLS: bob cannot see alice's targeted memory under hybrid.
    set_current_user_id(bob_uid)
    bob_project_id = await db.get_or_create_project(
        f"/tmp/hybrid-bob-{run_id}", owner_user_id=bob_uid,
    )
    bob_results = await db.search_memories(
        query_embedding=query_embedding,
        project_id=bob_project_id,
        owner_user_id=bob_uid,
        limit=5,
        hybrid=True,
        query_text=query_text,
    )
    check("cross-user hybrid search does not leak alice's memory",
          all(r["id"] != str(targeted_id) for r in bob_results))

    # Cleanup.
    async with rls.admin_conn(pool) as conn:
        await conn.execute("DELETE FROM users WHERE id = ANY($1)",
                           [alice_uid, bob_uid])
    await db.close_pool()

    print("---")
    print(f"{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
