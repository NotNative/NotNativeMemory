#!/usr/bin/env python3
"""Codex UserPromptSubmit hook for NotNativeMemory.

Captures the user prompt into NNM verbatim storage and injects relevant memory
context. Low-signal prompts use `verbatim_recent` to recover the current topic.
"""

from codex_hook_common import (  # noqa: E402
    build_recent_query,
    capture_content,
    filter_relevant,
    format_memory_context,
    memory_facts,
    memory_search,
    project_from,
    prompt_from,
    read_payload,
    recent_chunks,
    session_from,
    should_walk_back,
    transcript_tail_text,
    write_additional_context,
)


def main() -> None:
    payload = read_payload()
    prompt = prompt_from(payload)
    project = project_from(payload)
    session_id = session_from(payload)

    if prompt:
        capture_content(
            content=prompt,
            session_id=session_id,
            project=project,
            source_event="user.prompt.submit",
            agent="codex:user",
        )

    if not prompt:
        return

    query_source = "prompt"
    query = prompt
    if should_walk_back(prompt):
        chunks = recent_chunks(session_id, project)
        if chunks:
            query = build_recent_query(chunks, prompt)
            query_source = "verbatim_recent"
        else:
            tail = transcript_tail_text(str(payload.get("transcript_path") or ""), 1200)
            # Keep this fallback compact; the normal path is verbatim_recent.
            query = f"Current Codex transcript context:\n{tail}\n\nLatest user prompt: {prompt}"
            query_source = "transcript_path"

    memories = filter_relevant(memory_search(query, project))
    facts = memory_facts(query, project)
    context = format_memory_context(memories, facts)
    if not context:
        return
    if query_source != "prompt":
        context = f"Memory query source: {query_source}\n\n{context}"
    write_additional_context(context, "UserPromptSubmit")


if __name__ == "__main__":
    main()
