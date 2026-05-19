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
    assert "Learnable observations" in prompt
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


# -- Brevity contract on extracted facts ----------------------------------
# Memories should be short, easily-consumable, single-thought entries. The
# earlier prompt told the model to "Include both the WHY and the WHAT-TO-DO"
# and used a ~400-character soft target, which produced run-on memories with
# two glued-together clauses. These tests pin the tighter contract so future
# prompt tweaks can't silently regress.

def test_session_prompt_does_not_mandate_combined_why_and_what():
    """The model must not be told to ALWAYS include WHY+WHAT-TO-DO. That
    mandate is what produced the run-on, semicolon-joined memories."""
    prompt = core.build_analysis_prompt("u", "m")
    bad_phrases = [
        "Include both the WHY and the WHAT-TO-DO",
        "cause and remedy together",
    ]
    for bad in bad_phrases:
        assert bad not in prompt, (
            f"Session prompt still mandates {bad!r}; this pushes the model "
            f"toward compound run-on memories. Soften to opt-in."
        )
    print("[OK] session prompt does not mandate combined WHY+WHAT-TO-DO")


def test_session_prompt_targets_short_facts():
    """The length target must be in words (~25), not characters (~400).

    ~400 chars is roughly 60-80 words: too long, encourages run-ons. ~25
    words aligns with the memory_store style guide."""
    prompt = core.build_analysis_prompt("u", "m")
    assert "400 character" not in prompt and "400-character" not in prompt, (
        "Session prompt still uses the 400-character target. Switch to "
        "~25 words to encourage single-thought entries."
    )
    assert "25 words" in prompt, (
        "Session prompt should give a ~25-word soft length target."
    )
    print("[OK] session prompt targets ~25-word facts, not 400 chars")


def test_session_prompt_encourages_brevity_and_opt_in_why():
    """The new contract: terse by default; reason only when needed."""
    prompt = core.build_analysis_prompt("u", "m")
    lower = prompt.lower()
    assert "be terse" in lower, (
        "Session prompt should explicitly tell the model to be terse."
    )
    # Either of these phrasings makes the why-clause opt-in instead of mandatory.
    assert (
        "only when the rule wouldn't stand alone" in prompt
        or "only when" in lower and "self-evident" in lower
    ), "Session prompt should make the why-clause explicitly optional."
    print("[OK] session prompt encourages brevity + opt-in why")


def test_worker_prompt_targets_short_facts():
    """Worker prompt mirrors the session contract: ~25-word target, no
    400-character ceiling, no WHY+WHAT-TO-DO mandate."""
    prompt = core.build_worker_analysis_prompt("envelope", "output")
    assert "400 character" not in prompt and "400-character" not in prompt
    assert "25 words" in prompt
    assert "be terse" in prompt.lower(), (
        "Worker prompt should explicitly tell the model to be terse."
    )
    assert "Include WHY and WHAT-TO-DO when applicable" not in prompt, (
        "Worker prompt still mandates WHY+WHAT-TO-DO together. Soften."
    )
    print("[OK] worker prompt targets ~25-word facts and opt-in why")


def test_session_prompt_bad_example_includes_run_on_shape():
    """The bad-example set must call out the run-on shape explicitly,
    because that's the most common failure mode under reasoning models.
    A model that has never seen 'this is wrong' for a run-on will produce
    run-ons."""
    prompt = core.build_analysis_prompt("u", "m")
    assert "run-on" in prompt.lower(), (
        "Session prompt's bad-example set should label the run-on shape "
        "as wrong; otherwise the model has no negative exemplar for it."
    )
    print("[OK] session prompt's bad-example set covers the run-on shape")


# -- Shape coercion -------------------------------------------------------

