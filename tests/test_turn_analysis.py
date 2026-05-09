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

# turn_analysis_core lives in each bundle's _internal/ after the
# hooks_shared dissolve. Both bundle copies are byte-equivalent at
# the moment; this suite exercises the Claude bundle copy. NNA can
# diverge and add its own tests later.
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT / "hook_bundles" / "claude" / "notnative-memory"))

from _internal import turn_analysis_core as core  # noqa: E402


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
    assert "Learnable facts" in prompt
    assert "Promise tracking" in prompt
    assert "shouldNudge" in prompt
    print("[OK] build_analysis_prompt covers both sections")


def test_build_analysis_prompt_specifies_new_schema_and_quality_bar():
    """The prompt must steer extraction toward standalone facts.

    Regression guard for the schema reshape: we must keep the 'fact' field,
    confidence levels, and the 'no quota / quality not quantity' framing.
    Smaller local models read this prompt verbatim; drift here silently
    degrades extraction quality.
    """
    prompt = core.build_analysis_prompt("u", "m")
    assert '"fact"' in prompt
    assert '"tags"' in prompt
    assert '"confidence"' in prompt
    assert "stand alone" in prompt or "self-contained" in prompt.lower()
    assert "no quota" in prompt.lower() or "no upper target" in prompt.lower()
    # The bad-example block is a key part of the steering.
    assert "BAD" in prompt or "bad" in prompt
    print("[OK] build_analysis_prompt encodes the standalone-fact schema and quality bar")


def test_build_worker_analysis_prompt_steers_toward_vendor_quirks():
    """Worker prompt must steer extraction toward operational/integration
    knowledge, not user preferences. This is the core difference from the
    session prompt; drift here would cause workers to extract chat-style
    preference memories that have no value for future workers.
    """
    prompt = core.build_worker_analysis_prompt("envelope", "output")
    # The vendor/integration framing must be explicit.
    lower = prompt.lower()
    assert "vendor" in lower
    assert "tool-result" in lower or "tool result" in lower
    assert "operational" in lower or "gotcha" in lower
    # Schema must match the session prompt so storage is symmetric.
    assert '"fact"' in prompt
    assert '"summary"' in prompt
    assert '"shouldNudge"' in prompt
    # Must explicitly forbid run-specific transient data.
    assert "transient" in lower or "$4.99" in prompt or "Run-specific" in prompt
    print("[OK] build_worker_analysis_prompt steers toward vendor/operational extraction")


def test_build_worker_analysis_prompt_caps_lengths():
    long_envelope = "ENV" * 1000      # 3000 chars
    long_output = "OUT" * 2000        # 6000 chars
    prompt = core.build_worker_analysis_prompt(long_envelope, long_output)
    # Re-uses USER_PROMPT_CAP_CHARS / MODEL_RESPONSE_CAP_CHARS so a single
    # noisy worker run cannot blow the analyzer's context budget.
    assert prompt.count("ENV") < 1000
    assert prompt.count("OUT") < 2000
    print("[OK] build_worker_analysis_prompt caps envelope/output lengths")


def test_build_analysis_prompt_includes_summary_section():
    """The summary section drives §3.7 (compacted conversation summaries to RAG).

    The prompt must explicitly tell the LLM (a) to produce a summary, (b) to
    cover dialogue only and exclude tool calls/results, (c) to keep it brief.
    Drift here could fill RAG with verbose summaries that include mechanical
    artifacts the user explicitly does not want stored.
    """
    prompt = core.build_analysis_prompt("u", "m")
    assert '"summary"' in prompt
    # Dialogue-only constraint must be explicit; this is load-bearing.
    assert "tool calls" in prompt.lower() or "tool results" in prompt.lower()
    assert "dialogue" in prompt.lower() or "discussed" in prompt.lower()
    print("[OK] build_analysis_prompt includes the summary section with dialogue-only rule")


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
        "summary": "A short summary of the turn.",
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
        "summary": "",
    }
    print("[OK] coerce_analysis fills missing keys with safe defaults")


