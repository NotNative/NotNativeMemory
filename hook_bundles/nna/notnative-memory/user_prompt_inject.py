#!/usr/bin/env python3
"""
NotNativeMemory - UserPromptSubmit Hook

Fires when the user sends a message, before the model processes it.
Uses the user's message as a semantic query against the memory server
and injects top matches as additionalContext so relevant decisions,
preferences, and constraints are in scope for the whole turn.

This is the "decisions get framed when the user speaks" hook. Pairs
well with the PreToolUse hook: UserPromptSubmit primes the turn with
relevant context up front; PreToolUse surfaces action-specific
gotchas right before risky operations.

Exit codes:
    0 - success (with or without context injected)
    1 - non-fatal error (prompt still reaches the model)
"""

import datetime
import json
import os
import sys
import urllib.request
import urllib.error

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

# -- Configuration ---------------------------------------------------------

MCP_URL = os.environ.get("MEMORY_MCP_URL", "http://localhost:9500/mcp")

# Threshold tuned for user-prompt noise: user messages are longer and
# more varied than tool arguments, so a higher floor filters chit-chat.
# Override with MEMORY_PROMPT_THRESHOLD.
SIMILARITY_THRESHOLD = float(os.environ.get("MEMORY_PROMPT_THRESHOLD", "0.45"))

# Memories tagged high or critical surface at a lower floor because
# the operator explicitly flagged them as load-bearing.
HIGH_IMPORTANCE_THRESHOLD = float(
    os.environ.get("MEMORY_PROMPT_HIGH_THRESHOLD", "0.35")
)

# Keep the injection lean at turn start to avoid flooding context.
MAX_MEMORIES = int(os.environ.get("MEMORY_PROMPT_MAX_RESULTS", "3"))
SEARCH_LIMIT = 10

# Skip trivial prompts ("ok", "yes", "continue") — not worth a search.
MIN_PROMPT_CHARS = int(os.environ.get("MEMORY_PROMPT_MIN_CHARS", "15"))

# Prompts that match this set verbatim (case-insensitive, after stripping
# trailing punctuation) are degenerate-confirmations: they carry no topical
# signal but the *prior* assistant turn does. Walk back to it so the
# memory search has something meaningful to embed.
AFFIRMATIVE_SET = frozenset({
    "yes", "y", "yep", "yeah", "yup",
    "ok", "okay", "k",
    "proceed", "go", "go ahead", "continue", "keep going",
    "sure", "do it", "sounds good", "good", "fine",
    "next", "more",
})

# Truncate prior assistant text used as walk-back basis. The point is to
# give the embedding model a topical anchor, not to ship the entire prior
# response into the query.
WALKBACK_PRIOR_MAX_CHARS = 600

TIMEOUT_SECONDS = 5

LOG_PATH = os.environ.get(
    "MEMORY_PROMPT_LOG",
    os.path.expanduser("~/.nna/memory_prompt_hook.log"),
)

# Cap query length so we don't push multi-page user dumps through the
# embedding model unnecessarily. The first 500 chars almost always
# capture the topic.
MAX_QUERY_CHARS = 500

# Attach Bearer auth when MEMORY_MCP_TOKEN is set. The MCP server
# requires auth since Phase 5; hooks satisfy it with either a token
# (set MEMORY_MCP_TOKEN in hooks.env, minted via /tokens) or the
# server-side localhost bypass (MEMORY_AUTH_LOCALHOST_BYPASS=1 +
# MEMORY_AUTH_LOCALHOST_USER=<name> in the server's .env). Blank
# token means no Authorization header — relies on the bypass.
_MCP_TOKEN = os.environ.get("MEMORY_MCP_TOKEN", "").strip()
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}
if _MCP_TOKEN:
    _HEADERS["Authorization"] = f"Bearer {_MCP_TOKEN}"


def _normalize_affirmative(prompt: str) -> str:
    """Lower-case, strip whitespace + trailing punctuation. Used to match
    against AFFIRMATIVE_SET. We trim ., !, ?, … so "Yes!" still matches."""
    return prompt.strip().lower().rstrip(".!?…")


def _should_walk_back(prompt: str) -> bool:
    """Return True when the current prompt carries no topical signal.

    Two triggers:
      - Length below MIN_PROMPT_CHARS (already skipped pre-fix, but now
        we try to recover signal instead of dropping the search).
      - Verbatim match against AFFIRMATIVE_SET (catches "please proceed"
        at any length).
    """
    if len(prompt) < MIN_PROMPT_CHARS:
        return True
    return _normalize_affirmative(prompt) in AFFIRMATIVE_SET


