"""
Tests that both bundles' install templates document the analyzer
configuration keys end-to-end.

The bundles ship two different generation paths:

  - Claude bundle: `hooks.env.example` is a static reference file copied
    verbatim into `~/.claude/hooks/notnative-memory/hooks.env` on first
    install. We read it as plain text.
  - NNA bundle: `merge_hooks.py::_write_hooks_env(target_dir, mcp_url)`
    writes hooks.env inline on every install. We render it into a temp
    dir and read the result.

Both must declare:
  - `OPENAI_BASE_URL` with the LM Studio default so a fresh install has
    a working LLM endpoint without needing the user to set shell env
    vars. (Pre-fix: the line was commented out; users discovered the
    silent failure only after the analyzer wrote zero memories.)
  - `MEMORY_EXTRACT_PROJECT` with a valid write target (default
    `_global`). The server rejects 'general'; an unset key would let
    the inherited 'general' default sneak in.
  - `MEMORY_EXTRACT_DISABLE_REASONING=1` so reasoning-model backends
    (Qwen3-think, DeepSeek-R1) don't burn the subprocess timeout on
    hidden <think> before emitting the JSON.

Usage:
    python tests/test_hooks_env_template.py
"""

import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))


def _parse_env(text: str) -> dict:
    """Parse a hooks.env-style file into {key: value}. Commented lines
    do NOT count as set — we want to assert the keys are LIVE."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        # Strip trailing inline-comment block ("KEY=v  # explanation").
        for i in range(len(val) - 1):
            if val[i] in (" ", "\t") and val[i + 1] == "#":
                val = val[:i]
                break
        out[key.strip()] = val.strip()
    return out


def run() -> int:
    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    # -- Claude bundle: static hooks.env.example -------------------------
    claude_example = os.path.join(
        ROOT, "hook_bundles", "claude", "notnative-memory",
        "hooks.env.example",
    )
    with open(claude_example, "r", encoding="utf-8") as fh:
        claude_text = fh.read()
    claude_env = _parse_env(claude_text)

    check("[claude] OPENAI_BASE_URL set (not commented)",
          "OPENAI_BASE_URL" in claude_env and claude_env["OPENAI_BASE_URL"])
    check("[claude] OPENAI_BASE_URL points at a /v1 endpoint",
          claude_env.get("OPENAI_BASE_URL", "").endswith("/v1"))
    check("[claude] OPENAI_API_KEY set (not commented)",
          "OPENAI_API_KEY" in claude_env and claude_env["OPENAI_API_KEY"])
    check("[claude] MEMORY_EXTRACT_PROJECT is a valid write target",
          claude_env.get("MEMORY_EXTRACT_PROJECT", "") in (
              "_global", "_domain_general",
          )
          or claude_env.get("MEMORY_EXTRACT_PROJECT", "").startswith("_domain_"))
    check("[claude] MEMORY_EXTRACT_PROJECT not 'general'",
          claude_env.get("MEMORY_EXTRACT_PROJECT", "") != "general")
    check("[claude] MEMORY_EXTRACT_DISABLE_REASONING=1 by default",
          claude_env.get("MEMORY_EXTRACT_DISABLE_REASONING", "") in ("1", "true", "yes"))

    # -- NNA bundle: render _write_hooks_env() into a temp dir -----------
    nna_path = os.path.join(
        ROOT, "hook_bundles", "nna", "notnative-memory", "merge_hooks.py",
    )
    spec = importlib.util.spec_from_file_location("_nna_merge_hooks", nna_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with tempfile.TemporaryDirectory() as tmp:
        mod._write_hooks_env(tmp, "http://test:9500/mcp")
        env_path = os.path.join(tmp, "hooks.env")
        with open(env_path, "r", encoding="utf-8") as fh:
            nna_text = fh.read()
    nna_env = _parse_env(nna_text)

    check("[nna] MEMORY_MCP_URL reflects the passed argument",
          nna_env.get("MEMORY_MCP_URL") == "http://test:9500/mcp")
    check("[nna] OPENAI_BASE_URL set (not commented)",
          "OPENAI_BASE_URL" in nna_env and nna_env["OPENAI_BASE_URL"])
    check("[nna] OPENAI_BASE_URL points at a /v1 endpoint",
          nna_env.get("OPENAI_BASE_URL", "").endswith("/v1"))
    check("[nna] OPENAI_API_KEY set (not commented)",
          "OPENAI_API_KEY" in nna_env and nna_env["OPENAI_API_KEY"])
    check("[nna] MEMORY_EXTRACT_PROJECT is a valid write target",
          nna_env.get("MEMORY_EXTRACT_PROJECT", "") in (
              "_global", "_domain_general",
          )
          or nna_env.get("MEMORY_EXTRACT_PROJECT", "").startswith("_domain_"))
    check("[nna] MEMORY_EXTRACT_PROJECT not 'general'",
          nna_env.get("MEMORY_EXTRACT_PROJECT", "") != "general")
    check("[nna] MEMORY_EXTRACT_DISABLE_REASONING=1 by default",
          nna_env.get("MEMORY_EXTRACT_DISABLE_REASONING", "") in ("1", "true", "yes"))

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
