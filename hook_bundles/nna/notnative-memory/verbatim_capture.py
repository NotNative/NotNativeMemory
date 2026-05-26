#!/usr/bin/env python3
"""
NotNativeMemory - Verbatim Capture Hook (nna adapter)

Dispatches to the verbatim-capture core based on the hook event in the
stdin payload. Subscribes to turn:pre, turn:post, and tool.call:post.

Replaces NNA's removed `src/services/verbatim/writer.ts` JSONL writer.
All transcript persistence now lives in NNM's `verbatim_chunks` table
via the `verbatim_capture` MCP tool.

Exit codes:
    0 - success (or expected skip)
    1 - non-fatal error (stdin unreadable, missing required fields)

Gate: NNA_VERBATIM_CAPTURE=0 disables the hook entirely (default: on).
"""

from __future__ import annotations

import datetime
import json
import os
import sys

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
    capture_turn_pre,
)

LOG_PATH = os.environ.get(
    "VERBATIM_CAPTURE_LOG",
    os.path.expanduser("~/.nna/verbatim_capture.log"),
)


def _gated_off() -> bool:
    val = os.environ.get("NNA_VERBATIM_CAPTURE", "1").strip().lower()
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


def _resolve_session_id(payload: dict) -> str:
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    env_sid = os.environ.get("NNA_SESSION_ID", "").strip()
    if env_sid:
        return env_sid
    return "unknown-session"


def _resolve_loaded_skills(payload: dict) -> list:
    raw = payload.get("loaded_skills")
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, str)]
    raw_env = os.environ.get("NNA_LOADED_SKILLS", "").strip()
    if raw_env:
        return [s.strip() for s in raw_env.split(",") if s.strip()]
    return []


def _resolve_phase(event: str, payload: dict) -> str:
    """Infer the engine phase from payload shape.

    NNA's drop-in hook contract dispatches the raw engine payload to each
    subscriber and does NOT inject a `phase` field — phase is metadata
    on the subscription (declared in manifest.json), not on the payload.
    We re-derive it from which fields are populated:

      turn:pre   — payload.prompt set, payload.model_response absent.
      turn:post  — payload.model_response set (engine writes it before
                   dispatching :post).
      tool.call:pre  — payload.tool_input set, payload.tool_output absent.
      tool.call:post — payload.tool_output set (engine writes it before
                       dispatching :post). `is_error` is REQUIRED on
                       ToolCallPayload so its presence can't disambiguate.

    For events with only one subscribed phase (e.g. tool.call where the
    bundle only registers :post), shape inference would be redundant but
    is kept for forward-compat.
    """
    if event == "turn":
        if payload.get("model_response"):
            return "post"
        return "pre"
    if event == "tool.call":
        if "tool_output" in payload and payload.get("tool_output") is not None:
            return "post"
        return "pre"
    return ""


def _dispatch(payload: dict) -> int:
    event = (payload.get("event") or "").strip()
    phase = (payload.get("phase") or "").strip() or _resolve_phase(event, payload)
    session_id = _resolve_session_id(payload)
    loaded_skills = _resolve_loaded_skills(payload)
    mission_id = payload.get("mission_id") or None
    mission_type = payload.get("mission_type") or None

    if event == "turn" and phase == "pre":
        prompt = payload.get("prompt") or payload.get("user_prompt") or ""
        return capture_turn_pre(
            session_id=session_id,
            user_prompt=prompt,
            loaded_skills=loaded_skills,
            mission_id=mission_id,
            mission_type=mission_type,
        )

    if event == "turn" and phase == "post":
        prompt = payload.get("prompt") or payload.get("user_prompt") or ""
        response = payload.get("model_response") or ""
        return capture_turn_post(
            session_id=session_id,
            user_prompt=prompt,
            model_response=response,
            loaded_skills=loaded_skills,
            mission_id=mission_id,
            mission_type=mission_type,
        )

    if event == "tool.call" and phase == "post":
        tool_name = payload.get("tool_name") or "<unknown>"
        return capture_tool_call_post(
            session_id=session_id,
            tool_name=tool_name,
            tool_input=payload.get("tool_input"),
            tool_output=payload.get("tool_output"),
            is_error=bool(payload.get("is_error", False)),
            loaded_skills=loaded_skills,
            mission_id=mission_id,
            mission_type=mission_type,
        )

    # Unknown event:phase — log and exit success so the hook contract
    # stays clean.
    _log(f"{event}:{phase}", "ignored", 0)
    return 0


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

    try:
        stored = _dispatch(payload)
    except Exception as exc:  # noqa: BLE001
        _log(payload.get("event", "<no-event>"), f"exc:{type(exc).__name__}", 0)
        return 1

    # Reconstruct the (event, phase) tuple the same way _dispatch did so
    # the log shows the resolved phase (e.g. "turn:post") instead of
    # "turn:?" when NNA's payload omitted the phase field.
    event = (payload.get("event") or "").strip()
    phase = (payload.get("phase") or "").strip() or _resolve_phase(event, payload)
    _log(f"{event or '?'}:{phase or '?'}", "ok", stored)
    return 0


if __name__ == "__main__":
    sys.exit(main())
