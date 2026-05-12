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


# -- Fact query in parallel with memory search (Fix #2) ------------------

def test_extract_subject_candidates_drops_stopwords_and_short_tokens():
    text = "What is the model on the inference-host with port 9500"
    out = inject._extract_subject_candidates(text)
    # Stopwords ("what", "the", "with", "on") and short tokens must be dropped.
    assert "the" not in out and "what" not in out and "on" not in out
    # Hyphenated subject must survive as a single candidate.
    assert "inference-host" in out
    print("[OK] _extract_subject_candidates drops stopwords and preserves hyphenated subjects")


def test_extract_subject_candidates_caps_at_max():
    text = " ".join([f"alpha{i}beta{i}" for i in range(20)])
    out = inject._extract_subject_candidates(text)
    assert len(out) <= inject.FACT_QUERY_MAX_SUBJECTS
    print("[OK] _extract_subject_candidates caps at FACT_QUERY_MAX_SUBJECTS")


def test_extract_subject_candidates_dedupes_preserving_order():
    out = inject._extract_subject_candidates("qwen3 qwen3 ollama qwen3")
    assert out.count("qwen3") == 1
    assert out.index("qwen3") < out.index("ollama")
    print("[OK] _extract_subject_candidates dedupes while preserving first-occurrence order")


def test_extract_subject_candidates_empty_text():
    assert inject._extract_subject_candidates("") == []
    assert inject._extract_subject_candidates("a b c") == []  # all too short
    assert inject._extract_subject_candidates("the and but for") == []  # all stopwords
    print("[OK] _extract_subject_candidates returns empty for noise-only input")


def test_query_facts_for_subject_parses_response():
    """Wire-shape regression guard: memory_fact_query response is wrapped
    in content[].text exactly like memory_search; the parser must reach
    into the inner facts array."""
    cfg_facts = [
        {"subject": "inference-host", "predicate": "model", "object": "qwen3", "confidence": 1.0},
    ]
    body = json.dumps({
        "result": {
            "content": [{"type": "text", "text": json.dumps({"facts": cfg_facts, "count": 1})}],
            "isError": False,
        }
    }).encode("utf-8")

    with mock.patch.object(inject.urllib.request, "urlopen") as urlopen_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = body
        out = inject._query_facts_for_subject("inference-host", "/cwd")

    assert out == cfg_facts
    print("[OK] _query_facts_for_subject extracts facts from wrapped MCP response")


def test_query_facts_for_subject_returns_empty_on_network_error():
    import urllib.error
    with mock.patch.object(inject.urllib.request, "urlopen") as urlopen_mock:
        urlopen_mock.side_effect = urllib.error.URLError("down")
        out = inject._query_facts_for_subject("anything", "/cwd")
    assert out == []
    print("[OK] _query_facts_for_subject returns [] on MCP unreachable")


def test_query_facts_dedupes_by_subject_predicate():
    """If two extracted subjects somehow point at the same (subject,
    predicate) pair, we keep only the first — facts are temporally
    unique per pair."""
    calls = []
    def fake_query(subject, project):
        calls.append(subject)
        # Both queries return the same fact tuple.
        return [{"subject": "host", "predicate": "model", "object": "qwen3", "confidence": 1.0}]

    with mock.patch.object(inject, "_extract_subject_candidates", return_value=["host", "qwen3"]), \
            mock.patch.object(inject, "_query_facts_for_subject", side_effect=fake_query):
        out = inject._query_facts("doesn't matter", "/cwd")

    assert len(out) == 1, "duplicate (subject, predicate) facts must be deduped"
    assert calls == ["host", "qwen3"]
    print("[OK] _query_facts dedupes by (subject, predicate) across subject candidates")


def test_query_facts_returns_empty_when_no_candidates():
    with mock.patch.object(inject, "_extract_subject_candidates", return_value=[]):
        assert inject._query_facts("foo", "/cwd") == []
    print("[OK] _query_facts skips when no candidate subjects survive extraction")


def test_format_facts_emits_current_state_header():
    facts = [
        {"subject": "host", "predicate": "model", "object": "qwen3"},
        {"subject": "host", "predicate": "port", "object": "9500"},
    ]
    out = inject._format_facts(facts)
    assert out.startswith("Current state:")
    assert "host" in out and "model" in out and "qwen3" in out
    assert "9500" in out
    print("[OK] _format_facts emits a 'Current state:' header followed by triples")


