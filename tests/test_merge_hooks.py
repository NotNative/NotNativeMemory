#!/usr/bin/env python3
"""
Tests for claude/hooks/merge_hooks.py — the installer.

Covers: file copying into the deploy dir, hooks.env preservation across
re-runs, MCP URL patching on first install, settings.json upsert, and
retired-script sweeping. The user's actual ~/.claude/ is not touched —
we monkeypatch _claude_home() to a temp dir for each test.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Add path to the merge_hooks module.
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT / "claude" / "hooks"))
sys.path.insert(0, str(_REPO_ROOT))

import merge_hooks  # noqa: E402


def _make_fake_repo(tmp: str) -> str:
    """Build a minimal repo layout with the source files merge_hooks expects."""
    repo = os.path.join(tmp, "fake_repo")
    os.makedirs(os.path.join(repo, "claude", "hooks"))
    os.makedirs(os.path.join(repo, "hooks_shared"))

    for script in merge_hooks._HOOK_SCRIPTS:
        with open(os.path.join(repo, "claude", "hooks", script), "w", encoding="utf-8") as fh:
            fh.write(f"# fake {script}\n")

    with open(os.path.join(repo, "claude", "hooks", "hooks.env.example"), "w", encoding="utf-8") as fh:
        fh.write("MEMORY_MCP_URL=http://localhost:9500/mcp\n")
        fh.write("MEMORY_MCP_TOKEN=\n")

    with open(os.path.join(repo, "hooks_shared", "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write("")
    with open(os.path.join(repo, "hooks_shared", "env_loader.py"), "w", encoding="utf-8") as fh:
        fh.write("# fake env_loader\n")

    return repo


def test_install_creates_deploy_dir_and_copies_files():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_fake_repo(tmp)
        fake_home = os.path.join(tmp, "home")

        with mock.patch.object(merge_hooks, "_claude_home", return_value=os.path.join(fake_home, ".claude")):
            merge_hooks.install(repo, "http://localhost:9500/mcp")

        deploy_dir = os.path.join(fake_home, ".claude", "hooks", "notnative-memory")
        assert os.path.isdir(deploy_dir)
        for script in merge_hooks._HOOK_SCRIPTS:
            assert os.path.isfile(os.path.join(deploy_dir, script)), f"missing {script}"
        assert os.path.isfile(os.path.join(deploy_dir, "hooks_shared", "__init__.py"))
        assert os.path.isfile(os.path.join(deploy_dir, "hooks_shared", "env_loader.py"))
        assert os.path.isfile(os.path.join(deploy_dir, "hooks.env"))
        assert os.path.isfile(os.path.join(deploy_dir, "hooks.env.example"))
        assert os.path.isfile(os.path.join(deploy_dir, "VERSION"))
    print("[OK] install copies all expected files into deploy dir")


def test_install_creates_hooks_env_with_mcp_url_patched():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_fake_repo(tmp)
        fake_home = os.path.join(tmp, "home")

        with mock.patch.object(merge_hooks, "_claude_home", return_value=os.path.join(fake_home, ".claude")):
            merge_hooks.install(repo, "http://custom-host:9500/mcp")

        env_path = os.path.join(fake_home, ".claude", "hooks", "notnative-memory", "hooks.env")
        with open(env_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        assert "MEMORY_MCP_URL=http://custom-host:9500/mcp" in content
    print("[OK] install patches MEMORY_MCP_URL into newly created hooks.env")


def test_install_preserves_existing_hooks_env_on_rerun():
    """Re-running installer must NOT overwrite user-edited hooks.env."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_fake_repo(tmp)
        fake_home = os.path.join(tmp, "home")

        with mock.patch.object(merge_hooks, "_claude_home", return_value=os.path.join(fake_home, ".claude")):
            # First install creates hooks.env from .example.
            merge_hooks.install(repo, "http://first-host:9500/mcp")

            env_path = os.path.join(fake_home, ".claude", "hooks", "notnative-memory", "hooks.env")
            # User edits hooks.env to add their own values.
            with open(env_path, "a", encoding="utf-8") as fh:
                fh.write("\nOPENAI_BASE_URL=http://my-gpu:1234/v1\n")
                fh.write("MEMORY_EXTRACT_MODEL=my-pinned-model\n")

            # Second install with a DIFFERENT mcp URL.
            merge_hooks.install(repo, "http://second-host:9500/mcp")

            with open(env_path, "r", encoding="utf-8") as fh:
                content = fh.read()
        # User additions must survive.
        assert "OPENAI_BASE_URL=http://my-gpu:1234/v1" in content
        assert "MEMORY_EXTRACT_MODEL=my-pinned-model" in content
        # Original MCP URL stays — re-install does not patch existing files.
        assert "first-host" in content
        assert "second-host" not in content
    print("[OK] install preserves existing hooks.env (incl. user additions) across re-runs")


