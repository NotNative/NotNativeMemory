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

# Bundle-local helpers under _internal/.
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HOOK_DIR)
from _internal.env_loader import load_hooks_env  # noqa: E402

load_hooks_env(__file__)

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

# Subject extraction for memory_fact_query. The injection hook fires
# fact_query in parallel with memory_search so the model gets "current
# state" alongside semantic background. Facts are keyed by subject,
# which means we need a small candidate list pulled from the prompt
# text rather than a free-text query.
FACT_QUERY_MAX_SUBJECTS = int(os.environ.get("MEMORY_PROMPT_FACT_MAX_SUBJECTS", "5"))
FACT_QUERY_MIN_TOKEN_LEN = 3

# Stopwords filtered out of subject candidates. Kept short by design;
# the goal is to drop the worst noise, not to be a real NLP pipeline.
# Anything that survives this list is a candidate; the fact_query call
# itself is cheap, so over-extraction is fine.
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
    # NNM-specific noise that surfaces too often to be useful as a subject
    "memory", "memories", "user", "system", "prompt", "task", "tool",
})

TIMEOUT_SECONDS = 5

LOG_PATH = os.environ.get(
    "MEMORY_PROMPT_LOG",
    os.path.expanduser("~/.claude/memory_prompt_hook.log"),
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

    UserPromptSubmit fires before the new prompt is appended to the
    transcript on disk in some harnesses, and after in others. Either
    way the LAST assistant entry on file is "what was just said by the
    assistant before the user replied" — exactly the topical anchor
    walk-back needs.

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

    Transcripts mix two shapes: plain strings and lists of typed blocks
    ({type: text, text: ...} interleaved with tool_use / tool_result).
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
    """Pull up to FACT_QUERY_MAX_SUBJECTS candidate subjects from text.

    Heuristic: tokenize on non-word chars, lowercase, drop stopwords and
    short tokens, dedupe preserving order, cap at the configured max.
    Hyphenated tokens ("inference-host", "lm-studio") are preserved as
    a single subject candidate, because fact subjects often look like that.
    """
    if not text:
        return []
    # Split on whitespace + punctuation but keep hyphens inside tokens
    # so "inference-host" survives as one candidate.
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
    """Single memory_fact_query call. Returns the list of fact dicts or [].

    Missing/error responses are silently treated as empty so a partial
    failure in one subject doesn't poison the whole injection.
    """
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
    """Run memory_fact_query for each subject candidate extracted from
    the query text. Returns a deduped list of fact dicts.

    Dedupe key is (subject, predicate) — for any subject/predicate pair
    only the most recent fact is current (valid_to NULL), so duplicates
    here only happen when multiple subjects share a predicate, which is
    a legitimate signal worth keeping.
    """
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
    """Format facts into a 'Current state:' block.

    Plain subject — predicate: object lines, one per fact. The framing
    deliberately differs from semantic memories: facts are present-tense
    declarative state, memories are narrative context.
    """
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
# the injection by class gives the model a stronger prior: rules are
# constraints, preferences are user-stated choices, plain memories
# are background that may be stale. Pre-fix everything landed in one
# undifferentiated "From memory:" bullet list.
_CLASS_HEADERS = (
    ("rule", "Standing rules:"),
    ("preference", "User preferences:"),
    ("memory", "Background context (may be stale):"),
)


def _bucket_memories_by_class(memories: list) -> dict:
    """Group memories into class buckets, preserving order within each.

    Memories with class=None or an unknown class drop into the 'memory'
    bucket so they still surface — losing them silently would be worse
    than mis-labeling them as background.
    """
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
    → background. Empty buckets are skipped. The fixed order matters:
    rules are non-negotiable, preferences shape style, background is
    optional reading. The model reads top-to-bottom; the most binding
    constraints come first.
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
        # Combine prior assistant text with the current prompt. Order
        # matters: prior first carries the topic; the affirmative tail
        # keeps the user's actual reply visible to the embedding.
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

    # Fire memory_search (semantic context) and memory_fact_query
    # (current state) against the same query basis. Facts answer
    # "what is true right now"; memories answer "what's relevant
    # background to this topic". Both belong in the injection.
    results = _search_memories(query, project_cwd)
    relevant = _filter_relevant(results)
    facts = _query_facts(query, project_cwd)

    top_similarity = max(
        (m.get("similarity", 0) for m in results),
        default=0.0,
    )
    _log_execution(len(prompt), len(results), len(relevant), top_similarity)

    blocks = []
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