def test_format_facts_empty_returns_empty_string():
    assert inject._format_facts([]) == ""
    # All-malformed facts also produce empty (no triples to render).
    assert inject._format_facts([{"subject": "", "predicate": "", "object": ""}]) == ""
    print("[OK] _format_facts returns empty string when there's nothing to render")


def test_main_injects_facts_alongside_memories():
    """End-to-end: hook output must contain BOTH a 'Current state:' block
    (from fact_query) AND a 'From memory:' block (from memory_search),
    separated by a blank line."""
    hook_input = {
        "prompt": "What model is the inference-host running right now?",
        "cwd": "/cwd",
    }
    fake_memories = [{"content": "Prefer terse responses.", "similarity": 0.9}]
    fake_facts = [{"subject": "inference-host", "predicate": "model",
                   "object": "qwen3-30b-a3b", "confidence": 1.0}]

    stdin = io.StringIO(json.dumps(hook_input))
    stdout = io.StringIO()

    def fake_exit(code=0):
        raise SystemExit(code)

    with mock.patch.object(inject.sys, "stdin", stdin), \
            mock.patch.object(inject.sys, "stdout", stdout), \
            mock.patch.object(inject.sys, "exit", side_effect=fake_exit), \
            mock.patch.object(inject, "_search_memories", return_value=fake_memories), \
            mock.patch.object(inject, "_filter_relevant", return_value=fake_memories), \
            mock.patch.object(inject, "_query_facts", return_value=fake_facts), \
            mock.patch.object(inject, "_log_execution"):
        try:
            inject.main()
        except SystemExit:
            pass

    output = json.loads(stdout.getvalue())
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "Current state:" in context
    # Class-less memory falls through to the background bucket header.
    assert "Background context (may be stale):" in context
    assert "qwen3-30b-a3b" in context
    assert "Prefer terse" in context
    # Facts block should precede memories block.
    assert context.index("Current state:") < context.index("Background context")
    print("[OK] main injects facts block before memories block")


def test_main_emits_facts_only_when_no_memories_match():
    """Even when memory_search returns nothing, a fact match alone is
    enough to fire the injection. Pre-fix this would short-circuit
    entirely."""
    hook_input = {"prompt": "long enough query about the inference host", "cwd": "/cwd"}
    fake_facts = [{"subject": "inference-host", "predicate": "model", "object": "qwen3"}]

    stdin = io.StringIO(json.dumps(hook_input))
    stdout = io.StringIO()

    def fake_exit(code=0):
        raise SystemExit(code)

    with mock.patch.object(inject.sys, "stdin", stdin), \
            mock.patch.object(inject.sys, "stdout", stdout), \
            mock.patch.object(inject.sys, "exit", side_effect=fake_exit), \
            mock.patch.object(inject, "_search_memories", return_value=[]), \
            mock.patch.object(inject, "_filter_relevant", return_value=[]), \
            mock.patch.object(inject, "_query_facts", return_value=fake_facts), \
            mock.patch.object(inject, "_log_execution"):
        try:
            inject.main()
        except SystemExit:
            pass

    output = json.loads(stdout.getvalue())
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "Current state:" in context
    assert "From memory:" not in context
    assert "Background context" not in context  # no memories means no memory section
    print("[OK] main emits a facts-only injection when no memories match")


# -- Class-aware injection framing (Fix #3) -------------------------------

def test_class_headers_pin_load_bearing_strings():
    """Tier 2: snapshot the headers. These strings reach the model
    verbatim every turn; drift here changes how every memory is framed.

    Order is load-bearing too: rules first (constraints) → preferences
    (style) → background (optional). The model reads top-to-bottom.
    """
    keys = [k for k, _ in inject._CLASS_HEADERS]
    headers = [h for _, h in inject._CLASS_HEADERS]
    assert keys == ["rule", "preference", "memory"], (
        "class order must be rule → preference → memory; constraints first"
    )
    assert headers[0] == "Standing rules:"
    assert headers[1] == "User preferences:"
    assert headers[2] == "Background context (may be stale):"
    print("[OK] _CLASS_HEADERS snapshot: order + verbatim headers")


