#!/usr/bin/env python3
"""
Tests for the UserPromptSubmit injection hook.

Covers:
- Walk-back trigger detection (length-based + affirmative-set)
- Prior-assistant-text extraction from transcript JSONL
- Content stringification (plain string + typed blocks)
- Main flow: walk-back uses prior turn; falls back to skip when no transcript
- Class-aware framing of injected memories (Fix #3)

Both bundle copies (claude + nna) implement identical walk-back logic;
this suite exercises the claude bundle copy as the canonical source.
"""

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT / "hook_bundles" / "claude" / "notnative-memory"))

import user_prompt_inject as inject  # noqa: E402


# -- Walk-back trigger detection ------------------------------------------

def test_should_walk_back_short_prompt():
    """Any prompt below MIN_PROMPT_CHARS triggers walk-back. The trivial
    drop-on-floor path is no longer the only option."""
    assert inject._should_walk_back("hi")
    assert inject._should_walk_back("go")
    assert inject._should_walk_back("")
    print("[OK] _should_walk_back triggers on prompts below MIN_PROMPT_CHARS")


def test_should_walk_back_affirmative_set_long_enough():
    """Longer affirmatives like 'please proceed' must still trigger walk-back
    even though they exceed the length floor. The whole point of the set is
    to catch the cases where length alone misses the signal."""
    for prompt in ["proceed", "go ahead", "keep going", "sounds good"]:
        assert inject._should_walk_back(prompt), (
            f"{prompt!r} should match the affirmative set"
        )
    print("[OK] _should_walk_back triggers on affirmative-set verbatim matches")


def test_should_walk_back_affirmative_case_insensitive():
    """Affirmative matching is case-insensitive and strips trailing punctuation."""
    for prompt in ["YES", "Yes.", "yes!", "Sure!", "OK?"]:
        assert inject._should_walk_back(prompt), f"{prompt!r} should match"
    print("[OK] _should_walk_back is case-insensitive + tolerates trailing punctuation")


def test_should_walk_back_substantive_prompt_does_not_trigger():
    """A real question must NOT trigger walk-back; the prompt itself
    carries enough topical signal for memory_search."""
    substantive = "Should we use single quotes when shelling PowerShell from bash?"
    assert not inject._should_walk_back(substantive)
    print("[OK] _should_walk_back leaves substantive prompts alone")


def test_should_walk_back_long_non_affirmative_no_trigger():
    """A long prompt that happens to start with 'yes' is NOT a degenerate
    affirmative — it carries topical signal."""
    assert not inject._should_walk_back(
        "Yes, but also tell me about the bash escape rule."
    )
    print("[OK] _should_walk_back does not match prompts that merely contain 'yes'")


# -- Content stringification ---------------------------------------------

def test_stringify_content_plain_string():
    assert inject._stringify_content("hello") == "hello"
    print("[OK] _stringify_content passes plain strings through")


def test_stringify_content_text_blocks():
    blocks = [
        {"type": "text", "text": "first"},
        {"type": "tool_use", "name": "Read"},  # ignored
        {"type": "text", "text": "second"},
    ]
    out = inject._stringify_content(blocks)
    assert "first" in out and "second" in out
    assert "Read" not in out, "tool_use blocks must not pollute the walk-back query"
    print("[OK] _stringify_content keeps text blocks and drops tool_use noise")


def test_stringify_content_empty_or_invalid():
    assert inject._stringify_content(None) == ""
    assert inject._stringify_content([]) == ""
    assert inject._stringify_content(42) == ""
    print("[OK] _stringify_content defends against invalid content shapes")


# -- Prior-assistant extraction from transcript --------------------------

