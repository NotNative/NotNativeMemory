#!/usr/bin/env python3
"""
Tests for turn analysis: shared core + nna and Claude Code adapters.

Covers:
- Shared core: prompt construction, fence stripping, shape coercion,
  LLM call (Anthropic Messages and OpenAI-compat shapes), config
  resolution from env, model auto-discovery, ingest helpers.
- Claude adapter: transcript JSONL parsing, last-turn extraction,
  content-block flattening, stop_hook_active short-circuit.
- nna adapter: legacy log cleanup.

LLM endpoints are mocked. No live network calls.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Add the repo root to sys.path so `from hooks_shared.turn_analysis_core import ...` works.
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from hooks_shared import turn_analysis_core as core  # noqa: E402


# -- Fence stripping -------------------------------------------------------

def test_strip_markdown_fences_json_block():
    raw = """```json
{"results": [{"type": "behavioral"}]}
```"""
    out = core.strip_markdown_fences(raw)
    assert out.startswith("{") and out.endswith("}")
    assert "```" not in out
    print("[OK] strip_markdown_fences handles ```json blocks")


def test_strip_markdown_fences_plain_block():
    raw = """```
{"results": []}
```"""
    assert core.strip_markdown_fences(raw) == '{"results": []}'
    print("[OK] strip_markdown_fences handles plain ``` blocks")


def test_strip_markdown_fences_no_fence():
    raw = '{"results": []}'
    assert core.strip_markdown_fences(raw) == raw
    print("[OK] strip_markdown_fences leaves unfenced JSON alone")


# -- Prompt construction --------------------------------------------------

def test_build_analysis_prompt_includes_both_sides():
    user = "Stop summarizing what you did."
    model = "Acknowledged. I will be terse."
    prompt = core.build_analysis_prompt(user, model)
    assert user in prompt
    assert model in prompt
    assert "Learnable patterns" in prompt
    assert "Promise tracking" in prompt
    assert "shouldNudge" in prompt
    print("[OK] build_analysis_prompt covers both sections")


def test_build_analysis_prompt_caps_lengths():
    long_user = "USERTOK" * 1000     # 7000 chars
    long_model = "MODELTOK" * 1000   # 8000 chars
    prompt = core.build_analysis_prompt(long_user, long_model)
    # User capped at 2000 chars; model capped at 4000 chars.
    assert prompt.count("USERTOK") < 1000
    assert prompt.count("MODELTOK") == 500
    print("[OK] build_analysis_prompt caps user/model lengths")


# -- Shape coercion -------------------------------------------------------

def test_coerce_analysis_full_shape():
    parsed = {
        "results": [{"type": "behavioral"}],
        "unfulfilledPromises": [{"promise": "x"}],
        "shouldNudge": True,
        "nudgeText": "follow up",
    }
    out = core.coerce_analysis(parsed)
    assert out == parsed
    print("[OK] coerce_analysis preserves full valid shape")


def test_coerce_analysis_missing_keys_default_safe():
    out = core.coerce_analysis({"results": []})
    assert out == {
        "results": [],
        "unfulfilledPromises": [],
        "shouldNudge": False,
        "nudgeText": "",
    }
    print("[OK] coerce_analysis fills missing keys with safe defaults")


def test_coerce_analysis_wrong_types_default_safe():
    out = core.coerce_analysis({
        "results": "not a list",
        "unfulfilledPromises": 42,
        "shouldNudge": "truthy-string",
        "nudgeText": None,
    })
    assert out["results"] == []
    assert out["unfulfilledPromises"] == []
    assert out["shouldNudge"] is True  # bool() of non-empty string
    assert out["nudgeText"] == ""
    print("[OK] coerce_analysis defends against wrong field types")


# -- Config resolution ----------------------------------------------------

def _make_config(api: str, **overrides) -> core.AnalysisConfig:
    base = dict(
        api=api,
        endpoint="http://test.local/x",
        model="test-model",
        headers={"Content-Type": "application/json"},
        models_url=None,
    )
    base.update(overrides)
    return core.AnalysisConfig(**base)


def test_resolve_config_anthropic_default():
    env = {"ANTHROPIC_API_KEY": "sk-ant-fake"}
    cfg = core.resolve_config_from_env(env)
    assert cfg.api == "anthropic_messages"
    assert cfg.endpoint == "https://api.anthropic.com/v1/messages"
    assert cfg.model == core.DEFAULT_ANTHROPIC_MODEL
    assert cfg.headers["x-api-key"] == "sk-ant-fake"
    assert cfg.headers["anthropic-version"] == core.DEFAULT_ANTHROPIC_VERSION
    print("[OK] resolve_config defaults to Anthropic Messages w/ Haiku")


def test_resolve_config_openai_base_url():
    env = {
        "OPENAI_BASE_URL": "http://localhost:1234/v1",
        "OPENAI_API_KEY": "lm-studio",
    }
    cfg = core.resolve_config_from_env(env)
    assert cfg.api == "openai_compat"
    assert cfg.endpoint == "http://localhost:1234/v1/chat/completions"
    assert cfg.models_url == "http://localhost:1234/v1/models"
    assert cfg.model is None  # auto-discover at call time
    assert cfg.headers["Authorization"] == "Bearer lm-studio"
    print("[OK] resolve_config picks up OPENAI_BASE_URL -> openai_compat")


def test_resolve_config_anthropic_base_url_proxy():
    env = {
        "ANTHROPIC_BASE_URL": "http://localhost:9000",
        "ANTHROPIC_API_KEY": "x",
    }
    cfg = core.resolve_config_from_env(env)
    assert cfg.api == "anthropic_messages"
    assert cfg.endpoint == "http://localhost:9000/v1/messages"
    print("[OK] resolve_config respects ANTHROPIC_BASE_URL for proxies")


def test_resolve_config_explicit_url_wins():
    env = {
        "MEMORY_EXTRACT_LLM_URL": "http://elsewhere/v1/chat/completions",
        "OPENAI_BASE_URL": "http://ignored/v1",
    }
    cfg = core.resolve_config_from_env(env)
    assert cfg.endpoint == "http://elsewhere/v1/chat/completions"
    assert cfg.api == "openai_compat"  # inferred from URL pattern
    print("[OK] resolve_config: explicit MEMORY_EXTRACT_LLM_URL wins")


def test_resolve_config_explicit_model_wins():
    env = {"OPENAI_BASE_URL": "http://x/v1", "MEMORY_EXTRACT_MODEL": "model-fixture-a"}
    cfg = core.resolve_config_from_env(env)
    assert cfg.model == "model-fixture-a"
    print("[OK] resolve_config: MEMORY_EXTRACT_MODEL pins the model")


# -- Model auto-discovery -------------------------------------------------

def test_discover_model_picks_first_id():
    cfg = _make_config(
        "openai_compat",
        models_url="http://x/v1/models",
        model=None,
    )
    body = json.dumps({
        "data": [{"id": "model-fixture-a"}, {"id": "model-fixture-b"}]
    }).encode("utf-8")

    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = body
        model = core.discover_model(cfg)

    assert model == "model-fixture-a"
    print("[OK] discover_model returns first id from /v1/models")


def test_discover_model_returns_none_on_empty():
    cfg = _make_config("openai_compat", models_url="http://x/v1/models", model=None)
    body = json.dumps({"data": []}).encode("utf-8")

    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = body
        assert core.discover_model(cfg) is None

    print("[OK] discover_model returns None when /v1/models is empty")


def test_discover_model_returns_none_on_failure():
    import urllib.error
    cfg = _make_config("openai_compat", models_url="http://x/v1/models", model=None)
    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock:
        urlopen_mock.side_effect = urllib.error.URLError("down")
        assert core.discover_model(cfg) is None
    print("[OK] discover_model returns None on URLError")


def test_discover_model_no_url_returns_none():
    cfg = _make_config("anthropic_messages", models_url=None)
    assert core.discover_model(cfg) is None
    print("[OK] discover_model returns None when models_url not set")


# -- LLM call shape (per-API) --------------------------------------------

def _mock_openai_response(content_obj: dict) -> bytes:
    return json.dumps({
        "choices": [{"message": {"content": json.dumps(content_obj)}}]
    }).encode("utf-8")


def _mock_anthropic_response(content_obj: dict) -> bytes:
    return json.dumps({
        "content": [{"type": "text", "text": json.dumps(content_obj)}]
    }).encode("utf-8")


def test_call_analysis_llm_openai_shape():
    cfg = _make_config("openai_compat", model="qwen-something")
    payload = {
        "results": [{"type": "behavioral", "category": "c", "key": "k", "value": "v"}],
        "unfulfilledPromises": [],
        "shouldNudge": False,
        "nudgeText": "",
    }
    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = _mock_openai_response(payload)
        out = core.call_analysis_llm("u" * 200, "m" * 200, cfg)
    assert len(out["results"]) == 1
    print("[OK] call_analysis_llm parses OpenAI-compat response")


def test_call_analysis_llm_anthropic_shape():
    cfg = _make_config("anthropic_messages", model="claude-haiku-4-5-20251001")
    payload = {
        "results": [],
        "unfulfilledPromises": [],
        "shouldNudge": True,
        "nudgeText": "follow up on X",
    }
    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = _mock_anthropic_response(payload)
        out = core.call_analysis_llm("u" * 200, "m" * 200, cfg)
    assert out["shouldNudge"] is True
    assert out["nudgeText"] == "follow up on X"
    print("[OK] call_analysis_llm parses Anthropic Messages response")


def test_call_analysis_llm_skips_short_conversation():
    cfg = _make_config("openai_compat", model="x")
    out = core.call_analysis_llm("ok", "got it", cfg)
    assert out == core.empty_analysis()
    print("[OK] call_analysis_llm skips conversations below min length")


def test_call_analysis_llm_empty_when_no_model_resolved():
    # openai_compat with no explicit model and no working models_url
    cfg = _make_config("openai_compat", model=None, models_url="http://x/v1/models")
    import urllib.error
    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock:
        urlopen_mock.side_effect = urllib.error.URLError("nope")
        out = core.call_analysis_llm("u" * 200, "m" * 200, cfg)
    assert out == core.empty_analysis()
    print("[OK] call_analysis_llm bails when model can't be resolved")


def test_call_analysis_llm_empty_on_invalid_json():
    cfg = _make_config("openai_compat", model="x")
    bad_body = json.dumps({
        "choices": [{"message": {"content": "this is not json {{ malformed"}}]
    }).encode("utf-8")
    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = bad_body
        out = core.call_analysis_llm("u" * 200, "m" * 200, cfg)
    assert out == core.empty_analysis()
    print("[OK] call_analysis_llm tolerates malformed LLM JSON output")


# -- Storage helpers ------------------------------------------------------

def test_store_pending_nudge_skips_empty_text():
    cfg = _make_config("openai_compat")
    with mock.patch.object(core, "rag_ingest") as ingest_mock:
        result = core.store_pending_nudge("   ", "conv12345", cfg)
    assert result is False
    ingest_mock.assert_not_called()
    print("[OK] store_pending_nudge skips empty/whitespace text")


def test_store_pending_nudge_high_importance_and_tagged():
    cfg = _make_config("openai_compat")
    with mock.patch.object(core, "rag_ingest", return_value=True) as ingest_mock:
        result = core.store_pending_nudge("Earlier I said I'd check X.", "conv12345", cfg)
    assert result is True
    _, kwargs = ingest_mock.call_args
    assert kwargs["importance"] == "high"
    assert "pending_nudge" in kwargs["tags"]
    assert kwargs["title"].startswith("pending_nudge:")
    print("[OK] store_pending_nudge writes with importance=high + pending_nudge tag")


def test_store_extractions_skips_malformed_items():
    cfg = _make_config("openai_compat")
    items = [
        {"type": "behavioral"},  # missing category/key/value
        {"category": "x", "key": "y", "value": "z"},  # missing type
    ]
    with mock.patch.object(core, "rag_ingest", return_value=True) as ingest_mock:
        stored = core.store_extractions(items, "conv12345", cfg)
    assert stored == 0
    ingest_mock.assert_not_called()
    print("[OK] store_extractions skips items missing required fields")


def test_store_extractions_caps_at_max():
    cfg = _make_config("openai_compat")
    items = [
        {"type": "behavioral", "category": "c", "key": f"k{i}", "value": "v"}
        for i in range(20)
    ]
    with mock.patch.object(core, "rag_ingest", return_value=True) as ingest_mock:
        stored = core.store_extractions(items, "conv12345", cfg)
    assert stored == cfg.max_extractions
    assert ingest_mock.call_count == cfg.max_extractions
    print("[OK] store_extractions caps at config.max_extractions")


def test_store_extractions_high_confidence_maps_to_high_importance():
    cfg = _make_config("openai_compat")
    items = [
        {"type": "behavioral", "category": "c", "key": "k1", "value": "v", "confidence": "high"},
        {"type": "behavioral", "category": "c", "key": "k2", "value": "v", "confidence": "low"},
    ]
    captured = []

    def _capture(title, content, tags, importance, config):
        captured.append(importance)
        return True

    with mock.patch.object(core, "rag_ingest", side_effect=_capture):
        core.store_extractions(items, "conv12345", cfg)
    assert captured == ["high", "normal"]
    print("[OK] store_extractions maps confidence->importance")


# -- analyze_turn end-to-end ---------------------------------------------

def test_analyze_turn_short_circuit_below_min_length():
    cfg = _make_config("openai_compat", model="x")
    with mock.patch.object(core, "rag_ingest") as ingest_mock:
        out = core.analyze_turn("u", "m", "/some/cwd", cfg)
    assert out["stored"] == 0
    assert out["nudge_stored"] is False
    ingest_mock.assert_not_called()
    print("[OK] analyze_turn short-circuits below min length")


def test_analyze_turn_persists_extractions_and_nudge():
    cfg = _make_config("openai_compat", model="x")
    payload = {
        "results": [
            {"type": "behavioral", "category": "correction", "key": "k", "value": "v"}
        ],
        "unfulfilledPromises": [{"promise": "p", "reason": "r"}],
        "shouldNudge": True,
        "nudgeText": "follow up",
    }
    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock, \
            mock.patch.object(core, "rag_ingest", return_value=True) as ingest_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = _mock_openai_response(payload)
        out = core.analyze_turn("u" * 200, "m" * 200, "/some/cwd", cfg)
    assert out["stored"] == 1
    assert out["nudge_stored"] is True
    # 1 extraction + 1 nudge = 2 ingest calls.
    assert ingest_mock.call_count == 2
    print("[OK] analyze_turn persists extractions + nudge end-to-end")


# -- Claude adapter: transcript parsing ----------------------------------

# The Claude hook lives at <repo>/claude/hooks/turn_analysis.py. Add it
# to sys.path so we can import it directly.
sys.path.insert(0, str(_REPO_ROOT / "claude" / "hooks"))
import turn_analysis as claude_adapter  # noqa: E402


def test_extract_text_content_string():
    assert claude_adapter._extract_text_content("hello world") == "hello world"
    print("[OK] _extract_text_content passes plain string through")


def test_extract_text_content_text_blocks():
    blocks = [
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ]
    out = claude_adapter._extract_text_content(blocks)
    assert "first" in out and "second" in out
    print("[OK] _extract_text_content concatenates text blocks")


def test_extract_text_content_includes_tool_annotations():
    blocks = [
        {"type": "text", "text": "let me check"},
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}},
        {"type": "tool_result", "content": "found 3 matches"},
    ]
    out = claude_adapter._extract_text_content(blocks)
    assert "let me check" in out
    assert "[tool_use: Grep]" in out
    assert "[tool_result: found 3 matches]" in out
    print("[OK] _extract_text_content annotates tool blocks")


def test_extract_text_content_handles_empty_and_unknown():
    assert claude_adapter._extract_text_content(None) == ""
    assert claude_adapter._extract_text_content([]) == ""
    assert claude_adapter._extract_text_content([{"type": "thinking"}]) == ""
    print("[OK] _extract_text_content handles empty and unknown block types")


def _write_transcript(path: str, entries: list) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def test_extract_last_turn_basic_pair():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "transcript.jsonl")
        _write_transcript(p, [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "hello"}},
            {"type": "user", "message": {"role": "user", "content": "what is 2+2"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "4"}},
        ])
        u, m = claude_adapter.extract_last_turn(p)
    assert u == "what is 2+2"
    assert m == "4"
    print("[OK] extract_last_turn returns the most recent (user, assistant) pair")


def test_extract_last_turn_with_typed_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "transcript.jsonl")
        _write_transcript(p, [
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "run grep"}
            ]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "running"},
                {"type": "tool_use", "name": "Grep", "input": {}},
            ]}},
        ])
        u, m = claude_adapter.extract_last_turn(p)
    assert "run grep" in u
    assert "running" in m
    assert "[tool_use: Grep]" in m
    print("[OK] extract_last_turn handles typed content blocks")


def test_extract_last_turn_flat_role_format():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "transcript.jsonl")
        _write_transcript(p, [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ])
        u, m = claude_adapter.extract_last_turn(p)
    assert u == "go"
    assert m == "ok"
    print("[OK] extract_last_turn handles flat {role, content} entries")


def test_extract_last_turn_missing_assistant_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "transcript.jsonl")
        _write_transcript(p, [
            {"role": "user", "content": "alone"},
        ])
        u, m = claude_adapter.extract_last_turn(p)
    assert u == "" and m == ""
    print("[OK] extract_last_turn returns empty when no assistant entry")


def test_extract_last_turn_missing_user_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "transcript.jsonl")
        _write_transcript(p, [
            {"role": "assistant", "content": "orphaned"},
        ])
        u, m = claude_adapter.extract_last_turn(p)
    assert u == "" and m == ""
    print("[OK] extract_last_turn returns empty when no preceding user")


def test_extract_last_turn_nonexistent_path_returns_empty():
    u, m = claude_adapter.extract_last_turn("/nonexistent/path.jsonl")
    assert u == "" and m == ""
    print("[OK] extract_last_turn returns empty for missing file")


def test_extract_last_turn_skips_malformed_lines():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "transcript.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"role": "user", "content": "ok"}) + "\n")
            fh.write("\n")  # blank
            fh.write("{broken json\n")
            fh.write(json.dumps({"role": "assistant", "content": "fine"}) + "\n")
        u, m = claude_adapter.extract_last_turn(p)
    assert u == "ok"
    assert m == "fine"
    print("[OK] extract_last_turn skips malformed JSONL lines")


# -- Runner --------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        # core: fence + prompt
        test_strip_markdown_fences_json_block,
        test_strip_markdown_fences_plain_block,
        test_strip_markdown_fences_no_fence,
        test_build_analysis_prompt_includes_both_sides,
        test_build_analysis_prompt_caps_lengths,
        # core: shape coercion
        test_coerce_analysis_full_shape,
        test_coerce_analysis_missing_keys_default_safe,
        test_coerce_analysis_wrong_types_default_safe,
        # core: config
        test_resolve_config_anthropic_default,
        test_resolve_config_openai_base_url,
        test_resolve_config_anthropic_base_url_proxy,
        test_resolve_config_explicit_url_wins,
        test_resolve_config_explicit_model_wins,
        # core: model discovery
        test_discover_model_picks_first_id,
        test_discover_model_returns_none_on_empty,
        test_discover_model_returns_none_on_failure,
        test_discover_model_no_url_returns_none,
        # core: LLM call
        test_call_analysis_llm_openai_shape,
        test_call_analysis_llm_anthropic_shape,
        test_call_analysis_llm_skips_short_conversation,
        test_call_analysis_llm_empty_when_no_model_resolved,
        test_call_analysis_llm_empty_on_invalid_json,
        # core: storage
        test_store_pending_nudge_skips_empty_text,
        test_store_pending_nudge_high_importance_and_tagged,
        test_store_extractions_skips_malformed_items,
        test_store_extractions_caps_at_max,
        test_store_extractions_high_confidence_maps_to_high_importance,
        # core: analyze_turn
        test_analyze_turn_short_circuit_below_min_length,
        test_analyze_turn_persists_extractions_and_nudge,
        # claude adapter: text extraction
        test_extract_text_content_string,
        test_extract_text_content_text_blocks,
        test_extract_text_content_includes_tool_annotations,
        test_extract_text_content_handles_empty_and_unknown,
        # claude adapter: transcript parsing
        test_extract_last_turn_basic_pair,
        test_extract_last_turn_with_typed_blocks,
        test_extract_last_turn_flat_role_format,
        test_extract_last_turn_missing_assistant_returns_empty,
        test_extract_last_turn_missing_user_returns_empty,
        test_extract_last_turn_nonexistent_path_returns_empty,
        test_extract_last_turn_skips_malformed_lines,
    ]
    for t in tests:
        t()
    print(f"\n[SUCCESS] All {len(tests)} tests passed")
