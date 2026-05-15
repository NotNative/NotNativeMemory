"""
Unit test: session_start.py captures unhandled exceptions to
`last_error.log` next to the hook script.

Background: when the SessionStart hook crashes, the Claude Code harness
chrome shows only the first line ("Traceback (most recent call last):")
with the body truncated. The hook writes the full traceback to a
sibling file so we can recover it after the fact.

Usage:
    python tests/test_session_start_error_log.py

The test forks a subprocess that imports the hook with a sabotaged
`_internal.env_loader` (or hooks.env) to force a controlled failure,
then asserts:
    1. The hook exits non-zero.
    2. `last_error.log` exists with the most recent exception body.
"""

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

BUNDLES = [
    ("claude", os.path.join(ROOT, "hook_bundles", "claude", "notnative-memory")),
    ("nna", os.path.join(ROOT, "hook_bundles", "nna", "notnative-memory")),
]


def _copy_bundle(src: str, dst: str) -> None:
    """Mirror the hook bundle into a tempdir so we can sabotage env
    loading without touching the real install."""
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            if name == "__pycache__":
                continue
            shutil.copytree(s, d, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(s, d)


def _force_failure_claude(bundle_dir: str) -> None:
    """The claude bundle imports `_internal.env_loader`. Replace the
    module with one that raises at import time."""
    internal_dir = os.path.join(bundle_dir, "_internal")
    os.makedirs(internal_dir, exist_ok=True)
    with open(os.path.join(internal_dir, "__init__.py"), "w") as f:
        f.write("")
    with open(os.path.join(internal_dir, "env_loader.py"), "w") as f:
        f.write("raise RuntimeError('sabotaged for test')\n")


def _force_failure_nna(bundle_dir: str) -> None:
    """The nna bundle reads hooks.env inline. Write a hooks.env whose
    parsing raises (binary garbage)."""
    # Make MAX_TOKENS parse fail by setting it to a non-int value via env.
    # Simpler: inject a hooks.env line that crashes int() on MAX_TOKENS.
    # The bundle calls `int(os.environ.get("MEMORY_SESSION_MAX_TOKENS", "600"))`
    # at module load *after* env-load. Sabotage the env-load itself by
    # making the file unreadable as utf-8.
    with open(os.path.join(bundle_dir, "hooks.env"), "wb") as f:
        f.write(b"\xff\xfe not = valid utf-8 \x00\x80\n")


def _run_hook(bundle_dir: str) -> int:
    """Invoke the hook in a subprocess and return its exit code."""
    script = os.path.join(bundle_dir, "session_start.py")
    proc = subprocess.run(
        [sys.executable, script],
        input='{"source":"clear","cwd":"."}',
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.returncode


def run() -> int:
    failed = 0

    def check(label: str, cond: bool) -> None:
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    for label, src in BUNDLES:
        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "notnative-memory")
            _copy_bundle(src, dst)

            if label == "claude":
                _force_failure_claude(dst)
            else:
                _force_failure_nna(dst)

            exit_code = _run_hook(dst)
            log_path = os.path.join(dst, "last_error.log")

            check(f"{label}: hook exits non-zero on failure",
                  exit_code != 0)
            check(f"{label}: last_error.log written",
                  os.path.exists(log_path))

            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8", errors="replace") as lf:
                    body = lf.read()
                check(f"{label}: log contains 'Traceback'",
                      "Traceback" in body)
                check(f"{label}: log has timestamp marker",
                      "session_start" in body)

    if failed:
        print(f"\n{failed} check(s) FAILED")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
