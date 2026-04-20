#!/usr/bin/env python3
"""
NotNativeMemory - NotNativeCoder SessionStart Hook

Fires at the start of an NNC session (source="startup"), when the
user resumes a prior conversation (source="resume"), and after
compaction (source="compact"). Pulls hot and critical memories for
the current project via `memory_context` and emits them as startup
context so the model has working continuity without waiting for the
next user message.

NNC payload for SessionStart:
    {"hook_event_name": "SessionStart", "source": "...", "cwd": "..."}

Exit codes:
    0 - success (with or without context to inject)
    1 - non-fatal error (session proceeds without injection)
"""

import json
import os
import sys
import urllib.request
import urllib.error

# -- Load config from hooks.env alongside this script ----------------------
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_FILE = os.path.join(_HOOK_DIR, "hooks.env")
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE, "r") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ.setdefault(_key.strip(), _val.strip())

MCP_URL = os.environ.get("MEMORY_MCP_URL", "http://localhost:9500/mcp")
MAX_TOKENS = int(os.environ.get("MEMORY_SESSION_MAX_TOKENS", "600"))
TIMEOUT_SECONDS = 5


def _fetch_context(project_dir: str) -> list:
    """Call memory_context via HTTP and return the result list."""
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
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
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

    memories = _fetch_context(project_dir)
    ctx = _format_context(memories, source)

    if ctx:
        sys.stdout.write(ctx)
        sys.stdout.write("\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
