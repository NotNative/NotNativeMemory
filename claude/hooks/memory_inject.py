#!/usr/bin/env python3
"""
NotNativeMemory - PreToolUse Hook (DEPRECATED — no-op stub)

Formerly injected memories on Edit/Write/Bash/Read/Grep/Glob tool
calls. Removed in the 2026-04-19 hook-restructure pass because the
injection was structurally mis-timed: PreToolUse fires AFTER the model
has already decided what tool to call and with what arguments. The
context arrived as informational, not decisional, so guidance that
should have shaped the decision (e.g. "use a hash table, not else-if
chains") reached the model too late to matter.

Replacement coverage:
    - Turn-level framing: `user_prompt_inject.py` (UserPromptSubmit)
    - Session-level framing: `session_start.py` (SessionStart)
    - Compaction survival: `compact_guard.py` (PreCompact)

This file is retained as a no-op so installed Claude Code settings
that still reference it do not error on tool calls. The next
`merge_hooks.py` run removes the settings entry entirely. After that,
this file can be deleted.

Exit codes:
    0 - always (no-op)
"""

import sys


def main():
    # Do not read stdin — the harness will close it. Just succeed and
    # let the tool call proceed.
    sys.exit(0)


if __name__ == "__main__":
    main()
