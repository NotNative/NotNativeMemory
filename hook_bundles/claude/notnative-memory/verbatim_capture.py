#!/usr/bin/env python3
"""
NotNativeMemory - Verbatim Capture Hook (claude adapter)

Dispatches to the verbatim-capture core based on which Claude Code hook
event fired. Registered against Stop and PostToolUse — together those
cover the per-turn and per-tool-invocation chunks the dreaming loop
needs as primary-source ground truth.

Claude Code's hook payload shapes differ from NNA's engine events:

  - Stop:        {hook_event_name, session_id, transcript_path, cwd,
                  stop_hook_active}
                 → the user prompt + assistant response have to be
                 reconstructed by tailing the JSONL transcript file.
  - PostToolUse: {hook_event_name, session_id, tool_name, tool_input,
                  tool_response, cwd}
                 → tool data is already inline; no transcript walk.

Chunks captured here are tagged `agent='claude-code'` so the future NNA
curator (Phase D) can filter them out of skill-mutation passes while
verbatim_search still returns them across both harnesses.

Gate: NNM_VERBATIM_CAPTURE=0 disables (default: on).

Exit codes:
    0 - success (or expected skip)
    1 - non-fatal error (stdin unreadable, missing required fields)
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from typing import Any, Optional, Tuple

_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))

# Optional hooks.env loader, matching the pattern used by turn_analysis.py.
_ENV_FILE = os.path.join(_HOOK_DIR, "hooks.env")
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ.setdefault(_key.strip(), _val.strip())

sys.path.insert(0, _HOOK_DIR)
from _internal.verbatim_core import (  # noqa: E402
    capture_tool_call_post,
    capture_turn_post,
)

LOG_PATH = os.environ.get(
    "VERBATIM_CAPTURE_LOG",
    os.path.expanduser("~/.claude/verbatim_capture.log"),
)

# Tag every Claude-sourced chunk so downstream filters can distinguish
# them from NNA-sourced chunks. The curator (Phase D of v2) uses this to
# exclude Claude-session chunks from skill-mutation passes (Claude Code
# has its own skill system; mutating NNA skills from Claude transcripts
# would be cross-harness coupling).
_CLAUDE_AGENT_TAG = "claude-code"


def _gated_off() -> bool:
    val = os.environ.get("NNM_VERBATIM_CAPTURE", "1").strip().lower()
    return val in ("0", "false", "no", "off")


def _log(event: str, status: str, stored: int) -> None:
    try:
        parent = os.path.dirname(LOG_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{ts}\tevent={event}\tstatus={status}\tstored={stored}\n")
    except OSError:
        pass


# -- Transcript walk (Claude-specific) --------------------------------------

def _extract_text_content(content: Any) -> str:
    """Flatten Claude Code's transcript content blocks into plain text.

    Mirrors the helper in turn_analysis.py but kept local so this adapter
    has no inter-script imports.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(str(block.get("text", "")))
            elif btype == "tool_use":
                name = block.get("name", "")
                parts.append(f"[tool_use: {name}]")
            elif btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, str):
                    parts.append(f"[tool_result: {inner[:200]}]")
                elif isinstance(inner, list):
                    for sub in inner:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            parts.append(
                                f"[tool_result: {str(sub.get('text', ''))[:200]}]"
                            )
        return "\n".join(p for p in parts if p)
    return str(content)


def _extract_last_turn(transcript_path: str) -> Tuple[str, str]:
    """Tail the JSONL transcript and return (user_prompt, model_response).

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

    def _role(entry: dict) -> Optional[str]:
        if entry.get("role"):
            return entry["role"]
        if entry.get("type") in ("user", "assistant"):
            return entry["type"]
        msg = entry.get("message")
        if isinstance(msg, dict):
            return msg.get("role")
        return None

    def _content_of(entry: dict):
        if "message" in entry and isinstance(entry["message"], dict):
            return entry["message"].get("content")
        return entry.get("content")

    last_assistant_idx = None
    for i in range(len(entries) - 1, -1, -1):
        if _role(entries[i]) == "assistant":
            last_assistant_idx = i
            break

    if last_assistant_idx is None:
        return "", ""

    last_user_idx = None
    for i in range(last_assistant_idx - 1, -1, -1):
        if _role(entries[i]) == "user":
            last_user_idx = i
            break

    if last_user_idx is None:
        return "", ""

    user_prompt = _extract_text_content(_content_of(entries[last_user_idx]))
    model_response = _extract_text_content(_content_of(entries[last_assistant_idx]))
    return user_prompt, model_response


# -- Dispatch ---------------------------------------------------------------

def _resolve_session_id(payload: dict) -> str:
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    return "unknown-session"


def _is_error_tool_response(tool_response: Any) -> bool:
    """Heuristic: Claude Code tool_response usually carries either an
    `error` key or an `is_error: true` flag on failure. Both are honored
    so the curator's failure-fix pair detection still works on Claude
    transcripts."""
    if not isinstance(tool_response, dict):
        return False
    if tool_response.get("is_error") is True:
        return True
    if tool_response.get("error"):
        return True
    return False


def _dispatch_stop(payload: dict) -> int:
    if payload.get("stop_hook_active"):
        # Avoid re-firing during a stop-hook chain; turn_analysis uses
        # the same guard.
        return 0

    transcript_path = payload.get("transcript_path", "")
    user_prompt, model_response = _extract_last_turn(transcript_path)
    if not model_response:
        return 0
    return capture_turn_post(
        session_id=_resolve_session_id(payload),
        user_prompt=user_prompt,
        model_response=model_response,
        agent=_CLAUDE_AGENT_TAG,
    )


def _dispatch_post_tool_use(payload: dict) -> int:
    tool_name = payload.get("tool_name") or "<unknown>"
    return capture_tool_call_post(
        session_id=_resolve_session_id(payload),
        tool_name=tool_name,
        tool_input=payload.get("tool_input"),
        tool_output=payload.get("tool_response"),
        is_error=_is_error_tool_response(payload.get("tool_response")),
        # Override the default agent tag (which would be "tool:<name>")
        # by tagging the chunk's agent as Claude — the topic still carries
        # the tool name so the curator can filter that way.
    )


def main() -> int:
    if _gated_off():
        return 0

    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        _log("stdin", "parse_error", 0)
        return 1

    if not isinstance(payload, dict):
        _log("stdin", "non_dict", 0)
        return 1

    event = (payload.get("hook_event_name") or "").strip()
    try:
        if event == "Stop":
            stored = _dispatch_stop(payload)
        elif event == "PostToolUse":
            stored = _dispatch_post_tool_use(payload)
        else:
            _log(event or "<no-event>", "ignored", 0)
            return 0
    except Exception as exc:  # noqa: BLE001
        _log(event, f"exc:{type(exc).__name__}", 0)
        return 1

    _log(event, "ok", stored)
    return 0


if __name__ == "__main__":
    sys.exit(main())
