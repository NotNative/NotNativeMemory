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
import re
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

MCP_URL = os.environ.get("MEMORY_MCP_URL", "http://127.0.0.1:9500/mcp")

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
# signal but the recent conversation does. Walk back to recent turns so
# memory_search has something meaningful to embed.
AFFIRMATIVE_SET = frozenset({
    "yes", "y", "yep", "yeah", "yup",
    "ok", "okay", "k",
    "proceed", "go", "go ahead", "continue", "keep going",
    "keep working", "carry on",
    "sure", "do it", "sounds good", "good", "fine", "agreed",
    "nice", "sweet", "wonderful", "perfect",
    "next", "more",
})

# Pronoun-heavy follow-ups are also low-signal because "this/that/it" only
# resolves against recent turns.
PRONOUN_FOLLOWUP_SET = frozenset({
    "this", "that", "it", "same", "same thing", "that one", "this one",
    "the first", "the second", "the latter", "the former",
})

# Truncate recent-turn text used as walk-back basis. The point is to give the
# embedding model a topical anchor, not to ship the entire transcript into the
# query.
WALKBACK_PRIOR_MAX_CHARS = 600
RECENT_CHUNK_LIMIT = int(os.environ.get("MEMORY_PROMPT_RECENT_CHUNKS", "12"))
RECENT_QUERY_MAX_CHARS = int(os.environ.get("MEMORY_PROMPT_RECENT_QUERY_CHARS", "1200"))
RECENT_SOURCE_EVENTS = [
    "user.prompt.submit",
    "turn.post",
    "tool.call.post",
]

# Subject extraction for memory_fact_query. The injection hook fires
# fact_query in parallel with memory_search so the model gets "current
# state" alongside semantic background. Facts are keyed by subject,
# which means we need a small candidate list pulled from the prompt
# text rather than a free-text query.
FACT_QUERY_MAX_SUBJECTS = int(os.environ.get("MEMORY_PROMPT_FACT_MAX_SUBJECTS", "5"))
FACT_QUERY_MIN_TOKEN_LEN = 3

_STOPWORDS = frozenset({
    "the", "and", "but", "for", "are", "was", "were", "what", "when", "where",
    "why", "how", "this", "that", "with", "from", "into", "about", "would",
    "could", "should", "have", "has", "had", "you", "your", "yours", "they",
    "them", "their", "ours", "let", "lets", "please", "thank", "thanks",
    "want", "need", "needs", "needed", "make", "made", "use", "used", "using",
    "can", "will", "wont", "won", "don", "doesnt", "doesn", "didn", "didnt",
    "ive", "isnt", "isn", "arent", "aren", "wasnt", "wasn",
    "all", "any", "some", "one", "two", "three", "four", "five",
    "got", "get", "gets", "getting",
    "now", "still", "also", "just", "only", "very", "much", "more", "less",
    "okay", "yes", "yep", "yeah", "sure", "proceed", "continue", "next",
    "going", "goes", "went",
    "memory", "memories", "user", "system", "prompt", "task", "tool",
})

TIMEOUT_SECONDS = 5

LOG_PATH = os.environ.get(
    "MEMORY_PROMPT_LOG",
    os.path.expanduser("~/.nna/memory_prompt_hook.log"),
)

_STATE_DIR = os.path.expanduser("~/.nna/state")
_LAST_REMINDED_FILE = os.path.join(_STATE_DIR, "last_reminded_session")

# The deferred-tools reminder used to be the entire point of session_start.py
# (now deleted). Folded in here so it lands on the first user prompt of each
# session — same effective injection point, one fewer dead hook. Gated by
# session_id so it doesn't spam every turn.
_TOOL_LOAD_REMINDER = (
    "[Session Start] Memory MCP tools are deferred by the harness. "
    "Call ToolSearch with "
    "`select:memory_store,memory_search,memory_list,memory_forget,"
    "memory_context,memory_fact_add,memory_fact_query,"
    "memory_project_configure` before trying to use them."
)


def _session_once_reminder(session_id: str) -> str:
    """Return the deferred-tools reminder once per session, "" thereafter.

    Marker file stores the most recently reminded session_id. When the
    incoming session differs (or marker is unreadable / session_id is
    missing) we emit the reminder and refresh the marker. Fail-open so a
    broken state dir never silently drops the load-bearing reminder.
    """
    if not session_id:
        return _TOOL_LOAD_REMINDER

    try:
        with open(_LAST_REMINDED_FILE, "r", encoding="utf-8") as fh:
            if fh.read().strip() == session_id:
                return ""
    except OSError:
        pass

    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_LAST_REMINDED_FILE, "w", encoding="utf-8") as fh:
            fh.write(session_id)
    except OSError:
        pass

    return _TOOL_LOAD_REMINDER


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
_MCP_TOKEN = (
    os.environ.get("MEMORY_MCP_TOKEN", "").strip()
    or os.environ.get("NNM_AUTH_TOKEN", "").strip()
)
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}
if _MCP_TOKEN:
    _HEADERS["Authorization"] = f"Bearer {_MCP_TOKEN}"


