#!/usr/bin/env python3
"""Codex PostToolUse hook for NotNativeMemory verbatim capture."""

from codex_hook_common import (  # noqa: E402
    capture_content,
    project_from,
    read_payload,
    session_from,
    stringify,
)


def main() -> None:
    payload = read_payload()
    session_id = session_from(payload)
    project = project_from(payload)
    tool_name = str(payload.get("tool_name") or payload.get("tool") or "tool")
    tool_input = payload.get("tool_input", payload.get("input", {}))
    tool_output = payload.get("tool_output", payload.get("output", payload.get("result", "")))
    is_error = bool(payload.get("is_error") or payload.get("error"))

    body = "\n".join([
        f"[tool] {tool_name}",
        "[input] " + stringify(tool_input),
        "[output] " + stringify(tool_output),
    ])
    capture_content(
        content=body,
        session_id=session_id,
        project=project,
        source_event="tool.call.post",
        agent=f"codex:tool:{tool_name}",
        topic=f"tool.{tool_name}",
        is_error=is_error,
    )


if __name__ == "__main__":
    main()
