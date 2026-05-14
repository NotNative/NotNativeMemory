"""
Tests that the 7 daily-driver core tools advertise
`_meta["anthropic/alwaysLoad"]: true` in their MCP tools/list response.

Why this matters: NNA's deferred-tools mechanism treats every MCP tool
as deferred (not in the loaded `tools[]` array; reachable only via
ToolSearch) UNLESS the server flags it. Without the flag every memory
operation costs the model a two-turn round-trip (ToolSearch → fetch
schema → call), even hot-loop ops like memory_context and memory_search.

The locked core set (per NNA's docs/planning/Deferred-Tools-And-Discovery
§10): memory_context, memory_search, memory_store, memory_fact_add,
memory_fact_query, recall, rag_search. Changing this list is a deliberate
design decision — don't relax this test without re-locking the NNA-side
contract.

Usage:
    python tests/test_always_load_meta.py
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


CORE_TOOLS = {
    "memory_context",
    "memory_search",
    "memory_store",
    "memory_fact_add",
    "memory_fact_query",
    "recall",
    "rag_search",
}


async def run() -> int:
    import server

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

    # FastMCP exposes the registered tools via list_tools(); each Tool
    # has the `_meta` dict that will be emitted on the wire.
    tools = await server.mcp.list_tools()
    by_name = {t.name: t for t in tools}

    # Each core tool exists.
    for name in sorted(CORE_TOOLS):
        check(f"{name} is registered", name in by_name)

    # Each core tool carries anthropic/alwaysLoad=True in its _meta.
    for name in sorted(CORE_TOOLS):
        if name not in by_name:
            continue
        meta = getattr(by_name[name], "_meta", None) or getattr(
            by_name[name], "meta", None
        )
        ok = isinstance(meta, dict) and meta.get("anthropic/alwaysLoad") is True
        check(
            f"{name} _meta carries anthropic/alwaysLoad=True (got {meta!r})",
            ok,
        )

    # Non-core tools must NOT have the flag set — otherwise the deferred-
    # tools mechanism degenerates (every tool becomes daily-driver core
    # and the whole point of deferral is lost).
    for tool in tools:
        if tool.name in CORE_TOOLS:
            continue
        meta = getattr(tool, "_meta", None) or getattr(tool, "meta", None)
        leak = (
            isinstance(meta, dict)
            and meta.get("anthropic/alwaysLoad") is True
        )
        check(
            f"non-core tool {tool.name!r} does NOT have anthropic/alwaysLoad",
            not leak,
        )

    print(f"\n{total - failed}/{total} checks passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