def _normalize_affirmative(prompt: str) -> str:
    """Lower-case, strip whitespace + trailing punctuation. Used to match
    against AFFIRMATIVE_SET. We trim ., !, ?, … so "Yes!" still matches."""
    return prompt.strip().lower().strip("\"'` ").rstrip(".!?…")


def _has_strong_signal(prompt: str) -> bool:
    """Return True when the prompt is specific enough to search directly."""
    if not prompt:
        return False
    if re.search(r"([A-Za-z]:[\\/]|[/\\][\w.-]+|\.tsx?\b|\.py\b|\.ps1\b)", prompt):
        return True
    if re.search(r"(/[A-Za-z][\w-]*|[A-Z]{2,}|\d{3,}|error|exception|failed)", prompt, re.I):
        return True
    tokens = [
        t.lower()
        for t in re.findall(r"[A-Za-z][A-Za-z0-9\-_.]*", prompt)
        if t.lower() not in _STOPWORDS
    ]
    return len(tokens) >= 3


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
    normalized = _normalize_affirmative(prompt)
    if normalized in AFFIRMATIVE_SET or normalized in PRONOUN_FOLLOWUP_SET:
        return True
    return not _has_strong_signal(prompt)


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


def _extract_subject_candidates(text: str) -> list:
    """Pull up to FACT_QUERY_MAX_SUBJECTS candidate subjects from text."""
    if not text:
        return []
    raw_tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-_.]*", text)
    seen = set()
    candidates = []
    for tok in raw_tokens:
        norm = tok.strip("-_.").lower()
        if len(norm) < FACT_QUERY_MIN_TOKEN_LEN:
            continue
        if norm in _STOPWORDS:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        candidates.append(norm)
        if len(candidates) >= FACT_QUERY_MAX_SUBJECTS:
            break
    return candidates


