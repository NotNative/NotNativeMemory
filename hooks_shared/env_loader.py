#!/usr/bin/env python3
"""
NotNativeMemory - shared hooks env loader.

Resolves and loads `hooks.env` for any of the NotNativeMemory hook
scripts. Search order:

  1. ~/.claude/hooks/notnative-memory/hooks.env  (Claude Code installs)
  2. ~/.nnc/hooks/notnative-memory/hooks.env     (NotNativeCoder installs)
  3. <script_dir>/hooks.env                       (back-compat for older
                                                   installs that wrote
                                                   directly into the repo)

The first file found is loaded. Each KEY=VALUE line populates
`os.environ` via `setdefault` so an actual environment variable always
wins over the file (useful for tests and one-off overrides).

Usage in a hook script:

    from hooks_shared.env_loader import load_hooks_env
    load_hooks_env(__file__)

Returns the path that was loaded, or None if no env file existed in any
of the search locations.
"""

from __future__ import annotations

import os
from typing import Optional


def _candidate_paths(script_path: str) -> list:
    """Return ordered list of candidate hooks.env paths to try."""
    home = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    script_dir = os.path.dirname(os.path.abspath(script_path))
    return [
        os.path.join(home, ".claude", "hooks", "notnative-memory", "hooks.env"),
        os.path.join(home, ".nnc", "hooks", "notnative-memory", "hooks.env"),
        os.path.join(script_dir, "hooks.env"),
    ]


def load_hooks_env(script_path: str) -> Optional[str]:
    """Load the first hooks.env found into os.environ.

    Args:
        script_path: typically __file__ from the hook script. Used to
            compute the script-local fallback path.

    Returns:
        Absolute path of the file that was loaded, or None if none of
        the candidates existed.
    """
    for path in _candidate_paths(script_path):
        if os.path.exists(path):
            _apply_env_file(path)
            return path
    return None


def _strip_inline_comment(value: str) -> str:
    """Strip a trailing inline comment if it's preceded by whitespace.

    `KEY=value  # explanation` → `value`
    `KEY=token#with#hashes` → `token#with#hashes` (no leading whitespace)

    This matches the common convention used by most env-file parsers.
    """
    for i in range(len(value) - 1):
        if value[i] in (" ", "\t") and value[i + 1] == "#":
            return value[:i].rstrip()
    return value


def _apply_env_file(path: str) -> None:
    """Read KEY=VALUE lines from `path` and setdefault into os.environ."""
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), _strip_inline_comment(value.strip()))


def parse_env_file(path: str) -> dict:
    """Parse an env file and return its key/value pairs as a dict.

    Useful for installers/migrators that need to read existing values
    without mutating os.environ.
    """
    out: dict = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = _strip_inline_comment(value.strip())
    return out
