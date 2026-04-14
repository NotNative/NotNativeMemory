#!/usr/bin/env python3
"""
NotNativeMemory - NotNativeCoder UserPromptSubmit Hook

Fires when the user sends a message, before NNC processes it.
Uses the user's message as a semantic query against the memory
server and injects top matches as additionalContext so relevant
decisions, preferences, and constraints are in scope for the
whole turn.

NNC payload for UserPromptSubmit:
    {"hook_event_name": "UserPromptSubmit", "prompt": "..."}

Exit codes:
    0 - success (with or without context injected)
    1 - non-fatal error (prompt still reaches the model)
"""

import datetime
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

# -- Configuration ---------------------------------------------------------

MCP_URL = os.environ.get("MEMORY_MCP_URL", "http://localhost:9500/mcp")

SIMILARITY_THRESHOLD = float(os.environ.get("MEMORY_PROMPT_THRESHOLD", "0.45"))
HIGH_IMPORTANCE_THRESHOLD = float(
    os.environ.get("MEMORY_PROMPT_HIGH_THRESHOLD", "0.35")
)
MAX_MEMORIES = int(os.environ.get("MEMORY_PROMPT_MAX_RESULTS", "3"))
SEARCH_LIMIT = 10
MIN_PROMPT_CHARS = int(os.environ.get("MEMORY_PROMPT_MIN_CHARS", "15"))
TIMEOUT_SECONDS = 5

LOG_PATH = os.environ.get(
    "MEMORY_PROMPT_LOG",
    os.path.expanduser("~/.nnc/memory_prompt_hook.log"),
)

MAX_QUERY_CHARS = 500


def _search_memories(query: str) -> list:
    """Query the MCP memory server via HTTP. Returns list of memory dicts."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "memory_search",
            "arguments": {
                "query": query,
                "limit": SEARCH_LIMIT,
                "project": "",
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
        print(f"Prompt hook: server unreachable ({exc})", file=sys.stderr)
        return []

    result = body.get("result", {})
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                inner = json.loads(block["text"])
                return inner.get("results", [])
            except (json.JSONDecodeError, KeyError) as exc:
                print(f"Prompt hook: bad response format ({exc})", file=sys.stderr)

    return []


def _filter_relevant(results: list) -> list:
    """Apply similarity thresholds with a lower floor for load-bearing memories."""
    filtered = []
    for mem in results:
        similarity = mem.get("similarity", 0)
        importance = mem.get("importance", "normal")
        if similarity >= SIMILARITY_THRESHOLD:
            filtered.append(mem)
        elif importance in ("high", "critical") and similarity >= HIGH_IMPORTANCE_THRESHOLD:
            filtered.append(mem)
    return filtered[:MAX_MEMORIES]


def _format_memories(memories: list) -> str:
    """Format memories into a concise context block."""
    lines = ["[Memory Hook] Context from previous sessions relevant to this request:"]
    for i, mem in enumerate(memories, 1):
        tags = ", ".join(mem.get("tags", []))
        importance = mem.get("importance", "normal")
        similarity = mem.get("similarity", 0)
        scope = mem.get("scope", "")
        content = mem.get("content", "")
        scope_tag = f"|{scope}" if scope else ""
        lines.append(
            f"  {i}. [{importance}{scope_tag}|{similarity:.2f}] {content}"
            + (f" (tags: {tags})" if tags else "")
        )
    return "\n".join(lines)


def _log_execution(
    prompt_len: int,
    results_total: int,
    results_surfaced: int,
    top_similarity: float,
) -> None:
    """Append a telemetry row. Failures are swallowed."""
    try:
        parent = os.path.dirname(LOG_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as logf:
            logf.write(
                f"{datetime.datetime.now().isoformat(timespec='seconds')}\t"
                f"prompt_len={prompt_len}\t"
                f"hits={results_surfaced}/{results_total}\t"
                f"top={top_similarity:.3f}\n"
            )
    except OSError:
        pass


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(1)

    prompt = hook_input.get("prompt", "").strip()

    if len(prompt) < MIN_PROMPT_CHARS:
        _log_execution(len(prompt), 0, 0, 0.0)
        sys.exit(0)

    query = prompt[:MAX_QUERY_CHARS]

    results = _search_memories(query)
    relevant = _filter_relevant(results)

    top_similarity = max(
        (m.get("similarity", 0) for m in results),
        default=0.0,
    )
    _log_execution(len(prompt), len(results), len(relevant), top_similarity)

    if not relevant:
        sys.exit(0)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _format_memories(relevant),
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
