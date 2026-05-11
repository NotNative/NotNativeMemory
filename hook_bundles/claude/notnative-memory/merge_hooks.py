#!/usr/bin/env python3
"""
Idempotent install of NotNativeMemory hooks for Claude Code.

Copies the Python hook scripts and shared modules from the repo to a
deployment directory under the user's home, then registers them in
~/.claude/settings.json. Preserves existing user-set values in
hooks.env across re-runs.

Default deployment layout:

    ~/.claude/hooks/notnative-memory/
        compact_guard.py
        session_start.py
        turn_analysis.py
        user_prompt_inject.py
        _internal/
            __init__.py
            env_loader.py
            turn_analysis_core.py
        hooks.env             (user-editable config; preserved across re-runs)
        hooks.env.example     (canonical reference)
        VERSION               (timestamp of last install)

Reasons:
- Repo files stay clean (no environment-specific values).
- Settings.json references a stable deployment path, not the repo —
  moving / deleting / updating the repo doesn't break installed hooks.
- Multiple plugins coexist under ~/.claude/hooks/<plugin>/ without
  fighting over a single config file.

Usage:
    python merge_hooks.py <repo_path> [mcp_url]

Arguments:
    repo_path  - Absolute path to the NotNativeMemory checkout.
    mcp_url    - Optional MCP server URL. Default:
                 http://localhost:9500/mcp. Only applied when
                 hooks.env doesn't already exist.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
from typing import Tuple

# Plugin-relative install layout. ~/.claude/hooks/notnative-memory/
PLUGIN_NAME = "notnative-memory"

# Hook scripts to deploy. Source path is relative to repo's hook_bundles/claude/notnative-memory/.
_HOOK_SCRIPTS = {
    "compact_guard.py",
    "session_start.py",
    "turn_analysis.py",
    "user_prompt_inject.py",
    "pre_tool_safety.py",
}

# Retired scripts: any settings.json entry referencing one gets cleaned
# during merge. Keep these listed until you're willing to lose the
# auto-cleanup ability for users upgrading from older installs.
_RETIRED_SCRIPTS = {
    "memory_inject.py",       # retired 2026-04-19 (PreToolUse)
}

# Hook event registrations: which event triggers which deployed script.
_DESIRED_HOOKS = {
    "UserPromptSubmit": {
        "matcher": "",
        "script": "user_prompt_inject.py",
        "timeout": 10,
    },
    "SessionStart": {
        "matcher": "",
        "script": "session_start.py",
        "timeout": 10,
    },
    "PreCompact": {
        "matcher": "",
        "script": "compact_guard.py",
        "timeout": 10,
    },
    "Stop": {
        "matcher": "",
        "script": "turn_analysis.py",
        "timeout": 15,
    },
    "PreToolUse": {
        "matcher": "",
        "script": "pre_tool_safety.py",
        "timeout": 5,
    },
}

# All scripts known to the installer (current + retired). Used to
# RECOGNIZE our entries in settings.json.
_ALL_KNOWN_SCRIPTS = _HOOK_SCRIPTS | _RETIRED_SCRIPTS


def _claude_home() -> str:
    """Resolve ~/.claude/ across platforms (USERPROFILE on Windows)."""
    home = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    return os.path.join(home, ".claude")


def _deploy_dir() -> str:
    """The deployment directory for this plugin."""
    return os.path.join(_claude_home(), "hooks", PLUGIN_NAME)


def _settings_path() -> str:
    """Path to ~/.claude/settings.json."""
    return os.path.join(_claude_home(), "settings.json")


def _build_hook_command(deploy_dir: str, script_name: str) -> str:
    """Build the hook command string referencing the DEPLOYED script."""
    normalized = deploy_dir.replace("\\", "/")
    return f"python {normalized}/{script_name}"


def _group_script_name(group: dict) -> str:
    """Return the plugin script name for a settings hook group, or "".

    A group is "ours" if its command references one of _ALL_KNOWN_SCRIPTS.
    """
    for hook in group.get("hooks", []):
        cmd = hook.get("command", "")
        for script_name in _ALL_KNOWN_SCRIPTS:
            if script_name in cmd:
                return script_name
    return ""


def _load_settings(settings_file: str) -> dict:
    """Load settings.json, creating a backup on parse failure."""
    if not os.path.exists(settings_file):
        return {}
    with open(settings_file, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            print(f"  Warning: {settings_file} has invalid JSON. Creating backup.")
            try:
                f.seek(0)
                with open(settings_file + ".bak", "w", encoding="utf-8") as bak:
                    bak.write(f.read())
            except OSError as exc:
                print(f"  Warning: could not create backup: {exc}", file=sys.stderr)
            return {}


def _copy_runtime_files(repo_path: str, deploy_dir: str) -> int:
    """Copy hook scripts + bundle-local _internal/ + hooks.env.example.

    Returns the count of files copied (for logging).
    """
    os.makedirs(deploy_dir, exist_ok=True)
    src_hooks = os.path.join(repo_path, "hook_bundles", "claude", "notnative-memory")
    src_internal = os.path.join(src_hooks, "_internal")

    copied = 0

    # Hook scripts
    for script in _HOOK_SCRIPTS:
        src = os.path.join(src_hooks, script)
        dst = os.path.join(deploy_dir, script)
        if not os.path.exists(src):
            print(f"  Warning: source missing: {src}", file=sys.stderr)
            continue
        shutil.copy2(src, dst)
        copied += 1

    # Bundle-local _internal/ package (env_loader.py + turn_analysis_core.py + __init__.py)
    dst_internal = os.path.join(deploy_dir, "_internal")
    os.makedirs(dst_internal, exist_ok=True)
    if os.path.isdir(src_internal):
        for entry in os.listdir(src_internal):
            if entry == "__pycache__" or entry.startswith("."):
                continue
            src_path = os.path.join(src_internal, entry)
            dst_path = os.path.join(dst_internal, entry)
            if os.path.isfile(src_path):
                shutil.copy2(src_path, dst_path)
                copied += 1

    # Canonical hooks.env.example reference
    example_src = os.path.join(src_hooks, "hooks.env.example")
    if os.path.exists(example_src):
        shutil.copy2(example_src, os.path.join(deploy_dir, "hooks.env.example"))
        copied += 1

    # Stamp install timestamp
    with open(os.path.join(deploy_dir, "VERSION"), "w", encoding="utf-8") as fh:
        fh.write(f"installed_at={datetime.datetime.now().isoformat(timespec='seconds')}\n")
        fh.write(f"source_repo={repo_path.replace(chr(92), '/')}\n")

    return copied


def _ensure_hooks_env(deploy_dir: str, mcp_url: str) -> Tuple[str, bool]:
    """Ensure hooks.env exists at deploy_dir, copying from .example if missing.

    Preserves existing hooks.env on re-install — only the .example file
    gets refreshed from the repo. The active hooks.env is the user's
    edit space.

    Returns (path_to_hooks_env, created_new) where created_new is True
    only on first install (file didn't exist).
    """
    env_path = os.path.join(deploy_dir, "hooks.env")
    if os.path.exists(env_path):
        return env_path, False

    example_path = os.path.join(deploy_dir, "hooks.env.example")
    if not os.path.exists(example_path):
        # No template available — write a minimal stub.
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("# NotNativeMemory hooks config\n")
            fh.write(f"MEMORY_MCP_URL={mcp_url}\n")
            fh.write("MEMORY_MCP_TOKEN=\n")
        return env_path, True

    # Copy example to live config, then patch in the user-supplied MCP URL.
    shutil.copy2(example_path, env_path)
    if mcp_url and mcp_url != "http://localhost:9500/mcp":
        _patch_env_value(env_path, "MEMORY_MCP_URL", mcp_url)
    return env_path, True


def _patch_env_value(env_path: str, key: str, new_value: str) -> None:
    """Update a single KEY=VALUE line in env_path in place.

    Preserves comments, ordering, and other lines. If the key isn't
    present, appends it at the end.
    """
    with open(env_path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    found = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        existing_key = stripped.split("=", 1)[0].strip()
        if existing_key == key:
            lines[i] = f"{key}={new_value}\n"
            found = True
            break

    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"{key}={new_value}\n")

    with open(env_path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def _sweep_retired_hooks(settings: dict) -> int:
    """Remove settings.json entries pointing at retired scripts."""
    if "hooks" not in settings:
        return 0
    removed = 0
    for event_name in list(settings["hooks"].keys()):
        groups = settings["hooks"][event_name]
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            script = _group_script_name(group)
            if script and script in _RETIRED_SCRIPTS:
                removed += 1
                print(f"  Retired {event_name} hook for {script}")
                continue
            kept.append(group)
        if kept:
            settings["hooks"][event_name] = kept
        else:
            del settings["hooks"][event_name]
    return removed


def _upsert_hook_registrations(settings: dict, deploy_dir: str) -> int:
    """Add or update hook entries in settings.json. Returns # of changes."""
    if "hooks" not in settings:
        settings["hooks"] = {}

    changes = 0
    for event_name, config in _DESIRED_HOOKS.items():
        if event_name not in settings["hooks"]:
            settings["hooks"][event_name] = []

        groups = settings["hooks"][event_name]
        desired_command = _build_hook_command(deploy_dir, config["script"])

        found = False
        for group in groups:
            if _group_script_name(group) == config["script"]:
                group["matcher"] = config["matcher"]
                group["hooks"] = [{
                    "type": "command",
                    "command": desired_command,
                    "timeout": config["timeout"],
                }]
                found = True
                changes += 1
                print(f"  Updated existing {event_name} hook for {config['script']}")
                break

        if not found:
            groups.append({
                "matcher": config["matcher"],
                "hooks": [{
                    "type": "command",
                    "command": desired_command,
                    "timeout": config["timeout"],
                }],
            })
            changes += 1
            print(f"  Added {event_name} hook for {config['script']}")

    return changes


def install(
    repo_path: str,
    mcp_url: str = "http://localhost:9500/mcp",
) -> int:
    """Run the full install. Returns the number of changes made."""
    if not repo_path:
        raise ValueError("repo_path cannot be empty")
    repo_path = os.path.abspath(repo_path)

    deploy_dir = _deploy_dir()
    print(f"  Deploy dir: {deploy_dir}")

    # 1. Copy runtime files into the deploy dir.
    copied = _copy_runtime_files(repo_path, deploy_dir)
    print(f"  Copied {copied} files into deploy dir")

    # 2. Ensure hooks.env exists (copy from .example or stub).
    env_path, created = _ensure_hooks_env(deploy_dir, mcp_url)
    if created:
        print(f"  Created {env_path} (edit to configure analysis LLM, etc.)")
    else:
        print(f"  Preserved existing {env_path}")

    # 3. Update ~/.claude/settings.json.
    settings_file = _settings_path()
    os.makedirs(os.path.dirname(settings_file), exist_ok=True)
    settings = _load_settings(settings_file)

    changes = 0
    changes += _sweep_retired_hooks(settings)
    changes += _upsert_hook_registrations(settings, deploy_dir)

    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    print(f"  Saved settings.json ({changes} hook changes)")

    return changes


# Back-compat alias for any caller still using the old name.
merge = install


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python merge_hooks.py <repo_path> [mcp_url]")
        sys.exit(1)

    path = sys.argv[1]
    url = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:9500/mcp"
    install(path, url)