def test_coerce_analysis_wrong_types_default_safe():
    out = core.coerce_analysis({
        "results": "not a list",
        "unfulfilledPromises": 42,
        "shouldNudge": "truthy-string",
        "nudgeText": None,
        "summary": None,
    })
    assert out["results"] == []
    assert out["unfulfilledPromises"] == []
    assert out["shouldNudge"] is True  # bool() of non-empty string
    assert out["nudgeText"] == ""
    assert out["summary"] == ""
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


def test_resolve_config_default_max_tokens_is_generous():
    cfg = core.resolve_config_from_env({})
    assert cfg.max_tokens == core.DEFAULT_MAX_TOKENS
    assert cfg.max_tokens >= 16000, (
        "Default must leave headroom for reasoning models (Qwen, R1) to "
        "spend tokens on hidden CoT and still emit final JSON."
    )
    print("[OK] resolve_config defaults max_tokens to a reasoning-safe value")


def test_resolve_config_max_tokens_env_override():
    cfg = core.resolve_config_from_env({"MEMORY_EXTRACT_MAX_TOKENS": "2048"})
    assert cfg.max_tokens == 2048
    print("[OK] resolve_config honors MEMORY_EXTRACT_MAX_TOKENS override")


def test_call_analysis_llm_sends_configured_max_tokens():
    cfg = _make_config("openai_compat", model="x", max_tokens=12345)
    captured = {}

    def _capture(req, *_, **__):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        cm = mock.MagicMock()
        cm.__enter__.return_value.read.return_value = _mock_openai_response(
            core.empty_analysis()
        )
        return cm

    with mock.patch.object(core.urllib.request, "urlopen", side_effect=_capture):
        core.call_analysis_llm("u" * 200, "m" * 200, cfg)

    assert captured["body"]["max_tokens"] == 12345
    print("[OK] call_analysis_llm forwards configured max_tokens to the LLM")


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
        "results": [{"fact": "Bash escapes corrupt double-quoted PowerShell strings; use single quotes.", "tags": ["shell", "powershell"], "confidence": "high"}],
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


def test_store_conversation_summary_skips_empty_text():
    cfg = _make_config("openai_compat")
    with mock.patch.object(core, "rag_ingest") as ingest_mock:
        result = core.store_conversation_summary("", "conv12345", cfg)
    assert result is False
    result = core.store_conversation_summary("   \n\t  ", "conv12345", cfg)
    assert result is False
    ingest_mock.assert_not_called()
    print("[OK] store_conversation_summary skips empty/whitespace text")


def test_store_conversation_summary_writes_to_rag_with_session_summary_tag():
    """Summaries land in RAG (not memories) because they are narrative artifacts.

    Regression guard: tagged 'session-summary' so retrieval can filter them
    in or out, and tagged with the conv-prefix so all summaries from one
    session can be located together.
    """
    cfg = _make_config("openai_compat")
    summary = "PowerShell quoting from bash was clarified; single quotes are required."
    captured = {}

    def _capture(*, title, content, tags, importance, config):
        captured["title"] = title
        captured["content"] = content
        captured["tags"] = tags
        captured["importance"] = importance
        return True

    with mock.patch.object(core, "rag_ingest", side_effect=_capture):
        ok = core.store_conversation_summary(summary, "convABCDEFGH", cfg)

    assert ok is True
    assert captured["content"] == summary
    assert "session-summary" in captured["tags"]
    assert any(t.startswith("conv:") for t in captured["tags"])
    assert captured["title"].startswith("summary:")
    print("[OK] store_conversation_summary writes to RAG with session-summary + conv tags")


