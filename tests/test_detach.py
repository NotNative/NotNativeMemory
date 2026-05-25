"""Tests for the fire-and-forget detach helper used by turn_analysis.py.

Covers the three reachable modes:
  - Inline (NNM_TURN_ANALYSIS_INLINE=1): no detach, no stdin capture.
  - Worker (--worker <tmpfile> in argv): replays stdin from temp file.
  - Foreground (default): captures stdin, spawns detached subprocess.

Both bundles ship the same detach.py, byte-identical — test against the
nna copy and verify byte-equality with the claude copy.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).parent.parent
_NNA_BUNDLE = _REPO_ROOT / "hook_bundles" / "nna" / "notnative-memory"
_CLAUDE_BUNDLE = _REPO_ROOT / "hook_bundles" / "claude" / "notnative-memory"

# Use the claude bundle's _internal/ so the import surface matches what
# test_turn_analysis.py already pinned (it imports `_internal.env_loader`
# which only exists in the claude bundle). detach.py is byte-identical
# across bundles — see test_detach_is_byte_identical_across_bundles.
sys.path.insert(0, str(_CLAUDE_BUNDLE))
from _internal import detach  # noqa: E402


# -- Bundle parity ---------------------------------------------------------

def test_detach_is_byte_identical_across_bundles():
    nna = (_NNA_BUNDLE / "_internal" / "detach.py").read_bytes()
    claude = (_CLAUDE_BUNDLE / "_internal" / "detach.py").read_bytes()
    assert nna == claude, (
        "detach.py must be byte-identical between bundles — it's a generic "
        "helper with no harness-specific code. If they drift, the install "
        "and behavior story breaks."
    )
    print("[OK] detach.py is byte-identical across nna and claude bundles")


# -- Mode detection --------------------------------------------------------

def test_inline_mode_detected_for_truthy_env_values():
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        with mock.patch.dict(os.environ, {detach.INLINE_ENV_VAR: truthy}):
            assert detach._is_inline_mode() is True, f"{truthy!r} must enable inline"
    print("[OK] inline mode honors 1/true/yes/on (case-insensitive)")


def test_inline_mode_false_for_unset_and_falsy_values():
    for falsy in ("", "0", "false", "no", "off"):
        env = {detach.INLINE_ENV_VAR: falsy} if falsy else {}
        with mock.patch.dict(os.environ, env, clear=True):
            assert detach._is_inline_mode() is False, f"{falsy!r} must NOT enable inline"
    print("[OK] inline mode stays off for unset/falsy env values")


def test_worker_invocation_detected_when_flag_and_path_present():
    with mock.patch.object(sys, "argv", ["script.py", "--worker", "/tmp/x.json"]):
        assert detach._is_worker_invocation() is True
    print("[OK] --worker <path> argv recognized as worker invocation")


def test_worker_invocation_false_for_flag_without_path():
    with mock.patch.object(sys, "argv", ["script.py", "--worker"]):
        assert detach._is_worker_invocation() is False
    with mock.patch.object(sys, "argv", ["script.py"]):
        assert detach._is_worker_invocation() is False
    print("[OK] bare --worker (no path) does not trigger worker mode")


# -- Worker mode -----------------------------------------------------------

def test_resume_as_worker_replays_stdin_and_unlinks_temp_file():
    payload = '{"prompt":"hello","model_response":"world","cwd":"/x"}'
    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="detach_test_")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)

    saved_argv = sys.argv[:]
    saved_stdin = sys.stdin
    try:
        sys.argv = ["script.py", "--worker", tmp_path]
        detach._resume_as_worker()
        # Worker stdin must replay the original payload verbatim.
        assert sys.stdin.read() == payload
        # Worker-flag args stripped from argv.
        assert sys.argv == ["script.py"]
        # Temp file cleaned up.
        assert not os.path.exists(tmp_path), "worker should unlink the temp file"
    finally:
        sys.argv = saved_argv
        sys.stdin = saved_stdin
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    print("[OK] _resume_as_worker replays stdin, strips argv, unlinks temp")


# -- detach_or_resume top-level routing ------------------------------------

def test_detach_or_resume_inline_mode_skips_detach_and_keeps_stdin():
    """Inline mode must not touch sys.stdin — the caller's main() reads it."""
    saved_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO("ORIGINAL_STDIN_SENTINEL")
        with mock.patch.dict(os.environ, {detach.INLINE_ENV_VAR: "1"}):
            with mock.patch.object(detach.subprocess, "Popen") as popen_mock:
                detach.detach_or_resume("/some/script.py")
                popen_mock.assert_not_called()
        # stdin must be untouched.
        assert sys.stdin.read() == "ORIGINAL_STDIN_SENTINEL"
    finally:
        sys.stdin = saved_stdin
    print("[OK] inline mode skips detach and leaves stdin intact")


