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


def _log_execution(
    extracted_count: int, nudge_stored: bool, conversation_len: int
) -> None:
    try:
        parent = os.path.dirname(LOG_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as logf:
            logf.write(
                f"{datetime.datetime.now().isoformat(timespec='seconds')}\t"
                f"extracted={extracted_count}\t"
                f"nudge={'1' if nudge_stored else '0'}\t"
                f"conv_len={conversation_len}\n"
            )
    except OSError:
        pass


def main():
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
        _log_execution(0, False, 0)
        sys.exit(1)

    conversation_len = len(user_prompt) + len(model_response)
    config = resolve_config_from_env()
    outcome = analyze_turn(user_prompt, model_response, cwd, config)

    _log_execution(
        outcome["stored"],
        outcome["nudge_stored"],
        conversation_len,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