def test_store_conversation_summary_strips_surrounding_whitespace():
    cfg = _make_config("openai_compat")
    captured = {}

    def _capture(*, title, content, tags, importance, config):
        captured["content"] = content
        return True

    with mock.patch.object(core, "rag_ingest", side_effect=_capture):
        core.store_conversation_summary(
            "   A summary with leading and trailing whitespace.   \n",
            "conv12345",
            cfg,
        )
    assert captured["content"] == "A summary with leading and trailing whitespace."
    print("[OK] store_conversation_summary strips surrounding whitespace before storing")


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
        {"tags": ["x"], "confidence": "high"},      # missing fact
        {"fact": "", "tags": [], "confidence": "high"},  # empty fact
        {"fact": "   ", "confidence": "high"},      # whitespace-only fact
        {"fact": 12345, "confidence": "high"},      # non-string fact
        "not a dict",                                # wrong type entirely
    ]
    with mock.patch.object(core, "memory_store_call", return_value=True) as store_mock:
        stored = core.store_extractions(items, "conv12345", cfg)
    assert stored == 0
    store_mock.assert_not_called()
    print("[OK] store_extractions skips items missing/invalid fact field")


def test_store_extractions_caps_at_max():
    """Cap is a runaway hedge against malfunctioning LLMs, not a quota."""
    cfg = _make_config("openai_compat", max_extractions=3)
    items = [
        {"fact": f"Standalone fact number {i} with enough words to be useful.", "confidence": "high"}
        for i in range(20)
    ]
    with mock.patch.object(core, "memory_store_call", return_value=True) as store_mock:
        stored = core.store_extractions(items, "conv12345", cfg)
    assert stored == 3
    assert store_mock.call_count == 3
    print("[OK] store_extractions caps at config.max_extractions")


def test_store_extractions_default_max_is_high_enough_for_volume():
    """A turn that yields 30 facts must store all 30; volume is not the constraint."""
    cfg = core.AnalysisConfig(
        api="openai_compat",
        endpoint="http://x/y",
        model="m",
        headers={"Content-Type": "application/json"},
    )
    assert cfg.max_extractions >= 30, (
        "Default max_extractions must be high enough that legitimate rich turns "
        "are not silently truncated. Quality is the bar, not quantity."
    )
    items = [
        {"fact": f"Fact number {i} with enough body to be standalone.", "confidence": "medium"}
        for i in range(30)
    ]
    with mock.patch.object(core, "memory_store_call", return_value=True) as store_mock:
        stored = core.store_extractions(items, "conv12345", cfg)
    assert stored == 30
    assert store_mock.call_count == 30
    print("[OK] store_extractions default cap is high enough for rich turns (30+ facts)")


def test_store_extractions_confidence_maps_to_importance():
    cfg = _make_config("openai_compat")
    items = [
        {"fact": "High-confidence rule about A.", "confidence": "high"},
        {"fact": "Medium-confidence rule about B.", "confidence": "medium"},
        {"fact": "Low-confidence rule about C.", "confidence": "low"},
        {"fact": "Unspecified-confidence rule about D."},  # default -> normal
        {"fact": "Garbage-confidence rule about E.", "confidence": "wat"},  # unknown -> normal
    ]
    captured = []

    def _capture(content, tags, importance, source, config):
        captured.append(importance)
        return True

    with mock.patch.object(core, "memory_store_call", side_effect=_capture):
        core.store_extractions(items, "conv12345", cfg)
    assert captured == ["high", "normal", "low", "normal", "normal"]
    print("[OK] store_extractions maps confidence -> importance (high/medium/low + defaults)")


def test_store_extractions_writes_fact_verbatim_no_template():
    """The memory content must be the fact text, with no [TYPE]/Key/Value wrapping.

    Smaller local models read retrieved memories at injection time; ceremonial
    metadata in the body dilutes the signal and burns context.
    """
    cfg = _make_config("openai_compat")
    fact_text = (
        "When writing a PowerShell script from bash, use single quotes around "
        "strings containing special characters; bash's escape rendering will "
        "otherwise corrupt double-quoted strings before PowerShell sees them."
    )
    captured = {}

    def _capture(content, tags, importance, source, config):
        captured["content"] = content
        captured["tags"] = tags
        captured["importance"] = importance
        captured["source"] = source
        return True

    with mock.patch.object(core, "memory_store_call", side_effect=_capture):
        core.store_extractions(
            [{"fact": fact_text, "tags": ["shell", "powershell"], "confidence": "high"}],
            "conv12345abcdef",
            cfg,
        )

    assert captured["content"] == fact_text, "fact must be stored verbatim"
    assert "[BEHAVIORAL]" not in captured["content"]
    assert "Key:" not in captured["content"]
    assert "Value:" not in captured["content"]
    assert "Confidence:" not in captured["content"]
    assert captured["tags"] == ["shell", "powershell"]
    assert captured["importance"] == "high"
    print("[OK] store_extractions writes fact verbatim with no template wrapping")


