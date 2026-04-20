"""Self-test for NotNativeMemory installation."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


async def test():
    """Run store/search/forget cycle to verify installation."""
    from lib.embeddings import embed
    from lib.db import (
        get_pool, store_memory, search_memories,
        forget_memory, get_or_create_project, close_pool,
    )

    # Test embedding
    vec = embed("This is a test memory for NotNativeMemory setup.")
    if len(vec) != 768:
        print(f"  Embedding: FAIL (expected 768 dims, got {len(vec)})")
        sys.exit(1)
    print("  Embedding: OK (768 dimensions)")

    # Test DB operations
    project_id = await get_or_create_project("__self_test__", "Self Test")
    mem_id = await store_memory(
        content="Self-test memory: the installer is working correctly.",
        embedding=vec, project_id=project_id,
        tags=["test"], importance="low",
    )
    print(f"  Store: OK (id={mem_id})")

    results = await search_memories(
        query_embedding=vec, project_id=project_id, limit=1,
    )
    if len(results) != 1:
        print(f"  Search: FAIL (expected 1 result, got {len(results)})")
        sys.exit(1)
    sim = results[0]["similarity"]
    print(f"  Search: OK (similarity={sim})")

    deleted = await forget_memory(mem_id)
    if not deleted:
        print("  Forget: FAIL (memory not deleted)")
        sys.exit(1)
    print("  Forget: OK")

    # Clean up test project
    pool = await get_pool()
    await pool.execute("DELETE FROM projects WHERE directory = '__self_test__'")
    await close_pool()
    print("  Cleanup: OK")


asyncio.run(test())
print()
print("All self-tests passed!")
