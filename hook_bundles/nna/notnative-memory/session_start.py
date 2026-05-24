#!/usr/bin/env python3
"""
NotNativeMemory - SessionStart Hook (nna adapter)

Fires on session.start:post when NNA boots. Two jobs:

  1. Pre-warms NNM's memory cache via memory_context so the first
     turn:pre (user_prompt_inject.py) gets a cache hit rather than a
     cold embedding fetch against the full memory store.

  2. Logs the session start (session_id prefix + timestamp) so log
     files have clear session boundaries for post-mortem analysis.

Payload shape: { "event": "session.start", "session_id": "..." }
(SessionStartPayload from NNA engine/types.ts)

Context injection at session start is tracked as a future improvement.
Today that requires setup.ts to await the dispatch and propagate
payload.injected_context, which is not yet wired. Pre-warming is
already useful: the NNM server builds and caches the project's ranked
memory list, so the first turn:pre returns immediately from cache.

Exit codes:
    0 - success (pre-warm succeeded or server unreachable — non-blocking)
    1 - non-fatal input error (session continues)
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

MCP_URL = os.environ.get("MEMORY_MCP_URL", "http://127.0.0.1:9500/mcp")
MAX_TOKENS = int(os.environ.get("MEMORY_SESSION_MAX_TOKENS", "600"))
TIMEOUT_SECONDS = 5

_MCP_TOKEN = os.environ.get("MEMORY_MCP_TOKEN", "").strip()
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}
if _MCP_TOKEN:
    _HEADERS["Authorization"] = f"Bearer {_MCP_TOKEN}"

LOG_PATH = os.environ.get(
    "MEMORY_SESSION_LOG",
    os.path.expanduser("~/.nna/session_start.log"),
)


def _log(session_id: str, status: str) -> None:
    try:
        parent = os.path.dirname(LOG_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(
                f"{datetime.datetime.now().isoformat(timespec='seconds')}\t"
                f"session={session_id[:8]}\t"
                f"{status}\n"
            )
    except OSError:
        pass


def _prewarm(cwd: str) -> str:
    """Call memory_context to pre-warm NNM's ranked memory list.

    Returns a short status string for the log line. Failures are
    non-fatal — the cache simply won't be warm and the first turn:pre
    will pay the cold-fetch cost instead.
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "memory_context",
            "arguments": {
                "project": cwd,
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
        return f"prewarm_skip reason=unreachable detail={type(exc).__name__}"

    count = 0
    for block in body.get("result", {}).get("content", []):
        if block.get("type") == "text":
            try:
                inner = json.loads(block["text"])
                count = len(inner.get("context", []))
            except (json.JSONDecodeError, KeyError):
                pass

    return f"prewarm_ok memories={count}"


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(1)

    session_id = hook_input.get("session_id", "unknown")
    # SessionStartPayload doesn't carry cwd yet — infer from process cwd.
    # When setup.ts adds cwd to the payload this line updates to use it.
    cwd = hook_input.get("cwd") or os.getcwd()

    status = _prewarm(cwd)
    _log(session_id, status)
    sys.exit(0)


if __name__ == "__main__":
    main()
