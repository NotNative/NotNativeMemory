"""
Integration tests for lib/verbatim_store.

Covers:
- capture is idempotent on (owner, session_id, chunk_index): second
  call with the same coords returns the existing id with inserted=False
  and does not create a duplicate row.
- search_chunks hybrid path returns RRF-ranked rows; pure-vector path
  carries similarity only.
- Filter args (session_id, topic, mission_id, is_error, source_events,
  outcomes) actually restrict the result set.
- stamp_outcome flips NULL outcomes to the supplied value across a
  session and is a no-op without overwrite=True once set.
- RLS isolation: bob cannot see alice's chunks via search.

Requires:
    Live pgvector reachable via MEMORY_DB_* env, and the gte-large
    embedding model loaded at MEMORY_MODEL_PATH (defaults to
    models/gte-large-en-v1.5 under the repo root).

Run:
    python tests/test_verbatim_store.py
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


# Rare invented token so the text side of RRF has an unambiguous hit.
UNIQUE_TOKEN = "vorqualyx"


async def run() -> int:
    from lib import auth_db, db, rls, verbatim_store
    from lib.auth_context import set_current_user_id
    from lib.embeddings import embed

    failed = 0
    total = 0

    def check(label: str, cond: bool) -> None:
        nonlocal failed, total
        total += 1
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    run_id = secrets.token_hex(4)
    alice_name = f"verbatim-alice-{run_id}"
    bob_name = f"verbatim-bob-{run_id}"

    pool = await db.get_pool()
    alice = await auth_db.create_user(alice_name, "password-1234")
    alice_uid = UUID(alice["id"])
    bob = await auth_db.create_user(bob_name, "password-1234")
    bob_uid = UUID(bob["id"])

    set_current_user_id(alice_uid)
    project_dir = f"/tmp/verbatim-{run_id}"
    project_id = await db.get_or_create_project(
        project_dir, owner_user_id=alice_uid,
    )

    session_a = f"session-a-{run_id}"
    session_b = f"session-b-{run_id}"

    targeted_text = (
        f"On Aloha the {UNIQUE_TOKEN} step rebuilds the menu cache. "
        f"{UNIQUE_TOKEN} runs after every price import."
    )
    decoy_text = (
        "Morning standups cover blockers and priorities for the day. "
        "Nothing about caches or imports here."
    )

    # 1. Capture is idempotent on (session, index).
    first_id, first_inserted = await verbatim_store.store_chunk(
        content=targeted_text,
        embedding=embed(targeted_text),
        session_id=session_a,
        chunk_index=0,
        project_id=project_id,
        owner_user_id=alice_uid,
        source_event="turn.post",
        topic="aloha-menu",
        agent="main",
        loaded_skills=["Aloha_GetMenuItem"],
        mission_id=f"mission-{run_id}",
        mission_type="aloha-pos",
    )
    check("first capture inserts a new row", first_inserted is True)

    second_id, second_inserted = await verbatim_store.store_chunk(
        content=targeted_text + " (retry)",
        embedding=embed(targeted_text),
        session_id=session_a,
        chunk_index=0,
        project_id=project_id,
        owner_user_id=alice_uid,
        source_event="turn.post",
    )
    check("re-capture on same (session, index) returns same id",
          str(second_id) == str(first_id))
    check("re-capture reports inserted=False",
          second_inserted is False)

    # Add a decoy chunk and a second session so filters have signal.
    decoy_id, _ = await verbatim_store.store_chunk(
        content=decoy_text,
        embedding=embed(decoy_text),
        session_id=session_a,
        chunk_index=1,
        project_id=project_id,
        owner_user_id=alice_uid,
        source_event="turn.post",
        topic="standup",
    )
    other_session_chunk_id, _ = await verbatim_store.store_chunk(
        content=f"unrelated chatter about {UNIQUE_TOKEN}",
        embedding=embed(f"unrelated chatter about {UNIQUE_TOKEN}"),
        session_id=session_b,
        chunk_index=0,
        project_id=project_id,
        owner_user_id=alice_uid,
        source_event="tool.call.post",
        is_error=True,
    )

    query_emb = embed(UNIQUE_TOKEN)

    # 2. Hybrid search: targeted chunk surfaces, carries rrf_score.
    hybrid_hits = await verbatim_store.search_chunks(
        query_embedding=query_emb,
        project_id=project_id,
        owner_user_id=alice_uid,
        query_text=UNIQUE_TOKEN,
        hybrid=True,
        limit=10,
    )
    check("hybrid returns at least one row", len(hybrid_hits) >= 1)
    targeted_hit = next(
        (r for r in hybrid_hits if r["id"] == str(first_id)), None,
    )
    check("hybrid surfaces the targeted chunk", targeted_hit is not None)
    if targeted_hit is not None:
        check("targeted hit carries rrf_score > 0",
              "rrf_score" in targeted_hit
              and targeted_hit["rrf_score"] > 0)
        check("targeted hit carries text_score > 0",
              "text_score" in targeted_hit
              and targeted_hit["text_score"] > 0)

    # 3. Pure-vector path: similarity present, no rrf_score.
    vector_hits = await verbatim_store.search_chunks(
        query_embedding=query_emb,
        project_id=project_id,
        owner_user_id=alice_uid,
        hybrid=False,
        limit=10,
    )
    check("vector hits carry similarity",
          all("similarity" in r for r in vector_hits))
    check("vector hits do not carry rrf_score",
          all("rrf_score" not in r for r in vector_hits))

    # 4. Filters.
    only_a = await verbatim_store.search_chunks(
        query_embedding=query_emb,
        project_id=project_id,
        owner_user_id=alice_uid,
        query_text=UNIQUE_TOKEN,
        session_id=session_a,
        limit=10,
    )
    check("session_id filter excludes the other session",
          all(r["session_id"] == session_a for r in only_a)
          and len(only_a) >= 1)

    by_topic = await verbatim_store.search_chunks(
        query_embedding=query_emb,
        project_id=project_id,
        owner_user_id=alice_uid,
        query_text=UNIQUE_TOKEN,
        topic="aloha-menu",
        limit=10,
    )
    check("topic filter restricts results",
          all(r["topic"] == "aloha-menu" for r in by_topic)
          and len(by_topic) >= 1)

    by_error = await verbatim_store.search_chunks(
        query_embedding=query_emb,
        project_id=project_id,
        owner_user_id=alice_uid,
        query_text=UNIQUE_TOKEN,
        is_error=True,
        limit=10,
    )
    check("is_error=True returns only the error chunk",
          len(by_error) == 1
          and by_error[0]["id"] == str(other_session_chunk_id))

    by_source = await verbatim_store.search_chunks(
        query_embedding=query_emb,
        project_id=project_id,
        owner_user_id=alice_uid,
        query_text=UNIQUE_TOKEN,
        source_events=["tool.call.post"],
        limit=10,
    )
    check("source_events filter restricts to tool.call.post",
          all(r["source_event"] == "tool.call.post" for r in by_source)
          and len(by_source) >= 1)

    # 5. stamp_outcome.
    stamped = await verbatim_store.stamp_outcome(
        session_id=session_a,
        outcome="success",
        owner_user_id=alice_uid,
    )
    check("first stamp updates the unstamped chunks in session_a",
          stamped == 2)

    second_stamp = await verbatim_store.stamp_outcome(
        session_id=session_a,
        outcome="failure",
        owner_user_id=alice_uid,
    )
    check("second stamp without overwrite is a no-op",
          second_stamp == 0)

    overwrite_stamp = await verbatim_store.stamp_outcome(
        session_id=session_a,
        outcome="failure",
        owner_user_id=alice_uid,
        overwrite=True,
    )
    check("overwrite=True re-stamps the same rows",
          overwrite_stamp == 2)

    by_outcome = await verbatim_store.search_chunks(
        query_embedding=query_emb,
        project_id=project_id,
        owner_user_id=alice_uid,
        query_text=UNIQUE_TOKEN,
        outcomes=["failure"],
        limit=10,
    )
    check("outcomes filter finds the re-stamped session",
          all(r["outcome"] == "failure" for r in by_outcome)
          and any(r["session_id"] == session_a for r in by_outcome))

    # 6. RLS isolation: bob can't see alice's chunks.
    set_current_user_id(bob_uid)
    bob_project_id = await db.get_or_create_project(
        f"/tmp/verbatim-bob-{run_id}", owner_user_id=bob_uid,
    )
    bob_hits = await verbatim_store.search_chunks(
        query_embedding=query_emb,
        project_id=bob_project_id,
        owner_user_id=bob_uid,
        query_text=UNIQUE_TOKEN,
        limit=10,
    )
    check("cross-user search does not leak alice's chunks",
          all(r["id"] != str(first_id) for r in bob_hits))

    # Cleanup.
    async with rls.admin_conn(pool) as conn:
        await conn.execute(
            "DELETE FROM users WHERE id = ANY($1)",
            [alice_uid, bob_uid],
        )
    await db.close_pool()

    print("---")
    print(f"{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