def test_install_updates_hooks_env_example_on_rerun():
    """The .example file IS refreshed from the repo every install."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_fake_repo(tmp)
        fake_home = os.path.join(tmp, "home")
        deploy = os.path.join(fake_home, ".claude", "hooks", "notnative-memory")

        with mock.patch.object(merge_hooks, "_claude_home", return_value=os.path.join(fake_home, ".claude")):
            merge_hooks.install(repo, "http://localhost:9500/mcp")

            # Modify the repo's .example template.
            with open(os.path.join(repo, "claude", "hooks", "hooks.env.example"), "w", encoding="utf-8") as fh:
                fh.write("# updated template\nNEW_KEY=new_value\n")

            merge_hooks.install(repo, "http://localhost:9500/mcp")

            with open(os.path.join(deploy, "hooks.env.example"), "r", encoding="utf-8") as fh:
                content = fh.read()
        assert "NEW_KEY=new_value" in content
    print("[OK] install refreshes hooks.env.example from repo on every run")


def test_install_writes_settings_with_deploy_paths():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_fake_repo(tmp)
        fake_home = os.path.join(tmp, "home")

        with mock.patch.object(merge_hooks, "_claude_home", return_value=os.path.join(fake_home, ".claude")):
            merge_hooks.install(repo, "http://localhost:9500/mcp")

        settings_path = os.path.join(fake_home, ".claude", "settings.json")
        with open(settings_path, "r", encoding="utf-8") as fh:
            settings = json.load(fh)

        for event_name, config in merge_hooks._DESIRED_HOOKS.items():
            cmds = [
                h["command"]
                for group in settings["hooks"][event_name]
                for h in group.get("hooks", [])
            ]
            # Should reference deploy dir, NOT repo path.
            assert any("notnative-memory" in cmd for cmd in cmds)
            assert all(repo.replace("\\", "/") not in cmd for cmd in cmds)
    print("[OK] install registers hooks pointing at the deploy dir, not the repo")


def test_install_idempotent_on_settings_json():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_fake_repo(tmp)
        fake_home = os.path.join(tmp, "home")

        with mock.patch.object(merge_hooks, "_claude_home", return_value=os.path.join(fake_home, ".claude")):
            merge_hooks.install(repo, "http://localhost:9500/mcp")
            merge_hooks.install(repo, "http://localhost:9500/mcp")

            settings_path = os.path.join(fake_home, ".claude", "settings.json")
            with open(settings_path, "r", encoding="utf-8") as fh:
                settings = json.load(fh)

        for event_name in merge_hooks._DESIRED_HOOKS:
            # Each event should have exactly one group from us.
            our_groups = [
                g for g in settings["hooks"][event_name]
                if merge_hooks._group_script_name(g)
            ]
            assert len(our_groups) == 1, f"{event_name} should have 1 group, got {len(our_groups)}"
    print("[OK] install is idempotent — re-runs don't duplicate hook entries")


def test_install_sweeps_retired_hooks():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_fake_repo(tmp)
        fake_home = os.path.join(tmp, "home")

        # Pre-seed settings.json with a retired hook entry.
        claude_dir = os.path.join(fake_home, ".claude")
        os.makedirs(claude_dir)
        seed_settings = {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "",
                    "hooks": [{
                        "type": "command",
                        "command": "python /old/path/memory_inject.py",
                        "timeout": 10,
                    }],
                }],
            },
        }
        with open(os.path.join(claude_dir, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump(seed_settings, fh)

        with mock.patch.object(merge_hooks, "_claude_home", return_value=claude_dir):
            merge_hooks.install(repo, "http://localhost:9500/mcp")

            with open(os.path.join(claude_dir, "settings.json"), "r", encoding="utf-8") as fh:
                settings = json.load(fh)

        # PreToolUse entry should be gone (memory_inject.py is retired).
        assert "PreToolUse" not in settings["hooks"]
    print("[OK] install sweeps retired hooks from settings.json")


def test_patch_env_value_existing_key():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "test.env")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("# top comment\n")
            fh.write("KEY1=old_value\n")
            fh.write("KEY2=other\n")
        merge_hooks._patch_env_value(p, "KEY1", "new_value")
        with open(p, "r", encoding="utf-8") as fh:
            content = fh.read()
    assert "KEY1=new_value" in content
    assert "KEY2=other" in content
    assert "# top comment" in content
    print("[OK] _patch_env_value updates existing key, preserves rest")


def test_patch_env_value_appends_missing_key():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "test.env")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("EXISTING=value\n")
        merge_hooks._patch_env_value(p, "NEW_KEY", "new_value")
        with open(p, "r", encoding="utf-8") as fh:
            content = fh.read()
    assert "EXISTING=value" in content
    assert "NEW_KEY=new_value" in content
    print("[OK] _patch_env_value appends missing key")


if __name__ == "__main__":
    tests = [
        test_install_creates_deploy_dir_and_copies_files,
        test_install_creates_hooks_env_with_mcp_url_patched,
        test_install_preserves_existing_hooks_env_on_rerun,
        test_install_updates_hooks_env_example_on_rerun,
        test_install_writes_settings_with_deploy_paths,
        test_install_idempotent_on_settings_json,
        test_install_sweeps_retired_hooks,
        test_patch_env_value_existing_key,
        test_patch_env_value_appends_missing_key,
    ]
    for t in tests:
        t()
    print(f"\n[SUCCESS] All {len(tests)} merge_hooks tests passed")