def test_bucket_memories_by_class_routes_each_class():
    memories = [
        {"content": "Never use em-dashes.", "class": "rule"},
        {"content": "Prefer terse responses.", "class": "preference"},
        {"content": "We chose HS256 last quarter.", "class": "memory"},
    ]
    buckets = inject._bucket_memories_by_class(memories)
    assert len(buckets["rule"]) == 1 and buckets["rule"][0]["content"].startswith("Never")
    assert len(buckets["preference"]) == 1
    assert len(buckets["memory"]) == 1
    print("[OK] _bucket_memories_by_class routes each class to its own bucket")


def test_bucket_memories_unknown_or_missing_class_falls_into_memory():
    """Unclassified memories must not be silently dropped — they fall
    into the background bucket so the model still sees them."""
    memories = [
        {"content": "no class field"},
        {"content": "explicit null class", "class": None},
        {"content": "unknown class name", "class": "wat"},
    ]
    buckets = inject._bucket_memories_by_class(memories)
    assert buckets["rule"] == [] and buckets["preference"] == []
    assert len(buckets["memory"]) == 3
    print("[OK] _bucket_memories_by_class falls unknown/missing classes into 'memory'")


def test_bucket_memories_preserves_order_within_bucket():
    memories = [
        {"content": "first rule", "class": "rule"},
        {"content": "first pref", "class": "preference"},
        {"content": "second rule", "class": "rule"},
        {"content": "second pref", "class": "preference"},
    ]
    buckets = inject._bucket_memories_by_class(memories)
    assert [m["content"] for m in buckets["rule"]] == ["first rule", "second rule"]
    assert [m["content"] for m in buckets["preference"]] == ["first pref", "second pref"]
    print("[OK] _bucket_memories_by_class preserves arrival order within each bucket")


def test_format_memories_emits_all_three_sections_in_order():
    memories = [
        {"content": "be terse", "class": "preference"},
        {"content": "no em-dashes", "class": "rule"},
        {"content": "background note", "class": "memory"},
    ]
    out = inject._format_memories(memories)
    # Rules must come first regardless of input order; preferences second;
    # background last. This is the binding-constraints-first contract.
    assert out.index("Standing rules:") < out.index("User preferences:")
    assert out.index("User preferences:") < out.index("Background context")
    assert "no em-dashes" in out
    assert "be terse" in out
    assert "background note" in out
    print("[OK] _format_memories emits sections in rule -> preference -> background order")


def test_format_memories_skips_empty_buckets():
    """Only non-empty buckets get a header. A turn that returns 2
    rules and zero preferences must not print an empty 'User preferences:'
    section — that would be misleading framing."""
    memories = [{"content": "no em-dashes", "class": "rule"}]
    out = inject._format_memories(memories)
    assert "Standing rules:" in out
    assert "User preferences:" not in out
    assert "Background context" not in out
    print("[OK] _format_memories skips headers for empty class buckets")


def test_format_memories_empty_input_returns_empty_string():
    assert inject._format_memories([]) == ""
    print("[OK] _format_memories returns empty string when no memories")


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
        # Fix #2: parallel memory_fact_query
        test_extract_subject_candidates_drops_stopwords_and_short_tokens,
        test_extract_subject_candidates_caps_at_max,
        test_extract_subject_candidates_dedupes_preserving_order,
        test_extract_subject_candidates_empty_text,
        test_query_facts_for_subject_parses_response,
        test_query_facts_for_subject_returns_empty_on_network_error,
        test_query_facts_dedupes_by_subject_predicate,
        test_query_facts_returns_empty_when_no_candidates,
        test_format_facts_emits_current_state_header,
        test_format_facts_empty_returns_empty_string,
        test_main_injects_facts_alongside_memories,
        test_main_emits_facts_only_when_no_memories_match,
        # Fix #3: class-aware injection framing
        test_class_headers_pin_load_bearing_strings,
        test_bucket_memories_by_class_routes_each_class,
        test_bucket_memories_unknown_or_missing_class_falls_into_memory,
        test_bucket_memories_preserves_order_within_bucket,
        test_format_memories_emits_all_three_sections_in_order,
        test_format_memories_skips_empty_buckets,
        test_format_memories_empty_input_returns_empty_string,
    ]
    for t in tests:
        t()
    print(f"\n[SUCCESS] All {len(tests)} tests passed")