def _write_jsonl(path, entries):
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def test_extract_prior_assistant_flat_role_format():
    """Older transcripts use {role, content} entries flat at the top level."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "transcript.jsonl")
        _write_jsonl(path, [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "first response"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "second response"},
        ])
        prior = inject._extract_prior_assistant_text(path)
    assert prior == "second response"
    print("[OK] _extract_prior_assistant_text returns the most recent assistant entry (flat format)")


def test_extract_prior_assistant_wrapped_message_format():
    """Claude Code wraps entries in {type, message: {role, content}}."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "transcript.jsonl")
        _write_jsonl(path, [
            {"type": "user", "message": {"role": "user", "content": "ping"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "pong"}},
            {"type": "user", "message": {"role": "user", "content": "ok"}},
        ])
        prior = inject._extract_prior_assistant_text(path)
    assert prior == "pong"
    print("[OK] _extract_prior_assistant_text handles the wrapped-message envelope")


def test_extract_prior_assistant_typed_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "transcript.jsonl")
        _write_jsonl(path, [
            {"role": "assistant", "content": [
                {"type": "text", "text": "before tool"},
                {"type": "tool_use", "name": "Read"},
                {"type": "text", "text": "after tool"},
            ]},
        ])
        prior = inject._extract_prior_assistant_text(path)
    assert "before tool" in prior
    assert "after tool" in prior
    print("[OK] _extract_prior_assistant_text flattens typed-block content")


def test_extract_prior_assistant_truncates_long_text():
    """Walk-back basis is capped so we don't ship multi-page assistant
    replies through the embedding model."""
    long_text = "x" * (inject.WALKBACK_PRIOR_MAX_CHARS + 500)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "transcript.jsonl")
        _write_jsonl(path, [{"role": "assistant", "content": long_text}])
        prior = inject._extract_prior_assistant_text(path)
    assert len(prior) == inject.WALKBACK_PRIOR_MAX_CHARS
    print("[OK] _extract_prior_assistant_text truncates to WALKBACK_PRIOR_MAX_CHARS")


def test_extract_prior_assistant_no_assistant_entries_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "transcript.jsonl")
        _write_jsonl(path, [{"role": "user", "content": "alone"}])
        assert inject._extract_prior_assistant_text(path) == ""
    print("[OK] _extract_prior_assistant_text returns empty when no assistant turn")


def test_extract_prior_assistant_missing_path_returns_empty():
    assert inject._extract_prior_assistant_text("") == ""
    assert inject._extract_prior_assistant_text("/nonexistent.jsonl") == ""
    print("[OK] _extract_prior_assistant_text gracefully handles missing transcripts")


def test_extract_prior_assistant_tolerates_malformed_lines():
    """Real transcripts sometimes contain partial / non-JSON lines; the
    extractor must skip them and keep walking back."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "transcript.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": "assistant", "content": "earlier reply"}) + "\n")
            fh.write("not json at all\n")
            fh.write("{broken json\n")
            fh.write("\n")
        prior = inject._extract_prior_assistant_text(path)
    assert prior == "earlier reply"
    print("[OK] _extract_prior_assistant_text skips malformed JSONL lines")


# -- main() flow ----------------------------------------------------------

def _run_main(hook_input, search_results=None):
    """Run inject.main() with mocked stdin/stdout and mocked _search_memories.

    Returns (exit_code, captured_stdout, captured_query).
    """
    if search_results is None:
        search_results = []

    captured = {"query": None}

    def fake_search(query, project):
        captured["query"] = query
        return search_results

    stdin = io.StringIO(json.dumps(hook_input))
    stdout = io.StringIO()
    exit_code = {"code": None}

    def fake_exit(code=0):
        exit_code["code"] = code
        raise SystemExit(code)

    with mock.patch.object(inject.sys, "stdin", stdin), \
            mock.patch.object(inject.sys, "stdout", stdout), \
            mock.patch.object(inject.sys, "exit", side_effect=fake_exit), \
            mock.patch.object(inject, "_search_memories", side_effect=fake_search), \
            mock.patch.object(inject, "_log_execution"):
        try:
            inject.main()
        except SystemExit:
            pass

    return exit_code["code"], stdout.getvalue(), captured["query"]


def test_main_walks_back_when_prompt_is_affirmative():
    """A 'proceed' prompt with a transcript on hand must search using the
    prior assistant turn, not the affirmative alone. This is the core
    Fix #4 contract."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "transcript.jsonl")
        _write_jsonl(path, [
            {"role": "assistant", "content": "Let's discuss memory architecture and routing."},
        ])
        hook_input = {
            "prompt": "proceed",
            "cwd": "/some/cwd",
            "transcript_path": path,
        }
        _, _, query = _run_main(hook_input, search_results=[])

    assert query is not None, "search must run when walk-back has prior context"
    assert "memory architecture" in query, (
        "walk-back query must include prior assistant text as topical anchor"
    )
    assert "proceed" in query, (
        "walk-back query should preserve the user's affirmative tail"
    )
    print("[OK] main walks back to prior assistant turn when prompt is affirmative")


