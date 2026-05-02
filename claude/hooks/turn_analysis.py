#!/usr/bin/env python3
"""
NotNativeMemory - TurnAnalysis Hook (Claude Code adapter)

Fires on Claude Code's Stop event after each agent response. Reads the
session transcript JSONL at $transcript_path, extracts the most recent
(user, assistant) message pair, and delegates to the shared analysis
core to extract learnable patterns + detect unfulfilled promises.

Claude Code Stop event stdin shape:
  {
    "session_id": "...",
    "transcript_path": "/abs/path/to/transcript.jsonl",
    "cwd": "/abs/path/to/project",
    "hook_event_name": "Stop",
    "stop_hook_active": false
  }

LLM endpoint resolution (in resolve_config_from_env):
  - Explicit MEMORY_EXTRACT_LLM_URL/API/MODEL win
  - ANTHROPIC_BASE_URL → Anthropic Messages at that base
  - OPENAI_BASE_URL → OpenAI-compat at that base (LM Studio / Ollama)
  - Default → api.anthropic.com Messages with claude-haiku-4-5-20251001
For openai_compat without MEMORY_EXTRACT_MODEL, GET /v1/models picks
the first loaded model.

Exit codes:
    0 - success (analysis completed or skipped — non-blocking)
    1 - fatal input error (malformed stdin, missing transcript)
"""

import datetime
import json
import os
import sys

# Resolve hooks_shared/ regardless of whether this script runs from the
# deployed location (~/.claude/hooks/notnative-memory/) or directly from
# the repo (<repo>/claude/hooks/). In the deployed layout, hooks_shared
# sits alongside this script. In the repo, it lives two levels up.
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HOOK_DIR)
sys.path.insert(0, os.path.dirname(os.path.dirname(_HOOK_DIR)))
from hooks_shared.env_loader import load_hooks_env  # noqa: E402
from hooks_shared.turn_analysis_core import (  # noqa: E402
    analyze_turn,
    resolve_config_from_env,
)

load_hooks_env(__file__)

LOG_PATH = os.environ.get(
    "MEMORY_EXTRACT_LOG",
    os.path.expanduser("~/.claude/turn_analysis.log"),
)

# Skip if Claude Code chained another Stop hook to prevent loops.
# (Documented behavior: when stop_hook_active is true, the agent is
# already mid-stop and shouldn't trigger more analysis cycles.)
SKIP_WHEN_STOP_HOOK_ACTIVE = True


def _extract_text_content(content) -> str:
    """Pull plain text out of Claude Code's transcript content shape.

    Transcript entries store `content` as either a plain string or a
    list of typed blocks: {type: 'text', text: '...'},
    {type: 'tool_use', ...}, {type: 'tool_result', content: ...}, etc.
    For analysis purposes we want the human-readable text — concatenate
    text blocks, ignore tool blocks.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text", "")))
        elif btype == "tool_use":
            # Light annotation so the analyzer sees that tools were called
            # without dumping full args.
            name = block.get("name", "?")
            parts.append(f"[tool_use: {name}]")
        elif btype == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                parts.append(f"[tool_result: {inner[:200]}]")
            elif isinstance(inner, list):
                for sub in inner:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        parts.append(f"[tool_result: {str(sub.get('text', ''))[:200]}]")
    return "\n".join(p for p in parts if p)


def extract_last_turn(transcript_path: str) -> tuple:
    """Read the JSONL transcript and return (user_prompt, model_response).

    Strategy: walk the file, find the last assistant entry, then scan
    backward for the most recent user entry preceding it. Returns
    ('', '') if either piece is missing.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return "", ""

    entries = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return "", ""

    # Find the last assistant entry.
    last_assistant_idx = None
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        # Claude Code transcript shape: {type: 'assistant', message: {role, content}}
        # or a flat {role: 'assistant', content: ...} depending on version.
        if entry.get("type") == "assistant" or entry.get("role") == "assistant":
            last_assistant_idx = i
            break
        msg = entry.get("message")
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            last_assistant_idx = i
            break

    if last_assistant_idx is None:
        return "", ""

    # Find the most recent user entry before it.
    last_user_idx = None
    for i in range(last_assistant_idx - 1, -1, -1):
        entry = entries[i]
        if entry.get("type") == "user" or entry.get("role") == "user":
            last_user_idx = i
            break
        msg = entry.get("message")
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user_idx = i
            break

    def _content_of(entry: dict):
        if "message" in entry and isinstance(entry["message"], dict):
            return entry["message"].get("content")
        return entry.get("content")

    if last_user_idx is None:
        # An orphaned assistant entry without a preceding user prompt
        # can't be analyzed — the LLM needs the pair. Return empty so
        # the caller skips.
        return "", ""

    user_prompt = _extract_text_content(_content_of(entries[last_user_idx]))
    model_response = _extract_text_content(_content_of(entries[last_assistant_idx]))

    return user_prompt, model_response


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
    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(1)

    if SKIP_WHEN_STOP_HOOK_ACTIVE and hook_input.get("stop_hook_active"):
        # Already inside a Stop chain — bail to avoid recursion.
        sys.exit(0)

    transcript_path = hook_input.get("transcript_path", "")
    cwd = hook_input.get("cwd") or os.getcwd()

    user_prompt, model_response = extract_last_turn(transcript_path)
    if not user_prompt or not model_response:
        # Nothing to analyze (first turn, malformed transcript, or
        # tool-only response). Non-fatal — exit 0 so the harness moves on.
        _log_execution(0, False, 0)
        sys.exit(0)

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
