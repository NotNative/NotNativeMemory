#!/usr/bin/env python3
"""
NotNativeMemory - Compaction.Post Hook

Fires immediately after context compaction completes. Re-injects the
project's hottest/most-critical memories so the model regains working
continuity in the same turn that just lost detail to the compaction
summary.

Supersedes the previous session.start:post recovery path, which:
  - fired on startup/resume/compact (only the compact case was useful);
  - emitted plain stdout (NNA's shellHook discards anything that isn't
    a JSON envelope, so the injection silently never landed).

Uses the `hookSpecificOutput.additionalContext` envelope, which NNA's
shellHook folds into `payload.injected_context`. query.ts:551 awaits
the dispatch and yields the resulting string as a synthetic
`hook_additional_context` attachment in the post-compact message
stream.

Exit codes:
    0 - success (with or without context to inject)
    1 - non-fatal error (compaction proceeds without injection)
"""

import datetime
import json
import os
import sys
import traceback
import urllib.request
import urllib.error

# Windows cp1252 stdout crashes on non-ASCII memory content (arrows, em-dashes).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
_ERROR_LOG = os.path.join(_HOOK_DIR, "last_error.log")


def _log_traceback(label: str) -> None:
    """Append the current exception's traceback to last_error.log and
    mirror it to stderr. Hook failures otherwise surface only as a
    single-line 'Traceback (most recent call last):' in the harness
    chrome with the body truncated."""
    try:
        with open(_ERROR_LOG, "a", encoding="utf-8") as lf:
            lf.write(f"\n--- {datetime.datetime.now().isoformat()} {label} ---\n")
            traceback.print_exc(file=lf)
    except Exception:
        pass
    traceback.print_exc(file=sys.stderr)


# -- Load config from hooks.env alongside this script ----------------------
try:
    _ENV_FILE = os.path.join(_HOOK_DIR, "hooks.env")
    if os.path.exists(_ENV_FILE):
        with open(_ENV_FILE, "r") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _key, _val = _line.split("=", 1)
                    os.environ.setdefault(_key.strip(), _val.strip())
except BaseException:
    _log_traceback("compaction_post env-load")
    sys.exit(1)

MCP_URL = os.environ.get("MEMORY_MCP_URL", "http://127.0.0.1:9500/mcp")
MAX_TOKENS = int(os.environ.get("MEMORY_COMPACT_POST_MAX_TOKENS", "600"))
TIMEOUT_SECONDS = 5

_MCP_TOKEN = (
    os.environ.get("MEMORY_MCP_TOKEN", "").strip()
    or os.environ.get("NNM_AUTH_TOKEN", "").strip()
)
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}
if _MCP_TOKEN:
    _HEADERS["Authorization"] = f"Bearer {_MCP_TOKEN}"


def _fetch_context(project_dir: str) -> list:
    """Call memory_context via HTTP and return the result list.

    `memory_context` auto-includes globals and any domains declared for
    the project, so a single call covers local + global + domain. No
    user query is required — the server ranks by importance first and
    thermal activity second, which is exactly what we want when the
    conversation just lost detail.
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
        print(f"Compaction post: server unreachable ({exc})", file=sys.stderr)
        return []

    result = body.get("result", {})
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                inner = json.loads(block["text"])
                return inner.get("context", [])
            except (json.JSONDecodeError, KeyError) as exc:
                print(f"Compaction post: bad response format ({exc})", file=sys.stderr)

    return []


def _format_context(memories: list) -> str:
    """Shape the memory list into a compact recovery block."""
    if not memories:
        return ""

    lines = [
        "[Post-Compact Recovery] Working-set memories restored after compaction:"
    ]
    for mem in memories:
        content = mem.get("content", "")
        lines.append(f"- {content}")
    return "\n".join(lines)


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(1)

    # CompactionPayload carries `cwd` from the NNA dispatch site; fall
    # back to the process cwd (the hook's own dir — wrong, but better
    # than the cross-project bleed of passing "").
    project_dir = hook_input.get("cwd", "") or os.getcwd()

    memories = _fetch_context(project_dir)
    ctx = _format_context(memories)
    if not ctx:
        sys.exit(0)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostCompact",
            "additionalContext": ctx,
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        _log_traceback("compaction_post main")
        sys.exit(1)
