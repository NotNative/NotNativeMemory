#!/usr/bin/env python3
"""
NotNativeMemory - SessionStart Hook

Fires at the very beginning of a session (source="startup"), when the
user resumes a prior conversation (source="resume"), and immediately
after context compaction (source="compact"). Pulls the hottest and
most-critical memories for the current project via `memory_context`
and emits them as startup context so the model has working continuity
without waiting for the next user message.

Triple duty:
    1. Primes a fresh session with relevant memories.
    2. Reminds the model to ToolSearch-load the deferred memory MCP
       tools so `memory_store` / `memory_search` / etc. are callable
       without the user having to ask.
    3. Covers the post-compact case — the previous one-turn amnesia
       gap where UserPromptSubmit was the earliest hook that could
       fire after compaction.

Output channel is plain stdout; the harness folds that into session
context. `hookSpecificOutput` for SessionStart is not in the
schema-approved envelope shape in current Claude Code versions.

Exit codes:
    0 - success (with or without context to inject)
    1 - non-fatal error (session proceeds without injection)
"""

import json
import os
import sys
import urllib.request
import urllib.error

# Bundle-local helpers under _internal/.
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HOOK_DIR)
from _internal.env_loader import load_hooks_env  # noqa: E402

load_hooks_env(__file__)

MCP_URL = os.environ.get("MEMORY_MCP_URL", "http://localhost:9500/mcp")
MAX_TOKENS = int(os.environ.get("MEMORY_SESSION_MAX_TOKENS", "600"))
TIMEOUT_SECONDS = 5

# The MCP server requires auth since Phase 5. Two paths for hooks:
#   1. Set MEMORY_MCP_TOKEN in hooks.env — a Bearer token minted via
#      the /tokens web page or POST /auth/login. Works everywhere.
#   2. Configure MEMORY_AUTH_LOCALHOST_BYPASS=1 +
#      MEMORY_AUTH_LOCALHOST_USER=<name> in the SERVER's .env — lets
#      unauthenticated loopback calls auth-as that named user. Simpler
#      for single-user local dev but only works when the server binds
#      loopback-only and the hook runs on the same host.
# When MEMORY_MCP_TOKEN is blank, we send no Authorization header, which
# the server will accept under option 2 and reject under token auth.
_MCP_TOKEN = os.environ.get("MEMORY_MCP_TOKEN", "").strip()
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}
if _MCP_TOKEN:
    _HEADERS["Authorization"] = f"Bearer {_MCP_TOKEN}"

# Standing reminder about the deferred-tools issue. Claude Code's
# system message marks MCP tools as "deferred" and does not load their
# schemas up front, which means `mcp__memory__*` tools are not callable
# until ToolSearch pulls them. We surface this at session start so the
# model does the load itself before the user has to notice.
_TOOL_LOAD_REMINDER = (
    "[Session Start] Memory MCP tools are deferred by the harness. "
    "Call ToolSearch with "
    "`select:memory_store,memory_search,memory_list,memory_forget,"
    "memory_context,memory_fact_add,memory_fact_query,"
    "memory_project_configure` before trying to use them."
)


def _fetch_context(project_dir: str) -> list:
    """Call memory_context via HTTP and return the result list.

    `memory_context` auto-includes globals and any domains declared for
    the project, so a single call covers local + global + domain. No
    user query is required — the server ranks by importance first and
    thermal activity second.
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "memory_context",
            "arguments": {
                "project": project_dir,
                "max_tokens": MAX_TOKENS,
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
        print(f"Session start: server unreachable ({exc})", file=sys.stderr)
        return []

    result = body.get("result", {})
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                inner = json.loads(block["text"])
                return inner.get("context", [])
            except (json.JSONDecodeError, KeyError) as exc:
                print(f"Session start: bad response format ({exc})", file=sys.stderr)

    return []


def _format_context(memories: list, source: str) -> str:
    """Shape the memory list into a compact stdout block."""
    if not memories:
        return ""

    header = (
        f"[Session Start | source={source}] Working-set memories for this project:"
    )
    lines = [header]
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

    source = hook_input.get("source", "startup")
    project_dir = hook_input.get("cwd", "") or os.getcwd()

    # Always emit the tool-load reminder; it's cheap and load-bearing
    # for sessions where the user expects memory tools to work without
    # prompting.
    parts = [_TOOL_LOAD_REMINDER]

    memories = _fetch_context(project_dir)
    ctx = _format_context(memories, source)
    if ctx:
        parts.append(ctx)

    sys.stdout.write("\n\n".join(parts))
    sys.stdout.write("\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
