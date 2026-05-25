#!/usr/bin/env python3
"""Pure-Python unit tests for hygiene heuristics."""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from lib import hygiene  # noqa: E402


def test_infer_class_rule_imperative():
    assert hygiene._infer_class("You must never strip the LICENSE block.") == "rule"
    assert hygiene._infer_class("Always test every behavior change.") == "rule"
    assert hygiene._infer_class("Do not write to Development_Notes.txt.") == "rule"
    print("[OK] _infer_class detects imperative rules")


def test_infer_class_preference():
    assert hygiene._infer_class("I prefer terse responses.") == "preference"
    assert hygiene._infer_class("Shanz likes to see diffs not summaries.") == "preference"
    print("[OK] _infer_class detects preferences")


def test_infer_class_memory_state():
    assert hygiene._infer_class("The NNM server is running on port 9500.") == "memory"
    assert hygiene._infer_class("The latest commit was an installer fix.") == "memory"
    print("[OK] _infer_class detects state-of-the-world memories")


def test_infer_class_unknown():
    assert hygiene._infer_class("XYZ ABC") is None
    assert hygiene._infer_class("") is None
    print("[OK] _infer_class returns None for ambiguous content")


def test_is_contradiction_matching():
    a = "The migration must run before deployment finishes locally."
    b = "The migration must never run before deployment finishes locally."
    assert hygiene._is_contradiction(a, b)
    print("[OK] _is_contradiction catches must / must never on shared topic")


def test_is_contradiction_unrelated():
    a = "Always commit on feature branches before merging."
    b = "Never use docker compose without --pull."
    assert not hygiene._is_contradiction(a, b)
    print("[OK] _is_contradiction does not fire on unrelated polarity")


def test_hygiene_report_shape():
    r = hygiene.HygieneReport(
        classified=1, conflicts_auto_resolved=2,
        conflicts_queued_for_review=3, promoted_to_critical=4,
        demoted=5, deduplicated=0, duration_ms=42,
    )
    d = r.as_dict()
    assert d["classified"] == 1
    assert d["duration_ms"] == 42
    assert "deduplicated" in d
    print("[OK] HygieneReport.as_dict() round-trips all fields")


def main():
    fns = [
        test_infer_class_rule_imperative,
        test_infer_class_preference,
        test_infer_class_memory_state,
        test_infer_class_unknown,
        test_is_contradiction_matching,
        test_is_contradiction_unrelated,
        test_hygiene_report_shape,
    ]
    for fn in fns:
        fn()
    print(f"\n[UNIT] All {len(fns)} tests passed")


if __name__ == "__main__":
    main()