def test_store_extractions_marks_source_as_model_inferred():
    """Extracted facts must always carry source='model-inferred' for attribution.

    This is what lets curation downstream distinguish LLM-extracted memories
    from user-stated or tool-result ones, even though we are not currently
    using source for retrieval bias.
    """
    cfg = _make_config("openai_compat")
    captured = []

    def _capture(content, tags, importance, source, config):
        captured.append(source)
        return True

    with mock.patch.object(core, "memory_store_call", side_effect=_capture):
        core.store_extractions(
            [
                {"fact": "First extracted rule.", "confidence": "high"},
                {"fact": "Second extracted rule.", "confidence": "low"},
            ],
            "conv12345",
            cfg,
        )

    assert captured == ["model-inferred", "model-inferred"]
    print("[OK] store_extractions marks every extracted fact source='model-inferred'")


def test_store_extractions_handles_missing_or_garbage_tags():
    cfg = _make_config("openai_compat")
    items = [
        {"fact": "Fact with no tags field.", "confidence": "high"},
        {"fact": "Fact with non-list tags.", "tags": "shell", "confidence": "high"},
        {"fact": "Fact with mixed tag types.", "tags": ["shell", 42, None, "  "], "confidence": "high"},
    ]
    captured = []

    def _capture(content, tags, importance, source, config):
        captured.append(tags)
        return True

    with mock.patch.object(core, "memory_store_call", side_effect=_capture):
        stored = core.store_extractions(items, "conv12345", cfg)

    assert stored == 3
    assert captured[0] == []                          # missing -> empty
    assert captured[1] == []                          # non-list -> empty
    assert captured[2] == ["shell", "42"]             # garbage stripped, ints stringified
    print("[OK] store_extractions tolerates missing/malformed tags")


def test_memory_store_call_posts_correct_jsonrpc_shape():
    """Regression guard for the MCP tool name and argument shape.

    Renaming or restructuring the memory_store MCP arguments without
    updating this helper would silently start dropping every extraction.
    """
    cfg = _make_config("openai_compat", model="x")
    captured = {}

    def _capture(req, *_, **__):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        cm = mock.MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps(
            {"result": {"id": "abc", "stored": True}}
        ).encode("utf-8")
        return cm

    with mock.patch.object(core.urllib.request, "urlopen", side_effect=_capture):
        ok = core.memory_store_call(
            "Some standalone fact.",
            ["a", "b"],
            "high",
            "model-inferred",
            cfg,
        )

    assert ok is True
    body = captured["body"]
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "memory_store"
    args = body["params"]["arguments"]
    assert args["content"] == "Some standalone fact."
    assert args["tags"] == ["a", "b"]
    assert args["importance"] == "high"
    assert args["source"] == "model-inferred"
    print("[OK] memory_store_call posts the expected JSON-RPC payload to the MCP")


def test_memory_store_call_returns_false_on_network_error():
    cfg = _make_config("openai_compat")
    import urllib.error
    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock:
        urlopen_mock.side_effect = urllib.error.URLError("down")
        ok = core.memory_store_call("fact", [], "normal", "model-inferred", cfg)
    assert ok is False
    print("[OK] memory_store_call returns False when MCP is unreachable")


def test_min_conversation_chars_default_is_low():
    """Short corrections (e.g. 'stop using em-dashes') must reach the LLM.

    Anything around 40 chars or below is the threshold the design accepts;
    above that and useful one-line teachings get dropped.
    """
    assert core.DEFAULT_MIN_CONVERSATION_CHARS <= 50, (
        "Min conversation length must stay low; short user corrections are "
        "some of the highest-value extractions."
    )
    print("[OK] DEFAULT_MIN_CONVERSATION_CHARS is low enough to capture short corrections")


