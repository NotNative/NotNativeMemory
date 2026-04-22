"""
Integration tests for the BM25-style hybrid path in search_docs.

Covers:
- Structural invariants: hybrid results carry rrf_score; pure-vector
  results do not.
- Behavioral: a document whose chunk contains an exact-keyword match
  surfaces under hybrid with a non-zero rrf_score, and is ranked
  ahead of a decoy chunk without the keyword.
- Isolation: hybrid mode still respects owner_user_id boundaries.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)
    MEMORY_MODEL_PATH                            (loaded)

Usage:
    python tests/test_rag_search_hybrid.py
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


UNIQUE_TOKEN = "zyloburn"


async def run() -> int:
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id
    from lib.rag.ingest import ingest_text
    from lib.rag.search import search_docs

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
    alice_name = f"rhybrid-alice-{run_id}"
    bob_name = f"rhybrid-bob-{run_id}"

    pool = await db.get_pool()
    alice = await auth_db.create_user(alice_name, "password-1234")
    alice_uid = UUID(alice["id"])
    bob = await auth_db.create_user(bob_name, "password-1234")
    bob_uid = UUID(bob["id"])

    set_current_user_id(alice_uid)
    project_dir = f"/tmp/rhybrid-{run_id}"
    project_id = await db.get_or_create_project(project_dir, owner_user_id=alice_uid)

    # Seed two documents. The targeted doc mentions UNIQUE_TOKEN in
    # its content; the decoy is a longer unrelated passage.
    targeted_content = (
        f"Infrastructure note. The build cache key is derived via "
        f"{UNIQUE_TOKEN}. Any run that touches {UNIQUE_TOKEN} must "
        f"be treated as a cache-invalidating event."
    )
    decoy_content = (
        "A collection of generally-relevant engineering practices. "
        "Code review coverage, rollback readiness, feature flag "
        "hygiene, and observability baselines live here. Nothing "
        "in particular about cache derivation."
    )

    targeted = await ingest_text(
        owner_user_id=alice_uid,
        project_id=project_id,
        title="targeted doc",
        content=targeted_content,
    )
    decoy = await ingest_text(
        owner_user_id=alice_uid,
        project_id=project_id,
        title="decoy doc",
        content=decoy_content,
    )

    targeted_doc_id = targeted["document_id"]
    decoy_doc_id = decoy["document_id"]

    # 1. Pure vector: no rrf_score keys.
    vector_hits = await search_docs(
        owner_user_id=alice_uid,
        project_id=project_id,
        query=UNIQUE_TOKEN,
        limit=5,
    )
    check("pure vector returns at least one hit", len(vector_hits) >= 1)
    check("pure vector hits do NOT carry rrf_score",
          all("rrf_score" not in h for h in vector_hits))

    # 2. Hybrid: targeted chunk surfaces with rrf_score.
    hybrid_hits = await search_docs(
        owner_user_id=alice_uid,
        project_id=project_id,
        query=UNIQUE_TOKEN,
        limit=5,
        hybrid=True,
    )
    check("hybrid returns at least one hit", len(hybrid_hits) >= 1)

    targeted_hit = next(
        (h for h in hybrid_hits if h["document_id"] == targeted_doc_id),
        None,
    )
    check("hybrid surfaces the targeted chunk", targeted_hit is not None)
    if targeted_hit is not None:
        check("targeted hit carries rrf_score",
              "rrf_score" in targeted_hit
              and targeted_hit["rrf_score"] > 0)
        check("targeted hit carries text_score",
              "text_score" in targeted_hit
              and targeted_hit["text_score"] > 0)

    # 3. Targeted ahead of decoy under hybrid.
    ranks = {h["document_id"]: i for i, h in enumerate(hybrid_hits)}
    if targeted_doc_id in ranks and decoy_doc_id in ranks:
        check("targeted doc ranked ahead of decoy under hybrid",
              ranks[targeted_doc_id] < ranks[decoy_doc_id])

    # 4. RLS isolation: bob does not see alice's chunks.
    set_current_user_id(bob_uid)
    bob_project_id = await db.get_or_create_project(
        f"/tmp/rhybrid-bob-{run_id}", owner_user_id=bob_uid,
    )
    bob_hits = await search_docs(
        owner_user_id=bob_uid,
        project_id=bob_project_id,
        query=UNIQUE_TOKEN,
        limit=5,
        hybrid=True,
    )
    check("cross-user hybrid search does not leak alice's chunks",
          all(h["document_id"] != targeted_doc_id for h in bob_hits))

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
