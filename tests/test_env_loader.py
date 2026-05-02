#!/usr/bin/env python3
"""
Tests for hooks_shared/env_loader.py.

Covers candidate path ordering, KEY=VALUE parsing, inline-comment
stripping, env var precedence (file values use setdefault, so explicit
env wins), and the parse_env_file helper.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Add repo root so `from hooks_shared.env_loader import ...` works.
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from hooks_shared import env_loader  # noqa: E402


def test_strip_inline_comment_with_space():
    assert env_loader._strip_inline_comment("0.45    # similarity") == "0.45"
    print("[OK] _strip_inline_comment strips ` # comment` with whitespace")


def test_strip_inline_comment_with_tab():
    assert env_loader._strip_inline_comment("3\t# max results") == "3"
    print("[OK] _strip_inline_comment strips `\\t# comment` with tab")


def test_strip_inline_comment_no_comment():
    assert env_loader._strip_inline_comment("plain-value") == "plain-value"
    print("[OK] _strip_inline_comment leaves values without comments alone")


def test_strip_inline_comment_keeps_hash_in_value():
    # `#` immediately after non-whitespace is NOT a comment marker.
    assert env_loader._strip_inline_comment("token#with#hashes") == "token#with#hashes"
    print("[OK] _strip_inline_comment preserves `#` inside a value (no whitespace)")


def test_strip_inline_comment_empty():
    assert env_loader._strip_inline_comment("") == ""
    print("[OK] _strip_inline_comment handles empty string")


def test_parse_env_file_basic():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "hooks.env")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("# comment line\n")
            fh.write("\n")
            fh.write("KEY1=value1\n")
            fh.write("KEY2=value2  # with comment\n")
            fh.write("KEY3=token#hash\n")
            fh.write("malformed_no_equals\n")
        result = env_loader.parse_env_file(p)
    assert result == {
        "KEY1": "value1",
        "KEY2": "value2",
        "KEY3": "token#hash",
    }
    print("[OK] parse_env_file handles comments, blanks, inline-comments, malformed lines")


def test_parse_env_file_missing_path():
    assert env_loader.parse_env_file("/nonexistent/path") == {}
    print("[OK] parse_env_file returns {} for missing file")


def test_load_hooks_env_setdefault_lets_env_win():
    """An actual env var should override the file value."""
    with tempfile.TemporaryDirectory() as tmp:
        script_dir = os.path.join(tmp, "scripts")
        os.makedirs(script_dir)
        env_file = os.path.join(script_dir, "hooks.env")
        with open(env_file, "w", encoding="utf-8") as fh:
            fh.write("OVERRIDABLE_KEY=from_file\n")

        # Pre-set env: should NOT be overridden.
        with mock.patch.dict(os.environ, {"OVERRIDABLE_KEY": "from_env"}, clear=False), \
             mock.patch.object(env_loader, "_candidate_paths", return_value=[env_file]):
            env_loader.load_hooks_env(os.path.join(script_dir, "fake_hook.py"))
            assert os.environ["OVERRIDABLE_KEY"] == "from_env"
    print("[OK] load_hooks_env uses setdefault (env wins over file)")


def test_load_hooks_env_returns_loaded_path():
    with tempfile.TemporaryDirectory() as tmp:
        script_dir = os.path.join(tmp, "scripts")
        os.makedirs(script_dir)
        env_file = os.path.join(script_dir, "hooks.env")
        with open(env_file, "w", encoding="utf-8") as fh:
            fh.write("X=1\n")

        with mock.patch.object(env_loader, "_candidate_paths", return_value=[env_file]):
            result = env_loader.load_hooks_env(os.path.join(script_dir, "h.py"))
        assert result == env_file
    print("[OK] load_hooks_env returns path of loaded file")


def test_load_hooks_env_returns_none_when_nothing_found():
    with tempfile.TemporaryDirectory() as tmp:
        # Real but empty dir guarantees the candidate file does not exist.
        nonexistent = os.path.join(tmp, "definitely-not-here.env")
        with mock.patch.object(env_loader, "_candidate_paths", return_value=[nonexistent]):
            result = env_loader.load_hooks_env(os.path.join(tmp, "h.py"))
    assert result is None
    print("[OK] load_hooks_env returns None when no candidate exists")


def test_candidate_paths_includes_claude_then_script_local():
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "subdir", "fake.py")
        os.makedirs(os.path.dirname(script))
        paths = env_loader._candidate_paths(script)
    # Must include the deployed plugin path and the script-local fallback.
    assert any(".claude" in p and "notnative-memory" in p for p in paths)
    assert any(p.endswith(os.path.join(os.path.dirname(script), "hooks.env")) for p in paths)
    # Deployed location should come BEFORE script-local fallback.
    claude_idx = next(i for i, p in enumerate(paths) if ".claude" in p and "notnative-memory" in p)
    local_idx = next(i for i, p in enumerate(paths) if p.endswith(os.path.join(os.path.dirname(script), "hooks.env")))
    assert claude_idx < local_idx, "deployed location should be searched before script-local"
    print("[OK] _candidate_paths orders deployed-location before script-local")


def test_load_first_match_wins():
    """When multiple candidates exist, only the first is loaded."""
    with tempfile.TemporaryDirectory() as tmp:
        a = os.path.join(tmp, "a.env")
        b = os.path.join(tmp, "b.env")
        with open(a, "w", encoding="utf-8") as fh:
            fh.write("WINNER=from_a\n")
        with open(b, "w", encoding="utf-8") as fh:
            fh.write("WINNER=from_b\n")

        # Clear any pre-existing value.
        os.environ.pop("WINNER", None)
        try:
            with mock.patch.object(env_loader, "_candidate_paths", return_value=[a, b]):
                env_loader.load_hooks_env("/script.py")
            assert os.environ["WINNER"] == "from_a"
        finally:
            os.environ.pop("WINNER", None)
    print("[OK] load_hooks_env loads only the first match")


if __name__ == "__main__":
    tests = [
        test_strip_inline_comment_with_space,
        test_strip_inline_comment_with_tab,
        test_strip_inline_comment_no_comment,
        test_strip_inline_comment_keeps_hash_in_value,
        test_strip_inline_comment_empty,
        test_parse_env_file_basic,
        test_parse_env_file_missing_path,
        test_load_hooks_env_setdefault_lets_env_win,
        test_load_hooks_env_returns_loaded_path,
        test_load_hooks_env_returns_none_when_nothing_found,
        test_candidate_paths_includes_claude_then_script_local,
        test_load_first_match_wins,
    ]
    for t in tests:
        t()
    print(f"\n[SUCCESS] All {len(tests)} env_loader tests passed")
