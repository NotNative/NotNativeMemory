"""
NNA-bundle-only tests for user_prompt_inject.py session-once reminder.

The reminder used to live in session_start.py (now deleted from the nna
bundle). It was folded into user_prompt_inject.py with a marker file at
~/.nna/state/last_reminded_session keyed on the dispatch site's
session_id. These tests cover the gating logic — the rest of the hook
is exercised by tests/test_user_prompt_inject.py against the claude
bundle copy.

Why two test files for the same logical hook: the bundles intentionally
diverge as of 2026-05-19 (session.start:post path no longer present in
nna). Keeping the nna-specific assertions in a sibling file makes the
divergence visible.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).parent.parent
_NNA_BUNDLE = _REPO_ROOT / "hook_bundles" / "nna" / "notnative-memory"


def _load_nna_inject():
    """Load the nna bundle's user_prompt_inject.py under a unique module
    name so it doesn't collide with the claude-bundle import done by
    tests/test_user_prompt_inject.py."""
    spec = importlib.util.spec_from_file_location(
        "user_prompt_inject_nna",
        str(_NNA_BUNDLE / "user_prompt_inject.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["user_prompt_inject_nna"] = module
    spec.loader.exec_module(module)
    return module


nna_inject = _load_nna_inject()


def _with_state_dir(tmp_path: str):
    """Point the module's state file at a tempdir so the test does not
    touch the real ~/.nna/state/."""
    state_dir = os.path.join(tmp_path, "state")
    marker = os.path.join(state_dir, "last_reminded_session")
    return mock.patch.multiple(
        nna_inject,
        _STATE_DIR=state_dir,
        _LAST_REMINDED_FILE=marker,
    )


def test_reminder_emitted_when_session_id_missing():
    """Fail-open contract: when the dispatch site forgot to populate
    session_id, we err on the side of emitting the reminder rather than
    silently dropping the load-bearing context."""
    with tempfile.TemporaryDirectory() as tmp:
        with _with_state_dir(tmp):
            result = nna_inject._session_once_reminder("")
    assert result == nna_inject._TOOL_LOAD_REMINDER
    print("[OK] reminder fires when session_id is empty (fail-open)")


def test_reminder_emitted_on_fresh_session():
    """First invocation in a session writes the marker AND returns the
    reminder. Subsequent invocations should suppress."""
    with tempfile.TemporaryDirectory() as tmp:
        with _with_state_dir(tmp):
            first = nna_inject._session_once_reminder("session-abc")
            second = nna_inject._session_once_reminder("session-abc")
    assert first == nna_inject._TOOL_LOAD_REMINDER, "first call must emit reminder"
    assert second == "", "second call same session must suppress"
    print("[OK] reminder fires once per session, suppressed thereafter")


def test_reminder_re_emitted_when_session_id_changes():
    """A new session_id means a new NNA process (or resumed session); the
    reminder belongs in front of its first prompt too."""
    with tempfile.TemporaryDirectory() as tmp:
        with _with_state_dir(tmp):
            first = nna_inject._session_once_reminder("session-one")
            second = nna_inject._session_once_reminder("session-two")
    assert first == nna_inject._TOOL_LOAD_REMINDER
    assert second == nna_inject._TOOL_LOAD_REMINDER
    print("[OK] reminder re-fires when session_id changes")


def test_reminder_emitted_when_marker_unreadable():
    """If the state dir can't be read (perm issue, race), fall back to
    emitting the reminder. Better redundant than silently dropped."""
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = os.path.join(tmp, "state")
        # Use a path under a non-existent parent so the read raises
        # OSError; the write fallback will also fail because we patch it.
        bogus_marker = os.path.join(tmp, "does", "not", "exist", "marker")
        with mock.patch.multiple(
            nna_inject,
            _STATE_DIR=state_dir,
            _LAST_REMINDED_FILE=bogus_marker,
        ):
            # Force makedirs to fail too so we exercise the catch.
            with mock.patch("os.makedirs", side_effect=OSError("boom")):
                result = nna_inject._session_once_reminder("session-x")
    assert result == nna_inject._TOOL_LOAD_REMINDER
    print("[OK] reminder fires when marker state dir is unwritable (fail-open)")


def test_reminder_marker_persists_across_calls():
    """The marker file should actually appear on disk after a successful
    fresh-session emit; otherwise the suppression on the second call
    can't possibly be working for the right reason."""
    with tempfile.TemporaryDirectory() as tmp:
        with _with_state_dir(tmp):
            nna_inject._session_once_reminder("session-persist")
            marker_path = nna_inject._LAST_REMINDED_FILE
            assert os.path.isfile(marker_path), "marker file must be written"
            with open(marker_path, "r", encoding="utf-8") as fh:
                assert fh.read().strip() == "session-persist"
    print("[OK] marker file persists the session_id on first emit")


if __name__ == "__main__":
    test_reminder_emitted_when_session_id_missing()
    test_reminder_emitted_on_fresh_session()
    test_reminder_re_emitted_when_session_id_changes()
    test_reminder_emitted_when_marker_unreadable()
    test_reminder_marker_persists_across_calls()