def test_max_extractions_default_is_volume_friendly():
    """Default cap must not silently truncate legitimate rich turns."""
    assert core.DEFAULT_MAX_EXTRACTIONS >= 30, (
        "Default max_extractions must accommodate rich turns. Cap is a "
        "runaway hedge, not a quota."
    )
    print("[OK] DEFAULT_MAX_EXTRACTIONS is high enough to be a runaway hedge, not a quota")


# -- analyze_turn end-to-end ---------------------------------------------

def test_analyze_turn_short_circuit_below_min_length():
    cfg = _make_config("openai_compat", model="x")
    with mock.patch.object(core, "rag_ingest") as rag_mock, \
            mock.patch.object(core, "memory_store_call") as mem_mock:
        out = core.analyze_turn("u", "m", "/some/cwd", cfg)
    assert out["stored"] == 0
    assert out["nudge_stored"] is False
    assert out["summary_stored"] is False
    rag_mock.assert_not_called()
    mem_mock.assert_not_called()
    print("[OK] analyze_turn short-circuits below min length")


def test_analyze_turn_persists_extractions_summary_and_nudge():
    """End-to-end: extractions -> memories; summary -> RAG; nudge -> RAG.

    The three-way split is intentional and load-bearing:
      - Memories carry thermal/dedup/conflict semantics for discrete facts.
      - Summaries land in RAG as narrative artifacts (longer than a fact,
        searchable but not consolidated).
      - Nudges live on RAG as session-bridging meta-state until the
        promise-detection migration to NNA happens.
    """
    cfg = _make_config("openai_compat", model="x")
    payload = {
        "results": [
            {
                "fact": "Bash escape rendering corrupts double-quoted PowerShell strings; use single quotes when shelling out.",
                "tags": ["shell", "powershell", "correction"],
                "confidence": "high",
            }
        ],
        "unfulfilledPromises": [{"promise": "p", "reason": "r"}],
        "shouldNudge": True,
        "nudgeText": "follow up",
        "summary": "PowerShell quoting was clarified; single quotes required from bash.",
    }
    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock, \
            mock.patch.object(core, "memory_store_call", return_value=True) as mem_mock, \
            mock.patch.object(core, "rag_ingest", return_value=True) as rag_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = _mock_openai_response(payload)
        out = core.analyze_turn("u" * 200, "m" * 200, "/some/cwd", cfg)
    assert out["stored"] == 1
    assert out["nudge_stored"] is True
    assert out["summary_stored"] is True
    # Extraction goes to memories; summary + nudge both go to RAG (2 RAG calls).
    assert mem_mock.call_count == 1
    assert rag_mock.call_count == 2
    print("[OK] analyze_turn routes extractions to memories; summary and nudge to RAG")


def test_attach_mission_tags_appends_when_mission_id_set():
    items = [
        {"fact": "F1", "tags": ["shell"]},
        {"fact": "F2", "tags": []},
        {"fact": "F3"},  # no tags key at all
    ]
    out = core._attach_mission_tags(items, "abc-123", None)
    assert out[0]["tags"] == ["shell", "mission:abc-123"]
    assert out[1]["tags"] == ["mission:abc-123"]
    assert out[2]["tags"] == ["mission:abc-123"]
    # Original input must not be mutated; the function returns a new list.
    assert items[0]["tags"] == ["shell"]
    print("[OK] _attach_mission_tags appends mission:<id> without mutating input")


def test_attach_mission_tags_includes_assignment_when_set():
    items = [{"fact": "F", "tags": ["t1"]}]
    out = core._attach_mission_tags(items, "m1", "a1")
    assert "mission:m1" in out[0]["tags"]
    assert "assignment:a1" in out[0]["tags"]
    print("[OK] _attach_mission_tags appends assignment:<id> alongside mission:<id>")


def test_attach_mission_tags_noop_when_neither_id_set():
    items = [{"fact": "F", "tags": ["t1"]}]
    out = core._attach_mission_tags(items, None, None)
    # Same list returned verbatim.
    assert out is items
    print("[OK] _attach_mission_tags is a no-op when no ids are supplied")


