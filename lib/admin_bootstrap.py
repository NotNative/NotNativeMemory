"""
First-run root bootstrap.

When the operator chooses to claim the instance and no user has
is_admin=true, we write a random token to `./state/admin_bootstrap.txt`.
The operator (who by definition has filesystem access to the server
host) reads the file and presents the token to the claim page. On
successful claim, NNM creates the reserved root principal, returns a
root Bearer token once, and deletes the file.

Filesystem = operator premise:

    The whole scheme assumes that whoever has access to the state
    directory IS authorized to claim root. That holds for personal
    deployments where one person runs the server on their own box.
    For shared-host / multi-tenant scenarios the operator must
    protect the state directory with Unix perms (0700 on the dir,
    0600 on the file — both set below) so only the intended identity
    can read it. Documented in SECURITY.md.

Idempotent on restart:

    If a bootstrap file already exists and no admin is registered,
    subsequent startups leave the file alone. The operator can read
    it on their own schedule; server restarts do not rotate a token
    they might already have copied.

Recovery:

    `python server.py --reset-admin` demotes every existing admin
    (setting is_admin=false and bumping their token_generation),
    then wipes the bootstrap file. The next startup regenerates it,
    and the operator claims admin fresh.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from typing import Optional


_log = logging.getLogger("notnative.admin_bootstrap")


# Paths are relative to the server's working directory. The Docker
# image has WORKDIR=/app so `./state/` lands at `/app/state/`, which
# docker-compose.yml bind-mounts back to the host's `./state/`.
STATE_DIR = "state"
BOOTSTRAP_FILENAME = "admin_bootstrap.txt"


def _is_unix() -> bool:
    return sys.platform != "win32"


def state_dir_path() -> str:
    """Absolute path of the state directory."""
    return os.path.abspath(STATE_DIR)


def bootstrap_file_path() -> str:
    """Absolute path of the bootstrap token file."""
    return os.path.join(state_dir_path(), BOOTSTRAP_FILENAME)


def _ensure_state_dir() -> None:
    path = state_dir_path()
    os.makedirs(path, exist_ok=True)
    if _is_unix():
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            _log.debug("chmod 0700 on %s failed: %s", path, exc)


def _write_token(path: str, token: str) -> None:
    # Write atomically-enough: open + write + flush. The file is small
    # and the only reader is a human with a terminal. If a restart
    # races with a partial write, the human re-reads.
    with open(path, "w", encoding="utf-8") as f:
        f.write(token + "\n")
        f.flush()
    if _is_unix():
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            _log.debug("chmod 0600 on %s failed: %s", path, exc)


def _generate_token() -> str:
    # 32 url-safe bytes = 43 chars of base64. Plenty of entropy for a
    # one-shot handshake token. Prefix helps the operator recognize
    # the file's purpose at a glance.
    return "nnm_admin_" + secrets.token_urlsafe(32)


def bootstrap_file_exists() -> bool:
    return os.path.isfile(bootstrap_file_path())


def read_bootstrap_token() -> Optional[str]:
    """Return the current file's token, or None if no file exists."""
    path = bootstrap_file_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError as exc:
        _log.debug("could not read %s: %s", path, exc)
        return None


def delete_bootstrap_file() -> bool:
    """Remove the bootstrap file if present. Returns True if it was
    removed, False if it was already absent."""
    path = bootstrap_file_path()
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        _log.debug("could not delete %s: %s", path, exc)
        return False


def validate_bootstrap_token(candidate: str) -> bool:
    """
    Constant-time compare of a caller-supplied token against the
    current bootstrap file. Returns False when the file is missing
    or the token does not match. The constant-time compare matters
    because an attacker who knows we're waiting for a bootstrap can
    otherwise time-probe the comparison.
    """
    if not isinstance(candidate, str) or not candidate:
        return False
    stored = read_bootstrap_token()
    if stored is None:
        return False
    import hmac
    return hmac.compare_digest(candidate, stored)


async def ensure_bootstrap_if_needed() -> Optional[str]:
    """
    Called on server startup. If no admin exists, make sure a bootstrap
    token file is on disk. Returns the token path when a file is
    present (new or existing), or None when no bootstrap is needed.

    Safe to call every startup — if a file already exists and no admin
    has claimed, the existing file is preserved so the operator is not
    forced to re-copy a fresh token.
    """
    from lib import auth_db

    try:
        admin_count = await auth_db.count_admins()
    except Exception as exc:
        # Schema not yet migrated on the very first call (migrations
        # run slightly later in the current boot path). Defer — the
        # next startup after migrations will handle it.
        _log.debug("count_admins failed during bootstrap check: %s", exc)
        return None

    if admin_count > 0:
        # An admin exists. Make sure a stale file isn't lying around
        # from a previous session; if one is, delete it so the admin
        # flag is the sole source of truth.
        if bootstrap_file_exists():
            delete_bootstrap_file()
            _log.info("admin exists; removed stale bootstrap file")
        return None

    path = bootstrap_file_path()
    if bootstrap_file_exists():
        # Preserve the existing token across restarts.
        return path

    _ensure_state_dir()
    token = _generate_token()
    _write_token(path, token)
    return path


def log_bootstrap_banner(path: str) -> None:
    """
    Print a prominent startup banner telling the operator where to
    find the bootstrap token. Shouted to stderr so it's hard to miss.
    """
    msg = [
        "=" * 72,
        "  ROOT CLAIM CODE READY",
        "",
        "  No root/admin principal is registered. A one-time claim code has",
        "  been written to:",
        f"      {path}",
        "",
        "  Copy the token (a single line) and browse to /enable-multiuser",
        "  to claim this instance. The file will be",
        "  deleted on successful claim.",
        "=" * 72,
    ]
    for line in msg:
        print(line, file=sys.stderr)