def _extract_prior_assistant_text(transcript_path: str) -> str:
    """Return the text content of the most recent assistant entry in the
    transcript, or "" when none is available.

    Gracefully returns "" for any failure (missing path, unreadable,
    no assistant turns) so the caller can fall back to the skip path.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return ""

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = entry.get("role")
        if not role:
            msg = entry.get("message")
            if isinstance(msg, dict):
                role = msg.get("role")
        if role != "assistant":
            continue
        content = entry.get("content")
        if content is None:
            msg = entry.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
        text = _stringify_content(content)
        if text:
            return text[:WALKBACK_PRIOR_MAX_CHARS]
    return ""


def _stringify_content(content) -> str:
    """Flatten transcript content into plain text.

    Transcripts mix two shapes: plain strings and lists of typed blocks.
    For walk-back we only need the text blocks — tool calls don't help
    the embedding model anchor the topic.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(p for p in parts if p)


def _search_memories(query: str, project: str) -> list:
    """Query the MCP memory server via HTTP. Returns list of memory dicts.

    The `project` arg must be the Claude Code session's cwd (the
    project directory where this hook is firing). The server uses it to
    resolve the local project row and expand to (local + globals +
    declared domains) — anything outside that set gets filtered out
    server-side. Passing "" instead opts out of scope filtering, which
    is how cross-project memories used to bleed in.
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "memory_search",
            "arguments": {
                "query": query,
                "limit": SEARCH_LIMIT,
                "project": project,
            },
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        MCP_URL,
        data=payload,
        headers=dict(_HEADERS),
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"Prompt hook: server unreachable ({exc})", file=sys.stderr)
        return []

    result = body.get("result", {})
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                inner = json.loads(block["text"])
                return inner.get("results", [])
            except (json.JSONDecodeError, KeyError) as exc:
                print(f"Prompt hook: bad response format ({exc})", file=sys.stderr)

    return []


def _filter_relevant(results: list) -> list:
    """Apply similarity thresholds with a lower floor for load-bearing memories."""
    filtered = []
    for mem in results:
        similarity = mem.get("similarity", 0)
        importance = mem.get("importance", "normal")
        if similarity >= SIMILARITY_THRESHOLD:
            filtered.append(mem)
        elif importance in ("high", "critical") and similarity >= HIGH_IMPORTANCE_THRESHOLD:
            filtered.append(mem)
    return filtered[:MAX_MEMORIES]


def _format_memories(memories: list) -> str:
    """Format memories into a concise context block.

    Plain `From memory:` header + one bullet per item. No importance,
    scope, similarity, or tag metadata — small local models read those
    prefixes as noise. Metadata stays available server-side for
    retrieval and curation; the consumer only sees the content.
    """
    lines = ["From memory:"]
    for mem in memories:
        content = mem.get("content", "")
        lines.append(f"- {content}")
    return "\n".join(lines)


def _log_execution(
    prompt_len: int,
    results_total: int,
    results_surfaced: int,
    top_similarity: float,
) -> None:
    """Append a telemetry row. Failures are swallowed."""
    try:
        parent = os.path.dirname(LOG_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as logf:
            logf.write(
                f"{datetime.datetime.now().isoformat(timespec='seconds')}\t"
                f"prompt_len={prompt_len}\t"
                f"hits={results_surfaced}/{results_total}\t"
                f"top={top_similarity:.3f}\n"
            )
    except OSError:
        pass


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(1)

    prompt = hook_input.get("prompt", "").strip()

    # Walk-back: when the current prompt is too short or a verbatim
    # affirmative ("yes", "please proceed"), the prior assistant turn
    # carries the topical signal. Fall back to it so memory_search has
    # something meaningful to embed. If no transcript is available,
    # preserve the original skip behavior.
    if _should_walk_back(prompt):
        prior = _extract_prior_assistant_text(hook_input.get("transcript_path", ""))
        if not prior:
            _log_execution(len(prompt), 0, 0, 0.0)
            sys.exit(0)
        query = (prior + "\n\n" + prompt)[:MAX_QUERY_CHARS]
    else:
        query = prompt[:MAX_QUERY_CHARS]

    # Scope the search to the current project. Claude Code ships a
    # `cwd` field in the hook stdin; fall back to process cwd if the
    # field is ever missing. Passing the path (not "") lets the server
    # expand it to the declared visible set — local + globals + any
    # domains this project has declared — so cross-project "local"
    # memories stay contained.
    project_cwd = hook_input.get("cwd") or os.getcwd()

    results = _search_memories(query, project_cwd)
    relevant = _filter_relevant(results)

    top_similarity = max(
        (m.get("similarity", 0) for m in results),
        default=0.0,
    )
    _log_execution(len(prompt), len(results), len(relevant), top_similarity)

    if not relevant:
        sys.exit(0)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _format_memories(relevant),
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
