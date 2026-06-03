"""
Verbatim capture core (shared across nna + claude hook bundles).

Drives the per-event capture of transcript chunks into NotNativeMemory's
`verbatim_chunks` table via the `verbatim_capture` MCP tool. Replaces the
v1 JSONL-on-disk path that lived in NNA's `src/services/verbatim/writer.ts`.

Responsibilities:
  - Resolve session_id + per-session monotonic chunk_index.
  - Chunk over-long content into 800-char windows with 200-char overlap.
  - Infer a coarse topic via keyword match (MemPalace-style).
  - POST `verbatim_capture` to the NNM MCP server.

The MCP server, project, and auth header are read from env on every call
so the bundle never holds long-lived state. The chunk_index counter is
the only persistent state — kept under ~/.nna/state/verbatim-counters/.

Errors are swallowed by callers. A failed verbatim write must never
affect agent behaviour; the writer is a passive observer.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# -- Chunking ---------------------------------------------------------------

DEFAULT_CHUNK_CHARS = 800
DEFAULT_OVERLAP_CHARS = 200
DEFAULT_FLOOR_CHARS = 30
DEFAULT_MCP_TIMEOUT_SECONDS = 20


def chunk_content(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_OVERLAP_CHARS,
    floor: int = DEFAULT_FLOOR_CHARS,
) -> List[str]:
    """
    Split text into overlapping windows. A chunk shorter than `floor` is
    dropped to avoid one-char trailing windows. The first chunk is always
    emitted even if it is below the floor (so a 5-char turn still gets
    captured).
    """
    if text is None:
        return []
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    if overlap >= chunk_size:
        # Defensive: degenerate config would make stride <= 0.
        overlap = chunk_size // 4
    stride = chunk_size - overlap
    out: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        piece = text[start:end].strip()
        if not piece:
            break
        if out and len(piece) < floor:
            # Tiny tail; let the previous chunk's overlap cover it.
            break
        out.append(piece)
        if end == len(text):
            break
        start += stride
    return out


# -- Topic inference --------------------------------------------------------

# Minimal keyword table inspired by MemPalace's TOPIC_KEYWORDS. Kept small
# and editable. First-match wins; otherwise "general".
_TOPIC_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("debugging", ["error", "exception", "traceback", "stack trace", "fail",
                    "broken", "regress", "bug", "panic"]),
    ("decisions", ["decided", "going with", "chose", "settled on",
                    "we will", "let's go with", "the call is"]),
    ("architecture", ["architecture", "schema", "interface",
                       "contract", "boundary"]),
    ("planning", ["plan", "roadmap", "phase", "milestone", "next step",
                   "tasks", "agenda"]),
    ("refactor", ["refactor", "rename", "rip", "delete", "remove",
                   "deprecate", "migrate", "cleanup"]),
    ("testing", ["test", "spec", "fixture", "snapshot", "mock",
                  "regression", "assertion"]),
    ("docs", ["documentation", "readme", "comment", "docstring",
               "explain", "write up"]),
    ("performance", ["latency", "slow", "fast", "performance", "throughput",
                      "memory leak", "cpu"]),
    ("security", ["secret", "token", "credential", "auth", "rls",
                   "permission", "vulnerability"]),
]


def infer_topic(text: str) -> str:
    """First-match keyword topic inference. Returns 'general' by default."""
    if not text:
        return "general"
    haystack = text.lower()
    for topic, needles in _TOPIC_KEYWORDS:
        for needle in needles:
            if needle in haystack:
                return topic
    return "general"


# -- Chunk-index counter ----------------------------------------------------

def _counter_dir() -> Path:
    base = os.environ.get("NNA_STATE_DIR", "").strip()
    if base:
        root = Path(base)
    else:
        root = Path.home() / ".nna" / "state"
    return root / "verbatim-counters"


def _counter_path(session_id: str) -> Path:
    # Filename is the raw session_id; sessions are UUID-ish so no escaping
    # is needed in practice, but slashes get folded just in case.
    safe = session_id.replace("/", "_").replace("\\", "_")
    return _counter_dir() / f"{safe}.json"


def next_chunk_index(session_id: str) -> int:
    """
    Return the next monotonic chunk_index for a session and persist the
    advance to disk. First call for a session returns 0.

    Idempotency is owned by NNM's UNIQUE(owner_user_id, session_id,
    chunk_index) — if the same index is sent twice the second insert is a
    no-op. So this counter just needs to be monotonic per process, not
    globally unique across crashes.
    """
    path = _counter_path(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fall through; the open below will raise and we swallow.
        pass

    current = -1
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            current = int(data.get("last_index", -1))
        except (OSError, ValueError, json.JSONDecodeError):
            current = -1

    nxt = current + 1
    try:
        with path.open("w", encoding="utf-8") as fh:
            json.dump({"last_index": nxt}, fh)
    except OSError:
        pass
    return nxt


# -- MCP call ---------------------------------------------------------------

def _mcp_endpoint() -> str:
    return os.environ.get("MEMORY_MCP_URL", "http://127.0.0.1:9500/mcp")


def _mcp_headers() -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    token = (
        os.environ.get("MEMORY_MCP_TOKEN", "").strip()
        or os.environ.get("NNM_AUTH_TOKEN", "").strip()
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _mcp_timeout_seconds() -> int:
    raw = (
        os.environ.get("NNA_VERBATIM_CAPTURE_TIMEOUT_SECONDS", "").strip()
        or os.environ.get("MEMORY_VERBATIM_CAPTURE_TIMEOUT_SECONDS", "").strip()
    )
    if not raw:
        return DEFAULT_MCP_TIMEOUT_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MCP_TIMEOUT_SECONDS


def _project() -> str:
    """Resolve the NNM project key for this capture.

    Order of precedence:
      1. MEMORY_VERBATIM_PROJECT  (explicit override per bundle)
      2. MEMORY_EXTRACT_PROJECT   (shared with turn_analysis)
      3. NNA_CWD                  (NNA-supplied working directory)
      4. cwd of the hook process
    """
    for key in ("MEMORY_VERBATIM_PROJECT", "MEMORY_EXTRACT_PROJECT", "NNA_CWD"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return os.getcwd()


def _post_verbatim_capture(
    *,
    content: str,
    session_id: str,
    chunk_index: int,
    source_event: str,
    topic: Optional[str],
    agent: Optional[str],
    is_error: bool,
    loaded_skills: Optional[List[str]],
    mission_id: Optional[str],
    mission_type: Optional[str],
    timeout: int,
) -> bool:
    """JSON-RPC POST `verbatim_capture`. Returns True on success."""
    args: Dict[str, Any] = {
        "content": content,
        "session_id": session_id,
        "chunk_index": chunk_index,
        "source_event": source_event,
        "is_error": is_error,
        "project": _project(),
    }
    if topic is not None:
        args["topic"] = topic
    if agent is not None:
        args["agent"] = agent
    if loaded_skills:
        args["loaded_skills"] = list(loaded_skills)
    if mission_id is not None:
        args["mission_id"] = mission_id
    if mission_type is not None:
        args["mission_type"] = mission_type

    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "verbatim_capture", "arguments": args},
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            _mcp_endpoint(),
            data=payload,
            headers=_mcp_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            envelope = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False

    # The MCP envelope wraps the tool result inside result.content[0].text
    # as a JSON string. We don't need to parse it — anything that came
    # back without an "error" key at the envelope level counts as
    # accepted by the server (idempotent stores return {stored: True,
    # inserted: False} which is success).
    return "error" not in envelope


# -- High-level entry points called by the adapter --------------------------

def capture_turn_pre(
    *,
    session_id: str,
    user_prompt: str,
    agent: str = "user",
    loaded_skills: Optional[List[str]] = None,
    mission_id: Optional[str] = None,
    mission_type: Optional[str] = None,
    timeout: Optional[int] = None,
) -> int:
    """Capture the user prompt at turn:pre. Returns number of chunks stored."""
    chunks = chunk_content(user_prompt)
    if not chunks:
        return 0
    topic = infer_topic(user_prompt)
    stored = 0
    for piece in chunks:
        idx = next_chunk_index(session_id)
        ok = _post_verbatim_capture(
            content=piece,
            session_id=session_id,
            chunk_index=idx,
            source_event="turn.pre",
            topic=topic,
            agent=agent,
            is_error=False,
            loaded_skills=loaded_skills,
            mission_id=mission_id,
            mission_type=mission_type,
            timeout=timeout if timeout is not None else _mcp_timeout_seconds(),
        )
        if ok:
            stored += 1
    return stored


def capture_turn_post(
    *,
    session_id: str,
    user_prompt: str,
    model_response: str,
    agent: str = "assistant",
    loaded_skills: Optional[List[str]] = None,
    mission_id: Optional[str] = None,
    mission_type: Optional[str] = None,
    timeout: Optional[int] = None,
) -> int:
    """Capture the assistant response at turn:post. Topic inferred from
    the user prompt (the question shapes the topic more than the answer).
    Returns total chunks stored."""
    chunks = chunk_content(model_response)
    if not chunks:
        return 0
    topic = infer_topic(user_prompt or model_response)
    stored = 0
    for piece in chunks:
        idx = next_chunk_index(session_id)
        ok = _post_verbatim_capture(
            content=piece,
            session_id=session_id,
            chunk_index=idx,
            source_event="turn.post",
            topic=topic,
            agent=agent,
            is_error=False,
            loaded_skills=loaded_skills,
            mission_id=mission_id,
            mission_type=mission_type,
            timeout=timeout if timeout is not None else _mcp_timeout_seconds(),
        )
        if ok:
            stored += 1
    return stored


def capture_tool_call_post(
    *,
    session_id: str,
    tool_name: str,
    tool_input: Any,
    tool_output: Any,
    is_error: bool,
    loaded_skills: Optional[List[str]] = None,
    mission_id: Optional[str] = None,
    mission_type: Optional[str] = None,
    timeout: Optional[int] = None,
) -> int:
    """Capture a tool invocation: tool_name + tool_input + tool_output
    packed into one chunk (or split if very long). Topic is the tool
    name itself prefixed with 'tool.' for the curator to filter on."""
    body_parts: List[str] = [f"[tool] {tool_name}"]
    try:
        body_parts.append("[input] " + json.dumps(tool_input, default=str)[:4000])
    except (TypeError, ValueError):
        body_parts.append("[input] <unserializable>")
    try:
        body_parts.append("[output] " + json.dumps(tool_output, default=str)[:4000])
    except (TypeError, ValueError):
        body_parts.append("[output] <unserializable>")
    body = "\n".join(body_parts)

    chunks = chunk_content(body)
    if not chunks:
        return 0
    topic = f"tool.{tool_name}"
    stored = 0
    for piece in chunks:
        idx = next_chunk_index(session_id)
        ok = _post_verbatim_capture(
            content=piece,
            session_id=session_id,
            chunk_index=idx,
            source_event="tool.call.post",
            topic=topic,
            agent=f"tool:{tool_name}",
            is_error=is_error,
            loaded_skills=loaded_skills,
            mission_id=mission_id,
            mission_type=mission_type,
            timeout=timeout if timeout is not None else _mcp_timeout_seconds(),
        )
        if ok:
            stored += 1
    return stored
