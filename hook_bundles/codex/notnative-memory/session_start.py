#!/usr/bin/env python3
"""Codex SessionStart hook for NotNativeMemory.

Provides a small working-set reminder when a Codex session starts. This hook is
additive and non-blocking: if NNM is unavailable, it exits silently.
"""

from codex_hook_common import (  # noqa: E402
    diagnostic_context,
    memory_context,
    project_from,
    read_payload,
    write_additional_context,
)


def main() -> None:
    payload = read_payload()
    project = project_from(payload)
    diagnostic_context("SessionStart", f"project={project}")
    context = memory_context(project)
    if context:
        write_additional_context(context, "SessionStart")


if __name__ == "__main__":
    main()