def test_detach_or_resume_foreground_captures_stdin_spawns_and_exits():
    """Foreground mode must read all of stdin, spawn Popen with the worker
    flag, and call sys.exit(0). The subprocess receives the captured
    payload via a temp file passed as argv[2]."""
    saved_stdin = sys.stdin
    saved_argv = sys.argv[:]
    captured_args = {}

    def _fake_popen(cmd, **kwargs):
        captured_args["cmd"] = cmd
        captured_args["kwargs"] = kwargs
        return mock.MagicMock()

    try:
        sys.stdin = io.StringIO("STDIN_PAYLOAD")
        sys.argv = ["script.py"]
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(detach.subprocess, "Popen", side_effect=_fake_popen):
                try:
                    detach.detach_or_resume("/some/script.py")
                except SystemExit as e:
                    assert e.code == 0
                else:
                    raise AssertionError("foreground mode must call sys.exit(0)")
    finally:
        sys.stdin = saved_stdin
        sys.argv = saved_argv

    cmd = captured_args["cmd"]
    assert cmd[0] == sys.executable
    # detach normalizes the script path via os.path.abspath so workers
    # don't rely on the parent's cwd. Compare against the same normalized
    # form rather than the raw input.
    assert cmd[1] == os.path.abspath("/some/script.py")
    assert cmd[2] == detach.WORKER_FLAG
    tmp_path = cmd[3]
    # The captured stdin payload is in the temp file the worker will read.
    with open(tmp_path, "r", encoding="utf-8") as fh:
        assert fh.read() == "STDIN_PAYLOAD"
    os.unlink(tmp_path)

    # Subprocess must be detached from the parent's stdio.
    kwargs = captured_args["kwargs"]
    assert kwargs["stdin"] == detach.subprocess.DEVNULL
    assert kwargs["stdout"] == detach.subprocess.DEVNULL
    assert kwargs["stderr"] == detach.subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    if sys.platform == "win32":
        assert "creationflags" in kwargs
    else:
        assert kwargs.get("start_new_session") is True
    print("[OK] foreground mode captures stdin, spawns detached worker, exits 0")


def test_detach_or_resume_falls_back_to_inline_if_spawn_fails():
    """If Popen raises OSError (e.g. python not on PATH), the script must
    not lose the turn — fall back to inline execution by replaying the
    captured payload back into sys.stdin."""
    saved_stdin = sys.stdin
    saved_argv = sys.argv[:]
    try:
        sys.stdin = io.StringIO("FALLBACK_PAYLOAD")
        sys.argv = ["script.py"]
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(
                detach.subprocess, "Popen", side_effect=OSError("no python")
            ):
                # No SystemExit — caller continues inline.
                detach.detach_or_resume("/some/script.py")
        assert sys.stdin.read() == "FALLBACK_PAYLOAD"
    finally:
        sys.stdin = saved_stdin
        sys.argv = saved_argv
    print("[OK] spawn failure falls back to inline with replayed stdin")


# -- Surrogate round-trip --------------------------------------------------

def test_detach_round_trips_lone_surrogates():
    """Windows stdin can smuggle non-UTF-8 bytes as lone surrogates
    (\\udc80..\\udcff). Both the parent write side and the worker read
    side must use errors='surrogateescape' so the payload round-trips
    without raising UnicodeEncodeError."""
    payload = 'prefix \udc90\udcff suffix'
    saved_stdin = sys.stdin
    saved_argv = sys.argv[:]
    captured = {}

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return mock.MagicMock()

    try:
        sys.stdin = io.StringIO(payload)
        sys.argv = ["script.py"]
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(detach.subprocess, "Popen", side_effect=_fake_popen):
                try:
                    detach.detach_or_resume("/some/script.py")
                except SystemExit:
                    pass

        tmp_path = captured["cmd"][3]
        # Replay as worker: must read back the same surrogate-bearing string.
        sys.argv = ["script.py", "--worker", tmp_path]
        detach._resume_as_worker()
        assert sys.stdin.read() == payload
    finally:
        sys.stdin = saved_stdin
        sys.argv = saved_argv
    print("[OK] lone surrogates round-trip through write/read cycle")


# -- Runner ---------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_detach_is_byte_identical_across_bundles,
        test_inline_mode_detected_for_truthy_env_values,
        test_inline_mode_false_for_unset_and_falsy_values,
        test_worker_invocation_detected_when_flag_and_path_present,
        test_worker_invocation_false_for_flag_without_path,
        test_resume_as_worker_replays_stdin_and_unlinks_temp_file,
        test_detach_or_resume_inline_mode_skips_detach_and_keeps_stdin,
        test_detach_or_resume_foreground_captures_stdin_spawns_and_exits,
        test_detach_or_resume_falls_back_to_inline_if_spawn_fails,
        test_detach_round_trips_lone_surrogates,
    ]
    for t in tests:
        t()
    print(f"\n[SUCCESS] All {len(tests)} detach tests passed")
