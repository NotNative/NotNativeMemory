#!/usr/bin/env python3
"""
Tests for hook_bundles/nna/notnative-memory/merge_hooks.py — the NNA installer.

Verifies parity with the Claude installer's contract: manifest is sourced from
the bundle on disk (not hardcoded), hooks.env is preserved across re-installs,
VERSION is stamped, and the canonical manifest carries the reasoning-safe
turn_analysis timeout. The user's actual ~/.nna/ is not touched — we monkeypatch
_hooks_dir() to a temp directory for each test.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).parent.parent
_BUNDLE_DIR = _REPO_ROOT / "hook_bundles" / "nna" / "notnative-memory"


def _load_merge_hooks():
    """Load NNA's merge_hooks.py by path under a unique module name.

    Both bundles ship a module literally named ``merge_hooks``; importing
    by bare name lets whichever bundle was first on sys.path win, which
    makes pytest order-dependent. Loading by file path with a unique
    module name avoids the collision.
    """
    spec = importlib.util.spec_from_file_location(
        "merge_hooks_nna", str(_BUNDLE_DIR / "merge_hooks.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["merge_hooks_nna"] = module
    spec.loader.exec_module(module)
    return module


merge_hooks = _load_merge_hooks()


def _make_fake_repo(tmp: str, manifest_override: dict | None = None) -> str:
    """Build a minimal repo layout that mirrors the real NNA bundle."""
    repo = os.path.join(tmp, "fake_repo")
    bundle_dir = os.path.join(repo, "hook_bundles", "nna", "notnative-memory")
    internal_dir = os.path.join(bundle_dir, "_internal")
    os.makedirs(internal_dir)

    for script in merge_hooks._SCRIPTS:
        with open(os.path.join(bundle_dir, script), "w", encoding="utf-8") as fh:
            fh.write(f"# fake {script}\n")

    with open(os.path.join(internal_dir, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write("")
    with open(os.path.join(internal_dir, "turn_analysis_core.py"), "w", encoding="utf-8") as fh:
        fh.write("# fake core\n")
    with open(os.path.join(internal_dir, "verbatim_core.py"), "w", encoding="utf-8") as fh:
        fh.write("# fake verbatim core\n")

    manifest = manifest_override if manifest_override is not None else {
        "name": "notnative-memory",
        "version": "1.1.0",
        "subscriptions": [
            {
                "event": "user.prompt.submit",
                "phase": "post",
                "command": "python turn_analysis.py",
                "blocking": False,
                "timeout_ms": 90000,
            },
        ],
    }
    with open(os.path.join(bundle_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    return repo


def test_install_copies_scripts_internal_and_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_fake_repo(tmp)
        fake_hooks_root = os.path.join(tmp, "fake_nna_hooks")

        with mock.patch.object(merge_hooks, "_hooks_dir", return_value=fake_hooks_root):
            merge_hooks.merge(repo, "http://localhost:9500/mcp")

        target = os.path.join(fake_hooks_root, "notnative-memory")
        assert os.path.isdir(target)
        for script in merge_hooks._SCRIPTS:
            assert os.path.isfile(os.path.join(target, script)), f"missing {script}"
        assert os.path.isfile(os.path.join(target, "_internal", "turn_analysis_core.py"))
        assert os.path.isfile(os.path.join(target, "_internal", "verbatim_core.py"))
        assert os.path.isfile(os.path.join(target, "manifest.json"))
        assert os.path.isfile(os.path.join(target, "hooks.env"))
        assert os.path.isfile(os.path.join(target, "VERSION"))
    print("[OK] NNA install copies scripts, _internal, manifest, hooks.env, VERSION")


def test_install_uses_manifest_from_bundle_not_hardcoded():
    """Regression guard: previous installer hardcoded a stale manifest in code
    while a different manifest.json sat in the repo, producing silent drift
    between the v1.0.0 written on disk and the v1.1.0 reviewers thought was
    canonical. The installer must read manifest.json from the bundle so the
    JSON is the only source of truth."""
    custom = {
        "name": "notnative-memory",
        "version": "9.9.9-test",
        "subscriptions": [
            {
                "event": "user.prompt.submit",
                "phase": "post",
                "command": "python turn_analysis.py",
                "blocking": False,
                "timeout_ms": 90000,
            },
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_fake_repo(tmp, manifest_override=custom)
        fake_hooks_root = os.path.join(tmp, "fake_nna_hooks")

        with mock.patch.object(merge_hooks, "_hooks_dir", return_value=fake_hooks_root):
            merge_hooks.merge(repo, "http://localhost:9500/mcp")

        installed = os.path.join(fake_hooks_root, "notnative-memory", "manifest.json")
        with open(installed, "r", encoding="utf-8") as fh:
            written = json.load(fh)
    assert written == custom, (
        "Installed manifest must match the bundle's manifest.json byte-for-byte. "
        "If they diverge, the installer is hardcoding subscriptions and we've "
        "regressed back into silent drift."
    )
    print("[OK] NNA install reads manifest from bundle (not hardcoded)")


def test_real_repo_manifest_has_reasoning_safe_turn_analysis_timeout():
    """Sanity check the ACTUAL committed manifest, not a fixture.

    turn_analysis fires a per-turn LLM call; reasoning models routinely burn
    30-60s on hidden CoT before emitting JSON. NNA's harness kills any
    subprocess that exceeds timeout_ms, silently dropping the analyzer's
    work and the row that would have been logged. 90s is the floor that
    keeps reasoning-model backends working; do not drop this below 60s.
    """
    real_manifest = _BUNDLE_DIR / "manifest.json"
    with open(real_manifest, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    turn_subs = [
        sub for sub in manifest["subscriptions"]
        if "turn_analysis.py" in sub.get("command", "")
    ]
    assert turn_subs, "manifest must subscribe turn_analysis.py to user.prompt.submit/post"
    for sub in turn_subs:
        assert sub["timeout_ms"] >= 60000, (
            f"turn_analysis timeout_ms must be >= 60000 (got {sub['timeout_ms']}). "
            f"Reasoning models exceed shorter timeouts and the harness kills the "
            f"subprocess before the analyzer can log."
        )
    print("[OK] committed NNA manifest has reasoning-safe turn_analysis timeout (>= 60000ms)")


def test_real_repo_manifest_has_generous_verbatim_timeout():
    real_manifest = _BUNDLE_DIR / "manifest.json"
    with open(real_manifest, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    verbatim_subs = [
        sub for sub in manifest["subscriptions"]
        if "verbatim_capture.py" in sub.get("command", "")
    ]
    assert verbatim_subs, "manifest must subscribe verbatim_capture.py"
    for sub in verbatim_subs:
        assert sub["timeout_ms"] >= 20000, (
            f"verbatim_capture timeout_ms must be >= 20000 (got {sub['timeout_ms']}). "
            "First-use embedding/model warmup can exceed the old 8s budget and "
            "make the MCP client disconnect while NNM still stores the chunk."
        )
    print("[OK] committed NNA manifest has generous verbatim_capture timeout (>= 20000ms)")


def test_install_preserves_existing_hooks_env_on_rerun():
    """User edits to hooks.env (tokens, model pins) must survive a re-install.
    Only scripts and the manifest get refreshed."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_fake_repo(tmp)
        fake_hooks_root = os.path.join(tmp, "fake_nna_hooks")
        target = os.path.join(fake_hooks_root, "notnative-memory")

        with mock.patch.object(merge_hooks, "_hooks_dir", return_value=fake_hooks_root):
            merge_hooks.merge(repo, "http://first-host:9500/mcp")

            env_path = os.path.join(target, "hooks.env")
            with open(env_path, "a", encoding="utf-8") as fh:
                fh.write("\nMEMORY_MCP_TOKEN=nnm_test.userpasted\n")
                fh.write("MEMORY_EXTRACT_MODEL=my-pinned-model\n")

            merge_hooks.merge(repo, "http://second-host:9500/mcp")

            with open(env_path, "r", encoding="utf-8") as fh:
                content = fh.read()

        assert "MEMORY_MCP_TOKEN=nnm_test.userpasted" in content
        assert "MEMORY_EXTRACT_MODEL=my-pinned-model" in content
        assert "first-host" in content
        assert "second-host" not in content
    print("[OK] NNA install preserves existing hooks.env across re-runs")


