#!/usr/bin/env python3
"""
NotNativeMemory - TurnAnalysis Hook (nna adapter)

Fires at the END of each turn (post phase) in the nna harness, after
the model responds. Pulls user_prompt + model_response straight from
nna's Stop hook stdin, then delegates to the shared analysis core.

The bundle-local core (_internal/turn_analysis_core.py) handles:
  1. Learnable-pattern extraction → RAG-ingested into NotNativeMemory.
  2. Promise tracking → high-importance nudge memory for next-turn surfacing.

Renamed from turn_extractor.py on 2026-04-26 to reflect the broader
analysis scope.

Exit codes:
    0 - success (analysis completed or skipped)
    1 - non-fatal error (does not block agent operation)
"""

import datetime
import json
import os
import sys

# -- Load config from hooks.env alongside this script ----------------------
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_FILE = os.path.join(_HOOK_DIR, "hooks.env")
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE, "r") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ.setdefault(_key.strip(), _val.strip())

# Bundle-local helpers under _internal/. Same shape in both repo and
# deployed layouts.
sys.path.insert(0, _HOOK_DIR)
from _internal.detach import detach_or_resume  # noqa: E402
from _internal.turn_analysis_core import (  # noqa: E402
    analyze_turn,
    resolve_config_from_env,
)

LOG_PATH = os.environ.get(
    "MEMORY_EXTRACT_LOG",
    os.path.expanduser("~/.nna/turn_analysis.log"),
)

# Legacy log path from when this script was named turn_extractor.py.
_LEGACY_LOG_PATH = os.path.expanduser("~/.nna/turn_extractor.log")


def _cleanup_legacy_log() -> None:
    try:
        if os.path.exists(_LEGACY_LOG_PATH):
            os.remove(_LEGACY_LOG_PATH)
    except OSError:
        pass


def _count_candidates(outcome: dict) -> dict:
    analysis = outcome.get("analysis")
    if not isinstance(analysis, dict):
        analysis = {}

    results = analysis.get("results")
    facts = analysis.get("state_assertions")
    relationships = analysis.get("relationship_assertions")
    summary = analysis.get("summary")

    result_count = len(results) if isinstance(results, list) else 0
    fact_count = len(facts) if isinstance(facts, list) else 0
    relationship_count = len(relationships) if isinstance(relationships, list) else 0
    summary_count = 1 if isinstance(summary, str) and summary.strip() else 0

    return {
        "candidates": result_count + fact_count + relationship_count + summary_count,
        "candidate_memories": result_count,
        "candidate_facts": fact_count,
        "candidate_relationships": relationship_count,
        "candidate_summary": summary_count,
    }


def _log_execution(
    stored_count: int,
    nudge_stored: bool,
    conversation_len: int,
    *,
    status: str = "ok",
    reason: str = "",
    facts_stored: int = 0,
    relationships_stored: int = 0,
    summary_stored: bool = False,
    candidates: int = 0,
    candidate_memories: int = 0,
    candidate_facts: int = 0,
    candidate_relationships: int = 0,
    candidate_summary: int = 0,
) -> None:
    try:
        parent = os.path.dirname(LOG_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fields = [
            f"{datetime.datetime.now().isoformat(timespec='seconds')}",
            f"status={status}",
            # Keep extracted= for legacy readers, but it means stored memories.
            f"extracted={stored_count}",
            f"stored={stored_count}",
            f"facts={facts_stored}",
            f"relationships={relationships_stored}",
            f"summary={'1' if summary_stored else '0'}",
            f"candidates={candidates}",
            f"candidate_memories={candidate_memories}",
            f"candidate_facts={candidate_facts}",
            f"candidate_relationships={candidate_relationships}",
            f"candidate_summary={candidate_summary}",
            f"nudge={'1' if nudge_stored else '0'}",
            f"conv_len={conversation_len}",
        ]
        if reason:
            safe_reason = str(reason).replace("\t", " ").replace("\n", " ")[:500]
            fields.append(f"reason={safe_reason}")
        with open(LOG_PATH, "a", encoding="utf-8") as logf:
            logf.write("\t".join(fields) + "\n")
    except OSError:
        pass


def main():
    # Detach into a background worker before doing any real work. The
    # harness gets back exit 0 in well under a second; the worker
    # process runs the LLM extraction with no timeout pressure. In
    # worker mode this returns immediately with sys.stdin replaying
    # the original payload. NNM_TURN_ANALYSIS_INLINE=1 bypasses for
    # tests.
    detach_or_resume(__file__)

    _cleanup_legacy_log()

    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(1)

    user_prompt = hook_input.get("prompt", "")
    model_response = hook_input.get("model_response", "")
    cwd = hook_input.get("cwd") or os.getcwd()

    if not user_prompt or not model_response:
        error_msg = (
            f"Missing data in hook input: prompt={bool(user_prompt)}, "
            f"model_response={bool(model_response)}"
        )
        print(f"[ERROR] {error_msg}", file=sys.stderr)
        _log_execution(0, False, 0, status="skipped", reason=error_msg)
        sys.exit(1)

    # NNA passes model_name in the turn:post payload. Use it to skip the
    # /v1/models probe in discover_model(). Explicit MEMORY_EXTRACT_MODEL
    # in hooks.env still wins — setdefault only fills when the var is unset.
    model_name = hook_input.get("model_name", "").strip()
    if model_name:
        os.environ.setdefault("MEMORY_EXTRACT_MODEL", model_name)

    conversation_len = len(user_prompt) + len(model_response)
    try:
        config = resolve_config_from_env()
        outcome = analyze_turn(user_prompt, model_response, cwd, config)
    except Exception as exc:
        _log_execution(
            0,
            False,
            conversation_len,
            status="error",
            reason=f"{type(exc).__name__}: {exc}",
        )
        raise

    candidate_counts = _count_candidates(outcome)
    _log_execution(
        outcome["stored"],
        outcome["nudge_stored"],
        conversation_len,
        facts_stored=outcome.get("facts_stored", 0),
        relationships_stored=outcome.get("relationships_stored", 0),
        summary_stored=bool(outcome.get("summary_stored")),
        **candidate_counts,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
