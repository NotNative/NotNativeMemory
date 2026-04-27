#!/usr/bin/env python3
"""
Tests for turn_analysis.py — combined extraction + promise detection hook.

These tests verify the parsing, schema-shape coercion, prompt construction,
nudge-storage path, and legacy-log cleanup that the analysis hook relies on.
LLM calls themselves are not exercised — those are integration concerns.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Add hooks directory to path for imports.
_HOOKS_DIR = Path(__file__).parent.parent / "nna" / "hooks"
sys.path.insert(0, str(_HOOKS_DIR))

import turn_analysis  # noqa: E402  (import after sys.path tweak)


# -- Schema / parsing ------------------------------------------------------

def test_strip_markdown_fences_json_block():
    raw = """```json
{"results": [{"type": "behavioral"}]}
```"""
    out = turn_analysis._strip_markdown_fences(raw)
    assert out.startswith("{")
    assert out.endswith("}")
    assert "```" not in out
    print("[OK] _strip_markdown_fences handles ```json blocks")


def test_strip_markdown_fences_plain_block():
    raw = """```
{"results": []}
```"""
    out = turn_analysis._strip_markdown_fences(raw)
    assert out == '{"results": []}'
    print("[OK] _strip_markdown_fences handles plain ``` blocks")


def test_strip_markdown_fences_no_fence():
    raw = '{"results": []}'
    assert turn_analysis._strip_markdown_fences(raw) == raw
    print("[OK] _strip_markdown_fences leaves unfenced JSON alone")


def test_build_analysis_prompt_includes_both_sides():
    user = "Stop summarizing what you did."
    model = "Acknowledged. I will be terse."
    prompt = turn_analysis._build_analysis_prompt(user, model)
    assert user in prompt
    assert model in prompt
    assert "Learnable patterns" in prompt
    assert "Promise tracking" in prompt
    assert "shouldNudge" in prompt
    print("[OK] _build_analysis_prompt covers both sections")


def test_build_analysis_prompt_caps_lengths():
    # Use unique multi-char tokens so we don't collide with the schema text.
    long_user = "USERTOK" * 1000     # 7000 chars total
    long_model = "MODELTOK" * 1000   # 8000 chars total
    prompt = turn_analysis._build_analysis_prompt(long_user, long_model)
    # User capped at 2000 chars → 2000 // 7 = 285 full tokens (+ partial).
    # Model capped at 4000 chars → 4000 // 8 = 500 full tokens.
    assert prompt.count("USERTOK") < 1000, "user prompt must be truncated"
    assert prompt.count("MODELTOK") == 500, "model response must be truncated to 4000 chars"
    print("[OK] _build_analysis_prompt caps user/model lengths")


# -- _call_analysis_llm shape coercion -------------------------------------

def _mock_llm_response(content_obj: dict) -> bytes:
    """Build a mock OpenAI-style chat completion response body."""
    return json.dumps({
        "choices": [{"message": {"content": json.dumps(content_obj)}}]
    }).encode("utf-8")


def test_call_analysis_llm_parses_full_shape():
    payload = {
        "results": [
            {"type": "behavioral", "category": "correction", "key": "x", "value": "y"}
        ],
        "unfulfilledPromises": [{"promise": "look up X", "reason": "no result"}],
        "shouldNudge": True,
        "nudgeText": "Earlier I said I'd check X.",
    }

    with mock.patch.object(turn_analysis.urllib.request, "urlopen") as urlopen_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = _mock_llm_response(payload)
        out = turn_analysis._call_analysis_llm({
            "user_prompt": "x" * 200,
            "model_response": "y" * 200,
        })

    assert len(out["results"]) == 1
    assert len(out["unfulfilledPromises"]) == 1
    assert out["shouldNudge"] is True
    assert "X" in out["nudgeText"]
    print("[OK] _call_analysis_llm returns full shape")


def test_call_analysis_llm_coerces_missing_keys():
    # LLM returns only "results" — promise fields should default to empty/false.
    payload = {"results": []}

    with mock.patch.object(turn_analysis.urllib.request, "urlopen") as urlopen_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = _mock_llm_response(payload)
        out = turn_analysis._call_analysis_llm({
            "user_prompt": "x" * 200,
            "model_response": "y" * 200,
        })

    assert out["results"] == []
    assert out["unfulfilledPromises"] == []
    assert out["shouldNudge"] is False
    assert out["nudgeText"] == ""
    print("[OK] _call_analysis_llm coerces missing keys to safe defaults")


def test_call_analysis_llm_skips_short_conversation():
    # Below MIN_CONVERSATION_LENGTH * 10 = 300 chars total.
    out = turn_analysis._call_analysis_llm({
        "user_prompt": "ok",
        "model_response": "got it",
    })
    assert out["results"] == []
    assert out["shouldNudge"] is False
    print("[OK] _call_analysis_llm skips short conversations")


def test_call_analysis_llm_returns_empty_on_llm_failure():
    import urllib.error

    with mock.patch.object(turn_analysis.urllib.request, "urlopen") as urlopen_mock:
        urlopen_mock.side_effect = urllib.error.URLError("connection refused")
        out = turn_analysis._call_analysis_llm({
            "user_prompt": "x" * 200,
            "model_response": "y" * 200,
        })

    assert out["results"] == []
    assert out["shouldNudge"] is False
    print("[OK] _call_analysis_llm returns empty shape on LLM failure")


# -- Nudge storage ---------------------------------------------------------

def test_store_pending_nudge_skips_empty_text():
    with mock.patch.object(turn_analysis, "_rag_ingest") as ingest_mock:
        result = turn_analysis._store_pending_nudge("   ", "conv1234")
    assert result is False
    ingest_mock.assert_not_called()
    print("[OK] _store_pending_nudge skips empty/whitespace text")


def test_store_pending_nudge_calls_rag_with_high_importance():
    with mock.patch.object(turn_analysis, "_rag_ingest", return_value=True) as ingest_mock:
        result = turn_analysis._store_pending_nudge(
            "Earlier I said I'd check X.",
            "conv12345678",
        )
    assert result is True
    args, kwargs = ingest_mock.call_args
    # Either positional or keyword form — accept both.
    call_kwargs = kwargs if kwargs else dict(zip(["title", "content", "tags", "importance"], args))
    assert call_kwargs["importance"] == "high"
    assert "pending_nudge" in call_kwargs["tags"]
    assert call_kwargs["title"].startswith("pending_nudge:")
    print("[OK] _store_pending_nudge tags + importance set correctly")


# -- Extraction storage ----------------------------------------------------

def test_store_to_memoria_skips_malformed_items():
    items = [
        {"type": "behavioral"},  # missing category/key/value
        {"category": "x", "key": "y", "value": "z"},  # missing type
    ]
    with mock.patch.object(turn_analysis, "_rag_ingest", return_value=True) as ingest_mock:
        stored = turn_analysis._store_to_memoria(items, "conv12345678")
    assert stored == 0
    ingest_mock.assert_not_called()
    print("[OK] _store_to_memoria skips items missing required fields")


def test_store_to_memoria_caps_at_max_extractions():
    items = [
        {"type": "behavioral", "category": "c", "key": f"k{i}", "value": "v"}
        for i in range(20)
    ]
    with mock.patch.object(turn_analysis, "_rag_ingest", return_value=True) as ingest_mock:
        stored = turn_analysis._store_to_memoria(items, "conv12345678")
    assert stored == turn_analysis.MAX_EXTRACTIONS_PER_TURN
    assert ingest_mock.call_count == turn_analysis.MAX_EXTRACTIONS_PER_TURN
    print("[OK] _store_to_memoria caps at MAX_EXTRACTIONS_PER_TURN")


def test_store_to_memoria_marks_high_confidence_as_high_importance():
    items = [
        {"type": "behavioral", "category": "c", "key": "k", "value": "v", "confidence": "high"},
        {"type": "behavioral", "category": "c", "key": "k2", "value": "v", "confidence": "low"},
    ]
    captured: list = []

    def _capture(title, content, tags, importance):
        captured.append(importance)
        return True

    with mock.patch.object(turn_analysis, "_rag_ingest", side_effect=_capture):
        turn_analysis._store_to_memoria(items, "conv12345678")
    assert captured == ["high", "normal"]
    print("[OK] _store_to_memoria maps confidence -> importance")


# -- Legacy-log cleanup ----------------------------------------------------

def test_cleanup_legacy_log_removes_old_file():
    with tempfile.TemporaryDirectory() as tmp:
        legacy_path = os.path.join(tmp, "turn_extractor.log")
        with open(legacy_path, "w") as f:
            f.write("old log content")
        with mock.patch.object(turn_analysis, "_LEGACY_LOG_PATH", legacy_path):
            turn_analysis._cleanup_legacy_log()
        assert not os.path.exists(legacy_path)
    print("[OK] _cleanup_legacy_log removes turn_extractor.log")


def test_cleanup_legacy_log_no_op_if_missing():
    with tempfile.TemporaryDirectory() as tmp:
        legacy_path = os.path.join(tmp, "turn_extractor.log")
        # File deliberately not created.
        with mock.patch.object(turn_analysis, "_LEGACY_LOG_PATH", legacy_path):
            turn_analysis._cleanup_legacy_log()  # Should not raise.
    print("[OK] _cleanup_legacy_log is a no-op when legacy log is absent")


# -- Log path configuration -------------------------------------------------

def test_log_path_uses_renamed_file():
    # The default path uses the new name.
    assert "turn_analysis.log" in turn_analysis.LOG_PATH or os.environ.get("MEMORY_EXTRACT_LOG")
    assert "turn_extractor.log" not in turn_analysis.LOG_PATH or os.environ.get("MEMORY_EXTRACT_LOG")
    print(f"[OK] LOG_PATH = {turn_analysis.LOG_PATH}")


# -- Runner ----------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_strip_markdown_fences_json_block,
        test_strip_markdown_fences_plain_block,
        test_strip_markdown_fences_no_fence,
        test_build_analysis_prompt_includes_both_sides,
        test_build_analysis_prompt_caps_lengths,
        test_call_analysis_llm_parses_full_shape,
        test_call_analysis_llm_coerces_missing_keys,
        test_call_analysis_llm_skips_short_conversation,
        test_call_analysis_llm_returns_empty_on_llm_failure,
        test_store_pending_nudge_skips_empty_text,
        test_store_pending_nudge_calls_rag_with_high_importance,
        test_store_to_memoria_skips_malformed_items,
        test_store_to_memoria_caps_at_max_extractions,
        test_store_to_memoria_marks_high_confidence_as_high_importance,
        test_cleanup_legacy_log_removes_old_file,
        test_cleanup_legacy_log_no_op_if_missing,
        test_log_path_uses_renamed_file,
    ]
    for t in tests:
        t()
    print(f"\n[SUCCESS] All {len(tests)} tests passed")