def test_install_overwrites_scripts_and_manifest_on_rerun():
    """Scripts and manifest MUST get refreshed even when already present —
    that's the whole point of running the installer after a `git pull`."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_fake_repo(tmp)
        fake_hooks_root = os.path.join(tmp, "fake_nna_hooks")
        target = os.path.join(fake_hooks_root, "notnative-memory")

        with mock.patch.object(merge_hooks, "_hooks_dir", return_value=fake_hooks_root):
            merge_hooks.merge(repo, "http://localhost:9500/mcp")

            # Repo evolves: bump a script body and the manifest version.
            new_script_body = "# v2 contents\n"
            with open(os.path.join(repo, "hook_bundles", "nna", "notnative-memory", "turn_analysis.py"), "w", encoding="utf-8") as fh:
                fh.write(new_script_body)
            with open(os.path.join(repo, "hook_bundles", "nna", "notnative-memory", "manifest.json"), "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
            manifest["version"] = "9.9.9-bumped"
            with open(os.path.join(repo, "hook_bundles", "nna", "notnative-memory", "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, indent=2)

            merge_hooks.merge(repo, "http://localhost:9500/mcp")

            with open(os.path.join(target, "turn_analysis.py"), "r", encoding="utf-8") as fh:
                installed_script = fh.read()
            with open(os.path.join(target, "manifest.json"), "r", encoding="utf-8") as fh:
                installed_manifest = json.load(fh)

        assert installed_script == new_script_body, "re-install must overwrite scripts"
        assert installed_manifest["version"] == "9.9.9-bumped", "re-install must overwrite manifest"
    print("[OK] NNA install overwrites scripts and manifest on re-run")


def test_install_writes_version_marker():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_fake_repo(tmp)
        fake_hooks_root = os.path.join(tmp, "fake_nna_hooks")

        with mock.patch.object(merge_hooks, "_hooks_dir", return_value=fake_hooks_root):
            merge_hooks.merge(repo, "http://localhost:9500/mcp")

        version_path = os.path.join(fake_hooks_root, "notnative-memory", "VERSION")
        with open(version_path, "r", encoding="utf-8") as fh:
            content = fh.read()
    assert "installed_at=" in content
    assert "source_repo=" in content
    assert repo.replace("\\", "/") in content
    print("[OK] NNA install writes VERSION marker with timestamp and source repo")


def test_install_raises_when_manifest_missing_from_bundle():
    """If someone deletes manifest.json from the bundle by accident, the
    installer should surface that loudly rather than silently writing nothing
    or falling back to a stale hardcoded copy."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_fake_repo(tmp)
        fake_hooks_root = os.path.join(tmp, "fake_nna_hooks")

        os.remove(os.path.join(repo, "hook_bundles", "nna", "notnative-memory", "manifest.json"))

        with mock.patch.object(merge_hooks, "_hooks_dir", return_value=fake_hooks_root):
            try:
                merge_hooks.merge(repo, "http://localhost:9500/mcp")
            except FileNotFoundError:
                print("[OK] NNA install raises FileNotFoundError when manifest.json missing")
                return
    raise AssertionError("Expected FileNotFoundError when manifest.json missing from bundle")


if __name__ == "__main__":
    test_install_copies_scripts_internal_and_manifest()
    test_install_uses_manifest_from_bundle_not_hardcoded()
    test_real_repo_manifest_has_reasoning_safe_turn_analysis_timeout()
    test_real_repo_manifest_has_generous_verbatim_timeout()
    test_install_preserves_existing_hooks_env_on_rerun()
    test_install_overwrites_scripts_and_manifest_on_rerun()
    test_install_writes_version_marker()
    test_install_raises_when_manifest_missing_from_bundle()
