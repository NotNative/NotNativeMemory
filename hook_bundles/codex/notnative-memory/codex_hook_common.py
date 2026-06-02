"""
NotNativeMemory Codex hook helpers.

Codex has its own lifecycle hook contract. This module keeps the bundle's
plumbing local to Codex while leaving lifecycle behavior in the individual
hook scripts.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


PLUGIN_NAME = "notnative-memory"
AGENT_NAME = "codex"

DEFAULT_MCP_URL = "http://127.0.0.1:9500/mcp"
TIMEOUT_SECONDS = int(os.environ.get("MEMORY_HOOK_TIMEOUT", "8"))
SEARCH_LIMIT = int(os.environ.get("MEMORY_PROMPT_MAX_RESULTS", "3"))
SIMILARITY_THRESHOLD = float(os.environ.get("MEMORY_PROMPT_THRESHOLD", "0.45"))
HIGH_IMPORTANCE_THRESHOLD = float(os.environ.get("MEMORY_PROMPT_HIGH_THRESHOLD", "0.35"))
MIN_PROMPT_CHARS = int(os.environ.get("MEMORY_PROMPT_MIN_CHARS", "15"))
RECENT_CHUNK_LIMIT = int(os.environ.get("MEMORY_PROMPT_RECENT_CHUNKS", "12"))
RECENT_QUERY_MAX_CHARS = int(os.environ.get("MEMORY_PROMPT_RECENT_QUERY_CHARS", "1200"))
MAX_QUERY_CHARS = int(os.environ.get("MEMORY_PROMPT_MAX_QUERY_CHARS", "1200"))
MAX_CONTEXT_CHARS = int(os.environ.get("MEMORY_PROMPT_MAX_CONTEXT_CHARS", "3500"))

CHUNK_CHARS = int(os.environ.get("MEMORY_VERBATIM_CHUNK_CHARS", "800"))
OVERLAP_CHARS = int(os.environ.get("MEMORY_VERBATIM_OVERLAP_CHARS", "200"))
FLOOR_CHARS = int(os.environ.get("MEMORY_VERBATIM_FLOOR_CHARS", "30"))
TOOL_VALUE_CHARS = int(os.environ.get("MEMORY_VERBATIM_TOOL_VALUE_CHARS", "4000"))

RECENT_SOURCE_EVENTS = [
    "user.prompt.submit",
    "turn.post",
    "tool.call.post",
]

AFFIRMATIVE_SET = frozenset({
    "yes", "y", "yep", "yeah", "yup",
    "ok", "okay", "k",
    "proceed", "go", "go ahead", "continue", "keep going",
    "keep working", "carry on",
    "sure", "do it", "sounds good", "good", "fine", "agreed",
    "nice", "sweet", "wonderful", "perfect",
    "next", "more",
})

PRONOUN_FOLLOWUP_SET = frozenset({
    "this", "that", "it", "same", "same thing", "that one", "this one",
    "the first", "the second", "the latter", "the former",
})

STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can",
    "for", "from", "had", "has", "have", "how", "i", "if", "in",
    "is", "it", "me", "my", "of", "on", "or", "our", "please",
    "that", "the", "their", "them", "this", "to", "was", "we",
    "what", "when", "where", "which", "who", "why", "with", "you",
})

TOPIC_KEYWORDS = [
    ("debugging", ["error", "exception", "traceback", "stack trace", "fail", "bug"]),
    ("decisions", ["decided", "going with", "chose", "settled on", "we will"]),
    ("architecture", ["architecture", "schema", "interface", "contract", "boundary"]),
    ("planning", ["plan", "roadmap", "phase", "milestone", "next step", "tasks"]),
    ("refactor", ["refactor", "rename", "delete", "remove", "deprecate", "cleanup"]),
    ("testing", ["test", "spec", "fixture", "mock", "regression", "assertion"]),
    ("docs", ["documentation", "readme", "docstring", "write up"]),
    ("performance", ["latency", "slow", "fast", "performance", "throughput"]),
    ("security", ["secret", "token", "credential", "auth", "permission"]),
]


def hook_dir() -> Path:
    return Path(__file__).resolve().parent


def load_hooks_env() -> None:
    """Load KEY=VALUE lines from hooks.env beside the installed hook files."""
    env_path = hook_dir() / "hooks.env"
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


load_hooks_env()


def read_payload() -> Dict[str, Any]:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


def write_additional_context(context: str, event_name: str) -> None:
    if not context:
        return
    if os.environ.get("MEMORY_HOOK_OUTPUT_MODE", "").lower() == "plain":
        print(context[:MAX_CONTEXT_CHARS])
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context[:MAX_CONTEXT_CHARS],
        },
    }))


def diagnostic_context(event_name: str, message: str) -> None:
    """Emit a tiny Codex-visible diagnostic when explicitly enabled."""
    if os.environ.get("MEMORY_HOOK_DIAGNOSTIC", "").lower() not in {"1", "true", "yes"}:
        return
    write_additional_context(
        f"NNM_CODEX_HOOK_TEST: {event_name} hook executed. {message}",
        event_name,
    )


def mcp_url() -> str:
    return os.environ.get("MEMORY_MCP_URL", DEFAULT_MCP_URL).strip() or DEFAULT_MCP_URL


def mcp_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = (
        os.environ.get("MEMORY_MCP_TOKEN", "").strip()
        or os.environ.get("NNM_AUTH_TOKEN", "").strip()
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def mcp_tool(name: str, arguments: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            mcp_url(),
            data=payload,
            headers=mcp_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return {"_nnm_error": True}
    if "error" in body:
        return {"_nnm_error": True}
    for block in body.get("result", {}).get("content", []):
        if block.get("type") != "text":
            continue
        try:
            return json.loads(block.get("text", "{}"))
        except json.JSONDecodeError:
            return {"_nnm_error": True}
    return {}


def project_from(payload: Dict[str, Any]) -> str:
    return str(payload.get("cwd") or os.getcwd())


def session_from(payload: Dict[str, Any]) -> str:
    sid = str(payload.get("session_id") or "").strip()
    return sid or "codex-session-unknown"


def prompt_from(payload: Dict[str, Any]) -> str:
    for key in ("prompt", "user_prompt", "input"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def normalize_prompt(prompt: str) -> str:
    return prompt.strip().lower().strip("\"'` ").rstrip(".!?...")


def has_strong_signal(prompt: str) -> bool:
    if not prompt:
        return False
    if re.search(r"([A-Za-z]:[\\/]|[/\\][\w.-]+|\.[A-Za-z0-9]{1,8}\b)", prompt):
        return True
    if re.search(r"(/[A-Za-z][\w-]*|[A-Z]{2,}|\d{3,}|error|exception|failed)", prompt, re.I):
        return True
    tokens = [
        t.lower()
        for t in re.findall(r"[A-Za-z][A-Za-z0-9\-_.]*", prompt)
        if t.lower() not in STOPWORDS
    ]
    return len(tokens) >= 3


def should_walk_back(prompt: str) -> bool:
    if len(prompt) < MIN_PROMPT_CHARS:
        return True
    normalized = normalize_prompt(prompt)
    if normalized in AFFIRMATIVE_SET or normalized in PRONOUN_FOLLOWUP_SET:
        return True
    return not has_strong_signal(prompt)


def recent_chunks(session_id: str, project: str) -> List[Dict[str, Any]]:
    if not session_id:
        return []
    result = mcp_tool("verbatim_recent", {
        "session_id": session_id,
        "limit": RECENT_CHUNK_LIMIT,
        "project": project,
        "source_events": RECENT_SOURCE_EVENTS,
    })
    rows = result.get("results")
    return rows if isinstance(rows, list) else []


def build_recent_query(chunks: Iterable[Dict[str, Any]], prompt: str) -> str:
    lines = ["Current Codex session context for memory retrieval:"]
    if prompt:
        lines.append(f"Latest user prompt: {prompt}")
    for chunk in chunks:
        content = str(chunk.get("content", "")).strip()
        if not content:
            continue
        source = chunk.get("source_event") or "verbatim"
        topic = chunk.get("topic") or "general"
        snippet = re.sub(r"\s+", " ", content)[:600]
        lines.append(f"- {source}/{topic}: {snippet}")
    lines.append(
        "Need memories about prior decisions, user preferences, environment "
        "gotchas, previous fixes, and active project context."
    )
    return "\n".join(lines)[:RECENT_QUERY_MAX_CHARS]


def memory_search(query: str, project: str) -> List[Dict[str, Any]]:
    result = mcp_tool("memory_search", {
        "query": query[:MAX_QUERY_CHARS],
        "limit": SEARCH_LIMIT,
        "project": project,
    })
    rows = result.get("results")
    return rows if isinstance(rows, list) else []


def memory_facts(query: str, project: str) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    seen = set()
    for subject in subject_candidates(query):
        result = mcp_tool("memory_fact_query", {"subject": subject, "project": project})
        for fact in result.get("facts", []) or []:
            key = (fact.get("subject"), fact.get("predicate"))
            if key in seen:
                continue
            seen.add(key)
            facts.append(fact)
    return facts


def subject_candidates(text: str, limit: int = 4) -> List[str]:
    out: List[str] = []
    seen = set()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9\-_.]*", text or ""):
        norm = token.strip("-_.").lower()
        if len(norm) < 4 or norm in STOPWORDS or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
        if len(out) >= limit:
            break
    return out


def filter_relevant(results: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for mem in results:
        similarity = float(mem.get("similarity") or 0)
        importance = mem.get("importance", "normal")
        if similarity >= SIMILARITY_THRESHOLD:
            out.append(mem)
        elif importance in ("high", "critical") and similarity >= HIGH_IMPORTANCE_THRESHOLD:
            out.append(mem)
    return out[:SEARCH_LIMIT]


def format_memory_context(memories: Iterable[Dict[str, Any]], facts: Iterable[Dict[str, Any]]) -> str:
    parts: List[str] = []
    fact_lines = ["Current state:"]
    for fact in facts:
        subject = fact.get("subject", "")
        predicate = fact.get("predicate", "")
        obj = fact.get("object", "")
        if subject and predicate and obj:
            fact_lines.append(f"- {subject} - {predicate}: {obj}")
    if len(fact_lines) > 1:
        parts.append("\n".join(fact_lines))

    memory_lines = ["From memory:"]
    for mem in memories:
        content = str(mem.get("content", "")).strip()
        if content:
            memory_lines.append(f"- {content}")
    if len(memory_lines) > 1:
        parts.append("\n".join(memory_lines))
    return "\n\n".join(parts)[:MAX_CONTEXT_CHARS]


def memory_context(project: str) -> str:
    result = mcp_tool("memory_context", {
        "project": project,
        "max_tokens": int(os.environ.get("MEMORY_SESSION_MAX_TOKENS", "500")),
    })
    memories = result.get("memories") or result.get("results") or []
    if not isinstance(memories, list):
        return ""
    return format_memory_context(memories[:SEARCH_LIMIT], [])


def infer_topic(text: str) -> str:
    haystack = (text or "").lower()
    for topic, needles in TOPIC_KEYWORDS:
        if any(needle in haystack for needle in needles):
            return topic
    return "general"


def chunk_content(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= CHUNK_CHARS:
        return [text]
    if OVERLAP_CHARS >= CHUNK_CHARS:
        overlap = CHUNK_CHARS // 4
    else:
        overlap = OVERLAP_CHARS
    stride = CHUNK_CHARS - overlap
    out: List[str] = []
    start = 0
    while start < len(text):
        piece = text[start:start + CHUNK_CHARS].strip()
        if not piece:
            break
        if out and len(piece) < FLOOR_CHARS:
            break
        out.append(piece)
        if start + CHUNK_CHARS >= len(text):
            break
        start += stride
    return out


def counter_dir() -> Path:
    return Path.home() / ".codex" / "hooks" / PLUGIN_NAME / "verbatim-counters"


def next_chunk_index(session_id: str) -> int:
    path = counter_dir() / f"{session_id.replace('/', '_').replace(chr(92), '_')}.json"
    current = -1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            current = int(json.loads(path.read_text(encoding="utf-8")).get("last_index", -1))
        nxt = current + 1
        path.write_text(json.dumps({"last_index": nxt}), encoding="utf-8")
        return nxt
    except (OSError, ValueError, json.JSONDecodeError):
        return current + 1


def capture_content(
    *,
    content: str,
    session_id: str,
    project: str,
    source_event: str,
    agent: str,
    topic: Optional[str] = None,
    is_error: bool = False,
) -> int:
    stored = 0
    for piece in chunk_content(content):
        args = {
            "content": piece,
            "session_id": session_id,
            "chunk_index": next_chunk_index(session_id),
            "source_event": source_event,
            "topic": topic or infer_topic(piece),
            "agent": agent,
            "is_error": is_error,
            "project": project,
        }
        if not mcp_tool("verbatim_capture", args, timeout=TIMEOUT_SECONDS).get("_nnm_error"):
            stored += 1
    return stored


def stringify(value: Any, limit: int = TOOL_VALUE_CHARS) -> str:
    if isinstance(value, str):
        return value[:limit]
    try:
        return json.dumps(value, default=str)[:limit]
    except (TypeError, ValueError):
        return str(value)[:limit]


def transcript_tail_text(path: str, max_chars: int = 4000) -> str:
    if not path:
        return ""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    out: List[str] = []
    for line in reversed(lines[-50:]):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = stringify(entry, 1000)
        if text:
            out.append(text)
        if sum(len(x) for x in out) >= max_chars:
            break
    return "\n".join(reversed(out))[:max_chars]
