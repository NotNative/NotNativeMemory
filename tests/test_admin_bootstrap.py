"""
Unit tests for lib/admin_bootstrap.py.

Pure filesystem + stubbed auth_db. No DB, no network. Runs against
a temp directory so it never touches the real ./state/.

Usage:
    python tests/test_admin_bootstrap.py
"""

import asyncio
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from lib import admin_bootstrap


def run() -> int:
    failed = 0

    def check(label, cond):
        nonlocal failed
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failed += 1

    # Redirect the state dir to a temp location so we don't touch the
    # real project one.
    tmp = tempfile.mkdtemp(prefix="nnm-bootstrap-test-")
    os.chdir(tmp)

    try:
        # -- fresh state: file does not exist --------------------------
        check(
            "bootstrap_file_exists is False initially",
            not admin_bootstrap.bootstrap_file_exists(),
        )
        check(
            "read_bootstrap_token returns None when file missing",
            admin_bootstrap.read_bootstrap_token() is None,
        )

        # -- write a token and read it back ----------------------------
        admin_bootstrap._ensure_state_dir()
        admin_bootstrap._write_token(
            admin_bootstrap.bootstrap_file_path(), "my-test-token",
        )
        check(
            "bootstrap_file_exists True after write",
            admin_bootstrap.bootstrap_file_exists(),
        )
        check(
            "read_bootstrap_token returns exact value",
            admin_bootstrap.read_bootstrap_token() == "my-test-token",
        )

        # -- validate_bootstrap_token ---------------------------------
        check(
            "validate accepts correct token",
            admin_bootstrap.validate_bootstrap_token("my-test-token"),
        )
        check(
            "validate rejects wrong token",
            not admin_bootstrap.validate_bootstrap_token("wrong"),
        )
        check(
            "validate rejects empty string",
            not admin_bootstrap.validate_bootstrap_token(""),
        )
        check(
            "validate rejects None",
            not admin_bootstrap.validate_bootstrap_token(None),
        )

        # -- delete_bootstrap_file is idempotent ----------------------
        check(
            "delete_bootstrap_file returns True on first call",
            admin_bootstrap.delete_bootstrap_file(),
        )
        check(
            "delete_bootstrap_file returns False when already gone",
            not admin_bootstrap.delete_bootstrap_file(),
        )
        check(
            "validate returns False after delete",
            not admin_bootstrap.validate_bootstrap_token("my-test-token"),
        )

        # -- ensure_bootstrap_if_needed with stubbed count_admins ------
        # Patch auth_db so we don't need a live DB.
        import types

        class FakeAuthDB:
            count = 0

            async def count_admins(self):
                return self.count

        fake = FakeAuthDB()
        fake_mod = types.SimpleNamespace(count_admins=fake.count_admins)

        # Patch the imported reference used inside ensure_bootstrap.
        # ensure_bootstrap imports auth_db lazily at call time; we inject
        # a fake into sys.modules so that import resolves to our stub.
        import lib
        real_auth_db = sys.modules.get("lib.auth_db")
        sys.modules["lib.auth_db"] = fake_mod

        try:
            # 0 admins: creates file and returns path.
            fake.count = 0
            path = asyncio.run(admin_bootstrap.ensure_bootstrap_if_needed())
            check("no-admin path returns bootstrap file path",
                  path == admin_bootstrap.bootstrap_file_path())
            check("file exists after ensure", admin_bootstrap.bootstrap_file_exists())

            # Token is generated with the nnm_admin_ prefix.
            tok = admin_bootstrap.read_bootstrap_token()
            check("generated token has nnm_admin_ prefix",
                  tok is not None and tok.startswith("nnm_admin_"))

            # Second call preserves the same token (idempotent).
            path2 = asyncio.run(admin_bootstrap.ensure_bootstrap_if_needed())
            tok2 = admin_bootstrap.read_bootstrap_token()
            check("second ensure preserves existing token", tok == tok2)

            # When admin exists: returns None and cleans stale file.
            fake.count = 1
            result = asyncio.run(admin_bootstrap.ensure_bootstrap_if_needed())
            check("admin-exists path returns None", result is None)
            check(
                "admin-exists path deletes stale file",
                not admin_bootstrap.bootstrap_file_exists(),
            )

            # When count_admins raises: returns None, doesn't crash.
            async def broken_count():
                raise RuntimeError("db down")

            fake_mod.count_admins = broken_count
            result = asyncio.run(admin_bootstrap.ensure_bootstrap_if_needed())
            check("count_admins exception returns None", result is None)

        finally:
            if real_auth_db is not None:
                sys.modules["lib.auth_db"] = real_auth_db
            else:
                sys.modules.pop("lib.auth_db", None)

    finally:
        os.chdir(ROOT)
        shutil.rmtree(tmp, ignore_errors=True)

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
