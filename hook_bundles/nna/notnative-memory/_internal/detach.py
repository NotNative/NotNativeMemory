"""
Fire-and-forget detach helper for hook scripts.

Some hook scripts (notably turn_analysis.py) need to do an LLM call that
can take 30+ seconds on a local 27B model. The agent harness wraps each
hook in a timeout — Claude Code's Stop hook is bounded by the
`"timeout"` field in settings.json (default 60s), and NNA's manifest
caps the same dispatch with `timeout_ms`. A long extraction call gets
killed by these caps and writes nothing.

This helper makes the script truly fire-and-forget:

  - Foreground (default) mode: read all of stdin, stash it to a temp
    file, spawn a detached child of ourselves with `--worker <tmpfile>`,
    and exit 0 immediately. The harness sees a sub-second hook run.
  - Worker (`--worker <tmpfile>` in argv) mode: replay the temp file as
    sys.stdin, clean up, strip the flag from argv, and return so the
    rest of main() runs unchanged. The worker has no harness timeout —
    it can talk to the LLM for as long as it wants.

Test seam: NNM_TURN_ANALYSIS_INLINE=1 in the environment skips the
detach entirely and behaves like the old in-process script. Tests use
this to drive main() with a captured stdin without spawning subprocesses.

Cross-platform:
  - Windows uses DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP creationflags.
  - POSIX uses start_new_session=True.
  - DEVNULL on stdin/stdout/stderr + close_fds=True keeps the child from
    holding the parent's pipes open.

Fallback: if Popen raises (e.g. python interpreter unavailable in PATH),
we fall back to inline execution so we don't drop the turn entirely.
"""

import io
import os
import subprocess
import sys
import tempfile


WORKER_FLAG = "--worker"
INLINE_ENV_VAR = "NNM_TURN_ANALYSIS_INLINE"


def _is_inline_mode() -> bool:
    val = os.environ.get(INLINE_ENV_VAR, "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _is_worker_invocation() -> bool:
    return len(sys.argv) >= 3 and sys.argv[1] == WORKER_FLAG


def _resume_as_worker() -> None:
    """Worker mode: read the stdin payload from the temp file passed in
    argv, unlink the temp file, replace sys.stdin so the rest of main()
    sees the same bytes the harness originally sent, and strip the
    `--worker <path>` args so downstream argv inspection is clean."""
    tmp_path = sys.argv[2]
    try:
        with open(tmp_path, "r", encoding="utf-8") as fh:
            payload = fh.read()
    except OSError:
        sys.exit(1)
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    sys.stdin = io.StringIO(payload)
    del sys.argv[1:3]


def _spawn_detached(script_path: str, payload: str) -> bool:
    """Spawn ourselves as a detached worker. Returns True on success."""
    fd, tmp_path = tempfile.mkstemp(prefix="nnm_turn_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False

    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True

    try:
        subprocess.Popen(
            [sys.executable, script_path, WORKER_FLAG, tmp_path],
            **popen_kwargs,
        )
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False
    return True


def detach_or_resume(script_path: str) -> None:
    """Top-of-main() entry point.

    On first invocation (no --worker flag, NNM_TURN_ANALYSIS_INLINE
    unset), capture stdin, spawn a detached worker, and exit 0.
    On worker invocation, replay stdin from the temp file and return so
    the caller's main() runs.
    On inline mode, return immediately without consuming stdin so
    main() reads it normally.
    """
    if _is_inline_mode():
        return

    if _is_worker_invocation():
        _resume_as_worker()
        return

    payload = sys.stdin.read()
    if not _spawn_detached(os.path.abspath(script_path), payload):
        # Spawn failed — fall back to inline so we don't lose the turn.
        sys.stdin = io.StringIO(payload)
        return
    sys.exit(0)
