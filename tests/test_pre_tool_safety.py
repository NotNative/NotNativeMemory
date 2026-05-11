"""
Unit tests for the PreToolUse safety-gate hook.

Both bundle copies (claude/ and nna/) ship the same pure `evaluate`
function. This test loads each in-process and asserts:

- Disabled by default (no env): never blocks.
- Enabled: blocks the well-known destructive patterns.
- Enabled: allows benign ops.
- Bypass env: enabled + bypass = no block.
- Non-Bash tools with matching string fields: unaffected (rules are
  scoped to specific tool names).

No DB or network. Pure function tests.

Usage:
    python tests/test_pre_tool_safety.py
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))


def _load_hook(bundle: str):
    """Load pre_tool_safety.py from the given bundle into a fresh module."""
    path = os.path.join(
        ROOT, "hook_bundles", bundle, "notnative-memory",
        "pre_tool_safety.py",
    )
    bundle_dir = os.path.dirname(path)
    if bundle_dir not in sys.path:
        sys.path.insert(0, bundle_dir)
    spec = importlib.util.spec_from_file_location(
        f"_safety_{bundle}", path,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run() -> int:
    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    # The hook scripts read env at function-call time, so we toggle env
    # between calls. Start clean.
    for k in ("MEMORY_SAFETY_GATE_ENABLED", "MEMORY_SAFETY_GATE_BYPASS"):
        os.environ.pop(k, None)

    for bundle in ("claude", "nna"):
        hook = _load_hook(bundle)
        label = f"[{bundle}]"

        # Disabled by default: nothing blocks.
        os.environ.pop("MEMORY_SAFETY_GATE_ENABLED", None)
        blocked, _ = hook.evaluate(
            "Bash", {"command": "git push --force origin main"},
        )
        check(f"{label} default-disabled: force-push not blocked",
              not blocked)

        # Enable.
        os.environ["MEMORY_SAFETY_GATE_ENABLED"] = "1"

        # Block: git push --force
        blocked, reason = hook.evaluate(
            "Bash", {"command": "git push --force origin main"},
        )
        check(f"{label} blocks 'git push --force'", blocked)
        check(f"{label} reason mentions force",
              "force" in reason.lower())

        # Block: git push -f short flag
        blocked, _ = hook.evaluate(
            "Bash", {"command": "git push -f origin main"},
        )
        check(f"{label} blocks 'git push -f'", blocked)

        # Block: rm -rf /
        blocked, _ = hook.evaluate("Bash", {"command": "rm -rf /"})
        check(f"{label} blocks 'rm -rf /'", blocked)

        # Block: git reset --hard origin/main
        blocked, _ = hook.evaluate(
            "Bash", {"command": "git reset --hard origin/main"},
        )
        check(f"{label} blocks 'git reset --hard origin/...'", blocked)

        # Block: DROP DATABASE (case-insensitive)
        blocked, _ = hook.evaluate(
            "Bash", {"command": "psql -c 'drop database foo'"},
        )
        check(f"{label} blocks 'drop database' (case-insensitive)",
              blocked)

        # Allow: regular git push
        blocked, _ = hook.evaluate(
            "Bash", {"command": "git push origin main"},
        )
        check(f"{label} allows plain 'git push'", not blocked)

        # Allow: rm -rf in a specific dir (not root)
        blocked, _ = hook.evaluate("Bash", {"command": "rm -rf ./build"})
        check(f"{label} allows 'rm -rf ./build'", not blocked)

        # Allow: --force-with-lease passes (different word boundary)
        blocked, _ = hook.evaluate(
            "Bash",
            {"command": "git push --force-with-lease origin main"},
        )
        check(f"{label} allows '--force-with-lease'", not blocked)

        # Non-Bash tool with similar string content: not blocked
        # (Edit's file_path doesn't match the Bash regex shape).
        blocked, _ = hook.evaluate(
            "Edit",
            {"file_path": "git push --force.txt", "new_string": "x"},
        )
        check(f"{label} non-Bash tool unaffected by Bash rules",
              not blocked)

        # Bypass overrides enabled.
        os.environ["MEMORY_SAFETY_GATE_BYPASS"] = "1"
        blocked, _ = hook.evaluate(
            "Bash", {"command": "git push --force origin main"},
        )
        check(f"{label} bypass=1 short-circuits", not blocked)

        # Cleanup
        os.environ.pop("MEMORY_SAFETY_GATE_BYPASS", None)
        os.environ.pop("MEMORY_SAFETY_GATE_ENABLED", None)

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