def _query_facts_for_subject(subject: str, project: str) -> list:
    """Single memory_fact_query call. Returns list of fact dicts or []."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "memory_fact_query",
            "arguments": {
                "subject": subject,
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
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []

    for block in body.get("result", {}).get("content", []):
        if block.get("type") == "text":
            try:
                inner = json.loads(block["text"])
                return inner.get("facts", []) or []
            except (json.JSONDecodeError, KeyError):
                return []
    return []


def _query_facts(query: str, project: str) -> list:
    """Run memory_fact_query for each subject candidate. Returns a deduped
    list of fact dicts. Dedupe key is (subject, predicate)."""
    subjects = _extract_subject_candidates(query)
    if not subjects:
        return []

    seen = set()
    facts = []
    for subject in subjects:
        for fact in _query_facts_for_subject(subject, project):
            key = (fact.get("subject"), fact.get("predicate"))
            if key in seen:
                continue
            seen.add(key)
            facts.append(fact)
    return facts


def _format_facts(facts: list) -> str:
    """Format facts into a 'Current state:' block."""
    if not facts:
        return ""
    lines = ["Current state:"]
    for fact in facts:
        subject = fact.get("subject", "")
        predicate = fact.get("predicate", "")
        obj = fact.get("object", "")
        if not (subject and predicate and obj):
            continue
        lines.append(f"- {subject} — {predicate}: {obj}")
    return "\n".join(lines) if len(lines) > 1 else ""


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


def _fetch_recent_verbatim(session_id: str, project: str) -> list:
    """Return latest session chunks from NNM, or [] when unavailable."""
    if not session_id:
        return []
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "verbatim_recent",
            "arguments": {
                "session_id": session_id,
                "limit": RECENT_CHUNK_LIMIT,
                "project": project,
                "source_events": RECENT_SOURCE_EVENTS,
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
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []

    result = body.get("result", {})
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                inner = json.loads(block["text"])
                return inner.get("results", []) or []
            except (json.JSONDecodeError, KeyError):
                return []
    return []


def _build_recent_query(chunks: list, prompt: str) -> str:
    """Build a compact memory-search query from recent verbatim chunks."""
    lines = ["Current session context for memory retrieval:"]
    if prompt:
        lines.append(f"Latest user prompt: {prompt}")
    for chunk in chunks:
        content = str(chunk.get("content", "")).strip()
        if not content:
            continue
        source = chunk.get("source_event") or "verbatim"
        topic = chunk.get("topic") or "general"
        error = " error" if chunk.get("is_error") else ""
        snippet = re.sub(r"\s+", " ", content)[:WALKBACK_PRIOR_MAX_CHARS]
        lines.append(f"- {source}/{topic}{error}: {snippet}")
    lines.append(
        "Need memories about prior decisions, user preferences, environment "
        "gotchas, previous fixes, and active project context.",
    )
    return "\n".join(lines)[:RECENT_QUERY_MAX_CHARS]


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


# Class-aware framing. Memories carry a "class" field set by the
# server's classify pipeline (rule / preference / memory). Bucketing
# the injection by class gives the model a stronger prior.
_CLASS_HEADERS = (
    ("rule", "Standing rules:"),
    ("preference", "User preferences:"),
    ("memory", "Background context (may be stale):"),
)


def _bucket_memories_by_class(memories: list) -> dict:
    """Group memories into class buckets, preserving order within each.
    Unknown/missing class falls through to the 'memory' bucket."""
    buckets = {key: [] for key, _ in _CLASS_HEADERS}
    for mem in memories:
        cls = mem.get("class")
        if cls not in buckets:
            cls = "memory"
        buckets[cls].append(mem)
    return buckets


def _format_memories(memories: list) -> str:
    """Format memories into class-aware sections.

    One section per non-empty bucket, in fixed order: rules → preferences
    → background. Empty buckets are skipped.
    """
    buckets = _bucket_memories_by_class(memories)
    sections = []
    for key, header in _CLASS_HEADERS:
        items = buckets[key]
        if not items:
            continue
        lines = [header]
        for mem in items:
            content = mem.get("content", "")
            lines.append(f"- {content}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _log_execution(
    prompt_len: int,
    results_total: int,
    results_surfaced: int,
    top_similarity: float,
    query_source: str = "prompt",
    query_len: int = 0,
    recent_chunks: int = 0,
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
                f"query_source={query_source}\t"
                f"query_len={query_len}\t"
                f"recent_chunks={recent_chunks}\t"
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
    project_cwd = hook_input.get("cwd") or os.getcwd()
    session_id = hook_input.get("session_id", "")

    # Walk-back: when the current prompt is too short or a verbatim
    # affirmative ("yes", "please proceed"), the prior assistant turn
    # carries the topical signal. Prefer NNM verbatim_recent so the
    # query can see the last few turns; fall back to local transcript if
    # the server/tool is unavailable. If neither exists, preserve the
    # original skip behavior.
    query_source = "prompt"
    recent_chunks = []
    if _should_walk_back(prompt):
        recent_chunks = _fetch_recent_verbatim(session_id, project_cwd)
        if recent_chunks:
            query = _build_recent_query(recent_chunks, prompt)
            query_source = "verbatim_recent"
        else:
            prior = _extract_prior_assistant_text(hook_input.get("transcript_path", ""))
            if not prior:
                _log_execution(
                    len(prompt), 0, 0, 0.0,
                    query_source="none",
                    query_len=0,
                    recent_chunks=0,
                )
                sys.exit(0)
            query = (prior + "\n\n" + prompt)[:MAX_QUERY_CHARS]
            query_source = "local_transcript"
    else:
        query = prompt[:MAX_QUERY_CHARS]

    # Scope the search to the current project. Claude Code ships a
    # `cwd` field in the hook stdin; fall back to process cwd if the
    # field is ever missing. Passing the path (not "") lets the server
    # expand it to the declared visible set — local + globals + any
    # domains this project has declared — so cross-project "local"
    # memories stay contained.
    # Fire memory_search (semantic context) and memory_fact_query
    # (current state) against the same query basis.
    results = _search_memories(query, project_cwd)
    relevant = _filter_relevant(results)
    facts = _query_facts(query, project_cwd)

    top_similarity = max(
        (m.get("similarity", 0) for m in results),
        default=0.0,
    )
    _log_execution(
        len(prompt),
        len(results),
        len(relevant),
        top_similarity,
        query_source=query_source,
        query_len=len(query),
        recent_chunks=len(recent_chunks),
    )

    blocks = []
    reminder = _session_once_reminder(session_id)
    if reminder:
        blocks.append(reminder)
    facts_block = _format_facts(facts)
    if facts_block:
        blocks.append(facts_block)
    if relevant:
        blocks.append(_format_memories(relevant))

    if not blocks:
        sys.exit(0)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(blocks),
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
