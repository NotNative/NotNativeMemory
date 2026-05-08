#!/usr/bin/env python3
"""
NotNativeMemory - Promise Detector Hook (nna adapter)

Fires on tool.call:post — after every tool call during the agentic loop.
Tracks what the model commits to (task creation, file writes, stated plans)
and stores unfulfilled promises as TTL facts in the Memory MCP server.

The user_prompt_inject hook reads these facts on the next prompt and can
inject "you committed to X but didn't finish" context. The in-process
task-aware nudge in query.ts handles the immediate continuation; this
hook provides the accountability/observability layer.

Exit codes:
    0 - success
    1 - non-fatal error (does not block agent operation)
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
_MCP_TOKEN = os.environ.get("MEMORY_MCP_TOKEN", "").strip()
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}
if _MCP_TOKEN:
    _HEADERS["Authorization"] = f"Bearer {_MCP_TOKEN}"

# Tool names that represent commitments
TASK_TOOLS = {"TaskCreate", "TodoWrite", "TaskUpdate"}
FILE_TOOLS = {"Write", "Edit"}

# TTL for promise facts (seconds). Promises older than this are considered
# stale and won't be injected. 30 minutes covers a typical agentic session.
PROMISE_TTL_SECONDS = int(os.environ.get("MEMORY_PROMISE_TTL", "1800"))

LOG_PATH = os.environ.get(
    "MEMORY_PROMISE_LOG",
    os.path.expanduser("~/.nna/promise_detector.log"),
)


def _mcp_call(method: str, params: dict) -> dict:
    """Make a JSON-RPC call to the Memory MCP server."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": method,
            "arguments": params,
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        MCP_URL,
        data=payload,
        headers=dict(_HEADERS),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _store_promise(subject: str, predicate: str, value: str) -> bool:
    """Store a promise as a mutable fact with TTL semantics."""
    try:
        _mcp_call("memory_fact_add", {
            "subject": subject,
            "predicate": predicate,
            "object": value,
            "confidence": 0.9,
        })
        return True
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False


def _handle_task_create(tool_input: dict, tool_output: str) -> str | None:
    """Record a task creation promise."""
    subject = tool_input.get("subject", "")
    if not subject:
        return None

    task_id = ""
    if tool_output:
        for line in tool_output.split("\n"):
            if "id" in line.lower() and "#" in line:
                task_id = line.strip()
                break

    value = f"Created task: {subject[:100]}"
    if task_id:
        value += f" ({task_id})"

    _store_promise("agentic_session", "task_committed", value)
    return value


def _handle_task_update(tool_input: dict) -> str | None:
    """Record task status changes — completions clear promises."""
    task_id = tool_input.get("id", "")
    status = tool_input.get("status", "")

    if status == "completed" and task_id:
        _store_promise("agentic_session", "task_fulfilled", f"Completed: {task_id}")
        return f"fulfilled:{task_id}"

    return None


def _handle_todo_write(tool_input: dict) -> str | None:
    """Record todo list writes — track total open items."""
    tasks = tool_input.get("tasks", [])
    if not tasks:
        return None

    open_count = sum(
        1 for t in tasks
        if isinstance(t, dict) and t.get("status") in ("pending", "in_progress")
    )

    if open_count > 0:
        value = f"Todo list written with {open_count} open item(s)"
        _store_promise("agentic_session", "todos_pending", value)
        return value

    return None


def _log_execution(tool_name: str, action: str | None) -> None:
    """Append a telemetry row. Failures are swallowed."""
    try:
        parent = os.path.dirname(LOG_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as logf:
            logf.write(
                f"{datetime.datetime.now().isoformat(timespec='seconds')}\t"
                f"tool={tool_name}\t"
                f"action={action or 'skip'}\n"
            )
    except OSError:
        pass


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(1)

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    tool_output = hook_input.get("tool_output", "")
    is_error = hook_input.get("is_error", False)

    if is_error:
        _log_execution(tool_name, "skipped_error")
        sys.exit(0)

    if tool_name not in TASK_TOOLS and tool_name not in FILE_TOOLS:
        sys.exit(0)

    action = None

    if tool_name == "TaskCreate":
        action = _handle_task_create(tool_input, tool_output)
    elif tool_name == "TaskUpdate":
        action = _handle_task_update(tool_input)
    elif tool_name == "TodoWrite":
        action = _handle_todo_write(tool_input)
    elif tool_name in FILE_TOOLS:
        file_path = tool_input.get("file_path", "")
        if file_path:
            _store_promise(
                "agentic_session",
                "file_written",
                f"{os.path.basename(file_path)} at {datetime.datetime.now().isoformat(timespec='seconds')}",
            )
            action = f"file:{os.path.basename(file_path)}"

    _log_execution(tool_name, action)
    sys.exit(0)


if __name__ == "__main__":
    main()
