#!/usr/bin/env python3
"""Tests for the verbatim transcript adapter."""

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from lib import verbatim  # noqa: E402


def _seed(dir: Path, sid: str, entries: list[dict]) -> None:
    dir.mkdir(parents=True, exist_ok=True)
    path = dir / f"{sid}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def test_list_sessions_missing_dir(tmp_path):
    assert verbatim.list_sessions(tmp_path / "nope") == []
    print("[OK] list_sessions returns [] when directory is absent")


def test_list_sessions_returns_stems(tmp_path):
    _seed(tmp_path, "alpha", [{"role": "user", "content": "x"}])
    _seed(tmp_path, "beta", [{"role": "user", "content": "y"}])
    sessions = sorted(verbatim.list_sessions(tmp_path))
    assert sessions == ["alpha", "beta"]
    print("[OK] list_sessions returns filename stems")


def test_iter_session_yields_entries_in_order(tmp_path):
    _seed(
        tmp_path,
        "s",
        [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ],
    )
    out = list(verbatim.iter_session("s", tmp_path))
    assert len(out) == 2
    assert out[0]["content"] == "first"
    assert out[1]["role"] == "assistant"
    print("[OK] iter_session preserves append order")


def test_iter_session_skips_malformed_lines(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "s.jsonl"
    path.write_text(
        '{"role":"user","content":"good"}\nthis is not json\n'
        '{"role":"assistant","content":"also good"}\n',
        encoding="utf-8",
    )
    out = list(verbatim.iter_session("s", tmp_path))
    assert len(out) == 2
    assert out[0]["content"] == "good"
    assert out[1]["content"] == "also good"
    print("[OK] iter_session skips malformed lines")


def test_search_sessions_substring_match(tmp_path):
    _seed(
        tmp_path,
        "s1",
        [
            {"role": "user", "content": "the FROBNICATOR is broken"},
            {"role": "user", "content": "nothing relevant"},
        ],
    )
    _seed(tmp_path, "s2", [{"role": "assistant", "content": "another frobnicator hit"}])
    out = verbatim.search_sessions("frobnicator", limit=10, directory=tmp_path)
    assert len(out) == 2
    sids = sorted(r["session_id"] for r in out)
    assert sids == ["s1", "s2"]
    print("[OK] search_sessions finds case-insensitive substring matches across sessions")


def test_search_sessions_empty_query(tmp_path):
    _seed(tmp_path, "s", [{"role": "user", "content": "x"}])
    assert verbatim.search_sessions("", directory=tmp_path) == []
    print("[OK] search_sessions returns [] for empty query")


def test_search_sessions_respects_limit(tmp_path):
    _seed(
        tmp_path,
        "s",
        [{"role": "user", "content": f"hello {i}"} for i in range(20)],
    )
    out = verbatim.search_sessions("hello", limit=3, directory=tmp_path)
    assert len(out) == 3
    print("[OK] search_sessions truncates to limit")


def main():
    import tempfile

    fns = [
        test_list_sessions_missing_dir,
        test_list_sessions_returns_stems,
        test_iter_session_yields_entries_in_order,
        test_iter_session_skips_malformed_lines,
        test_search_sessions_substring_match,
        test_search_sessions_empty_query,
        test_search_sessions_respects_limit,
    ]
    for fn in fns:
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))
    print(f"\n[UNIT] All {len(fns)} tests passed")


if __name__ == "__main__":
    main()
