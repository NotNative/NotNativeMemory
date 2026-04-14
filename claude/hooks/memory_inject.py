#!/usr/bin/env python3
"""
NotNativeMemory - PreToolUse Hook

Fires before Edit, Write, and Bash tool calls. Extracts technology
context from the tool call and queries the MCP memory server for
relevant memories. Returns high-similarity matches as additionalContext
so the model has access to stored gotchas and decisions without needing
to remember to search.

Reads MCP server URL from MEMORY_MCP_URL environment variable.
Falls back to http://localhost:9500/mcp.

Exit codes:
    0 - success (with or without context to inject)
    1 - non-fatal error (hook failed, tool call proceeds anyway)
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

# -- Configuration ----------------------------------------------------------

MCP_URL = os.environ.get("MEMORY_MCP_URL", "http://localhost:9500/mcp")
SIMILARITY_THRESHOLD = float(os.environ.get("MEMORY_HOOK_THRESHOLD", "0.55"))
# Memories tagged importance=high or importance=critical surface at a
# lower similarity floor, because the operator explicitly flagged them
# as load-bearing and they should show up on any remotely-relevant
# match rather than waiting for a strong semantic hit.
HIGH_IMPORTANCE_THRESHOLD = float(
    os.environ.get("MEMORY_HOOK_HIGH_THRESHOLD", "0.40")
)
MAX_MEMORIES = int(os.environ.get("MEMORY_HOOK_MAX_RESULTS", "3"))
SEARCH_LIMIT = 10  # fetch more, filter down to MAX_MEMORIES above threshold
TIMEOUT_SECONDS = 5

# Telemetry log path. Flat file on purpose: keeps hook-execution data
# out of the semantic-search memory space (which would waste embedding
# cycles and pollute recall) and stays cheap enough that telemetry
# failure never blocks a tool call.
LOG_PATH = os.environ.get(
    "MEMORY_HOOK_LOG",
    os.path.expanduser("~/.claude/memory_hook.log"),
)

# -- File extension to technology mapping (enriches the query) --------------
# Not a filter - just adds technology keywords to improve semantic search.

_EXT_HINTS = {
    ".py": "Python",
    ".ps1": "PowerShell",
    ".psm1": "PowerShell module",
    ".psd1": "PowerShell data",
    ".sh": "Bash shell script",
    ".bash": "Bash shell script",
    ".ts": "TypeScript",
    ".tsx": "TypeScript React",
    ".js": "JavaScript",
    ".jsx": "JavaScript React",
    ".vue": "Vue.js",
    ".sql": "SQL database",
    ".yaml": "YAML configuration",
    ".yml": "YAML configuration",
    ".json": "JSON",
    ".toml": "TOML configuration",
    ".dockerfile": "Docker",
    ".rs": "Rust",
    ".go": "Go",
    ".rb": "Ruby",
    ".java": "Java",
    ".cs": "C#",
    ".cpp": "C++",
    ".c": "C",
    ".css": "CSS",
    ".scss": "SCSS",
    ".html": "HTML",
}

# Keywords in bash commands that hint at technologies.
_CMD_HINTS = {
    "docker": "Docker",
    "docker-compose": "Docker Compose",
    "docker compose": "Docker Compose",
    "npm": "npm Node.js",
    "pip": "pip Python",
    "git": "Git",
    "curl": "HTTP curl",
    "psql": "PostgreSQL",
    "redis": "Redis",
    "kubectl": "Kubernetes",
    "terraform": "Terraform",
    "ansible": "Ansible",
    "systemctl": "systemd",
    "uvicorn": "FastAPI uvicorn",
    "pytest": "pytest Python testing",
    "python": "Python",
    "powershell": "PowerShell",
    "pwsh": "PowerShell",
}


# Path segments that don't carry subsystem information and should be
# stripped from the query-enrichment step (they add noise, not signal).
_PATH_NOISE = {
    "", ".", "..", "src", "crates", "lib", "app", "main", "pkg",
    "internal", "bin", "cmd", "tests", "test", "spec",
}


def _extract_path_segments(file_path: str) -> list:
    """
    Extract meaningful subsystem hints from a file path for query
    enrichment. For `/myproject/rust/crates/my-cli/src/tui/widgets/transcript.rs`
    this returns roughly `["my-cli", "tui", "widgets"]` — the parts
    that actually say what the code is ABOUT, stripped of structural
    filler like `src` and `crates`.
    """
    if not file_path:
        return []
    normalized = file_path.replace("\\", "/")
    segments = [s for s in normalized.split("/") if s]
    # Drop drive letter on Windows (e.g. "D:")
    if segments and len(segments[0]) == 2 and segments[0].endswith(":"):
        segments = segments[1:]
    # Take the last few parents (exclude the filename itself) and
    # filter structural noise.
    parents = segments[-5:-1]
    return [s for s in parents if s.lower() not in _PATH_NOISE]


def _add_file_query_parts(parts: list, verb: str, file_path: str) -> None:
    """Shared query-enrichment for Edit/Write/Read."""
    if not file_path:
        return
    basename = os.path.basename(file_path)
    _, ext = os.path.splitext(basename)
    hint = _EXT_HINTS.get(ext.lower(), "")
    if hint:
        parts.append(hint)
    parts.append(f"{verb} {basename}")
    parts.extend(_extract_path_segments(file_path))
    if basename.lower() in ("dockerfile", "docker-compose.yml",
                             "docker-compose.yaml"):
        parts.append("Docker")


def _build_query(tool_name: str, tool_input: dict) -> str:
    """
    Build a natural language query from the tool call context.
    The goal is a descriptive string that the embedding model can match
    against stored memories semantically. Query enrichment pulls
    subsystem hints from file-path parent directories so the hook
    can surface craft lessons that are specific to a subsystem
    without requiring the operator to type "ratatui widgets" every
    time they read a widget file.
    """
    parts = []

    if tool_name == "Edit":
        _add_file_query_parts(parts, "editing", tool_input.get("file_path", ""))
    elif tool_name == "Write":
        _add_file_query_parts(parts, "writing", tool_input.get("file_path", ""))
    elif tool_name == "Read":
        _add_file_query_parts(parts, "reading", tool_input.get("file_path", ""))

    elif tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        if pattern:
            parts.append(f"searching for {pattern[:120]}")
        path = tool_input.get("path", "")
        parts.extend(_extract_path_segments(path))
        # Language hint from type filter
        type_hint = tool_input.get("type", "")
        if type_hint:
            parts.append(type_hint.capitalize())
        # Ext hint from glob filter
        glob_filter = tool_input.get("glob", "")
        if glob_filter:
            for ext_key, hint in _EXT_HINTS.items():
                if ext_key in glob_filter.lower():
                    parts.append(hint)
                    break

    elif tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        if pattern:
            parts.append(f"finding files matching {pattern[:120]}")
            # Derive ext hint from glob pattern itself
            pattern_lower = pattern.lower()
            for ext_key, hint in _EXT_HINTS.items():
                if ext_key in pattern_lower:
                    parts.append(hint)
                    break
        path = tool_input.get("path", "")
        parts.extend(_extract_path_segments(path))

    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        if command:
            parts.append(f"running command: {command[:200]}")

            # Add technology hints from command keywords
            cmd_lower = command.lower()
            for keyword, hint in _CMD_HINTS.items():
                if keyword in cmd_lower:
                    parts.append(hint)

    if not parts:
        return ""

    return " ".join(parts)


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
                "project": "",  # search all projects
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
        print(f"Memory hook: server unreachable ({exc})", file=sys.stderr)
        return []

    # Navigate MCP response structure
    result = body.get("result", {})
    # tool/call returns content as a list of content blocks
    content_blocks = result.get("content", [])
    for block in content_blocks:
        if block.get("type") == "text":
            try:
                inner = json.loads(block["text"])
                return inner.get("results", [])
            except (json.JSONDecodeError, KeyError) as exc:
                print(f"Memory hook: bad response format ({exc})", file=sys.stderr)

    return []


def _format_memories(memories: list) -> str:
    """Format memories into a concise context block."""
    lines = ["[Memory Hook] Relevant memories from previous sessions:"]
    for i, mem in enumerate(memories, 1):
        tags = ", ".join(mem.get("tags", []))
        importance = mem.get("importance", "normal")
        similarity = mem.get("similarity", 0)
        content = mem.get("content", "")
        lines.append(
            f"  {i}. [{importance}|{similarity:.2f}] {content}"
            + (f" (tags: {tags})" if tags else "")
        )
    return "\n".join(lines)


def _log_execution(
    tool_name: str,
    query: str,
    results_total: int,
    results_surfaced: int,
    top_similarity: float,
) -> None:
    """
    Append one telemetry row to LOG_PATH. Failures are swallowed so
    telemetry can never block or crash the hook pipeline — the tool
    call takes priority over observability every time.
    """
    try:
        parent = os.path.dirname(LOG_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as logf:
            logf.write(
                f"{datetime.datetime.now().isoformat(timespec='seconds')}\t"
                f"tool={tool_name}\t"
                f"q={query[:200]!r}\t"
                f"hits={results_surfaced}/{results_total}\t"
                f"top={top_similarity:.3f}\n"
            )
    except OSError:
        pass


def _filter_relevant(results: list) -> list:
    """
    Apply the similarity threshold with a lower floor for memories
    explicitly marked as high / critical importance. Anything the
    operator flagged as load-bearing gets surfaced on weaker matches
    because the cost of missing it is higher than the cost of a
    slightly off-topic hit.
    """
    filtered = []
    for mem in results:
        similarity = mem.get("similarity", 0)
        importance = mem.get("importance", "normal")
        if similarity >= SIMILARITY_THRESHOLD:
            filtered.append(mem)
        elif importance in ("high", "critical") and similarity >= HIGH_IMPORTANCE_THRESHOLD:
            filtered.append(mem)
    return filtered[:MAX_MEMORIES]


def main():
    # Read hook input from stdin
    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(1)

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    # Build a semantic query from the tool call context
    query = _build_query(tool_name, tool_input)
    if not query:
        _log_execution(tool_name, "", 0, 0, 0.0)
        sys.exit(0)

    # Search memories
    results = _search_memories(query)

    # Filter by similarity threshold (with high-importance bypass)
    relevant = _filter_relevant(results)

    top_similarity = max(
        (m.get("similarity", 0) for m in results),
        default=0.0,
    )
    _log_execution(tool_name, query, len(results), len(relevant), top_similarity)

    if not relevant:
        sys.exit(0)

    # Return as additionalContext
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": _format_memories(relevant),
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