def test_attach_mission_tags_does_not_duplicate():
    items = [{"fact": "F", "tags": ["mission:m1", "other"]}]
    out = core._attach_mission_tags(items, "m1", None)
    # Must not introduce a second mission:m1 entry.
    assert out[0]["tags"].count("mission:m1") == 1
    print("[OK] _attach_mission_tags does not duplicate an already-present tag")


def test_attach_mission_tags_handles_non_dict_items():
    """Non-dict items pass through unchanged (defense; should never happen)."""
    items = [{"fact": "F", "tags": []}, "not a dict", 42]
    out = core._attach_mission_tags(items, "m1", None)
    assert out[0]["tags"] == ["mission:m1"]
    assert out[1] == "not a dict"
    assert out[2] == 42
    print("[OK] _attach_mission_tags passes non-dict items through unchanged")


def test_call_worker_analysis_llm_skips_below_min_length():
    cfg = _make_config("openai_compat", model="x")
    out = core.call_worker_analysis_llm("env", "out", cfg)
    assert out == core.empty_analysis()
    print("[OK] call_worker_analysis_llm short-circuits below min length")


def test_call_worker_analysis_llm_uses_worker_prompt():
    """Regression guard: worker LLM call must build with the worker prompt,
    not the session prompt. Mocking the wire-level urlopen and inspecting
    the body is the cleanest way to verify the right prompt went out.
    """
    cfg = _make_config("openai_compat", model="x")
    captured = {}

    def _capture(req, *_, **__):
        body = json.loads(req.data.decode("utf-8"))
        captured["user_msg"] = body["messages"][-1]["content"]
        cm = mock.MagicMock()
        cm.__enter__.return_value.read.return_value = _mock_openai_response(
            core.empty_analysis()
        )
        return cm

    with mock.patch.object(core.urllib.request, "urlopen", side_effect=_capture):
        core.call_worker_analysis_llm("E" * 200, "O" * 200, cfg)

    # Worker prompt's vendor/operational steering must be present; session
    # prompt's user-preference framing must not.
    assert "vendor" in captured["user_msg"].lower()
    assert "TASK ENVELOPE" in captured["user_msg"]
    assert "WORKER OUTPUT" in captured["user_msg"]
    print("[OK] call_worker_analysis_llm sends the worker prompt (not the session prompt)")


def test_analyze_worker_run_persists_with_mission_tags():
    """End-to-end worker analysis: extractions land as memories with mission tags;
    summary lands in RAG; nudge lands in RAG.
    """
    cfg = _make_config("openai_compat", model="x")
    payload = {
        "results": [
            {
                "fact": "Acme renders price in data-price attribute, not visible text.",
                "tags": ["scrape", "vendor:acme"],
                "confidence": "high",
            }
        ],
        "unfulfilledPromises": [],
        "shouldNudge": True,
        "nudgeText": "follow up on B-tier endpoint",
        "summary": "A scrape against Acme succeeded after switching to data-price attribute.",
    }
    captured_tags = []

    def _mem_capture(content, tags, importance, source, config):
        captured_tags.append(tags)
        return True

    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock, \
            mock.patch.object(core, "memory_store_call", side_effect=_mem_capture), \
            mock.patch.object(core, "rag_ingest", return_value=True) as rag_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = _mock_openai_response(payload)
        out = core.analyze_worker_run(
            "E" * 200, "O" * 200, "/some/cwd", cfg,
            mission_id="mission-abc",
            assignment_id="asg-1",
        )

    assert out["stored"] == 1
    assert out["summary_stored"] is True
    assert out["nudge_stored"] is True
    # Memory call received mission/assignment tags merged with the LLM tags.
    assert captured_tags == [["scrape", "vendor:acme", "mission:mission-abc", "assignment:asg-1"]]
    # Two RAG calls: summary + nudge. Memory call: 1 extraction.
    assert rag_mock.call_count == 2
    print("[OK] analyze_worker_run tags extractions with mission/assignment and routes to memory + RAG")


