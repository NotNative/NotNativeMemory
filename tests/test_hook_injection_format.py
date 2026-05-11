"""
Unit tests for the simplified hook injection format.

Per memory-mcp-hardening-plan.md Track 3 item 4: hooks emit
`From memory:` / scoped-header + bullet-list with no `[importance|scope|similarity]`
prefixes and no `(tags: ...)` suffix. Metadata stays server-side; the
consumer (especially small local models) only sees the content.

This test imports the bundle modules in-process and exercises their
formatter functions with synthetic memory dicts. No DB or network.

Usage:
    python tests/test_hook_injection_format.py
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))


def _load_module(path: str, name: str):
    """Import a hook script by file path. The bundles aren't packages
    and have local imports that would fail if we just sys.path them in,
    so we resolve __file__ relative to the file's own directory by
    running spec.loader.exec_module after ensuring sys.path is set."""
    bundle_dir = os.path.dirname(path)
    if bundle_dir not in sys.path:
        sys.path.insert(0, bundle_dir)
    # Ensure _internal/ subpkg resolves when the hook imports it.
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fixture():
    """Synthetic memories with the metadata that the old format used to
    expose. The new format must drop all of it from the output."""
    return [
        {
            "content": "Every code change must include tests.",
            "importance": "critical",
            "scope": "global",
            "similarity": 0.94,
            "tags": ["rule", "testing", "constraint"],
        },
        {
            "content": "PowerShell $LASTEXITCODE resets every command.",
            "importance": "high",
            "scope": "_domain_powershell",
            "similarity": 0.81,
            "tags": ["gotcha"],
        },
    ]


def run() -> int:
    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    memories = _fixture()

    forbidden_substrings = [
        "[critical",
        "[high",
        "[normal",
        "[low",
        "|global",
        "|_domain",
        "0.94",
        "0.81",
        "(tags:",
        "rule, testing",
    ]

    # claude / user_prompt_inject
    upi_claude = _load_module(
        os.path.join(ROOT, "hook_bundles", "claude", "notnative-memory",
                     "user_prompt_inject.py"),
        "_claude_upi",
    )
    out = upi_claude._format_memories(memories)
    check("claude upi: starts with 'From memory:'",
          out.startswith("From memory:"))
    check("claude upi: contains first content",
          "Every code change must include tests." in out)
    check("claude upi: contains second content",
          "PowerShell $LASTEXITCODE resets every command." in out)
    for token in forbidden_substrings:
        check(f"claude upi: no '{token}'", token not in out)
    check("claude upi: bullet format", out.count("\n- ") == 2)

    # nna / user_prompt_inject
    upi_nna = _load_module(
        os.path.join(ROOT, "hook_bundles", "nna", "notnative-memory",
                     "user_prompt_inject.py"),
        "_nna_upi",
    )
    out = upi_nna._format_memories(memories)
    check("nna upi: starts with 'From memory:'",
          out.startswith("From memory:"))
    for token in forbidden_substrings:
        check(f"nna upi: no '{token}'", token not in out)
    check("nna upi: bullet format", out.count("\n- ") == 2)

    # claude / session_start
    ss_claude = _load_module(
        os.path.join(ROOT, "hook_bundles", "claude", "notnative-memory",
                     "session_start.py"),
        "_claude_ss",
    )
    out = ss_claude._format_context(memories, "startup")
    check("claude session_start: header retained",
          "[Session Start | source=startup]" in out)
    for token in forbidden_substrings:
        check(f"claude session_start: no '{token}'", token not in out)
    check("claude session_start: bullet format", out.count("\n- ") == 2)

    # nna / session_start
    ss_nna = _load_module(
        os.path.join(ROOT, "hook_bundles", "nna", "notnative-memory",
                     "session_start.py"),
        "_nna_ss",
    )
    out = ss_nna._format_context(memories, "clear")
    check("nna session_start: header retained",
          "[Session Start | source=clear]" in out)
    for token in forbidden_substrings:
        check(f"nna session_start: no '{token}'", token not in out)
    check("nna session_start: bullet format", out.count("\n- ") == 2)

    # claude / compact_guard
    cg_claude = _load_module(
        os.path.join(ROOT, "hook_bundles", "claude", "notnative-memory",
                     "compact_guard.py"),
        "_claude_cg",
    )
    out = cg_claude._format_memories(memories)
    check("claude compact_guard: header retained",
          "[Compact Guard]" in out)
    for token in forbidden_substrings:
        check(f"claude compact_guard: no '{token}'", token not in out)
    check("claude compact_guard: bullet format", out.count("\n- ") == 2)

    # nna / compact_guard
    cg_nna = _load_module(
        os.path.join(ROOT, "hook_bundles", "nna", "notnative-memory",
                     "compact_guard.py"),
        "_nna_cg",
    )
    out = cg_nna._format_memories(memories)
    check("nna compact_guard: header retained",
          "[Compact Guard]" in out)
    for token in forbidden_substrings:
        check(f"nna compact_guard: no '{token}'", token not in out)
    check("nna compact_guard: bullet format", out.count("\n- ") == 2)

    # Empty list returns empty string for session_start / compact_guard
    check("claude session_start: empty returns ''",
          ss_claude._format_context([], "startup") == "")
    check("nna session_start: empty returns ''",
          ss_nna._format_context([], "clear") == "")
    check("claude compact_guard: empty returns ''",
          cg_claude._format_memories([]) == "")
    check("nna compact_guard: empty returns ''",
          cg_nna._format_memories([]) == "")

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
