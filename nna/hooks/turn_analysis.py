#!/usr/bin/env python3
"""
NotNativeMemory - TurnAnalysis Hook

Fires at the END of each turn (post phase), after the model responds.
A single LLM call analyzes the full conversation (user prompt + model
response) for two things in one pass:

  1. Learnable patterns (corrections, preferences, gotchas, decisions)
     — stored to NotNativeMemory via RAG ingestion.

  2. Unfulfilled promises ("I'll look up X", tools called but no
     substantive answer delivered) — stored as a high-importance nudge
     memory that user_prompt_inject.py surfaces on the next turn.

Combining both into one LLM call shares the token cost: zero extra
inference for promise detection.

Renamed from turn_extractor.py on 2026-04-26 to reflect the broader
analysis scope.

Exit codes:
    0 - success (analysis completed or skipped)
    1 - non-fatal error (does not block agent operation)
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

# Minimum conversation length to trigger analysis (chars).
MIN_CONVERSATION_LENGTH = int(os.environ.get("MEMORY_EXTRACT_MIN_LENGTH", "30"))

# LLM temperature for analysis (low = deterministic).
ANALYSIS_TEMPERATURE = float(os.environ.get("MEMORY_EXTRACT_TEMP", "0.1"))

# Max extracted facts to store per turn (prevents over-extraction).
MAX_EXTRACTIONS_PER_TURN = int(os.environ.get("MEMORY_EXTRACT_MAX_RESULTS", "5"))

# Timeout for LLM calls.
TIMEOUT_SECONDS = int(os.environ.get("MEMORY_EXTRACT_TIMEOUT", "10"))

# Optional: override the chat completion endpoint.
ANALYSIS_LLM_URL = os.environ.get("MEMORY_EXTRACT_LLM_URL", "")

LOG_PATH = os.environ.get(
    "MEMORY_EXTRACT_LOG",
    os.path.expanduser("~/.nna/turn_analysis.log"),
)

# Legacy log path from when this script was named turn_extractor.py.
# Cleaned up on first run after rename so operators don't accumulate stale logs.
_LEGACY_LOG_PATH = os.path.expanduser("~/.nna/turn_extractor.log")

_MCP_TOKEN = os.environ.get("MEMORY_MCP_TOKEN", "").strip()
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}
if _MCP_TOKEN:
    _HEADERS["Authorization"] = f"Bearer {_MCP_TOKEN}"


def _cleanup_legacy_log() -> None:
    """Remove the pre-rename log file on first post-rename run."""
    try:
        if os.path.exists(_LEGACY_LOG_PATH):
            os.remove(_LEGACY_LOG_PATH)
    except OSError:
        # Non-fatal: legacy log will be orphaned but does not affect operation.
        pass


def _build_analysis_prompt(user_prompt: str, model_response: str) -> str:
    """Build the combined extraction + promise-detection prompt."""
    return f"""Analyze this conversation turn and return a single JSON object with TWO sections.

SECTION 1 — Learnable patterns ("results"):
  Each item: {{ "type": "behavioral|operational|gotcha|decision",
                "category": "correction|preference|frustration|tool-failure|policy|...",
                "key": "short_identifier",
                "value": "distilled rule or fact",
                "confidence": "high|medium|low" }}
  Extract: user corrections, preferences, infrastructure facts, tool failures, explicit decisions.
  Skip: trivial acknowledgments, model opinions, info without a clear lesson.

SECTION 2 — Promise tracking ("unfulfilledPromises", "shouldNudge", "nudgeText"):
  Did the assistant commit to a future action ("I'll look up X", "let me check Y")?
  Were those actions completed with substantive results?
  If tools were called but no meaningful answer delivered → flag as incomplete.
  Set "shouldNudge" true ONLY when the next turn could meaningfully act on it.
  "nudgeText" should be a single sentence the assistant could say next turn
  (e.g. "Earlier I said I'd check X — want me to follow through?").

Return ONLY valid JSON with this exact shape — no markdown, no commentary:

{{
  "results": [...],
  "unfulfilledPromises": [
    {{ "promise": "...", "reason": "tools called but no results delivered" }}
  ],
  "shouldNudge": false,
  "nudgeText": ""
}}

Conversation Turn:
--- USER ---
{user_prompt[:2000]}

