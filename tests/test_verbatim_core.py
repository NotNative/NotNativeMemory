#!/usr/bin/env python3
"""
Unit tests for hook_bundles/nna/notnative-memory/_internal/verbatim_core.

Covers the pure-function helpers (chunker, topic inferrer, chunk_index
counter). The MCP POST path is exercised live in Phase A's
test_verbatim_store.py and in the on-jill verification — not mocked here.

Run:
    python tests/test_verbatim_core.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
# The hook bundle is layered under hook_bundles/nna/notnative-memory; add
# that on the path so its `_internal/` package imports resolve.
sys.path.insert(0, os.path.join(ROOT, "hook_bundles", "nna", "notnative-memory"))

from _internal import verbatim_core  # noqa: E402


def _check(failed_box, total_box, label, cond):
    total_box[0] += 1
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        failed_box[0] += 1


def test_chunker_returns_single_chunk_for_short_text(failed, total):
    out = verbatim_core.chunk_content("hello world")
    _check(failed, total, "short text -> one chunk", out == ["hello world"])


def test_chunker_empty_returns_empty(failed, total):
    _check(failed, total, "empty -> []", verbatim_core.chunk_content("") == [])
    _check(failed, total, "whitespace -> []", verbatim_core.chunk_content("   \n  ") == [])
    _check(failed, total, "None -> []", verbatim_core.chunk_content(None) == [])


def test_chunker_overlap_on_long_text(failed, total):
    text = "A" * 2000
    chunks = verbatim_core.chunk_content(
        text, chunk_size=800, overlap=200, floor=30,
    )
    _check(failed, total, "2000 'A' splits into multiple chunks", len(chunks) >= 3)
    _check(
        failed, total,
        "first chunk has expected size",
        len(chunks[0]) == 800,
    )
    # Adjacent chunks share `overlap` chars at the boundary.
    if len(chunks) >= 2:
        tail = chunks[0][-200:]
        head = chunks[1][:200]
        _check(
            failed, total,
            "200-char overlap between consecutive chunks",
            tail == head,
        )


def test_topic_inference_default_general(failed, total):
    _check(failed, total, "no keywords -> general",
           verbatim_core.infer_topic("hello world") == "general")


def test_topic_inference_first_match_wins(failed, total):
    _check(
        failed, total, "error -> debugging",
        verbatim_core.infer_topic(
            "We hit an error in the migration; need to debug it.",
        ) == "debugging",
    )
    _check(
        failed, total, "refactor -> refactor",
        verbatim_core.infer_topic(
            "I want to refactor the auth module and rename a few helpers.",
        ) == "refactor",
    )
    _check(
        failed, total, "schema -> architecture",
        verbatim_core.infer_topic(
            "Let's redesign the schema for the new feature.",
        ) == "architecture",
    )


def test_chunk_index_monotonic_per_session(failed, total):
    with tempfile.TemporaryDirectory() as td:
        os.environ["NNA_STATE_DIR"] = td
        try:
            sid = "test-session-abc"
            a = verbatim_core.next_chunk_index(sid)
            b = verbatim_core.next_chunk_index(sid)
            c = verbatim_core.next_chunk_index(sid)
            _check(failed, total, "first index is 0", a == 0)
            _check(failed, total, "second index is 1", b == 1)
            _check(failed, total, "third index is 2", c == 2)
        finally:
            os.environ.pop("NNA_STATE_DIR", None)


def test_chunk_index_persists_across_calls(failed, total):
    with tempfile.TemporaryDirectory() as td:
        os.environ["NNA_STATE_DIR"] = td
        try:
            sid = "test-persist"
            for _ in range(5):
                verbatim_core.next_chunk_index(sid)
            counter_path = Path(td) / "verbatim-counters" / f"{sid}.json"
            _check(
                failed, total,
                "counter file written under NNA_STATE_DIR",
                counter_path.exists(),
            )
            # Re-call after the existing file is in place.
            nxt = verbatim_core.next_chunk_index(sid)
            _check(
                failed, total,
                "counter advances from persisted state (5 -> 5 = next 5)",
                nxt == 5,
            )
        finally:
            os.environ.pop("NNA_STATE_DIR", None)


def test_chunk_index_isolated_per_session(failed, total):
    with tempfile.TemporaryDirectory() as td:
        os.environ["NNA_STATE_DIR"] = td
        try:
            for _ in range(3):
                verbatim_core.next_chunk_index("session-A")
            b0 = verbatim_core.next_chunk_index("session-B")
            _check(
                failed, total,
                "session-B starts from 0 independent of session-A",
                b0 == 0,
            )
        finally:
            os.environ.pop("NNA_STATE_DIR", None)


def test_mcp_timeout_default_and_env_override(failed, total):
    for key in ("NNA_VERBATIM_CAPTURE_TIMEOUT_SECONDS", "MEMORY_VERBATIM_CAPTURE_TIMEOUT_SECONDS"):
        os.environ.pop(key, None)
    _check(
        failed, total,
        "verbatim MCP timeout defaults to a generous 20 seconds",
        verbatim_core._mcp_timeout_seconds() == 20,
    )
    os.environ["MEMORY_VERBATIM_CAPTURE_TIMEOUT_SECONDS"] = "31"
    _check(
        failed, total,
        "generic verbatim timeout env overrides the default",
        verbatim_core._mcp_timeout_seconds() == 31,
    )
    os.environ["NNA_VERBATIM_CAPTURE_TIMEOUT_SECONDS"] = "12"
    _check(
        failed, total,
        "NNA-specific verbatim timeout env takes precedence",
        verbatim_core._mcp_timeout_seconds() == 12,
    )
    os.environ["NNA_VERBATIM_CAPTURE_TIMEOUT_SECONDS"] = "not-an-int"
    _check(
        failed, total,
        "invalid verbatim timeout falls back to default",
        verbatim_core._mcp_timeout_seconds() == 20,
    )
    for key in ("NNA_VERBATIM_CAPTURE_TIMEOUT_SECONDS", "MEMORY_VERBATIM_CAPTURE_TIMEOUT_SECONDS"):
        os.environ.pop(key, None)


def main() -> int:
    failed = [0]
    total = [0]
    tests = [
        test_chunker_returns_single_chunk_for_short_text,
        test_chunker_empty_returns_empty,
        test_chunker_overlap_on_long_text,
        test_topic_inference_default_general,
        test_topic_inference_first_match_wins,
        test_chunk_index_monotonic_per_session,
        test_chunk_index_persists_across_calls,
        test_chunk_index_isolated_per_session,
        test_mcp_timeout_default_and_env_override,
    ]
    for fn in tests:
        fn(failed, total)
    print("---")
    print(f"{total[0] - failed[0]}/{total[0]} passed")
    return 0 if failed[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
