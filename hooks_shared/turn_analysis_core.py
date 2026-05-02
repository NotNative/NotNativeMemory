#!/usr/bin/env python3
"""
NotNativeMemory - Turn Analysis Core (shared)

Harness-agnostic turn analysis: takes (user_prompt, model_response, cwd)
plus LLM endpoint config and returns a structured analysis result.
Handles two LLM API shapes (OpenAI-compat and Anthropic Messages),
auto-discovers a model when one isn't pinned, and exposes the
ingest/storage helpers for adapters to call.

Adapter modules (nna/hooks/turn_analysis.py, claude/hooks/turn_analysis.py)
are thin wrappers that pull stdin in their harness's shape, then call
analyze_turn() with the right config.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Optional

# Default cheapest cloud model. Override via config.model.
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

# Conservative caps so a single turn never floods the analyzer.
USER_PROMPT_CAP_CHARS = 2000
MODEL_RESPONSE_CAP_CHARS = 4000

# Min combined chars to trigger analysis (filters trivial turns).
DEFAULT_MIN_CONVERSATION_CHARS = 300

# LLM call timeout (seconds).
DEFAULT_LLM_TIMEOUT = 10

# Default max items stored per turn so noisy LLMs can't spam memory.
DEFAULT_MAX_EXTRACTIONS = 5


class AnalysisConfig:
    """Resolved configuration for one analysis run."""

    def __init__(
        self,
        *,
        api: str,                        # 'anthropic_messages' | 'openai_compat'
        endpoint: str,                   # full URL to chat completion / messages
        model: Optional[str],            # may be None for openai_compat (auto-discover)
        headers: dict,                   # auth + content-type
        models_url: Optional[str] = None,  # for auto-discovery (openai_compat only)
        mcp_url: str = "http://localhost:9500/mcp",
        mcp_headers: Optional[dict] = None,
        temperature: float = 0.1,
        max_tokens: int = 1500,
        timeout: int = DEFAULT_LLM_TIMEOUT,
        min_conversation_chars: int = DEFAULT_MIN_CONVERSATION_CHARS,
        max_extractions: int = DEFAULT_MAX_EXTRACTIONS,
    ) -> None:
        if api not in ("anthropic_messages", "openai_compat"):
            raise ValueError(f"unknown api: {api}")
        self.api = api
        self.endpoint = endpoint
        self.model = model
        self.headers = headers
        self.models_url = models_url
        self.mcp_url = mcp_url
        self.mcp_headers = mcp_headers or {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.min_conversation_chars = min_conversation_chars
        self.max_extractions = max_extractions


def resolve_config_from_env(env: Optional[dict] = None) -> AnalysisConfig:
    """Build an AnalysisConfig from env vars using the documented precedence.

    Precedence:
      1. MEMORY_EXTRACT_LLM_URL (explicit) — pair with MEMORY_EXTRACT_API
         to disambiguate shape; defaults to anthropic_messages if URL ends
         in /v1/messages, openai_compat otherwise.
      2. ANTHROPIC_BASE_URL set → anthropic_messages at $ANTHROPIC_BASE_URL/v1/messages
      3. OPENAI_BASE_URL set → openai_compat at $OPENAI_BASE_URL/chat/completions
      4. Default → Anthropic Messages at api.anthropic.com
    """
    e = env if env is not None else os.environ

    explicit_url = e.get("MEMORY_EXTRACT_LLM_URL", "").strip()
    explicit_api = e.get("MEMORY_EXTRACT_API", "").strip()
    anthropic_base = e.get("ANTHROPIC_BASE_URL", "").strip().rstrip("/")
    openai_base = e.get("OPENAI_BASE_URL", "").strip().rstrip("/")

    if explicit_url:
        api = explicit_api or (
            "anthropic_messages" if explicit_url.endswith("/v1/messages") else "openai_compat"
        )
        endpoint = explicit_url
        models_url = None
        if api == "openai_compat":
            # Strip trailing '/chat/completions' to compute models endpoint
            base = explicit_url.rsplit("/chat/completions", 1)[0]
            models_url = f"{base}/models"
    elif anthropic_base:
        api = "anthropic_messages"
        endpoint = f"{anthropic_base}/v1/messages"
        models_url = None
    elif openai_base:
        api = "openai_compat"
        endpoint = f"{openai_base}/chat/completions"
        models_url = f"{openai_base}/models"
    else:
        api = "anthropic_messages"
        endpoint = "https://api.anthropic.com/v1/messages"
        models_url = None

    # Auth headers per API shape.
    if api == "anthropic_messages":
        api_key = e.get("ANTHROPIC_API_KEY", "").strip()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "anthropic-version": e.get("ANTHROPIC_VERSION", DEFAULT_ANTHROPIC_VERSION),
        }
        if api_key:
            headers["x-api-key"] = api_key
    else:
        api_key = e.get("OPENAI_API_KEY", "").strip()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    # Model resolution: explicit override > auto-discover (openai_compat) > Anthropic default.
    explicit_model = e.get("MEMORY_EXTRACT_MODEL", "").strip()
    if explicit_model:
        model: Optional[str] = explicit_model
    elif api == "anthropic_messages":
        model = DEFAULT_ANTHROPIC_MODEL
    else:
        # openai_compat without override — defer to auto-discover at call time.
        model = None

    mcp_url = e.get("MEMORY_MCP_URL", "http://localhost:9500/mcp")
    mcp_token = e.get("MEMORY_MCP_TOKEN", "").strip()
    mcp_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if mcp_token:
        mcp_headers["Authorization"] = f"Bearer {mcp_token}"

    temp = float(e.get("MEMORY_EXTRACT_TEMP", "0.1"))
    timeout = int(e.get("MEMORY_EXTRACT_TIMEOUT", str(DEFAULT_LLM_TIMEOUT)))
    min_chars = int(e.get("MEMORY_EXTRACT_MIN_LENGTH", str(DEFAULT_MIN_CONVERSATION_CHARS // 10))) * 10
    max_ext = int(e.get("MEMORY_EXTRACT_MAX_RESULTS", str(DEFAULT_MAX_EXTRACTIONS)))

    return AnalysisConfig(
        api=api,
        endpoint=endpoint,
        model=model,
        headers=headers,
        models_url=models_url,
        mcp_url=mcp_url,
        mcp_headers=mcp_headers,
        temperature=temp,
        timeout=timeout,
        min_conversation_chars=min_chars,
        max_extractions=max_ext,
    )


def discover_model(config: AnalysisConfig) -> Optional[str]:
    """For openai_compat without an explicit model, GET /models and pick first.

    Returns model id string or None on failure. Caller decides what to do
    with None — typically skip the analysis and log.
    """
    if not config.models_url:
        return None
    try:
        req = urllib.request.Request(
            config.models_url,
            headers={k: v for k, v in config.headers.items() if k != "Content-Type"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=config.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        data = body.get("data") or []
        for entry in data:
            mid = entry.get("id") or entry.get("name")
            if mid:
                return str(mid)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    return None


def build_analysis_prompt(user_prompt: str, model_response: str) -> str:
    """Build the combined extraction + promise-detection prompt."""
    return (
        "Analyze this conversation turn and return a single JSON object with TWO sections.\n"
        "\n"
        'SECTION 1 — Learnable patterns ("results"):\n'
        '  Each item: { "type": "behavioral|operational|gotcha|decision",\n'
        '                "category": "correction|preference|frustration|tool-failure|policy|...",\n'
        '                "key": "short_identifier",\n'
        '                "value": "distilled rule or fact",\n'
        '                "confidence": "high|medium|low" }\n'
        "  Extract: user corrections, preferences, infrastructure facts, tool failures, explicit decisions.\n"
        "  Skip: trivial acknowledgments, model opinions, info without a clear lesson.\n"
        "\n"
        'SECTION 2 — Promise tracking ("unfulfilledPromises", "shouldNudge", "nudgeText"):\n'
        '  Did the assistant commit to a future action ("I\'ll look up X", "let me check Y")?\n'
        "  Were those actions completed with substantive results?\n"
        "  If tools were called but no meaningful answer delivered → flag as incomplete.\n"
        '  Set "shouldNudge" true ONLY when the next turn could meaningfully act on it.\n'
        '  "nudgeText" should be a single sentence the assistant could say next turn\n'
        '  (e.g. "Earlier I said I\'d check X — want me to follow through?").\n'
        "\n"
        "Return ONLY valid JSON with this exact shape — no markdown, no commentary:\n"
        "\n"
        "{\n"
        '  "results": [...],\n'
        '  "unfulfilledPromises": [\n'
        '    { "promise": "...", "reason": "tools called but no results delivered" }\n'
        "  ],\n"
        '  "shouldNudge": false,\n'
        '  "nudgeText": ""\n'
        "}\n"
        "\n"
        "Conversation Turn:\n"
        "--- USER ---\n"
        f"{user_prompt[:USER_PROMPT_CAP_CHARS]}\n"
        "\n"
        "--- MODEL ---\n"
        f"{model_response[:MODEL_RESPONSE_CAP_CHARS]}\n"
        "--- END ---"
    )


def strip_markdown_fences(content: str) -> str:
    """Strip ```json ... ``` fences if the LLM wrapped its response."""
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    return content.strip()


def empty_analysis() -> dict:
    """Return the canonical empty analysis shape."""
    return {
        "results": [],
        "unfulfilledPromises": [],
        "shouldNudge": False,
        "nudgeText": "",
    }


def coerce_analysis(parsed: dict) -> dict:
    """Coerce LLM output into the canonical shape, filling defaults."""
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


def call_analysis_llm(
    user_prompt: str,
    model_response: str,
    config: AnalysisConfig,
) -> dict:
    """Call the analysis LLM and return the parsed analysis dict.

    Returns empty_analysis() on any failure (LLM unreachable, invalid
    JSON, no model resolved). Adapters can treat absence as "no analysis".
    """
    if len(user_prompt) + len(model_response) < config.min_conversation_chars:
        return empty_analysis()

    model = config.model
    if not model and config.api == "openai_compat":
        model = discover_model(config)
    if not model:
        print(
            "[WARN] No model resolved for analysis; set MEMORY_EXTRACT_MODEL or ensure /models is reachable.",
            file=sys.stderr,
        )
        return empty_analysis()

    system_msg = (
        "You are a learning + accountability engine. Analyze conversations "
        "and return ONLY valid JSON matching the schema specified in the "
        "user message."
    )
    user_msg = build_analysis_prompt(user_prompt, model_response)

    if config.api == "anthropic_messages":
        body = {
            "model": model,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "system": system_msg,
            "messages": [{"role": "user", "content": user_msg}],
        }
    else:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }

    payload = json.dumps(body).encode("utf-8")
    try:
        req = urllib.request.Request(
            config.endpoint,
            data=payload,
            headers=dict(config.headers),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=config.timeout) as resp:
            response = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] Analysis LLM unavailable ({exc}). Skipping.", file=sys.stderr)
        return empty_analysis()

    # Extract content per API shape.
    if config.api == "anthropic_messages":
        # response.content is a list of content blocks; first text block carries JSON.
        content = ""
        for block in response.get("content", []):
            if block.get("type") == "text":
                content = block.get("text", "")
                break
    else:
        content = (
            response.get("choices", [{}])[0].get("message", {}).get("content", "")
        )

    try:
        parsed = json.loads(strip_markdown_fences(content))
    except (json.JSONDecodeError, ValueError):
        return empty_analysis()

    return coerce_analysis(parsed)


def rag_ingest(
    title: str,
    content: str,
    tags: list,
    importance: str,
    config: AnalysisConfig,
) -> bool:
    """POST a single rag_ingest_text call to the MCP. Returns True on success."""
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
            config.mcp_url,
            data=payload,
            headers=dict(config.mcp_headers),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=config.timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return bool(result.get("result"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"[ERROR] Failed to ingest '{title}': {exc}", file=sys.stderr)
        return False


def store_extractions(items: list, conversation_id: str, config: AnalysisConfig) -> int:
    """Store extracted facts to NotNativeMemory via RAG ingestion."""
    stored = 0
    for item in items[: config.max_extractions]:
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

        if rag_ingest(title, content, base_tags, importance, config):
            stored += 1

    return stored


def store_pending_nudge(
    nudge_text: str, conversation_id: str, config: AnalysisConfig
) -> bool:
    """Store an unfulfilled-promise nudge as a high-importance memory."""
    if not nudge_text.strip():
        return False
    title = f"pending_nudge:{conversation_id[:8]}"
    content = (
        "[PENDING NUDGE] An earlier turn made a commitment that was not delivered.\n"
        f"Suggested follow-up: {nudge_text.strip()}"
    )
    return rag_ingest(
        title=title,
        content=content,
        tags=["pending_nudge", "cat:promise"],
        importance="high",
        config=config,
    )


def make_conversation_id(cwd: str) -> str:
    """Stable-shape conversation id for grouping ingested items."""
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{cwd}:{ts}"


def analyze_turn(
    user_prompt: str,
    model_response: str,
    cwd: str,
    config: AnalysisConfig,
) -> dict:
    """End-to-end: analyze the turn and persist extractions/nudges.

    Returns a dict { "stored": int, "nudge_stored": bool, "analysis": dict }
    so adapters can log/metric the outcome without re-running.
    """
    analysis = call_analysis_llm(user_prompt, model_response, config)
    conv_id = make_conversation_id(cwd)
    stored = store_extractions(analysis["results"], conv_id, config)
    nudge_stored = False
    if analysis["shouldNudge"] and analysis["nudgeText"]:
        nudge_stored = store_pending_nudge(analysis["nudgeText"], conv_id, config)
    return {
        "stored": stored,
        "nudge_stored": nudge_stored,
        "analysis": analysis,
    }
