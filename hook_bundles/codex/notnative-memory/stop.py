#!/usr/bin/env python3
"""Codex Stop hook for NotNativeMemory verbatim capture.

Codex Stop payloads can vary by client build, so this adapter first looks for
direct assistant-response fields and then falls back to a compact transcript
tail. It never blocks the turn when capture is unavailable.
"""

from codex_hook_common import (  # noqa: E402
    capture_content,
    project_from,
    read_payload,
    session_from,
    stringify,
    transcript_tail_text,
)


def _assistant_text(payload: dict) -> str:
    for key in (
        "assistant_response",
        "model_response",
        "response",
        "final_response",
        "output",
    ):
        val = payload.get(key)
        if val:
            return stringify(val, 4000)
    return transcript_tail_text(str(payload.get("transcript_path") or ""), 4000)


def main() -> None:
    payload = read_payload()
    if payload.get("stop_hook_active"):
        return
    text = _assistant_text(payload)
    if not text:
        return
    capture_content(
        content=text,
        session_id=session_from(payload),
        project=project_from(payload),
        source_event="turn.post",
        agent="codex:assistant",
    )


if __name__ == "__main__":
    main()
