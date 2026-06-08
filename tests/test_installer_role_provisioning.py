"""
Regression checks for installer handling of memory_app role provisioning.

These are text-level guards because the installers are shell scripts. The
failure they protect against is operational: continuing after
ensure_app_role.py fails leaves the MCP container in a crash loop with
"password authentication failed for user memory_app".

Usage:
    python tests/test_installer_role_provisioning.py
"""

import os
import sys


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), "r", encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    checks: list[tuple[str, bool]] = []

    windows = read("install_windows.ps1")
    linux = read("install_linux.sh")
    ensure = read("docker/init/ensure_app_role.py")

    checks.append((
        "windows installer hard-fails when docker role provisioning fails",
        "Role provisioning failed. MCP would fail to authenticate as $APP_DB_USER"
        in windows
        and "exit 1" in windows[
            windows.index("Role provisioning failed. MCP would fail")
            : windows.index("# -----------------------------------------------------------------------", windows.index("Role provisioning failed. MCP would fail"))
        ],
    ))
    checks.append((
        "linux installer hard-fails when docker role provisioning fails",
        "Role provisioning failed. MCP would fail to authenticate as $APP_DB_USER"
        in linux
        and "exit 1" in linux[
            linux.index("Role provisioning failed. MCP would fail")
            : linux.index("# -----------------------------------------------------------------------", linux.index("Role provisioning failed. MCP would fail"))
        ],
    ))
    checks.append((
        "docker installer recreates MCP container after env repair",
        "--force-recreate mcp" in windows and "--force-recreate mcp" in linux,
    ))
    checks.append((
        "ensure_app_role returns failure on insufficient privilege",
        "return 1" in ensure[
            ensure.index("except asyncpg.InsufficientPrivilegeError")
            : ensure.index("finally:", ensure.index("except asyncpg.InsufficientPrivilegeError"))
        ],
    ))
    checks.append((
        "installers no longer soft-continue role provisioning failures",
        "Role provisioning reported errors; continuing" not in windows
        and "Role provisioning reported errors; continuing" not in linux,
    ))

    failed = 0
    for label, ok in checks:
        if ok:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    print("---")
    print(f"{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
