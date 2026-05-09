#!/usr/bin/env python3
"""
NotNativeMemory - PreCompact Hook

Fires before context compaction. Injects critical safety rules and
recent memory context so they survive compression. This is the safety
net for long sessions where the model's original instructions would
otherwise be lost.

Also queries the MCP memory server for critical/high-importance memories
scoped to the current project, so task-specific constraints persist.

Exit codes:
    0 - success (context injected)
    1 - non-fatal error (compaction proceeds without injection)
"""

import json
import os
import sys
import urllib.request
import urllib.error

# Bundle-local helpers under _internal/. Same shape in both repo and
# deployed layouts.
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HOOK_DIR)
from _internal.env_loader import load_hooks_env  # noqa: E402

load_hooks_env(__file__)

MCP_URL = os.environ.get("MEMORY_MCP_URL", "http://localhost:9500/mcp")
MAX_MEMORIES = int(os.environ.get("MEMORY_COMPACT_MAX_RESULTS", "5"))
TIMEOUT_SECONDS = 5

# Auth: set MEMORY_MCP_TOKEN in hooks.env to send Authorization: Bearer.
# Blank token relies on the server-side localhost bypass. See the hooks
# README for the two supported configurations.
_MCP_TOKEN = os.environ.get("MEMORY_MCP_TOKEN", "").strip()
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}
if _MCP_TOKEN:
    _HEADERS["Authorization"] = f"Bearer {_MCP_TOKEN}"

# Critical rules that must survive compaction. These are intentionally
# generic safety rails — customize them for your workflow by editing
# this string (e.g. add project-specific constraints, change wording,
# or load from an external file). Keep it short: every token here
# competes with the compacted conversation.
_CRITICAL_RULES = """[Compact Guard] Critical rules preserved across context compaction:
- Search memory before making decisions or starting new work.
- Read files before editing.
- Confirm before destructive operations (delete, push, force-reset, schema changes).
- Discuss architectural decisions with the user before implementing them.
- If you stated an intent in this turn, complete it in the same response — don't stop after announcing."""


def _search_critical_memories(project_dir: str) -> list:
    """Fetch high/critical importance memories for the current project.

    `project_dir` comes from the hook stdin's `cwd`; passing it to the
    server scopes the search to this project's visible set (local +
    globals + declared domains). Earlier versions passed "" which
    opted out of scope filtering and pulled high-importance memories
    from every other project on the server.
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "memory_search",
            "arguments": {
                "query": "current task constraints decisions preferences",
                "limit": MAX_MEMORIES,
                "project": project_dir,
                "min_importance": "high",
            },
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        MCP_URL,
        data=payload,
        headers=dict(_HEADERS),
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"Compact guard: server unreachable ({exc})", file=sys.stderr)
        return []

    result = body.get("result", {})
    content_blocks = result.get("content", [])
    for block in content_blocks:
        if block.get("type") == "text":
            try:
                inner = json.loads(block["text"])
                return inner.get("results", [])
            except (json.JSONDecodeError, KeyError) as exc:
                print(f"Compact guard: bad response format ({exc})", file=sys.stderr)

    return []


def _format_memories(memories: list) -> str:
    """Format memories into a concise block."""
    if not memories:
        return ""

    lines = ["\n[Compact Guard] High-priority memories from previous sessions:"]
    for i, mem in enumerate(memories, 1):
        importance = mem.get("importance", "normal")
        content = mem.get("content", "")
        lines.append(f"  {i}. [{importance}] {content}")
    return "\n".join(lines)


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(1)

    project_dir = hook_input.get("cwd", "")

    # Build the context block
    context_parts = [_CRITICAL_RULES]

    # Try to fetch critical memories from the server
    memories = _search_critical_memories(project_dir)
    if memories:
        context_parts.append(_format_memories(memories))

    # PreCompact has no schema-approved `hookSpecificOutput` shape in
    # current Claude Code versions (only PreToolUse / UserPromptSubmit /
    # PostToolUse do). Emitting the JSON envelope causes a schema
    # validation failure and the context never lands. Plain stdout is
    # the universal hook-output channel that the harness folds into the
    # compaction prompt.
    sys.stdout.write("\n".join(context_parts))
    sys.stdout.write("\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