--- MODEL ---
{model_response[:4000]}
--- END ---"""


def _strip_markdown_fences(content: str) -> str:
    """Strip ```json ... ``` fences if the LLM wrapped its response."""
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    return content.strip()


def _call_analysis_llm(conversation: dict) -> dict:
    """Call the analysis LLM and return the parsed JSON object.

    Returns a dict with shape:
        {"results": [...], "unfulfilledPromises": [...],
         "shouldNudge": bool, "nudgeText": str}

    Returns an empty-shaped dict on any failure (LLM unreachable,
    invalid JSON, etc.) so callers can treat absence as "no analysis".
    """
    empty: dict = {
        "results": [],
        "unfulfilledPromises": [],
        "shouldNudge": False,
        "nudgeText": "",
    }

    user_prompt = conversation.get("user_prompt", "")
    model_response = conversation.get("model_response", "")

    # Skip trivial conversations (10x multiplier preserves prior behavior).
    if len(user_prompt) + len(model_response) < MIN_CONVERSATION_LENGTH * 10:
        return empty

    payload = json.dumps({
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a learning + accountability engine. Analyze "
                    "conversations and return ONLY valid JSON matching the "
                    "schema specified in the user message."
                ),
            },
            {"role": "user", "content": _build_analysis_prompt(user_prompt, model_response)},
        ],
        "temperature": ANALYSIS_TEMPERATURE,
        "max_tokens": 1500,
    }).encode("utf-8")

    if ANALYSIS_LLM_URL:
        chat_url = ANALYSIS_LLM_URL
    else:
        chat_url = MCP_URL.replace("/mcp", "/v1/chat/completions")

    try:
        req = urllib.request.Request(
            chat_url,
            data=payload,
            headers=dict(_HEADERS),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = json.loads(_strip_markdown_fences(content))

        # Coerce shape: missing keys default to empty/false.
        return {
            "results": parsed.get("results", []) if isinstance(parsed.get("results"), list) else [],
            "unfulfilledPromises": (
                parsed.get("unfulfilledPromises", [])
                if isinstance(parsed.get("unfulfilledPromises"), list)
                else []
            ),
            "shouldNudge": bool(parsed.get("shouldNudge", False)),
            "nudgeText": str(parsed.get("nudgeText", "") or ""),
        }

    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] Analysis LLM unavailable ({exc}). Skipping analysis for this turn.", file=sys.stderr)
        return empty


def _store_to_memoria(items: list, conversation_id: str) -> int:
    """Store extracted facts to NotNativeMemory via RAG ingestion."""
    stored = 0
    for item in items[:MAX_EXTRACTIONS_PER_TURN]:
        if not all(k in item for k in ["type", "category", "key", "value"]):
            continue

        base_tags = [item["type"], f"cat:{item['category']}"]
        importance = "high" if item.get("confidence") == "high" else "normal"
        title = f"{item['type']}:{item['key']}:{conversation_id[:8]}"

        content = (
            f"[{item['type'].upper()}] {item['category']}\n"
            f"Key: {item['key']}\n"
            f"Value: {item['value']}\n"
            f"Confidence: {item.get('confidence', 'medium')}"
        )

        if _rag_ingest(title, content, base_tags, importance):
            stored += 1

    return stored


def _store_pending_nudge(nudge_text: str, conversation_id: str) -> bool:
    """Store an unfulfilled-promise nudge as a high-importance memory.

    The next turn's user_prompt_inject.py picks it up via the existing
    HIGH_IMPORTANCE_THRESHOLD (0.35) — no new event wiring needed. The
    'pending_nudge' tag lets operators inspect or sweep stale nudges.
    """
    if not nudge_text.strip():
        return False

    title = f"pending_nudge:{conversation_id[:8]}"
    content = (
        "[PENDING NUDGE] An earlier turn made a commitment that was not delivered.\n"
        f"Suggested follow-up: {nudge_text.strip()}"
    )
    return _rag_ingest(
        title=title,
        content=content,
        tags=["pending_nudge", "cat:promise"],
        importance="high",
    )


def _rag_ingest(title: str, content: str, tags: list, importance: str) -> bool:
    """POST a single rag_ingest_text call. Returns True on success."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "rag_ingest_text",
            "arguments": {
                "title": title,
                "content": content,
                "tags": tags,
                "importance": importance,
            },
        },
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            MCP_URL,
            data=payload,
            headers=dict(_HEADERS),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return bool(result.get("result"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"[ERROR] Failed to ingest '{title}': {exc}", file=sys.stderr)
        return False


def _log_execution(extracted_count: int, nudge_stored: bool, conversation_len: int) -> None:
    """Append a telemetry row. Failures are swallowed."""
    try:
        parent = os.path.dirname(LOG_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as logf:
            logf.write(
                f"{datetime.datetime.now().isoformat(timespec='seconds')}\t"
                f"extracted={extracted_count}\t"
                f"nudge={'1' if nudge_stored else '0'}\t"
                f"conv_len={conversation_len}\n"
            )
    except OSError:
        pass


def main():
    _cleanup_legacy_log()

    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(1)

    user_prompt = hook_input.get("prompt", "")
    model_response = hook_input.get("model_response", "")
    cwd = hook_input.get("cwd") or os.getcwd()

    if not user_prompt or not model_response:
        error_msg = (
            f"Missing data in hook input: prompt={bool(user_prompt)}, "
            f"model_response={bool(model_response)}"
        )
        print(f"[ERROR] {error_msg}", file=sys.stderr)
        _log_execution(0, False, 0)
        sys.exit(1)

    conversation_len = len(user_prompt) + len(model_response)
    conv_id = f"{cwd}:{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"

    analysis = _call_analysis_llm({
        "user_prompt": user_prompt,
        "model_response": model_response,
    })

    stored = _store_to_memoria(analysis["results"], conv_id)

    nudge_stored = False
    if analysis["shouldNudge"] and analysis["nudgeText"]:
        nudge_stored = _store_pending_nudge(analysis["nudgeText"], conv_id)

    _log_execution(stored, nudge_stored, conversation_len)
    sys.exit(0)


if __name__ == "__main__":
    main()