def test_analyze_worker_run_skips_writes_when_below_min_length():
    cfg = _make_config("openai_compat", model="x")
    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock, \
            mock.patch.object(core, "memory_store_call") as mem_mock, \
            mock.patch.object(core, "rag_ingest") as rag_mock:
        out = core.analyze_worker_run("e", "o", "/cwd", cfg, mission_id="m1")
    assert out["stored"] == 0
    assert out["summary_stored"] is False
    assert out["nudge_stored"] is False
    urlopen_mock.assert_not_called()
    mem_mock.assert_not_called()
    rag_mock.assert_not_called()
    print("[OK] analyze_worker_run short-circuits cleanly below min length")


def test_analyze_turn_skips_summary_when_empty():
    """Empty summary string must not produce a RAG write."""
    cfg = _make_config("openai_compat", model="x")
    payload = {
        "results": [],
        "unfulfilledPromises": [],
        "shouldNudge": False,
        "nudgeText": "",
        "summary": "",
    }
    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock, \
            mock.patch.object(core, "memory_store_call", return_value=True) as mem_mock, \
            mock.patch.object(core, "rag_ingest", return_value=True) as rag_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = _mock_openai_response(payload)
        out = core.analyze_turn("u" * 200, "m" * 200, "/some/cwd", cfg)
    assert out["summary_stored"] is False
    assert out["nudge_stored"] is False
    assert out["stored"] == 0
    mem_mock.assert_not_called()
    rag_mock.assert_not_called()
    print("[OK] analyze_turn does not write a summary when the LLM returned empty")


# -- Claude adapter: transcript parsing ----------------------------------

# The Claude hook lives at <repo>/hook_bundles/claude/notnative-memory/turn_analysis.py.
# Add it to sys.path so we can import it directly.
sys.path.insert(0, str(_REPO_ROOT / "hook_bundles" / "claude" / "notnative-memory"))
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
        test_build_analysis_prompt_specifies_new_schema_and_quality_bar,
        test_build_analysis_prompt_includes_summary_section,
        test_build_worker_analysis_prompt_steers_toward_vendor_quirks,
        test_build_worker_analysis_prompt_caps_lengths,
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
        test_resolve_config_default_max_tokens_is_generous,
        test_resolve_config_max_tokens_env_override,
        test_call_analysis_llm_sends_configured_max_tokens,
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
        test_store_conversation_summary_skips_empty_text,
        test_store_conversation_summary_writes_to_rag_with_session_summary_tag,
        test_store_conversation_summary_strips_surrounding_whitespace,
        test_store_extractions_skips_malformed_items,
        test_store_extractions_caps_at_max,
        test_store_extractions_default_max_is_high_enough_for_volume,
        test_store_extractions_confidence_maps_to_importance,
        test_store_extractions_writes_fact_verbatim_no_template,
        test_store_extractions_marks_source_as_model_inferred,
        test_store_extractions_handles_missing_or_garbage_tags,
        test_memory_store_call_posts_correct_jsonrpc_shape,
        test_memory_store_call_returns_false_on_network_error,
        test_min_conversation_chars_default_is_low,
        test_max_extractions_default_is_volume_friendly,
        # core: analyze_turn
        test_analyze_turn_short_circuit_below_min_length,
        test_analyze_turn_persists_extractions_summary_and_nudge,
        test_analyze_turn_skips_summary_when_empty,
        # core: worker-mode (skeleton; no NNA caller yet)
        test_attach_mission_tags_appends_when_mission_id_set,
        test_attach_mission_tags_includes_assignment_when_set,
        test_attach_mission_tags_noop_when_neither_id_set,
        test_attach_mission_tags_does_not_duplicate,
        test_attach_mission_tags_handles_non_dict_items,
        test_call_worker_analysis_llm_skips_below_min_length,
        test_call_worker_analysis_llm_uses_worker_prompt,
        test_analyze_worker_run_persists_with_mission_tags,
        test_analyze_worker_run_skips_writes_when_below_min_length,
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
