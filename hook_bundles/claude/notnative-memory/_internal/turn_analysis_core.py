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

# No input-size caps. This is a local-first analyzer; token cost is not a
# concern, and truncating the user prompt or model response loses signal.
# Runaway protection comes from MEMORY_EXTRACT_TIMEOUT (urllib whole-request
# timeout) plus the outer hook-process timeout, not from input slicing.

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
        mcp_url: str = "http://127.0.0.1:9500/mcp",
        mcp_headers: Optional[dict] = None,
        temperature: float = 0.1,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: int = DEFAULT_LLM_TIMEOUT,
        min_conversation_chars: int = DEFAULT_MIN_CONVERSATION_CHARS,
        max_extractions: int = DEFAULT_MAX_EXTRACTIONS,
        disable_reasoning: bool = False,
        project: str = "_global",
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
        # When true and api == 'openai_compat', the LLM request includes
        # chat_template_kwargs={"enable_thinking": false}. Backends that
        # honor this kwarg (LM Studio, vLLM, llama.cpp Qwen3 templates)
        # skip the hidden <think> phase, which the analyzer doesn't need.
        # No-op on anthropic_messages.
        self.disable_reasoning = disable_reasoning
        # NNM project scope to write into. Must be a valid write target —
        # '_global', '_domain_<name>', or an absolute path. The server-side
        # default 'general' is rejected, so this MUST be supplied explicitly
        # or every store silently fails with stored=false.
        self.project = project


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

    mcp_url = e.get("MEMORY_MCP_URL", "http://127.0.0.1:9500/mcp")
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
    disable_reasoning = e.get("MEMORY_EXTRACT_DISABLE_REASONING", "").strip().lower() in ("1", "true", "yes")
    project = e.get("MEMORY_EXTRACT_PROJECT", "_global").strip() or "_global"

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
        disable_reasoning=disable_reasoning,
        project=project,
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

    Extraction emits two channels because NNM treats them differently:

    - state_assertions: mutable facts about the world right now
      (subject/predicate/object triples). Routed to memory_fact_add,
      where conflicting later values auto-supersede the old one with a
      timestamp. Example: ("inference-host", "model", "qwen3-30b-a3b").
    - results: durable observations, rules, preferences, decisions.
      Routed to memory_store. Carry tags, importance, and class
      taxonomy; never auto-superseded.

    The split exists because retrieval treats them differently too:
    fact_query returns "what's true right now"; memory_search returns
    narrative context that may be stale. If extraction sends a piece
    of mutable state through the memory channel, retrievers will surface
    its outdated form forever.
    """
    return (
        "Analyze this conversation turn and return a single JSON object with FOUR sections.\n"
        "\n"
        'SECTION 1A: State assertions ("state_assertions")\n'
        "\n"
        '  Each item is { "subject": "...", "predicate": "...", "object": "...", "confidence": 0.0-1.0 }\n'
        "\n"
        "  Rules for state_assertions:\n"
        "    1. ONLY use this section for assertions about mutable state that is TRUE RIGHT NOW\n"
        "       and could change later. Configuration choices, infrastructure state, deployed\n"
        "       versions, current selections, current values.\n"
        "    2. Subject is the entity (a server, service, component, project, person).\n"
        "    3. Predicate is the aspect/relationship (\"port\", \"version\", \"model\", \"branch\").\n"
        "    4. Object is the current value as a short string.\n"
        "    5. Keep all three fields short and concrete; no full sentences.\n"
        "    6. Confidence is a float 0.0-1.0. Default 0.9 if unsure.\n"
        "    7. If the same predicate had a previous value mentioned in the turn, emit only the\n"
        "       NEW assertion. Supersession is handled downstream by timestamp.\n"
        "    8. DO NOT use this section for rules, preferences, decisions, or anything that does\n"
        "       not change over time. Those go to SECTION 1B.\n"
        "    9. Empty array is fine and common. Most turns produce zero state assertions.\n"
        "\n"
        "  Examples of well-formed state_assertions:\n"
        '    - {"subject": "inference-host", "predicate": "model", "object": "qwen3-30b-a3b", "confidence": 0.95}\n'
        '    - {"subject": "nnm", "predicate": "mcp-port", "object": "9500", "confidence": 1.0}\n'
        '    - {"subject": "nna", "predicate": "default-provider", "object": "lmstudio", "confidence": 0.9}\n'
        "\n"
        "  Examples of BAD state_assertions (do not produce these):\n"
        '    - {"subject": "user", "predicate": "prefers", "object": "terse responses"} (preference → SECTION 1B)\n'
        '    - {"subject": "powershell", "predicate": "requires", "object": "single quotes"} (rule → SECTION 1B)\n'
        '    - {"subject": "session", "predicate": "discussed", "object": "memory architecture"} (meta → drop)\n'
        "\n"
        'SECTION 1B: Learnable observations ("results")\n'
        "\n"
        '  Each item is { "fact": "...", "tags": ["..."], "confidence": "high|medium|low", "source": "user-stated|tool-result|model-inferred" }\n'
        "\n"
        "  Choosing source:\n"
        '    - "user-stated": the user explicitly said the rule, preference, or decision in the\n'
        "      USER half of this turn. Highest curation value; downstream treats these as gospel.\n"
        '    - "tool-result": the lesson comes from a concrete tool output in the MODEL half\n'
        "      (file contents, command stderr, API response). Verifiable and stable.\n"
        '    - "model-inferred": neither of the above — you inferred the lesson from reasoning\n'
        "      about the turn. Default when in doubt; downstream applies stronger curation.\n"
        "    DO NOT label your own inferences as user-stated; that pollutes the source signal\n"
        "    and removes the model from later supervision loops.\n"
        "\n"
        "  Rules for each fact:\n"
        "    1. One self-contained sentence that reads correctly in isolation, weeks later.\n"
        "    2. Be terse. Add a brief reason only when the rule wouldn't stand alone without it.\n"
        "       Most facts don't need one. Don't force a why-clause when the action is self-evident.\n"
        "    3. State rules and preferences as rules, not as observations about a speaker.\n"
        '         Bad:  "The user said responses should be terse."\n'
        '         Good: "Keep responses terse unless asked for details."\n'
        '    4. Do not reference "the user", "this turn", "above", "the conversation",\n'
        "       or any speaker. The fact must stand alone with no context dependency.\n"
        "    5. One thought per entry. If a fact has two unrelated parts, split it into two entries.\n"
        "    6. Soft length target ~25 words. If you can't say it in one short sentence,\n"
        "       you have two thoughts: split them.\n"
        "    7. Tags are short lowercase keywords useful for later filtering\n"
        '       (e.g. "shell", "powershell", "correction", "preference", "infra", "tool-failure").\n'
        "    8. Skip an entry entirely if it cannot stand alone usefully. Better empty than noisy.\n"
        "\n"
        "  Volume: extract every standalone learnable fact present in the turn. If a turn legitimately\n"
        "  yields 30 facts, return 30. There is no quota and no upper target.\n"
        "\n"
        "  Examples of well-formed facts:\n"
        '    - "Em-dashes must never appear in any text output."\n'
        '    - "Use single quotes when shelling PowerShell from bash; bash mangles double quotes first."\n'
        '    - "Keep responses terse unless asked for details."\n'
        '    - "NNM RAG chunks are 2000 chars with 250-char overlap, sharing the memory embedding space."\n'
        "\n"
        "  Examples of BAD extractions (do not produce these):\n"
        '    - "The user prefers terse responses." (references the user; reframe as a rule)\n'
        '    - "This turn discussed PowerShell quoting." (meta about the turn, not a fact)\n'
        '    - "When writing a PowerShell script from bash, use single quotes around strings\n'
        "       containing special characters; bash's escape rendering will otherwise corrupt\n"
        '       double-quoted strings before PowerShell sees them." (run-on; same idea fits in one short sentence)\n'
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
        '  "state_assertions": [\n'
        '    { "subject": "...", "predicate": "...", "object": "...", "confidence": 0.9 }\n'
        "  ],\n"
        '  "results": [\n'
        '    { "fact": "...", "tags": ["..."], "confidence": "high", "source": "model-inferred" }\n'
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
        f"{user_prompt}\n"
        "\n"
        "--- MODEL ---\n"
        f"{model_response}\n"
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
        '  Each item is { "fact": "...", "tags": ["..."], "confidence": "high|medium|low", "source": "tool-result|model-inferred" }\n'
        "\n"
        "  Source attribution for worker runs:\n"
        '    - "tool-result": the lesson comes from a concrete observed tool output. Most worker\n'
        "      knowledge falls here — that's the whole point of a worker.\n"
        '    - "model-inferred": you inferred the pattern from reasoning rather than direct\n'
        '      tool evidence. Use sparingly. Never use "user-stated" — workers have no human in the loop.\n'
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
        "    - Be terse. Add a brief reason only when the action is not self-evident.\n"
        "    - No references to \"the worker\", \"this run\", \"the task above\".\n"
        "    - Soft length target ~25 words. If you can't say it in one short sentence,\n"
        "      you have two thoughts: split them.\n"
        "    - Tags are short lowercase keywords; include the affected\n"
        '       vendor or system if applicable (e.g. "vendor:acme", "scrape",\n'
        '       "api:stripe", "selector").\n'
        "\n"
        "  Volume: extract every durable lesson present. If a worker run produced\n"
        "  20 distinct vendor quirks, return all 20.\n"
        "\n"
        "  Examples of well-formed worker facts:\n"
        '    - "Acme pricing lives in .product-tile data-price, not the visible text."\n'
        '    - "Stripe checkout sessions older than 24h return 410 GONE; skip the retrieve, don\'t retry."\n'
        '    - "Cloudflare-protected vendor pages need the headless-browser tool, not raw requests."\n'
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
        '    { "fact": "...", "tags": ["..."], "confidence": "high", "source": "tool-result" }\n'
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
        f"{task_envelope}\n"
        "\n"
        "--- WORKER OUTPUT ---\n"
        f"{worker_output}\n"
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
        "state_assertions": [],
        "results": [],
        "unfulfilledPromises": [],
        "shouldNudge": False,
        "nudgeText": "",
        "summary": "",
    }


def coerce_analysis(parsed: dict) -> dict:
    """Coerce LLM output into the canonical shape, filling defaults."""
    return {
        "state_assertions": (
            parsed.get("state_assertions", [])
            if isinstance(parsed.get("state_assertions"), list)
            else []
        ),
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


def _log_analysis_failure(reason: str) -> None:
    """Append a single-line failure record to the analyzer log so silent
    LLM-call problems are observable.

    Adapters set MEMORY_EXTRACT_LOG to their per-harness log path; we
    reuse it so successes and failures land in the same file. When the
    env var is unset (e.g. core called in isolation), fall back to a
    neutral path so the failure is at least recoverable.
    """
    log_path = os.environ.get(
        "MEMORY_EXTRACT_LOG",
        os.path.expanduser("~/.nnm_turn_analysis_failures.log"),
    )
    try:
        parent = os.path.dirname(log_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(
                f"{datetime.datetime.now().isoformat(timespec='seconds')}\t"
                f"failure\treason={reason}\n"
            )
    except OSError:
        pass


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
        _log_analysis_failure(
            "no_model_resolved: set MEMORY_EXTRACT_MODEL or ensure /models is reachable"
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
        if config.disable_reasoning:
            # LM Studio / vLLM / llama.cpp Qwen3 honor this kwarg to skip
            # the hidden <think> phase. Reasoning is wasted compute for a
            # classifier prompt and burns the subprocess timeout budget.
            body["chat_template_kwargs"] = {"enable_thinking": False}

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
        _log_analysis_failure(f"llm_call_failed: {type(exc).__name__}: {exc}")
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
        preview = (content or "").strip()[:160].replace("\n", " ")
        _log_analysis_failure(f"unparseable_json: content_preview={preview!r}")
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


def _mcp_call_stored(envelope: dict) -> bool:
    """Return True if an MCP tools/call response indicates a successful write.

    The MCP server wraps tool results as:

        {"result": {"content": [{"type":"text","text": "{...inner json...}"}],
                    "isError": false}}

    A truthy ``result`` field at the JSON-RPC envelope level only means the
    HTTP/JSON-RPC call itself succeeded. The actual storage outcome lives
    inside ``content[0].text`` as a JSON string with a ``"stored"`` flag.
    A non-stored response looks like:

        {"stored": false, "error": "project 'general' is not a valid write target..."}

    Pre-fix the analyzer counted these as successes — every "store" silently
    failed but the log reported ``extracted=N``. This helper is the contract.
    """
    if not isinstance(envelope, dict):
        return False
    result = envelope.get("result")
    if not isinstance(result, dict):
        return False
    if result.get("isError"):
        return False
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return False
    first = content[0]
    if not isinstance(first, dict):
        return False
    text = first.get("text")
    if not isinstance(text, str):
        return False
    try:
        inner = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return bool(inner.get("stored"))


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
                "project": config.project,
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
            envelope = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        _log_analysis_failure(f"rag_ingest_failed: {type(exc).__name__}: {exc}")
        return False
    if not _mcp_call_stored(envelope):
        # Server returned a structured failure — surface the inner text so
        # the cause (e.g. invalid project, auth, schema) is in the log.
        try:
            inner = envelope.get("result", {}).get("content", [{}])[0].get("text", "")[:200]
        except Exception:  # noqa: BLE001
            inner = "<unparseable>"
        _log_analysis_failure(f"rag_ingest_rejected: title={title!r} inner={inner!r}")
        return False
    return True


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
                "project": config.project,
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
            envelope = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        preview = content[:60].replace("\n", " ")
        _log_analysis_failure(f"memory_store_failed: preview={preview!r} {type(exc).__name__}: {exc}")
        return False
    if not _mcp_call_stored(envelope):
        try:
            inner = envelope.get("result", {}).get("content", [{}])[0].get("text", "")[:200]
        except Exception:  # noqa: BLE001
            inner = "<unparseable>"
        preview = content[:60].replace("\n", " ")
        _log_analysis_failure(f"memory_store_rejected: preview={preview!r} inner={inner!r}")
        return False
    return True


def memory_fact_add_call(
    subject: str,
    predicate: str,
    obj: str,
    confidence: float,
    config: AnalysisConfig,
) -> bool:
    """POST a single memory_fact_add call to the MCP. Returns True on success.

    Used by store_fact_assertions to land each extracted state assertion
    as a temporal fact triple. Conflicting later values auto-supersede
    via timestamp; that is the whole reason this channel exists separate
    from memory_store.
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "memory_fact_add",
            "arguments": {
                "project": config.project,
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "confidence": confidence,
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
            envelope = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        triple = f"{subject!r}/{predicate!r}/{obj!r}"
        _log_analysis_failure(
            f"memory_fact_add_failed: triple={triple} {type(exc).__name__}: {exc}"
        )
        return False
    if not _mcp_call_stored(envelope):
        try:
            inner = envelope.get("result", {}).get("content", [{}])[0].get("text", "")[:200]
        except Exception:  # noqa: BLE001
            inner = "<unparseable>"
        triple = f"{subject!r}/{predicate!r}/{obj!r}"
        _log_analysis_failure(f"memory_fact_add_rejected: triple={triple} inner={inner!r}")
        return False
    return True


def _coerce_fact_confidence(value) -> float:
    """Coerce LLM-reported confidence into the 0.0-1.0 float fact_add expects.

    Accepts floats, ints, numeric strings, and the high/medium/low string
    vocabulary (mapped to 1.0/0.8/0.5). Unknown shapes default to 0.8.
    """
    if isinstance(value, bool):
        return 0.8
    if isinstance(value, (int, float)):
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.8
    if isinstance(value, str):
        v = value.strip().lower()
        if v == "high":
            return 1.0
        if v == "medium":
            return 0.8
        if v == "low":
            return 0.5
        try:
            return max(0.0, min(1.0, float(v)))
        except ValueError:
            return 0.8
    return 0.8


def store_fact_assertions(items: list, config: AnalysisConfig) -> int:
    """Store extracted state assertions as NNM temporal facts.

    Each item must be a dict with non-empty string subject, predicate,
    and object fields. Confidence is optional (default 0.8) and coerced
    to the 0.0-1.0 float NNM expects.

    The same runaway-hedge cap (config.max_extractions) applies — facts
    and observations share the cap so a malfunctioning LLM cannot blow
    out either channel.
    """
    stored = 0
    for item in items[: config.max_extractions]:
        if not isinstance(item, dict):
            continue
        subject = item.get("subject")
        predicate = item.get("predicate")
        obj = item.get("object")
        if not all(isinstance(v, str) and v.strip() for v in (subject, predicate, obj)):
            continue
        confidence = _coerce_fact_confidence(item.get("confidence", 0.8))

        if memory_fact_add_call(
            subject.strip(),
            predicate.strip(),
            obj.strip(),
            confidence,
            config,
        ):
            stored += 1

    return stored


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


_VALID_SOURCES = frozenset({"user-stated", "tool-result", "model-inferred"})


def _coerce_source(value) -> str:
    """Map the LLM's per-item source label to a valid NNM source kind.

    Defaults to 'model-inferred' on anything unrecognized. The default
    is intentionally pessimistic: misclassifying a model-inferred fact
    as user-stated removes it from downstream supervision; the reverse
    just costs us a curation pass.
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _VALID_SOURCES:
            return v
    return "model-inferred"


def store_extractions(items: list, conversation_id: str, config: AnalysisConfig) -> int:
    """Store extracted facts to NotNativeMemory as discrete memories.

    Each item must be a dict with at minimum a non-empty 'fact' string.
    Optional fields:
      - 'tags' (list of strings): used verbatim as memory tags.
      - 'confidence' (high|medium|low): mapped to importance.
      - 'source' (user-stated|tool-result|model-inferred): attribution.
        Unrecognized values default to 'model-inferred'.

    Each fact is stored verbatim as the memory content. No template wrapping,
    no metadata headers in the body. Source attribution lets downstream
    curation distinguish gospel (user-stated), verifiable (tool-result),
    and inference (model-inferred) without re-reading every memory.

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
        source = _coerce_source(item.get("source"))

        if memory_store_call(fact, tags, importance, source, config):
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
        "facts_stored": 0,
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
    facts_stored = store_fact_assertions(analysis["state_assertions"], config)
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
        "facts_stored": facts_stored,
        "nudge_stored": nudge_stored,
        "summary_stored": summary_stored,
        "analysis": analysis,
    }
