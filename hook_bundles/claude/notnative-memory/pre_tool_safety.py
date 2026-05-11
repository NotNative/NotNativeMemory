#!/usr/bin/env python3
"""
NotNativeMemory - PreToolUse safety-gate hook.

Fires before Claude Code dispatches a tool call. Inspects the tool name
and arguments against a small set of destructive patterns; if any match,
the hook exits 2 with a stderr reason, which Claude Code interprets as
"refuse the call." Otherwise the hook exits 0 silently and the call
proceeds.

This hook is intentionally separate from any memory-injection hook.
Memory injection brings context into the model; this gate refuses an
action the model has already chosen. Different jobs, different files.

Opt-in: disabled by default. Set MEMORY_SAFETY_GATE_ENABLED=1 in
hooks.env to enable. The installer registers the hook for PreToolUse
but the default-off behavior means an install never changes user
workflow unless the user flips the switch.

Bypass: set MEMORY_SAFETY_GATE_BYPASS=1 in a single command's
environment, or unset MEMORY_SAFETY_GATE_ENABLED, to skip the gate.

Exit codes:
    0  - allow (default; gate disabled or no rule matched)
    2  - block (matched a destructive rule; reason printed to stderr)
"""

import json
import os
import re
import sys

# Bundle-local helpers under _internal/.
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HOOK_DIR)
from _internal.env_loader import load_hooks_env  # noqa: E402

load_hooks_env(__file__)


def _enabled() -> bool:
    val = os.environ.get("MEMORY_SAFETY_GATE_ENABLED", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _bypassed() -> bool:
    val = os.environ.get("MEMORY_SAFETY_GATE_BYPASS", "").strip().lower()
    return val in ("1", "true", "yes", "on")


# Baseline destructive-action rules. Each entry: (tool_name, regex, reason).
# The regex is matched against a flattened string representation of the
# tool input — for Bash that's the `command` field, for Edit/Write the
# `file_path`, etc. Keep this list small and conservative; the user can
# fork or extend it locally.
_RULES = [
    (
        "Bash",
        re.compile(r"\bgit\s+push\b[^\n]*\s(--force(?!-)|-f)\b", re.IGNORECASE),
        "git push --force rewrites shared history. Use --force-with-lease "
        "if you really mean it, or remove the flag.",
    ),
    (
        "Bash",
        re.compile(r"\brm\s+-rf\s+/(\s|$)"),
        "rm -rf on the filesystem root is almost never intended.",
    ),
    (
        "Bash",
        re.compile(r"\bgit\s+reset\s+--hard\b[^\n]*\borigin/", re.IGNORECASE),
        "git reset --hard against an upstream ref discards local commits. "
        "Confirm intent explicitly.",
    ),
    (
        "Bash",
        re.compile(r"\bDROP\s+DATABASE\b", re.IGNORECASE),
        "DROP DATABASE is unrecoverable. Confirm intent explicitly.",
    ),
]


def _flatten_tool_input(tool_name: str, tool_input: dict) -> str:
    """Build a string the regex rules can scan over."""
    if not isinstance(tool_input, dict):
        return ""
    if tool_name == "Bash":
        return str(tool_input.get("command", ""))
    # For Edit/Write/etc., concatenate the obvious string fields.
    parts = []
    for key in ("command", "file_path", "path", "content", "new_string"):
        val = tool_input.get(key)
        if isinstance(val, str):
            parts.append(val)
    return "\n".join(parts)


def evaluate(tool_name: str, tool_input: dict):
    """Return (blocked: bool, reason: str). Pure function for testing."""
    if not _enabled() or _bypassed():
        return False, ""
    payload = _flatten_tool_input(tool_name, tool_input)
    if not payload:
        return False, ""
    for rule_tool, pattern, reason in _RULES:
        if rule_tool != tool_name:
            continue
        if pattern.search(payload):
            return True, reason
    return False, ""


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)  # malformed input is not the gate's job to police

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {}) or {}

    blocked, reason = evaluate(tool_name, tool_input)
    if blocked:
        sys.stderr.write(f"Safety gate refused: {reason}\n")
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
