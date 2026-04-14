#!/usr/bin/env python3
"""
NotNativeMemory - NotNativeCoder PreToolUse Hook

Fires before NNC's file tools (read_file, write_file, edit_file) and
bash. Extracts technology context from the tool arguments and queries
the MCP memory server for relevant memories, returning high-similarity
matches as additionalContext so the model has access to stored gotchas
and decisions without needing to remember to search.

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

# -- Configuration ---------------------------------------------------------

MCP_URL = os.environ.get("MEMORY_MCP_URL", "http://localhost:9500/mcp")
SIMILARITY_THRESHOLD = float(os.environ.get("MEMORY_HOOK_THRESHOLD", "0.55"))
HIGH_IMPORTANCE_THRESHOLD = float(
    os.environ.get("MEMORY_HOOK_HIGH_THRESHOLD", "0.40")
)
MAX_MEMORIES = int(os.environ.get("MEMORY_HOOK_MAX_RESULTS", "3"))
SEARCH_LIMIT = 10
TIMEOUT_SECONDS = 5

LOG_PATH = os.environ.get(
    "MEMORY_HOOK_LOG",
    os.path.expanduser("~/.nnc/memory_hook.log"),
)

# -- File extension to technology mapping (enriches the query) -------------

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

_CMD_HINTS = {
    "docker": "Docker",
    "docker-compose": "Docker Compose",
    "docker compose": "Docker Compose",
    "npm": "npm Node.js",
    "pip": "pip Python",
    "cargo": "Rust Cargo",
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

_PATH_NOISE = {
    "", ".", "..", "src", "crates", "lib", "app", "main", "pkg",
    "internal", "bin", "cmd", "tests", "test", "spec",
}


def _extract_path_segments(file_path: str) -> list:
    """Extract meaningful subsystem hints from a file path for query enrichment."""
    if not file_path:
        return []
    normalized = file_path.replace("\\", "/")
    segments = [s for s in normalized.split("/") if s]
    if segments and len(segments[0]) == 2 and segments[0].endswith(":"):
        segments = segments[1:]
    parents = segments[-5:-1]
    return [s for s in parents if s.lower() not in _PATH_NOISE]


def _get_path(tool_input: dict) -> str:
    """
    Pull a file path out of NNC's tool_input, trying common key names.
    NNC tools use 'path' but also accept 'file_path' variants in some
    plugin tools — be defensive.
    """
    for key in ("path", "file_path", "filename"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _add_file_query_parts(parts: list, verb: str, file_path: str) -> None:
    """Shared query-enrichment for read_file / write_file / edit_file."""
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
    """Build a natural language query from the tool call context."""
    parts = []
    name = (tool_name or "").lower()

    if name == "edit_file":
        _add_file_query_parts(parts, "editing", _get_path(tool_input))
    elif name == "write_file":
        _add_file_query_parts(parts, "writing", _get_path(tool_input))
    elif name == "read_file":
        _add_file_query_parts(parts, "reading", _get_path(tool_input))
    elif name == "bash":
        # NNC's bash tool uses "command" key
        command = tool_input.get("command", "")
        if command:
            parts.append(f"running command: {command[:200]}")
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
                "project": "",  # search all projects (pulls globals + domains)
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

    result = body.get("result", {})
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                inner = json.loads(block["text"])
                return inner.get("results", [])
            except (json.JSONDecodeError, KeyError) as exc:
                print(f"Memory hook: bad response format ({exc})", file=sys.stderr)

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
    """Append a telemetry row. Failures are swallowed."""
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


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(1)

    tool_name = hook_input.get("tool_name", "")
    # NNC pre-parses tool_input to a dict in the payload; fall back to
    # tool_input_json string if for some reason tool_input arrives as
    # something else.
    tool_input = hook_input.get("tool_input", {})
    if not isinstance(tool_input, dict):
        try:
            tool_input = json.loads(hook_input.get("tool_input_json", "{}"))
        except (TypeError, json.JSONDecodeError):
            tool_input = {}

    query = _build_query(tool_name, tool_input)
    if not query:
        _log_execution(tool_name, "", 0, 0, 0.0)
        sys.exit(0)

    results = _search_memories(query)
    relevant = _filter_relevant(results)

    top_similarity = max(
        (m.get("similarity", 0) for m in results),
        default=0.0,
    )
    _log_execution(tool_name, query, len(results), len(relevant), top_similarity)

    if not relevant:
        sys.exit(0)

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
