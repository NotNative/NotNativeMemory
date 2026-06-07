"""
NNA-bundle-only tests for _internal/turn_analysis_core.py.

The two bundles' core modules diverged 2026-05-19: promise detection was
ripped from the nna bundle (relocated to NNA proper at
src/services/promise-detector/) but PRESERVED in the claude bundle where
NNM remains the canonical implementation. test_turn_analysis.py covers
the canonical claude shape; this file covers the nna deltas.

Keeping the two test files lets either bundle evolve independently
without one breaking the other's contract assertions.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).parent.parent
_NNA_CORE = (
    _REPO_ROOT
    / "hook_bundles"
    / "nna"
    / "notnative-memory"
    / "_internal"
    / "turn_analysis_core.py"
)
_NNA_ADAPTER = (
    _REPO_ROOT
    / "hook_bundles"
    / "nna"
    / "notnative-memory"
    / "turn_analysis.py"
)


def _load_nna_core():
    """Load the nna bundle's turn_analysis_core under a unique module
    name so it doesn't collide with the claude-bundle import done by
    tests/test_turn_analysis.py."""
    spec = importlib.util.spec_from_file_location(
        "turn_analysis_core_nna", str(_NNA_CORE),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["turn_analysis_core_nna"] = module
    spec.loader.exec_module(module)
    return module


def _load_nna_adapter():
    """Load the nna bundle's turn_analysis.py under a unique module name."""
    spec = importlib.util.spec_from_file_location(
        "turn_analysis_nna_adapter", str(_NNA_ADAPTER),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["turn_analysis_nna_adapter"] = module
    spec.loader.exec_module(module)
    return module


core = _load_nna_core()
nna_adapter = _load_nna_adapter()


def _make_config(api: str, **overrides) -> "core.AnalysisConfig":
    base = dict(
        api=api,
        endpoint="http://test.local/x",
        model="test-model",
        headers={"Content-Type": "application/json"},
        models_url=None,
    )
    base.update(overrides)
    return core.AnalysisConfig(**base)


def _mock_openai_response(content_obj: dict) -> bytes:
    return json.dumps({
        "choices": [{"message": {"content": json.dumps(content_obj)}}]
    }).encode("utf-8")


# -- Prompt rip ------------------------------------------------------------

def test_session_prompt_does_not_request_promise_tracking():
    """NNA owns promise detection now. If this prompt still requests it,
    NNA and NNM will fight over the same conversation."""
    prompt = core.build_analysis_prompt("user message here", "model response here")
    assert "Promise tracking" not in prompt
    assert "shouldNudge" not in prompt
    assert "nudgeText" not in prompt
    assert "unfulfilledPromises" not in prompt
    print("[OK] nna bundle session prompt has no promise-detection sections")


def test_session_prompt_still_carries_extraction_and_summary():
    """The rip must NOT remove the extraction or summary sections."""
    prompt = core.build_analysis_prompt("u", "m")
    assert "state_assertions" in prompt
    assert "results" in prompt or '"fact"' in prompt
    assert "summary" in prompt
    print("[OK] nna bundle session prompt retains extraction and summary")


def test_worker_prompt_does_not_request_promise_tracking():
    prompt = core.build_worker_analysis_prompt("envelope", "output")
    assert "Promise tracking" not in prompt
    assert "shouldNudge" not in prompt
    print("[OK] nna bundle worker prompt has no promise-detection sections")


# -- Shape rip -------------------------------------------------------------

def test_empty_analysis_drops_promise_fields():
    out = core.empty_analysis()
    assert "unfulfilledPromises" not in out
    assert "shouldNudge" not in out
    assert "nudgeText" not in out
    # Canonical post-rip shape:
    assert set(out.keys()) == {
        "state_assertions",
        "relationship_assertions",
        "results",
        "summary",
    }
    print("[OK] empty_analysis returns the canonical post-rip shape")


def test_coerce_analysis_silently_drops_legacy_promise_fields():
    """A stale fine-tuned model or old prompt template might still emit
    promise fields. They must be dropped, not surfaced — surfacing them
    would let NNM and NNA fight over the same turn."""
    out = core.coerce_analysis({
        "results": [],
        "unfulfilledPromises": [{"promise": "x"}],
        "shouldNudge": True,
        "nudgeText": "ignored",
    })
    assert "unfulfilledPromises" not in out
    assert "shouldNudge" not in out
    assert "nudgeText" not in out
    print("[OK] coerce_analysis drops legacy promise fields silently")


# -- Storage rip -----------------------------------------------------------

def test_store_pending_nudge_function_removed():
    """The helper is gone. NNA's storePendingNudge handles this now,
    posting an equivalent memory_store call to the same MCP."""
    assert not hasattr(core, "store_pending_nudge"), (
        "store_pending_nudge must be removed from the nna bundle; NNA "
        "owns this path now. If you need it back, port the NNA "
        "implementation rather than re-introducing the function here."
    )
    print("[OK] store_pending_nudge is removed from the nna bundle")


# -- End-to-end ------------------------------------------------------------

def test_analyze_turn_returns_nudge_stored_false_even_when_llm_emits_legacy_fields():
    """End-to-end: even when the LLM ignores the slim prompt and emits
    promise fields, analyze_turn must:
      - return nudge_stored=False unconditionally (NNA owns nudging now);
      - never call rag_ingest for the nudge path (only the summary path).
    """
    cfg = _make_config("openai_compat", model="x")
    payload = {
        "results": [{
            "fact": "A standalone fact suitable for storage.",
            "tags": ["test"],
            "confidence": "high",
        }],
        # Stale-prompt emissions; must be dropped at coerce.
        "unfulfilledPromises": [{"promise": "p", "reason": "r"}],
        "shouldNudge": True,
        "nudgeText": "would-be nudge",
        "summary": "A short summary.",
    }
    with mock.patch.object(core.urllib.request, "urlopen") as urlopen_mock, \
            mock.patch.object(core, "memory_store_call", return_value=True) as mem_mock, \
            mock.patch.object(core, "rag_ingest", return_value=True) as rag_mock:
        urlopen_mock.return_value.__enter__.return_value.read.return_value = _mock_openai_response(payload)
        out = core.analyze_turn("u" * 200, "m" * 200, "/cwd", cfg)

    assert out["nudge_stored"] is False
    assert out["stored"] == 1
    assert out["summary_stored"] is True
    # One memory call for extraction; one RAG call for summary; no nudge.
    assert mem_mock.call_count == 1
    assert rag_mock.call_count == 1
    print("[OK] analyze_turn ignores legacy promise fields end-to-end")


# -- Adapter diagnostics ---------------------------------------------------

def test_adapter_counts_candidates_separately_from_stored_records():
    """Operator diagnostics need to distinguish analyzer output from MCP
    storage. If auth or project scope blocks writes, candidates can be >0
    while stored remains 0.
    """
    counts = nna_adapter._count_candidates({
        "stored": 0,
        "facts_stored": 0,
        "relationships_stored": 0,
        "summary_stored": False,
        "analysis": {
            "results": [{"fact": "one"}, {"fact": "two"}],
            "state_assertions": [{"subject": "host", "predicate": "port", "object": "9500"}],
            "relationship_assertions": [{"subject": "nna", "relation": "uses", "object": "nnm"}],
            "summary": "short summary",
        },
    })
    assert counts["candidates"] == 5
    assert counts["candidate_memories"] == 2
    assert counts["candidate_facts"] == 1
    assert counts["candidate_relationships"] == 1
    assert counts["candidate_summary"] == 1
    print("[OK] nna adapter counts candidates independently from stored records")


def test_adapter_log_line_includes_status_candidates_and_storage_breakdown(tmp_path):
    """The log should explain whether the hook fired, what it found, and
    what actually landed in NNM.
    """
    old_path = nna_adapter.LOG_PATH
    log_file = tmp_path / "turn_analysis.log"
    try:
        nna_adapter.LOG_PATH = str(log_file)
        nna_adapter._log_execution(
            0,
            False,
            123,
            status="ok",
            facts_stored=0,
            relationships_stored=0,
            summary_stored=False,
            candidates=3,
            candidate_memories=1,
            candidate_facts=1,
            candidate_relationships=1,
            candidate_summary=0,
        )
    finally:
        nna_adapter.LOG_PATH = old_path

    line = log_file.read_text(encoding="utf-8")
    assert "status=ok" in line
    assert "extracted=0" in line  # legacy alias for stored memories
    assert "stored=0" in line
    assert "candidates=3" in line
    assert "candidate_facts=1" in line
    assert "conv_len=123" in line
    print("[OK] nna adapter diagnostic log includes status/candidates/storage fields")


# -- Divergence guard ------------------------------------------------------

def test_bundles_diverge_on_promise_detection():
    """Pin the intentional divergence: claude bundle keeps the canonical
    promise-detection implementation, nna bundle does not. If someone
    syncs the bundles back to identical, this test fires and forces a
    deliberate decision rather than silent regression of either side.
    """
    claude_path = (
        _REPO_ROOT
        / "hook_bundles"
        / "claude"
        / "notnative-memory"
        / "_internal"
        / "turn_analysis_core.py"
    )
    nna_path = _NNA_CORE
    claude_body = claude_path.read_text(encoding="utf-8")
    nna_body = nna_path.read_text(encoding="utf-8")

    # Claude bundle must still implement promise detection.
    assert "store_pending_nudge" in claude_body, (
        "claude bundle's turn_analysis_core.py lost store_pending_nudge. "
        "If the relocation to NNA was intentional for the claude side "
        "too, also remove this assertion."
    )
    assert "shouldNudge" in claude_body, "claude bundle dropped shouldNudge wording"

    # nna bundle must NOT — check the active code shape, not docstring
    # mentions (the rip leaves explanatory comments that reference the
    # removed fields, which is fine and informative).
    assert "def store_pending_nudge" not in nna_body
    assert '"shouldNudge": false,\n' not in nna_body, (
        "nna bundle prompt still asks the LLM to emit shouldNudge — rip incomplete"
    )
    assert '"unfulfilledPromises": [\n' not in nna_body, (
        "nna bundle prompt still requests unfulfilledPromises — rip incomplete"
    )
    print("[OK] claude/nna bundles diverge on promise detection as intended")


if __name__ == "__main__":
    test_session_prompt_does_not_request_promise_tracking()
    test_session_prompt_still_carries_extraction_and_summary()
    test_worker_prompt_does_not_request_promise_tracking()
    test_empty_analysis_drops_promise_fields()
    test_coerce_analysis_silently_drops_legacy_promise_fields()
    test_store_pending_nudge_function_removed()
    test_analyze_turn_returns_nudge_stored_false_even_when_llm_emits_legacy_fields()
    test_adapter_counts_candidates_separately_from_stored_records()
    test_bundles_diverge_on_promise_detection()
