#!/usr/bin/env python3
"""
Idempotent install of NotNativeMemory hooks for Codex.

Deploys a Codex-specific bundle under ~/.codex/hooks/notnative-memory/ and
merges hook registrations into ~/.codex/hooks.json.

Usage:
    python merge_hooks.py <repo_path> [mcp_url]
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
from typing import Dict, List, Tuple


PLUGIN_NAME = "notnative-memory"

_HOOK_SCRIPTS = {
    "codex_hook_common.py",
    "session_start.py",
    "user_prompt_submit.py",
    "post_tool_use.py",
    "stop.py",
}

_DESIRED_HOOKS = [
    {
        "event": "SessionStart",
        "matcher": "",
        "script": "session_start.py",
        "timeout": 10,
        "statusMessage": "Loading NNM context",
    },
    {
        "event": "UserPromptSubmit",
        "matcher": "",
        "script": "user_prompt_submit.py",
        "timeout": 60,
        "statusMessage": "Searching NNM memory",
    },
    {
        "event": "PostToolUse",
        "matcher": "",
        "script": "post_tool_use.py",
        "timeout": 8,
        "statusMessage": "Capturing tool telemetry",
    },
    {
        "event": "Stop",
        "matcher": "",
        "script": "stop.py",
        "timeout": 8,
        "statusMessage": "Capturing turn summary",
    },
]


def _home() -> str:
    return os.environ.get("USERPROFILE", os.path.expanduser("~"))


def _codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(_home(), ".codex")


def _deploy_dir() -> str:
    return os.path.join(_codex_home(), "hooks", PLUGIN_NAME)


def _hooks_path() -> str:
    return os.path.join(_codex_home(), "hooks.json")


def _python_executable() -> str:
    return sys.executable or "python"


def _json_command(deploy_dir: str, script_name: str) -> str:
    py = _python_executable().replace("\\", "/")
    script = os.path.join(deploy_dir, script_name).replace("\\", "/")
    return f'"{py}" "{script}"'


def _load_json(path: str) -> Dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            backup = path + ".bak"
            try:
                shutil.copy2(path, backup)
                print(f"  Warning: invalid hooks.json; copied backup to {backup}")
            except OSError as exc:
                print(f"  Warning: could not create backup: {exc}", file=sys.stderr)
            return {}


def _script_from_group(group: Dict) -> str:
    for hook in group.get("hooks", []) or []:
        command = str(hook.get("command") or "")
        for script in _HOOK_SCRIPTS:
            if script in command:
                return script
    return ""


def _copy_runtime_files(repo_path: str, deploy_dir: str) -> int:
    os.makedirs(deploy_dir, exist_ok=True)
    src_dir = os.path.join(repo_path, "hook_bundles", "codex", PLUGIN_NAME)
    copied = 0
    for script in _HOOK_SCRIPTS:
        src = os.path.join(src_dir, script)
        dst = os.path.join(deploy_dir, script)
        if not os.path.exists(src):
            print(f"  Warning: source missing: {src}", file=sys.stderr)
            continue
        shutil.copy2(src, dst)
        copied += 1

    for extra in ("hooks.env.example", "README.md"):
        src = os.path.join(src_dir, extra)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(deploy_dir, extra))
            copied += 1

    with open(os.path.join(deploy_dir, "VERSION"), "w", encoding="utf-8") as fh:
        fh.write(f"installed_at={datetime.datetime.now().isoformat(timespec='seconds')}\n")
        fh.write(f"source_repo={repo_path.replace(chr(92), '/')}\n")
    return copied


def _ensure_hooks_env(deploy_dir: str, mcp_url: str) -> Tuple[str, bool]:
    env_path = os.path.join(deploy_dir, "hooks.env")
    if os.path.exists(env_path):
        return env_path, False
    example = os.path.join(deploy_dir, "hooks.env.example")
    if os.path.exists(example):
        shutil.copy2(example, env_path)
    else:
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("# NotNativeMemory Codex hooks config\n")
    _patch_env(env_path, "MEMORY_MCP_URL", mcp_url)
    return env_path, True


def _patch_env(env_path: str, key: str, value: str) -> None:
    with open(env_path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    found = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}\n"
            found = True
            break
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"{key}={value}\n")
    with open(env_path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def _upsert_hooks(config: Dict, deploy_dir: str) -> int:
    hooks = config.setdefault("hooks", {})
    changes = 0
    for desired in _DESIRED_HOOKS:
        event = desired["event"]
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            groups = []
            hooks[event] = groups

        command = _json_command(deploy_dir, desired["script"])
        group = {
            "matcher": desired["matcher"],
            "hooks": [{
                "type": "command",
                "command": command,
                "timeout": desired["timeout"],
                "statusMessage": desired["statusMessage"],
            }],
        }

        found = False
        for i, existing in enumerate(groups):
            if _script_from_group(existing) == desired["script"]:
                groups[i] = group
                found = True
                changes += 1
                print(f"  Updated {event} hook for {desired['script']}")
                break
        if not found:
            groups.append(group)
            changes += 1
            print(f"  Added {event} hook for {desired['script']}")
    return changes


def install(repo_path: str, mcp_url: str = "http://127.0.0.1:9500/mcp") -> int:
    repo_path = os.path.abspath(repo_path)
    deploy_dir = _deploy_dir()
    print(f"  Deploy dir: {deploy_dir}")

    copied = _copy_runtime_files(repo_path, deploy_dir)
    print(f"  Copied {copied} files into deploy dir")

    env_path, created = _ensure_hooks_env(deploy_dir, mcp_url)
    if created:
        print(f"  Created {env_path}")
    else:
        print(f"  Preserved existing {env_path}")

    hooks_path = _hooks_path()
    os.makedirs(os.path.dirname(hooks_path), exist_ok=True)
    config = _load_json(hooks_path)
    changes = _upsert_hooks(config, deploy_dir)
    with open(hooks_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    print(f"  Saved hooks.json ({changes} hook changes)")
    print("  Codex may ask you to trust these hooks with /hooks before they run.")
    return changes


merge = install


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python merge_hooks.py <repo_path> [mcp_url]")
        sys.exit(1)
    repo = sys.argv[1]
    url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:9500/mcp"
    install(repo, url)