def test_coerce_analysis_full_shape():
    parsed = {
        "state_assertions": [{"subject": "s", "predicate": "p", "object": "o", "confidence": 0.9}],
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
        "state_assertions": [],
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


# -- No input-size caps (local-first analyzer) ----------------------------
# Regression: build_analysis_prompt / build_worker_analysis_prompt used to
# truncate user_prompt at 2000 chars and model_response at 4000 chars.
# That throws away signal in turns with long tool results or detailed
# discussion. Token cost is not a concern for a local-only analyzer;
# runaway protection lives at the urllib timeout and the hook-process
# timeout, not in input slicing.

def test_build_analysis_prompt_does_not_truncate_long_inputs():
    user = "U" * 20000
    model = "M" * 50000
    prompt = core.build_analysis_prompt(user, model)
    assert user in prompt, "long user prompt must pass through verbatim"
    assert model in prompt, "long model response must pass through verbatim"
    print("[OK] build_analysis_prompt does not truncate long inputs")


def test_build_worker_analysis_prompt_does_not_truncate_long_inputs():
    envelope = "E" * 20000
    output = "O" * 50000
    prompt = core.build_worker_analysis_prompt(envelope, output)
    assert envelope in prompt
    assert output in prompt
    print("[OK] build_worker_analysis_prompt does not truncate long inputs")


# -- Reasoning suppression (MEMORY_EXTRACT_DISABLE_REASONING) -------------
# Regression: the hooks.env contract documents
# MEMORY_EXTRACT_DISABLE_REASONING=1 → core must send
# chat_template_kwargs={"enable_thinking": false}. Before the fix the
# flag was a no-op: reasoning models burned the subprocess timeout on
# hidden <think> and the JSON content arrived empty, so extractions
# silently stored zero memories.

def test_resolve_config_disable_reasoning_default_false():
    cfg = core.resolve_config_from_env({})
    assert cfg.disable_reasoning is False
    print("[OK] resolve_config: disable_reasoning defaults to False")


def test_resolve_config_disable_reasoning_honors_truthy_values():
    for truthy in ("1", "true", "TRUE", "yes"):
        cfg = core.resolve_config_from_env({"MEMORY_EXTRACT_DISABLE_REASONING": truthy})
        assert cfg.disable_reasoning is True, f"{truthy!r} should enable the flag"
    for falsy in ("0", "false", "no", ""):
        cfg = core.resolve_config_from_env({"MEMORY_EXTRACT_DISABLE_REASONING": falsy})
        assert cfg.disable_reasoning is False, f"{falsy!r} should leave the flag off"
    print("[OK] resolve_config: disable_reasoning parses truthy/falsy env values")


def test_openai_body_includes_chat_template_kwargs_when_disable_reasoning():
    cfg = _make_config("openai_compat", model="x", disable_reasoning=True)
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

    assert captured["body"].get("chat_template_kwargs") == {"enable_thinking": False}, (
        "MEMORY_EXTRACT_DISABLE_REASONING=1 must produce "
        "chat_template_kwargs={'enable_thinking': false} so vLLM/Qwen3 "
        "templates skip the hidden <think> phase."
    )
    assert captured["body"].get("reasoning_effort") == "off", (
        "MEMORY_EXTRACT_DISABLE_REASONING=1 must also produce "
        "reasoning_effort='off' so LM Studio reasoning-model adapters "
        "(Nemotron, gpt-oss, etc.) skip reasoning. Backends ignore unknown "
        "fields, so sending both covers the two ecosystems with one flag."
    )
    print("[OK] openai_compat body sends both chat_template_kwargs and reasoning_effort when disable_reasoning is True")


def test_openai_body_omits_chat_template_kwargs_by_default():
    cfg = _make_config("openai_compat", model="x")
    assert cfg.disable_reasoning is False
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

    assert "chat_template_kwargs" not in captured["body"], (
        "By default the body must not carry chat_template_kwargs — adding it "
        "unconditionally would break backends that reject unknown kwargs."
    )
    assert "reasoning_effort" not in captured["body"], (
        "By default the body must not carry reasoning_effort — same reason. "
        "Both reasoning toggles are gated behind disable_reasoning."
    )
    print("[OK] openai_compat body omits chat_template_kwargs and reasoning_effort by default")


def test_anthropic_body_ignores_disable_reasoning():
    cfg = _make_config("anthropic_messages", model="claude-fixture", disable_reasoning=True)
    captured = {}

    def _capture(req, *_, **__):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        cm = mock.MagicMock()
        cm.__enter__.return_value.read.return_value = _mock_anthropic_response(
            core.empty_analysis()
        )
        return cm

    with mock.patch.object(core.urllib.request, "urlopen", side_effect=_capture):
        core.call_analysis_llm("u" * 200, "m" * 200, cfg)

    assert "chat_template_kwargs" not in captured["body"], (
        "chat_template_kwargs is an OpenAI-compat extension; Anthropic Messages "
        "rejects unknown top-level fields."
    )
    print("[OK] anthropic body never carries chat_template_kwargs")


# -- Project scope + wrapped MCP response parsing -------------------------
# Regression: the analyzer historically passed no `project` to memory_store,
# letting the server fall back to the default "general" — which the server
# rejects as a write target. Worse, the storage helpers only checked the
# JSON-RPC envelope's `result` field for truthiness, missing the inner
# `{"stored": false, "error": "..."}` payload. Result: the log reported
# extracted=N but nothing actually landed in NNM. These tests pin the
# project argument, the wrapped-response parser, and the failure-logging
# behavior so that "stored=N" is now a real promise, not a lie.

def test_resolve_config_default_project_is_global():
    cfg = core.resolve_config_from_env({})
    assert cfg.project == "_global", (
        "Default must be a valid write target ('_global'). Server-side "
        "default 'general' is rejected; an unset project would silently "
        "fail every store."
    )
    print("[OK] resolve_config: project defaults to '_global'")


def test_resolve_config_project_env_override():
    cfg = core.resolve_config_from_env({"MEMORY_EXTRACT_PROJECT": "_domain_nna"})
    assert cfg.project == "_domain_nna"
    print("[OK] resolve_config: MEMORY_EXTRACT_PROJECT overrides default")


def test_resolve_config_empty_project_falls_back_to_global():
    cfg = core.resolve_config_from_env({"MEMORY_EXTRACT_PROJECT": "   "})
    assert cfg.project == "_global", (
        "Whitespace/empty MEMORY_EXTRACT_PROJECT must not produce '' — that "
        "would re-introduce the server-side default-to-'general' failure."
    )
    print("[OK] resolve_config: empty/whitespace project falls back to '_global'")


def test_memory_store_call_passes_project_argument():
    cfg = _make_config("openai_compat", model="x", project="_domain_test")
    captured = {}

    def _capture(req, *_, **__):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        cm = mock.MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({
            "result": {"content": [{"type": "text", "text": '{"stored": true, "id": "test-id"}'}], "isError": False}
        }).encode("utf-8")
        return cm

    with mock.patch.object(core.urllib.request, "urlopen", side_effect=_capture):
        ok = core.memory_store_call("a fact", ["t"], "normal", "model-inferred", cfg)

    assert ok is True
    args = captured["body"]["params"]["arguments"]
    assert args["project"] == "_domain_test", (
        "memory_store_call must pass `project` in the MCP arguments, or the "
        "server falls back to 'general' and rejects the write."
    )
    print("[OK] memory_store_call forwards project argument")


def test_rag_ingest_passes_project_argument():
    cfg = _make_config("openai_compat", model="x", project="_global")
    captured = {}

    def _capture(req, *_, **__):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        cm = mock.MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({
            "result": {"content": [{"type": "text", "text": '{"stored": true, "document_id": "doc-1"}'}], "isError": False}
        }).encode("utf-8")
        return cm

    with mock.patch.object(core.urllib.request, "urlopen", side_effect=_capture):
        ok = core.rag_ingest("title", "body", ["t"], "normal", cfg)

    assert ok is True
    args = captured["body"]["params"]["arguments"]
    assert args["project"] == "_global"
    print("[OK] rag_ingest forwards project argument")


def test_mcp_call_stored_success_envelope():
    envelope = {
        "result": {
            "content": [{"type": "text", "text": '{"stored": true, "id": "abc"}'}],
            "isError": False,
        }
    }
    assert core._mcp_call_stored(envelope) is True
    print("[OK] _mcp_call_stored returns True on inner stored=true")


def test_mcp_call_stored_inner_failure():
    """The exact failure shape that hid the bug for so long.

    Pre-fix the analyzer counted this as success because the JSON-RPC
    envelope has a truthy ``result`` field — even though the inner payload
    clearly says the write did not happen.
    """
    envelope = {
        "result": {
            "content": [{"type": "text", "text": '{"stored": false, "error": "project \'general\' is not a valid write target. Use \'_global\', _domain_<name>, or an absolute path.", "project": "general"}'}],
            "isError": False,
        }
    }
    assert core._mcp_call_stored(envelope) is False
    print("[OK] _mcp_call_stored returns False on inner stored=false (the bug shape)")


def test_mcp_call_stored_is_error_flag():
    envelope = {
        "result": {
            "content": [{"type": "text", "text": '{"stored": true}'}],
            "isError": True,
        }
    }
    assert core._mcp_call_stored(envelope) is False, (
        "isError=true at the envelope level must override inner content."
    )
    print("[OK] _mcp_call_stored returns False when isError=true")


def test_mcp_call_stored_malformed_envelopes():
    # Defensive: every malformed shape should be treated as failure, not crash.
    for envelope in [
        None,
        {},
        {"result": None},
        {"result": "string-not-dict"},
        {"result": {"content": []}},
        {"result": {"content": [{}]}},
        {"result": {"content": [{"text": "not-json"}]}},
        {"result": {"content": [{"text": '{"different_key": true}'}]}},
    ]:
        assert core._mcp_call_stored(envelope) is False, (
            f"malformed envelope must not return True: {envelope!r}"
        )
    print("[OK] _mcp_call_stored treats malformed envelopes as failure")


def test_memory_store_call_rejected_inner_returns_false(tmp_path):
    """End-to-end: when the server returns the 'project general not valid'
    rejection, the call must return False AND log the rejection reason."""
    log_file = tmp_path / "turn_analysis.log"
    cfg = _make_config("openai_compat", model="x")
    rejection = json.dumps({
        "result": {
            "content": [{"type": "text", "text": '{"stored": false, "error": "project \'general\' is not a valid write target."}'}],
            "isError": False,
        }
    }).encode("utf-8")

    with mock.patch.dict(os.environ, {"MEMORY_EXTRACT_LOG": str(log_file)}, clear=False):
        with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock:
            urlopen_mock.return_value.__enter__.return_value.read.return_value = rejection
            ok = core.memory_store_call("a fact", [], "normal", "model-inferred", cfg)

    assert ok is False
    line = log_file.read_text(encoding="utf-8")
    assert "memory_store_rejected" in line
    assert "not a valid write target" in line
    print("[OK] memory_store_call: inner rejection returns False and logs the reason")


# -- Failure observability (silent → logged) ------------------------------

def test_llm_call_failure_writes_log_line(tmp_path):
    """When the LLM call raises, the core must append a failure line to the
    analyzer log instead of silently swallowing it. Pre-fix the only signal
    was a stderr print that the subprocess-launching harness discarded, so
    "extracted=0" entries had no explanation in the log file.
    """
    import urllib.error
    log_file = tmp_path / "turn_analysis.log"
    cfg = _make_config("openai_compat", model="x")

    with mock.patch.dict(os.environ, {"MEMORY_EXTRACT_LOG": str(log_file)}, clear=False):
        with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock:
            urlopen_mock.side_effect = urllib.error.URLError("connection refused")
            result = core.call_analysis_llm("u" * 200, "m" * 200, cfg)

    assert result == core.empty_analysis()
    assert log_file.exists(), "failure must produce a log entry"
    line = log_file.read_text(encoding="utf-8")
    assert "failure" in line
    assert "llm_call_failed" in line
    assert "URLError" in line
    print("[OK] LLM call failure writes a diagnosable line to MEMORY_EXTRACT_LOG")


def test_unparseable_response_writes_log_line(tmp_path):
    """When the LLM returns non-JSON content, the core must log a preview
    of what came back so the user can tell whether the model emitted
    reasoning-only output, truncated output, or pure garbage.
    """
    log_file = tmp_path / "turn_analysis.log"
    cfg = _make_config("openai_compat", model="x")
    # Return a non-JSON response body so json.loads() fails downstream.
    bad_body = json.dumps({
        "choices": [{"message": {"content": "I was still thinking when…"}}]
    }).encode("utf-8")

    with mock.patch.dict(os.environ, {"MEMORY_EXTRACT_LOG": str(log_file)}, clear=False):
        with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock:
            urlopen_mock.return_value.__enter__.return_value.read.return_value = bad_body
            result = core.call_analysis_llm("u" * 200, "m" * 200, cfg)

    assert result == core.empty_analysis()
    assert log_file.exists()
    line = log_file.read_text(encoding="utf-8")
    assert "unparseable_json" in line
    assert "content_preview" in line
    print("[OK] unparseable response writes a content preview to the log")


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
        # Realistic MCP envelope: tool result wrapped in content[].text as a
        # JSON string. The pre-fix flat shape ({"result": {"stored": true}})
        # never matched what the server actually returns.
        cm.__enter__.return_value.read.return_value = json.dumps({
            "result": {
                "content": [{"type": "text", "text": '{"stored": true, "id": "abc"}'}],
                "isError": False,
            }
        }).encode("utf-8")
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
    assert args["project"] == "_global", "project must be passed; server rejects default 'general'"
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


# -- Fact-vs-memory extraction split (Fix #1) -----------------------------
# Extraction must emit two channels:
#   state_assertions -> memory_fact_add  (mutable subject/predicate/object)
#   results          -> memory_store     (durable observations/rules)
# Pre-fix, every extraction was routed to memory_store, so NNM's fact graph
# stayed nearly empty even though many turns made plain state assertions.


def test_build_analysis_prompt_includes_state_assertions_section():
    """Tier 2: snapshot the load-bearing fields of the new section.

    Drift here (renaming subject/predicate/object, or losing the
    fact-vs-observation distinction) would silently break the routing
    contract that downstream code depends on.
    """
    prompt = core.build_analysis_prompt("u", "m")
    assert '"state_assertions"' in prompt
    assert '"subject"' in prompt
    assert '"predicate"' in prompt
    assert '"object"' in prompt
    # The framing must distinguish mutable-now from durable-observation,
    # otherwise the LLM has no signal for which channel to use.
    lower = prompt.lower()
    assert "right now" in lower
    assert "could change later" in lower or "may change later" in lower
    # Negative steering: rules/preferences must NOT land in state_assertions.
    assert "preference" in lower
    print("[OK] build_analysis_prompt includes the state_assertions section with subject/predicate/object")


def test_empty_analysis_includes_state_assertions():
    out = core.empty_analysis()
    assert out["state_assertions"] == []
    print("[OK] empty_analysis includes state_assertions field")


def test_coerce_analysis_state_assertions_wrong_type_defaults_safe():
    out = core.coerce_analysis({"state_assertions": "not a list"})
    assert out["state_assertions"] == []
    print("[OK] coerce_analysis defends against non-list state_assertions")


def test_memory_fact_add_call_posts_correct_jsonrpc_shape():
    """Regression guard for the memory_fact_add MCP wire shape.

    The triple subject/predicate/object plus project must reach the server
    exactly as named; any drift causes silent fact drops.
    """
    cfg = _make_config("openai_compat", model="x", project="_domain_test")
    captured = {}

    def _capture(req, *_, **__):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        cm = mock.MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({
            "result": {
                "content": [{"type": "text", "text": '{"stored": true, "fact_id": "f-1"}'}],
                "isError": False,
            }
        }).encode("utf-8")
        return cm

    with mock.patch.object(core.urllib.request, "urlopen", side_effect=_capture):
        ok = core.memory_fact_add_call(
            "inference-host", "model", "qwen3-30b-a3b", 0.95, cfg,
        )

    assert ok is True
    body = captured["body"]
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "memory_fact_add"
    args = body["params"]["arguments"]
    assert args["subject"] == "inference-host"
    assert args["predicate"] == "model"
    assert args["object"] == "qwen3-30b-a3b"
    assert args["confidence"] == 0.95
    assert args["project"] == "_domain_test"
    print("[OK] memory_fact_add_call posts the expected JSON-RPC payload")


def test_memory_fact_add_call_returns_false_on_inner_rejection():
    """Inner-stored=false must surface as False with a log line."""
    with tempfile.TemporaryDirectory() as tmp:
        log_file = os.path.join(tmp, "turn_analysis.log")
        cfg = _make_config("openai_compat", model="x")
        rejection = json.dumps({
            "result": {
                "content": [{"type": "text", "text": '{"stored": false, "error": "Subject cannot be empty"}'}],
                "isError": False,
            }
        }).encode("utf-8")

        with mock.patch.dict(os.environ, {"MEMORY_EXTRACT_LOG": log_file}, clear=False):
            with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock:
                urlopen_mock.return_value.__enter__.return_value.read.return_value = rejection
                ok = core.memory_fact_add_call("s", "p", "o", 1.0, cfg)

        assert ok is False
        with open(log_file, encoding="utf-8") as fh:
            line = fh.read()
    assert "memory_fact_add_rejected" in line
    print("[OK] memory_fact_add_call: inner rejection returns False and logs")


def test_memory_fact_add_call_returns_false_on_network_error():
    cfg = _make_config("openai_compat")
    import urllib.error
    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock:
        urlopen_mock.side_effect = urllib.error.URLError("down")
        ok = core.memory_fact_add_call("s", "p", "o", 1.0, cfg)
    assert ok is False
    print("[OK] memory_fact_add_call returns False when MCP is unreachable")


def test_store_fact_assertions_skips_malformed_items():
    cfg = _make_config("openai_compat")
    items = [
        {"predicate": "p", "object": "o"},                     # missing subject
        {"subject": "s", "object": "o"},                       # missing predicate
        {"subject": "s", "predicate": "p"},                    # missing object
        {"subject": "  ", "predicate": "p", "object": "o"},    # whitespace subject
        {"subject": 1, "predicate": "p", "object": "o"},       # non-string
        "not a dict",                                          # wrong type
    ]
    with mock.patch.object(core, "memory_fact_add_call", return_value=True) as fact_mock:
        stored = core.store_fact_assertions(items, cfg)
    assert stored == 0
    fact_mock.assert_not_called()
    print("[OK] store_fact_assertions skips malformed items")


def test_store_fact_assertions_calls_fact_add_per_item():
    cfg = _make_config("openai_compat")
    items = [
        {"subject": "host", "predicate": "port", "object": "9500", "confidence": 1.0},
        {"subject": "host", "predicate": "model", "object": "qwen3", "confidence": 0.9},
    ]
    captured = []

    def _capture(subject, predicate, obj, confidence, config):
        captured.append((subject, predicate, obj, confidence))
        return True

    with mock.patch.object(core, "memory_fact_add_call", side_effect=_capture):
        stored = core.store_fact_assertions(items, cfg)

    assert stored == 2
    assert captured[0] == ("host", "port", "9500", 1.0)
    assert captured[1] == ("host", "model", "qwen3", 0.9)
    print("[OK] store_fact_assertions calls memory_fact_add_call per valid item")


def test_store_fact_assertions_coerces_confidence_strings():
    """Reasoning models tend to emit string confidences even when the
    schema asks for a float. The coercer normalizes high/medium/low and
    numeric strings; everything else falls back to 0.8."""
    cfg = _make_config("openai_compat")
    items = [
        {"subject": "a", "predicate": "p", "object": "o", "confidence": "high"},
        {"subject": "b", "predicate": "p", "object": "o", "confidence": "medium"},
        {"subject": "c", "predicate": "p", "object": "o", "confidence": "low"},
        {"subject": "d", "predicate": "p", "object": "o", "confidence": "0.42"},
        {"subject": "e", "predicate": "p", "object": "o", "confidence": "garbage"},
        {"subject": "f", "predicate": "p", "object": "o"},  # missing -> default 0.8
        {"subject": "g", "predicate": "p", "object": "o", "confidence": 2.5},  # clamped
    ]
    captured = []

    def _capture(subject, predicate, obj, confidence, config):
        captured.append(confidence)
        return True

    with mock.patch.object(core, "memory_fact_add_call", side_effect=_capture):
        core.store_fact_assertions(items, cfg)

    assert captured == [1.0, 0.8, 0.5, 0.42, 0.8, 0.8, 1.0]
    print("[OK] store_fact_assertions coerces confidence (high/medium/low + floats + clamps)")


def test_store_fact_assertions_caps_at_max_extractions():
    """Same runaway hedge as store_extractions; facts share the cap."""
    cfg = _make_config("openai_compat", max_extractions=2)
    items = [
        {"subject": f"s{i}", "predicate": "p", "object": "o"}
        for i in range(10)
    ]
    with mock.patch.object(core, "memory_fact_add_call", return_value=True) as fact_mock:
        stored = core.store_fact_assertions(items, cfg)
    assert stored == 2
    assert fact_mock.call_count == 2
    print("[OK] store_fact_assertions respects config.max_extractions")


def test_analyze_turn_routes_state_assertions_and_observations_separately():
    """End-to-end split: state_assertions -> memory_fact_add;
    results -> memory_store. Both fire for a turn that yields both.
    """
    cfg = _make_config("openai_compat", model="x")
    payload = {
        "state_assertions": [
            {"subject": "inference-host", "predicate": "model",
             "object": "qwen3-30b-a3b", "confidence": 0.95},
        ],
        "results": [
            {"fact": "Use single quotes when shelling PowerShell from bash.",
             "tags": ["shell"], "confidence": "high"},
        ],
        "unfulfilledPromises": [],
        "shouldNudge": False,
        "nudgeText": "",
        "summary": "",
    }
    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock, \
            mock.patch.object(core, "memory_store_call", return_value=True) as mem_mock, \
            mock.patch.object(core, "memory_fact_add_call", return_value=True) as fact_mock, \
            mock.patch.object(core, "rag_ingest", return_value=True):
        urlopen_mock.return_value.__enter__.return_value.read.return_value = _mock_openai_response(payload)
        out = core.analyze_turn("u" * 200, "m" * 200, "/cwd", cfg)

    assert out["stored"] == 1, "observation must route through memory_store"
    assert out["facts_stored"] == 1, "state assertion must route through memory_fact_add"
    assert mem_mock.call_count == 1
    assert fact_mock.call_count == 1
    # Confirm fact-add was called with the triple from the payload, not the observation text.
    args, _ = fact_mock.call_args
    assert args[0] == "inference-host"
    assert args[1] == "model"
    assert args[2] == "qwen3-30b-a3b"
    print("[OK] analyze_turn routes state_assertions to memory_fact_add and results to memory_store")


def test_analyze_turn_returns_facts_stored_field():
    """Return contract: analyze_turn must include facts_stored alongside
    stored. Adapters/loggers need both numbers to surface routing health.
    """
    cfg = _make_config("openai_compat", model="x")
    with mock.patch.object(core, "call_analysis_llm", return_value=core.empty_analysis()):
        out = core.analyze_turn("u" * 200, "m" * 200, "/cwd", cfg)
    assert "facts_stored" in out
    assert out["facts_stored"] == 0
    print("[OK] analyze_turn return dict includes facts_stored")


# -- Source attribution (Fix #5) ------------------------------------------
# The extraction prompt now asks the LLM to label each item with its
# provenance. Pre-fix every extraction was hardcoded source='model-inferred',
# erasing the distinction between user-stated gospel and inferred guesses.


def test_session_prompt_includes_source_attribution_vocabulary():
    """Tier 2: snapshot the source vocabulary. The three labels are the
    contract with the downstream curation pipeline; renaming any of them
    silently breaks source-keyed filtering on the server."""
    prompt = core.build_analysis_prompt("u", "m")
    assert '"source"' in prompt
    assert "user-stated" in prompt
    assert "tool-result" in prompt
    assert "model-inferred" in prompt
    # The negative-steering line about not labeling inferences as user-stated
    # is load-bearing: without it the LLM tends to over-attribute to the user.
    assert "DO NOT label your own inferences as user-stated" in prompt
    print("[OK] session prompt includes source attribution + negative-steering guidance")


def test_worker_prompt_includes_source_attribution_vocabulary():
    """Workers have no human in the loop so 'user-stated' should be
    explicitly forbidden. The vocabulary still pins tool-result + model-inferred."""
    prompt = core.build_worker_analysis_prompt("envelope", "output")
    assert '"source"' in prompt
    assert "tool-result" in prompt
    assert "model-inferred" in prompt
    # Workers must NEVER attribute to user-stated.
    assert 'Never use "user-stated"' in prompt or "no human in the loop" in prompt
    print("[OK] worker prompt includes source attribution + forbids user-stated")


def test_coerce_source_valid_values_pass_through():
    assert core._coerce_source("user-stated") == "user-stated"
    assert core._coerce_source("tool-result") == "tool-result"
    assert core._coerce_source("model-inferred") == "model-inferred"
    # Mixed case is normalized.
    assert core._coerce_source("USER-STATED") == "user-stated"
    print("[OK] _coerce_source passes valid labels through (case-insensitive)")


def test_coerce_source_unknown_defaults_to_model_inferred():
    """Defaulting to model-inferred is the pessimistic choice: never
    silently promote an unknown label to gospel."""
    for bad in [None, "", "user", "stated", "gospel", 42, ["user-stated"]]:
        assert core._coerce_source(bad) == "model-inferred", f"{bad!r} must default"
    print("[OK] _coerce_source defaults unknown values to 'model-inferred'")


def test_store_extractions_passes_per_item_source_through():
    """End-to-end: each item's source label reaches memory_store_call.
    This is the contract that lets the server distinguish gospel from
    inference at curation time."""
    cfg = _make_config("openai_compat")
    items = [
        {"fact": "User-stated rule.", "source": "user-stated", "confidence": "high"},
        {"fact": "Tool-observed pattern.", "source": "tool-result", "confidence": "medium"},
        {"fact": "Model-guessed rule.", "source": "model-inferred", "confidence": "low"},
    ]
    captured = []

    def _capture(content, tags, importance, source, config):
        captured.append(source)
        return True

    with mock.patch.object(core, "memory_store_call", side_effect=_capture):
        core.store_extractions(items, "conv12345", cfg)

    assert captured == ["user-stated", "tool-result", "model-inferred"]
    print("[OK] store_extractions passes per-item source label through to memory_store_call")


def test_store_extractions_unknown_source_defaults_to_model_inferred():
    """An item with no source field, or a typo'd label, must default to
    model-inferred — preserves the pessimistic-default invariant."""
    cfg = _make_config("openai_compat")
    items = [
        {"fact": "No source field.", "confidence": "high"},
        {"fact": "Typo'd source.", "source": "user-said", "confidence": "high"},
        {"fact": "Non-string source.", "source": 42, "confidence": "high"},
    ]
    captured = []

    def _capture(content, tags, importance, source, config):
        captured.append(source)
        return True

    with mock.patch.object(core, "memory_store_call", side_effect=_capture):
        core.store_extractions(items, "conv12345", cfg)

    assert captured == ["model-inferred", "model-inferred", "model-inferred"]
    print("[OK] store_extractions defaults unknown/missing source to 'model-inferred'")


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
        test_build_analysis_prompt_does_not_truncate_long_inputs,
        test_build_worker_analysis_prompt_does_not_truncate_long_inputs,
        # core: brevity contract on extracted facts
        test_session_prompt_does_not_mandate_combined_why_and_what,
        test_session_prompt_targets_short_facts,
        test_session_prompt_encourages_brevity_and_opt_in_why,
        test_worker_prompt_targets_short_facts,
        test_session_prompt_bad_example_includes_run_on_shape,
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
        # core: fact-vs-memory extraction split (Fix #1)
        test_build_analysis_prompt_includes_state_assertions_section,
        test_empty_analysis_includes_state_assertions,
        test_coerce_analysis_state_assertions_wrong_type_defaults_safe,
        test_memory_fact_add_call_posts_correct_jsonrpc_shape,
        test_memory_fact_add_call_returns_false_on_inner_rejection,
        test_memory_fact_add_call_returns_false_on_network_error,
        test_store_fact_assertions_skips_malformed_items,
        test_store_fact_assertions_calls_fact_add_per_item,
        test_store_fact_assertions_coerces_confidence_strings,
        test_store_fact_assertions_caps_at_max_extractions,
        test_analyze_turn_routes_state_assertions_and_observations_separately,
        test_analyze_turn_returns_facts_stored_field,
        # core: source attribution (Fix #5)
        test_session_prompt_includes_source_attribution_vocabulary,
        test_worker_prompt_includes_source_attribution_vocabulary,
        test_coerce_source_valid_values_pass_through,
        test_coerce_source_unknown_defaults_to_model_inferred,
        test_store_extractions_passes_per_item_source_through,
        test_store_extractions_unknown_source_defaults_to_model_inferred,
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