def test_main_walks_back_when_prompt_is_short():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "transcript.jsonl")
        _write_jsonl(path, [
            {"role": "assistant", "content": "Discussion about PowerShell quoting."},
        ])
        hook_input = {
            "prompt": "hi",
            "cwd": "/some/cwd",
            "transcript_path": path,
        }
        _, _, query = _run_main(hook_input, search_results=[])

    assert query is not None
    assert "PowerShell" in query
    print("[OK] main walks back when prompt length is below MIN_PROMPT_CHARS")


def test_main_skips_when_walk_back_has_no_transcript():
    """Affirmative + no transcript path: preserve original skip behavior.
    Walking back with no anchor would just embed the affirmative alone,
    which produces noise."""
    hook_input = {"prompt": "yes", "cwd": "/some/cwd"}  # no transcript_path
    _, stdout, query = _run_main(hook_input, search_results=[])

    assert query is None, "search must NOT run when walk-back has no prior to anchor"
    assert stdout == "", "no additionalContext must be emitted on skip"
    print("[OK] main skips cleanly when walk-back triggers but no transcript is available")


def test_main_substantive_prompt_does_not_walk_back():
    """Substantive prompts use their own text as the query, not the
    prior assistant turn. Walk-back must only kick in for degenerates."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "transcript.jsonl")
        _write_jsonl(path, [
            {"role": "assistant", "content": "an unrelated prior topic"},
        ])
        substantive = "Should we use single quotes when shelling PowerShell?"
        hook_input = {
            "prompt": substantive,
            "cwd": "/cwd",
            "transcript_path": path,
        }
        _, _, query = _run_main(hook_input, search_results=[])

    assert query is not None
    assert "single quotes" in query
    assert "unrelated prior topic" not in query, (
        "walk-back must not contaminate substantive prompts"
    )
    print("[OK] main leaves substantive prompts intact (no walk-back contamination)")


# -- Runner --------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_should_walk_back_short_prompt,
        test_should_walk_back_affirmative_set_long_enough,
        test_should_walk_back_affirmative_case_insensitive,
        test_should_walk_back_substantive_prompt_does_not_trigger,
        test_should_walk_back_long_non_affirmative_no_trigger,
        test_stringify_content_plain_string,
        test_stringify_content_text_blocks,
        test_stringify_content_empty_or_invalid,
        test_extract_prior_assistant_flat_role_format,
        test_extract_prior_assistant_wrapped_message_format,
        test_extract_prior_assistant_typed_blocks,
        test_extract_prior_assistant_truncates_long_text,
        test_extract_prior_assistant_no_assistant_entries_returns_empty,
        test_extract_prior_assistant_missing_path_returns_empty,
        test_extract_prior_assistant_tolerates_malformed_lines,
        test_main_walks_back_when_prompt_is_affirmative,
        test_main_walks_back_when_prompt_is_short,
        test_main_skips_when_walk_back_has_no_transcript,
        test_main_substantive_prompt_does_not_walk_back,
    ]
    for t in tests:
        t()
    print(f"\n[SUCCESS] All {len(tests)} tests passed")
