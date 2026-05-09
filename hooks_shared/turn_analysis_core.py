#!/usr/bin/env python3
"""
NotNativeMemory - Turn Analysis Core (shared)

Harness-agnostic turn analysis: takes (user_prompt, model_response, cwd)
plus LLM endpoint config and returns a structured analysis result.
Handles two LLM API shapes (OpenAI-compat and Anthropic Messages),
auto-discovers a model when one isn't pinned, and exposes the
ingest/storage helpers for adapters to call.

Adapter modules (hook_bundles/{nna,claude}/notnative-memory/turn_analysis.py)
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

# Min combined chars to trigger analysis. Set low: short corrections like
# "stop using em-dashes" are some of the highest-value extractions. The cap
# only filters truly degenerate input (single-word turns, empty exchanges).
DEFAULT_MIN_CONVERSATION_CHARS = 40

# LLM call timeout (seconds).
DEFAULT_LLM_TIMEOUT = 10

# Runaway hedge against a malfunctioning LLM. Not a real ceiling on volume:
# quality is the bar, and a rich turn can legitimately produce dozens of facts.
DEFAULT_MAX_EXTRACTIONS = 50

# Generous output budget so reasoning models (Qwen, DeepSeek-R1, etc.) can
# burn tokens on hidden chain-of-thought and still emit the final JSON.
# Override with MEMORY_EXTRACT_MAX_TOKENS for cost-sensitive cloud setups.
DEFAULT_MAX_TOKENS = 16000


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
        max_tokens: int = DEFAULT_MAX_TOKENS,
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
    max_tokens = int(e.get("MEMORY_EXTRACT_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
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
        max_tokens=max_tokens,
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
    """Build the combined extraction + promise-detection prompt.

    The extraction half of this prompt is shaped around one principle:
    every stored fact must be a self-contained statement that makes a
    smaller local model smarter when retrieved weeks from now. Quality
    is the bar, not quantity. A turn that yields 30 distinct standalone
    facts should return 30.
    """
    return (
        "Analyze this conversation turn and return a single JSON object with TWO sections.\n"
        "\n"
        'SECTION 1: Learnable facts ("results")\n'
        "\n"
        '  Each item is { "fact": "...", "tags": ["..."], "confidence": "high|medium|low" }\n'
        "\n"
        "  Rules for each fact:\n"
        "    1. One self-contained sentence that reads correctly in isolation, weeks later.\n"
        "    2. Include both the WHY and the WHAT-TO-DO when applicable (cause and remedy together).\n"
        "    3. State rules and preferences as rules, not as observations about a speaker.\n"
        '         Bad:  "The user said responses should be terse."\n'
        '         Good: "Responses should be terse; design ownership belongs to the user."\n'
        '    4. Do not reference "the user", "this turn", "above", "the conversation",\n'
        "       or any speaker. The fact must stand alone with no context dependency.\n"
        "    5. One thought per entry. If a fact has two unrelated parts, split it into two entries.\n"
        "    6. Soft length target ~400 characters. If a thought is longer, it is probably two thoughts.\n"
        "    7. Tags are short lowercase keywords useful for later filtering\n"
        '       (e.g. "shell", "powershell", "correction", "preference", "infra", "tool-failure").\n'
        "    8. Skip an entry entirely if it cannot stand alone usefully. Better empty than noisy.\n"
        "\n"
        "  Volume: extract every standalone learnable fact present in the turn. If a turn legitimately\n"
        "  yields 30 facts, return 30. There is no quota and no upper target.\n"
        "\n"
        "  Examples of well-formed facts:\n"
        '    - "When writing a PowerShell script from bash, use single quotes around strings\n'
        "       containing special characters; bash's escape rendering will otherwise corrupt\n"
        '       double-quoted strings before PowerShell sees them."\n'
        '    - "Em-dashes must never appear in any text output. Replace with semicolons or periods."\n'
        '    - "NotNativeMemory RAG ingestion uses 2000-character chunks with 250-character overlap,\n'
        "       sharing the gte-large-en-v1.5 embedding space with memories so hybrid retrieval can\n"
        '       fuse them via reciprocal rank fusion."\n'
        "\n"
        "  Examples of BAD extractions (do not produce these):\n"
        '    - "The user prefers terse responses." (references the user; reframe as a rule)\n'
        '    - "This turn discussed PowerShell quoting." (meta about the turn, not a fact)\n'
        '    - "Use single quotes." (no why, no scope, useless out of context)\n'
        '    - "OK." (trivial)\n'
        "\n"
        'SECTION 2: Promise tracking ("unfulfilledPromises", "shouldNudge", "nudgeText")\n'
        "\n"
        '  Did the assistant commit to a future action ("I\'ll look up X", "let me check Y")?\n'
        "  Were those actions completed with substantive results?\n"
        "  If tools were called but no meaningful answer delivered, flag as incomplete.\n"
        '  Set "shouldNudge" true ONLY when the next turn could meaningfully act on it.\n'
        '  "nudgeText" should be a single sentence the assistant could say next turn\n'
        '  (e.g. "Earlier I said I\'d check X; want me to follow through?").\n'
        "\n"
        'SECTION 3: Conversation summary ("summary")\n'
        "\n"
        "  A compact distilled summary of THIS turn's dialogue, written so a model\n"
        "  reading it weeks later can recall the shape of the discussion.\n"
        "\n"
        "  Rules for the summary:\n"
        "    1. Cover the user/assistant dialogue only. Do NOT describe tool calls,\n"
        "       tool results, file contents, or mechanical artifacts of how the work\n"
        "       happened. Capture WHAT was discussed and decided, not HOW it was done.\n"
        "    2. 1-3 sentences. Concise. Prose, not a list.\n"
        "    3. No references to speakers (\"the user\", \"the assistant\"). State the\n"
        "       discussion as a third-person account: what was decided, what problem\n"
        "       was solved, what direction was set.\n"
        "    4. If the turn had no substantive discussion (acknowledgment-only,\n"
        '       trivial exchange), return an empty string "" rather than padding.\n'
        "\n"
        "  Examples of well-formed summaries:\n"
        '    - "PowerShell quoting from bash was clarified: bash escape rendering\n'
        '       corrupts double-quoted strings before PowerShell sees them, so single\n'
        '       quotes are required when shelling across the boundary."\n'
        '    - "The proposed business scope was withdrawn after review; shared-team\n'
        '       deployments will use single-user mode instead, with multi-user mode\n'
        '       reserved for genuine isolation needs."\n'
        "\n"
        "Return ONLY valid JSON with this exact shape; no markdown fences, no commentary:\n"
        "\n"
        "{\n"
        '  "results": [\n'
        '    { "fact": "...", "tags": ["..."], "confidence": "high" }\n'
        "  ],\n"
        '  "unfulfilledPromises": [\n'
        '    { "promise": "...", "reason": "tools called but no results delivered" }\n'
        "  ],\n"
        '  "shouldNudge": false,\n'
        '  "nudgeText": "",\n'
        '  "summary": ""\n'
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


def build_worker_analysis_prompt(task_envelope: str, worker_output: str) -> str:
    """Build the worker-mode analysis prompt.

    Sibling to build_analysis_prompt. Same JSON schema (results / promise
    fields / summary). Different steering: workers learn vendor quirks,
    tool-result patterns, integration gotchas, and operational invariants
    rather than user preferences and corrections.

    The schema match is deliberate: extractions, summaries, and nudges all
    flow through the same downstream storage helpers regardless of source,
    so the worker path inherits memories+RAG semantics without forking
    the storage layer.
    """
    return (
        "Analyze this worker run and return a single JSON object with THREE sections.\n"
        "\n"
        "A worker is a one-shot agent executing a task envelope. There is no\n"
        "human in the loop. Your job is to extract durable knowledge from\n"
        "what happened so future workers (and chat sessions) on related\n"
        "tasks become smarter over time.\n"
        "\n"
        'SECTION 1: Learnable facts ("results")\n'
        "\n"
        '  Each item is { "fact": "...", "tags": ["..."], "confidence": "high|medium|low" }\n'
        "\n"
        "  What to extract from a worker run:\n"
        "    1. Vendor and integration quirks: site-specific scrape gotchas,\n"
        "       API behaviors, authentication patterns, rate limits, formats\n"
        "       that needed special handling.\n"
        "    2. Tool-result patterns: what shape of output a tool produced for\n"
        "       a given input, and how to interpret it next time.\n"
        "    3. Operational gotchas: things that broke, why, and the workaround.\n"
        "    4. Constants discovered in the wild: real vendor identifiers, real\n"
        "       endpoints, real schema fields that future runs will encounter.\n"
        "    5. Conditions where a task class succeeds or fails.\n"
        "\n"
        "  What NOT to extract:\n"
        "    - Run-specific transient data (the price was $4.99 today). Only\n"
        "      durable patterns survive (this vendor shows price in the\n"
        "      data-price attribute, not the visible text).\n"
        "    - Tool invocation mechanics (we ran curl, then we ran jq).\n"
        "      Capture the LESSON, not the keystroke history.\n"
        "    - Boilerplate task-completion summaries. If nothing was learned,\n"
        "      return an empty results array. Better empty than noisy.\n"
        "\n"
        "  Same fact rules as session extraction:\n"
        "    - One self-contained sentence that reads correctly in isolation.\n"
        "    - Include WHY and WHAT-TO-DO when applicable.\n"
        "    - No references to \"the worker\", \"this run\", \"the task above\".\n"
        "    - Soft length target ~400 characters.\n"
        "    - Tags are short lowercase keywords; include the affected\n"
        '       vendor or system if applicable (e.g. "vendor:acme", "scrape",\n'
        '       "api:stripe", "selector").\n'
        "\n"
        "  Volume: extract every durable lesson present. If a worker run produced\n"
        "  20 distinct vendor quirks, return all 20.\n"
        "\n"
        "  Examples of well-formed worker facts:\n"
        '    - "Acme Corp\'s pricing page renders price in a data-price attribute\n'
        '       on the .product-tile element; the visible text is a localized\n'
        '       formatted string and is unsafe to parse for the numeric value."\n'
        '    - "Stripe checkout sessions older than 24 hours return a 410 GONE\n'
        '       on retrieve; treat any retrieve failure on a session_id older\n'
        '       than a day as expected and skip rather than retry."\n'
        '    - "Cloudflare-protected vendor pages require a real browser fingerprint;\n'
        '       a plain requests.get returns a 403 challenge page rather than the\n'
        '       resource. Use the headless-browser tool, not raw HTTP."\n'
        "\n"
        'SECTION 2: Promise tracking ("unfulfilledPromises", "shouldNudge", "nudgeText")\n'
        "\n"
        "  Did the worker commit to a follow-up action that did not get done?\n"
        "  If a tool was invoked but its result was not consumed, flag it.\n"
        '  Set "shouldNudge" true ONLY when a downstream worker or chat session\n'
        "  could meaningfully act on the unfulfilled commitment.\n"
        '  "nudgeText" should be one sentence describing the missing follow-up.\n'
        "\n"
        'SECTION 3: Worker run summary ("summary")\n'
        "\n"
        "  A compact narrative of what the worker did and what conclusion it\n"
        "  reached. 1-3 sentences. Cover the outcome and the shape of the work,\n"
        "  not the mechanical tool-call sequence.\n"
        "\n"
        "  Rules:\n"
        "    1. Outcome-focused: what was attempted, what was learned, what changed.\n"
        "    2. No tool-call ledger. Skip the keystroke history.\n"
        "    3. No speaker references. Third-person account.\n"
        '    4. If the run was trivial or uninformative, return empty string "".\n'
        "\n"
        "  Examples:\n"
        '    - "A vendor scrape against Acme Corp succeeded after switching to\n'
        '       the data-price attribute selector; the visible-text approach had\n'
        '       been silently returning localized formatted strings."\n'
        '    - "An authentication probe against Stripe confirmed that checkout\n'
        '       sessions become unretrievable after 24 hours, returning 410 GONE."\n'
        "\n"
        "Return ONLY valid JSON with this exact shape; no markdown fences, no commentary:\n"
        "\n"
        "{\n"
        '  "results": [\n'
        '    { "fact": "...", "tags": ["..."], "confidence": "high" }\n'
        "  ],\n"
        '  "unfulfilledPromises": [\n'
        '    { "promise": "...", "reason": "tools called but no results delivered" }\n'
        "  ],\n"
        '  "shouldNudge": false,\n'
        '  "nudgeText": "",\n'
        '  "summary": ""\n'
        "}\n"
        "\n"
        "Worker Run:\n"
        "--- TASK ENVELOPE ---\n"
        f"{task_envelope[:USER_PROMPT_CAP_CHARS]}\n"
        "\n"
        "--- WORKER OUTPUT ---\n"
        f"{worker_output[:MODEL_RESPONSE_CAP_CHARS]}\n"
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
        "summary": "",
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
        "summary": str(parsed.get("summary", "") or ""),
    }


def _call_analysis_llm_with_prompt(
    user_msg: str,
    config: AnalysisConfig,
) -> dict:
    """Internal: send a fully-built user message to the analysis LLM and
    return a coerced analysis dict.

    Returns empty_analysis() on any failure (LLM unreachable, invalid JSON,
    no model resolved). Used by both the session-mode and worker-mode entry
    points so the LLM call shape is defined exactly once.
    """
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
        "You are a learning + accountability engine. Analyze the input "
        "and return ONLY valid JSON matching the schema specified in the "
        "user message."
    )

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


def call_analysis_llm(
    user_prompt: str,
    model_response: str,
    config: AnalysisConfig,
) -> dict:
    """Call the session-mode analysis LLM with a user/assistant turn.

    Returns empty_analysis() on any failure or when the conversation is
    below ``config.min_conversation_chars``.
    """
    if len(user_prompt) + len(model_response) < config.min_conversation_chars:
        return empty_analysis()
    user_msg = build_analysis_prompt(user_prompt, model_response)
    return _call_analysis_llm_with_prompt(user_msg, config)


def call_worker_analysis_llm(
    task_envelope: str,
    worker_output: str,
    config: AnalysisConfig,
) -> dict:
    """Call the worker-mode analysis LLM with a task envelope and run output.

    Same shape as ``call_analysis_llm`` but uses ``build_worker_analysis_prompt``
    so the LLM is steered toward vendor quirks, tool-result patterns, and
    operational gotchas instead of user preferences.

    Returns empty_analysis() on any failure or when the combined input is
    below ``config.min_conversation_chars``.
    """
    if len(task_envelope) + len(worker_output) < config.min_conversation_chars:
        return empty_analysis()
    user_msg = build_worker_analysis_prompt(task_envelope, worker_output)
    return _call_analysis_llm_with_prompt(user_msg, config)


def rag_ingest(
    title: str,
    content: str,
    tags: list,
    importance: str,
    config: AnalysisConfig,
) -> bool:
    """POST a single rag_ingest_text call to the MCP. Returns True on success.

    RAG ingestion is reserved for larger artifacts (documents, codebases,
    long-form transcripts). Single-fact extractions go through
    memory_store_call instead so they live as discrete memories with full
    thermal/decay/conflict semantics.
    """
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


def memory_store_call(
    content: str,
    tags: list,
    importance: str,
    source: str,
    config: AnalysisConfig,
) -> bool:
    """POST a single memory_store call to the MCP. Returns True on success.

    Used by store_extractions to land each distilled fact as a discrete
    memory rather than a RAG chunk. Memories carry source attribution,
    thermal state, dedup/conflict semantics, and class taxonomy that
    raw RAG chunks do not.
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "memory_store",
            "arguments": {
                "content": content,
                "tags": tags,
                "importance": importance,
                "source": source,
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
        preview = content[:60].replace("\n", " ")
        print(f"[ERROR] Failed to store memory '{preview}...': {exc}", file=sys.stderr)
        return False


def _confidence_to_importance(confidence: str) -> str:
    """Map LLM-reported confidence to NNM memory importance.

    high -> high, low -> low, anything else (including 'medium' and unknown
    values) -> normal. Confidence is the LLM's own self-rating; calibrate
    treats it conservatively.
    """
    c = (confidence or "").strip().lower()
    if c == "high":
        return "high"
    if c == "low":
        return "low"
    return "normal"


def store_extractions(items: list, conversation_id: str, config: AnalysisConfig) -> int:
    """Store extracted facts to NotNativeMemory as discrete memories.

    Each item must be a dict with at minimum a non-empty 'fact' string.
    Optional fields:
      - 'tags' (list of strings): used verbatim as memory tags.
      - 'confidence' (high|medium|low): mapped to importance.

    Each fact is stored verbatim as the memory content. No template wrapping,
    no metadata headers in the body. Source is recorded as 'model-inferred'
    so downstream curation can distinguish extracted facts from user-stated
    or tool-result memories.

    The conversation_id is intentionally not embedded in the memory body or
    title because memories already carry source_session_id structurally;
    duplicating it as text would only pollute the embedded surface.
    """
    # Conversation id is no longer needed in the memory itself, but the
    # signature is preserved so adapters that pass it don't break.
    del conversation_id

    stored = 0
    for item in items[: config.max_extractions]:
        if not isinstance(item, dict):
            continue
        fact = item.get("fact")
        if not isinstance(fact, str):
            continue
        fact = fact.strip()
        if not fact:
            continue

        raw_tags = item.get("tags", [])
        if isinstance(raw_tags, list):
            tags = [str(t).strip() for t in raw_tags if isinstance(t, (str, int, float)) and str(t).strip()]
        else:
            tags = []

        importance = _confidence_to_importance(item.get("confidence", "medium"))

        if memory_store_call(fact, tags, importance, "model-inferred", config):
            stored += 1

    return stored


def store_conversation_summary(
    summary_text: str, conversation_id: str, config: AnalysisConfig
) -> bool:
    """Store a compacted conversation summary as a RAG document.

    Summaries land in RAG (not memories) because they are narrative artifacts
    rather than discrete standalone facts: typically 1-3 sentences capturing
    the shape of a discussion. RAG handles longer narrative content and lets
    the same chunk participate in `recall` alongside extracted memories via
    Reciprocal Rank Fusion.

    Tagged with ``session-summary`` plus a ``conv:<id-prefix>`` tag so the
    web UI and `recall` callers can filter or boost session digests when
    they want them, and exclude them when they do not.
    """
    if not summary_text or not summary_text.strip():
        return False
    title = f"summary:{conversation_id[:8]}"
    return rag_ingest(
        title=title,
        content=summary_text.strip(),
        tags=["session-summary", f"conv:{conversation_id[:8]}"],
        importance="normal",
        config=config,
    )


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


def _attach_mission_tags(
    items: list,
    mission_id: Optional[str],
    assignment_id: Optional[str],
) -> list:
    """Return a new items list with mission/assignment tags appended to each
    item's tags array.

    Worker writes use tag-based mission scoping (no new scope tier). The
    tags ``mission:<id>`` and optionally ``assignment:<id>`` let downstream
    retrieval filter or boost by mission membership using the existing
    tag-filter infrastructure.
    """
    if not mission_id and not assignment_id:
        return items
    extras = []
    if mission_id:
        extras.append(f"mission:{mission_id}")
    if assignment_id:
        extras.append(f"assignment:{assignment_id}")

    out = []
    for item in items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        new_item = dict(item)
        existing = new_item.get("tags", [])
        if not isinstance(existing, list):
            existing = []
        new_tags = list(existing)
        for t in extras:
            if t not in new_tags:
                new_tags.append(t)
        new_item["tags"] = new_tags
        out.append(new_item)
    return out


def make_conversation_id(cwd: str) -> str:
    """Stable-shape conversation id for grouping ingested items."""
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{cwd}:{ts}"


def analyze_worker_run(
    task_envelope: str,
    worker_output: str,
    cwd: str,
    config: AnalysisConfig,
    *,
    mission_id: Optional[str] = None,
    assignment_id: Optional[str] = None,
) -> dict:
    """End-to-end worker-mode analysis: extract, summarize, optionally nudge.

    Symmetric to ``analyze_turn`` but uses the worker prompt and tags every
    written memory with ``mission:<id>`` (and ``assignment:<id>`` when
    given) so future workers and chat sessions can locate this run's
    learnings via the existing tag-filter retrieval path.

    There is no caller for this entry point yet; NNA wires it up when
    worker mode lands. The function is unit-testable in isolation via the
    same mocked-LLM strategy used for ``analyze_turn``.

    Returns:
        { "stored": int, "nudge_stored": bool, "summary_stored": bool,
          "analysis": dict }
    """
    analysis = call_worker_analysis_llm(task_envelope, worker_output, config)
    conv_id = make_conversation_id(cwd)

    tagged_results = _attach_mission_tags(
        analysis["results"], mission_id, assignment_id,
    )
    stored = store_extractions(tagged_results, conv_id, config)

    summary_stored = False
    if analysis.get("summary"):
        summary_stored = store_conversation_summary(
            analysis["summary"], conv_id, config,
        )

    nudge_stored = False
    if analysis["shouldNudge"] and analysis["nudgeText"]:
        nudge_stored = store_pending_nudge(
            analysis["nudgeText"], conv_id, config,
        )

    return {
        "stored": stored,
        "nudge_stored": nudge_stored,
        "summary_stored": summary_stored,
        "analysis": analysis,
    }


def analyze_turn(
    user_prompt: str,
    model_response: str,
    cwd: str,
    config: AnalysisConfig,
) -> dict:
    """End-to-end: analyze the turn and persist extractions, summary, and nudges.

    Returns a dict
        { "stored": int, "nudge_stored": bool, "summary_stored": bool,
          "analysis": dict }
    so adapters can log/metric the outcome without re-running.
    """
    analysis = call_analysis_llm(user_prompt, model_response, config)
    conv_id = make_conversation_id(cwd)
    stored = store_extractions(analysis["results"], conv_id, config)
    summary_stored = False
    if analysis.get("summary"):
        summary_stored = store_conversation_summary(
            analysis["summary"], conv_id, config,
        )
    nudge_stored = False
    if analysis["shouldNudge"] and analysis["nudgeText"]:
        nudge_stored = store_pending_nudge(analysis["nudgeText"], conv_id, config)
    return {
        "stored": stored,
        "nudge_stored": nudge_stored,
        "summary_stored": summary_stored,
        "analysis": analysis,
    }
