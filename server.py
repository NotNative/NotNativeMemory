"""
NotNativeMemory - MCP Memory Server

Persistent, vector-backed memory for Claude Code and LM Studio sessions.
Stores memories with semantic embeddings in Postgres/pgvector. Memories
survive context compaction, session boundaries, and model changes.

Supports two transport modes:
    stdio  - launched as a child process by Claude Code / LM Studio (default)
    http   - runs as a network service, clients connect remotely

Usage:
    python server.py              # stdio mode (launched by MCP client)
    python server.py --http       # HTTP mode on default port 9500
    python server.py --http 9500  # HTTP mode on custom port

Tools:
    memory_store   - Save a memory with tags and importance
    memory_search  - Find relevant memories by semantic similarity
    memory_forget  - Remove a memory by ID
    memory_list    - List memories with optional filters
"""

import logging
import os
import sys
from typing import Optional
from uuid import UUID

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

_log = logging.getLogger("notnative.server")

from lib.observability import instrumented

# Default port for HTTP transport mode
_DEFAULT_HTTP_PORT = 9500

mcp = FastMCP(
    "NotNativeMemory",
    json_response=True,
    stateless_http=True,
)


def _tool_error(tool_name: str, exc: Exception, empty: dict) -> dict:
    """
    Convert an unexpected tool-handler exception into a structured
    response. Each tool returns its own happy-path shape on success
    (e.g. {"results": [], "count": 0} for memory_search), so callers
    that iterate results[...] do not crash on the error path. The
    exception is logged once with traceback so operators can find it.

    The returned dict carries a private `_exception_type` key that the
    @instrumented decorator uses to populate the tool_errors counter
    label and the exception_type field in the structured event log.
    The decorator strips this key before the dict reaches the MCP
    client, so it is purely an internal channel.
    """
    _log.exception("%s failed: %s", tool_name, exc)
    return {
        **empty,
        "error": f"{type(exc).__name__}: {exc}",
        "_exception_type": type(exc).__name__,
    }

# Register the /auth/* and /health routes on the FastMCP instance.
# Runs at import time so the routes are present by the time anyone
# calls streamable_http_app() in either stdio-warmup or HTTP mode.
from lib.auth_routes import register_routes as _register_auth_routes
from lib.web_routes import register_routes as _register_web_routes
from lib.observability import register_routes as _register_observability_routes
_register_auth_routes(mcp)
_register_web_routes(mcp)
_register_observability_routes(mcp)


# Set to True when running in HTTP mode. In HTTP mode, the server's
# working directory is meaningless (it's wherever the server started).
# Falls back to "general" instead of os.getcwd().
_http_mode = False


def _detect_project_directory() -> str:
    """
    Detect the current project directory.

    stdio mode: Claude Code sets the working directory, so os.getcwd()
    is the right call. HTTP mode: working directory is meaningless,
    falls back to an env var or returns the "general" sentinel that
    the write-path validator rejects so callers can't silently pool
    writes into an unintended bucket.
    """
    default = os.environ.get("MEMORY_DEFAULT_PROJECT", "")
    if default:
        return default

    if not _http_mode:
        cwd = os.getcwd()
        if cwd and cwd != "/":
            return os.path.abspath(cwd)

    return "general"


# Reserved scope names exposed as write targets.
_GLOBAL_SCOPE = "_global"
_DOMAIN_PREFIX = "_domain_"


def _normalize_project(project: Optional[str]) -> str:
    """
    Normalize `project` into a canonical form used by both store and
    read paths, so `memory_store` and `memory_search` always agree on
    the DB key. Applied at every tool entry point.

    Rules:
      - None or "" -> fall through to _detect_project_directory()
      - `_global` / `_domain_<name>` -> returned verbatim
      - Absolute path -> os.path.normpath() (collapses slashes, dots)
      - Anything else -> returned as-is (validator will reject)
    """
    if project is None or not project.strip():
        project = _detect_project_directory()

    value = project.strip()

    if value == _GLOBAL_SCOPE or value.startswith(_DOMAIN_PREFIX):
        return value

    # Only normalize paths. Rel paths get caught by the validator next.
    looks_absolute = (
        value.startswith("/")
        or value.startswith("\\")
        or (len(value) >= 3 and value[1] == ":" and value[2] in ("/", "\\"))
    )
    if looks_absolute:
        return os.path.normpath(value)

    return value


def _validate_writable_scope(project: str) -> Optional[str]:
    """
    Return an error message if `project` is not a valid write target,
    or None if the value is acceptable. Read paths stay permissive and
    do not call this: historical scopes like "general" still need to
    be searchable even though we no longer accept writes to them.

    Accepted:
        "_global"                     (global scope)
        "_domain_<name>"              (domain scope, non-empty name)
        absolute path (Unix/Windows)  (local scope)
    Rejected:
        empty, "general", bare names, relative paths.
    """
    if not project or not project.strip():
        return "project is required for writes (pass an explicit value)"

    value = project.strip()

    if value == _GLOBAL_SCOPE:
        return None

    if value.startswith(_DOMAIN_PREFIX):
        domain_name = value[len(_DOMAIN_PREFIX):]
        if not domain_name:
            return (
                f"invalid domain scope: {value!r} "
                f"(expected {_DOMAIN_PREFIX}<name> with non-empty name)"
            )
        return None

    # Absolute path heuristic covers Unix (/foo, //server/share) and
    # Windows (C:\..., C:/...). Relative paths ("scratch", "general")
    # and bare identifiers are rejected — the silent fall to "general"
    # was the single biggest source of mis-scoped writes observed
    # through 2026-04-18.
    if value.startswith("/") or value.startswith("\\"):
        return None
    if len(value) >= 3 and value[1] == ":" and value[2] in ("/", "\\"):
        return None

    return (
        f"project {value!r} is not a valid write target. "
        f"Use {_GLOBAL_SCOPE!r}, "
        f"{_DOMAIN_PREFIX}<name>, or an absolute path."
    )


def _tool_auth_and_project(
    project: Optional[str],
    empty_shape: dict,
    *,
    writable: bool = False,
):
    """
    Shared preamble for MCP tool handlers that take a ``project`` arg.

    Returns ``(owner, project_dir, None)`` on success or
    ``(None, None, error_dict)`` when auth or scope validation fails.
    ``error_dict`` already folds in ``empty_shape`` so the handler can
    return it verbatim. On scope-validation failure the dict also
    carries the normalized ``project`` so the caller sees which scope
    was rejected.
    """
    from lib.auth_context import current_user_id

    owner = current_user_id()
    if owner is None:
        return None, None, {"error": "authentication required", **empty_shape}

    project_dir = _normalize_project(project)
    if writable:
        scope_err = _validate_writable_scope(project_dir)
        if scope_err:
            return None, None, {
                "error": scope_err, **empty_shape, "project": project_dir,
            }
    return owner, project_dir, None


@mcp.tool()
@instrumented("memory_store")
async def memory_store(
    content: str,
    tags: Optional[list[str]] = None,
    importance: str = "normal",
    project: Optional[str] = None,
    verbatim: bool = False,
) -> dict:
    """
    Preserve something you've learned that the user will need again —
    across compactions, sessions, and model changes. This is your hedge
    against context loss: anything stored here survives when your working
    memory does not.

    Memories get read back by any model that uses this MCP, from Opus
    to Qwen3 30B. Write for the widest audience.

    WHEN to use:
    - The user corrects you or states a preference — store it so you
      never make them repeat it ("no em dashes", "always use local tz").
    - A decision is made — store it with the reasoning, not just the
      outcome ("chose HS256 because single-tenant, simpler key mgmt").
    - You discover a constraint or gotcha that isn't obvious from the
      code ("this model loses instructions after two compactions").
    - The session sets a boundary ("read-only review, do not edit files")
      — store as critical so it surfaces even after compaction.

    WHEN NOT to use:
    - Ephemeral task state (what file am I editing right now) — that's
      your working context, not long-term memory.
    - Things already in the codebase — read the code instead.
    - Facts about current state that will change (use memory_fact_add).

    HOW to write (the memory content itself):
    - Short sentences. Target 15 to 25 words. No deeply nested clauses.
    - Imperative voice for rules: "Do X," not "X should be done" or
      "you might consider doing X."
    - Plain technical English. Common jargon (API, regex, CI, Bearer
      token, linter) is fine. Avoid literary words when a plain one
      carries the same meaning: "tangential" becomes "not directly
      about what was asked"; "substantive" becomes "real."
    - For rule-shaped memories, include a **Why:** line (the reason)
      and a **How to apply:** line (when it kicks in). These are
      structural anchors that any reader can latch onto.
    - Don't reference other memories by name or reason about how they
      compose. Each memory stands alone.
    - Write for a reader who has technical background but has not seen
      this project or this conversation before.

    Tags are auto-detected from content (decision, preference, gotcha,
    correction, constraint), so you don't need to get tagging perfect.
    Duplicates are auto-merged — storing the same insight twice updates
    rather than duplicates.

    Args:
        content: The memory text. Be specific and self-contained —
            future you has no context about this session.
        tags: Optional categorization tags. Auto-classification adds
            more based on content, so these are supplemental.
        importance: Controls search ranking and eviction priority.
            critical = surfaces in every relevant search, never evicted.
            high = prominent in results, very slow to cool.
            normal = standard memory.
            low = nice to have, first to be evicted under pressure.
        project: Where this memory belongs in the scope hierarchy.
            Auto-detected (local project) if omitted.

            Pass "_global" to store a memory that applies to EVERY
            project — user preferences, coding style rules, things
            that aren't tied to one codebase.

            Pass "_domain_<name>" to store a memory that applies to
            any project declaring that domain — e.g. "_domain_python"
            for Python patterns, "_domain_powershell" for PS gotchas,
            "_domain_docker" for container patterns. Local projects
            pick up these memories by running memory_project_configure
            with the matching domain name.

            Pass a real path (default) for project-specific memories.

            Prefer broader scopes when the knowledge is portable —
            gotchas and patterns that apply everywhere shouldn't be
            trapped in one project.
        verbatim: Set true when the full text matters — reasoning chains,
            user explanations, or conversation context that would lose
            value if you summarized it. Adds a "verbatim" tag.
    """
    if not content or not content.strip():
        return {"error": "Content cannot be empty", "stored": False}

    from lib.embeddings import embed
    from lib.db import store_memory, get_or_create_project
    from lib.limits import (
        MAX_MEMORY_CONTENT_BYTES,
        MAX_TAG_BYTES,
        PayloadTooLarge,
        enforce_field_len,
    )

    # Bound per-field sizes before we pay for embedding and DB work.
    try:
        enforce_field_len(content, MAX_MEMORY_CONTENT_BYTES, "content")
        for t in (tags or []):
            enforce_field_len(t, MAX_TAG_BYTES, "tag")
    except PayloadTooLarge as exc:
        return {"error": str(exc), "stored": False}

    owner, project_dir, err = _tool_auth_and_project(
        project, {"stored": False}, writable=True,
    )
    if err:
        return err

    store_tags = list(tags or [])
    if verbatim and "verbatim" not in store_tags:
        store_tags.append("verbatim")

    try:
        project_id = await get_or_create_project(project_dir, owner)
        embedding = embed(content)
        memory_id = await store_memory(
            content=content,
            embedding=embedding,
            project_id=project_id,
            owner_user_id=owner,
            tags=store_tags,
            importance=importance,
        )
    except Exception as exc:
        return _tool_error("memory_store", exc, {"stored": False})

    return {"id": str(memory_id), "stored": True}


@mcp.tool()
@instrumented("memory_search")
async def memory_search(
    query: str,
    limit: int = 10,
    project: Optional[str] = None,
    tags: Optional[list[str]] = None,
    min_importance: Optional[str] = None,
) -> dict:
    """
    Recall what you've learned before about a specific topic. This is
    your primary recovery tool — use it whenever you suspect relevant
    context exists but isn't in your current window.

    WHEN to use:
    - You're about to make a decision and want to check if the user
      has already expressed a preference or made a prior choice.
    - The user references something from a past session or says
      "we talked about this" / "remember when" / "like last time."
    - After context compaction — search for the topic you were just
      working on to recover lost detail.
    - Before starting work in an unfamiliar area of the codebase —
      past sessions may have captured gotchas or constraints.
    - You feel uncertain about a convention or approach — search
      before guessing.

    Use memory_context instead when you just need the critical working
    set without a specific question (e.g. session start, before Bash).

    Scope behavior: when searching from a local project, results
    automatically include global memories plus any domain memories
    matching that project's declared domains. Each result reports its
    scope (local/domain/global) so you can see where it came from.

    Args:
        query: Natural language — describe what you're looking for as
            if asking a colleague. "How does auth work in this project"
            beats "auth."
        limit: Max results (1-100, default 10).
        project: Project scope. Auto-detected if omitted.
            Pass empty string to search across all projects regardless
            of scope.
        tags: Filter to specific memory types (e.g. ["decision"]).
        min_importance: Floor — "high" excludes normal and low memories.
    """
    if not query or not query.strip():
        return {"error": "Query cannot be empty", "results": [], "count": 0}

    from lib.embeddings import embed
    from lib.db import search_memories, get_or_create_project

    owner, project_dir, err = _tool_auth_and_project(
        project, {"results": [], "count": 0},
    )
    if err:
        return err

    try:
        project_id = await get_or_create_project(project_dir, owner)
        query_embedding = embed(query)
        results = await search_memories(
            query_embedding=query_embedding,
            project_id=project_id,
            owner_user_id=owner,
            tags=tags,
            min_importance=min_importance,
            limit=limit,
        )
    except Exception as exc:
        return _tool_error("memory_search", exc,
                           {"results": [], "count": 0})

    return {"results": results, "count": len(results)}


@mcp.tool()
@instrumented("memory_forget")
async def memory_forget(memory_id: str) -> dict:
    """
    Delete a memory that is wrong, outdated, or actively harmful to
    keep. You are the curator — stale memories poison future sessions
    by resurfacing bad context.

    WHEN to use:
    - You discover a stored memory contradicts current reality — the
      decision was reversed, the constraint was lifted, the preference
      changed. Delete the old one, store the new one.
    - A memory is causing confusion — it's ambiguous, misleading, or
      missing enough context to be misinterpreted by a future session.
    - The user tells you to forget something.

    WHEN NOT to use:
    - The memory is still true but just old — age alone isn't a reason
      to forget. The thermal system handles natural decay.
    - For facts that changed — use memory_fact_add instead, which
      preserves history by invalidating rather than deleting.

    Args:
        memory_id: UUID of the memory to remove (from search/list results).
    """
    from lib.db import forget_memory
    from lib.auth_context import current_user_id

    owner = current_user_id()
    if owner is None:
        return {"forgotten": False, "error": "authentication required"}

    try:
        uid = UUID(memory_id)
    except ValueError:
        return {"forgotten": False, "error": "Invalid memory ID format"}

    try:
        deleted = await forget_memory(uid, owner)
    except Exception as exc:
        return _tool_error("memory_forget", exc, {"forgotten": False})

    return {"forgotten": deleted}


@mcp.tool()
@instrumented("memory_list")
async def memory_list(
    project: Optional[str] = None,
    tags: Optional[list[str]] = None,
    limit: int = 20,
) -> dict:
    """
    Browse what's been stored — for curation, not recall. Use this when
    you need to see the inventory rather than find a specific memory.

    WHEN to use:
    - The user asks "what do you remember?" or "what's stored?" — this
      gives them a reviewable list, not a semantic best-guess.
    - You want to audit a tag category — e.g. list all "decision" or
      "correction" memories to check for contradictions.
    - Before a cleanup pass — list low-importance or old memories to
      decide what to forget.
    - The user wants to see memories across projects (pass empty string
      for project).

    Use memory_search instead when you have a specific question — list
    is for browsing, search is for answering.

    Args:
        project: Project scope. Auto-detected if omitted.
            Pass empty string to list across all projects.
        tags: Filter to specific types (e.g. ["decision", "gotcha"]).
        limit: Max results (1-100, default 20).
    """
    from lib.db import list_memories, get_or_create_project

    owner, project_dir, err = _tool_auth_and_project(
        project, {"memories": [], "count": 0},
    )
    if err:
        return err

    try:
        project_id = await get_or_create_project(project_dir, owner)
        results = await list_memories(
            owner_user_id=owner,
            project_id=project_id,
            tags=tags,
            limit=limit,
        )
    except Exception as exc:
        return _tool_error("memory_list", exc,
                           {"memories": [], "count": 0})

    return {"memories": results, "count": len(results)}


@mcp.tool()
@instrumented("memory_fact_add")
async def memory_fact_add(
    subject: str,
    predicate: str,
    object: str,
    project: Optional[str] = None,
    confidence: float = 1.0,
) -> dict:
    """
    Record a fact that is true RIGHT NOW but may change later. Unlike
    memories (which capture observations and decisions that are always
    valid in their original context), facts track mutable state — and
    when the state changes, the old fact is preserved with a timestamp
    rather than deleted.

    Facts get read back by any model that uses this MCP, from Opus to
    Qwen3 30B. Keep subject, predicate, and object short and concrete.

    WHEN to use:
    - Infrastructure state: what model runs on which server, what port
      a service uses, what version is deployed. These change during
      upgrades and you need to track both current and historical state.
    - Configuration choices that evolve: auth algorithm, default branch
      name, primary database host. When these change, the old value
      matters for understanding past decisions.
    - Any assertion where "what was it before?" is a question someone
      might ask later.

    WHEN NOT to use:
    - Decisions and preferences — those are memories. "We chose HS256"
      is a decision (memory_store). "auth uses HS256" is a fact (here).
    - One-time observations or gotchas — those don't change, use
      memory_store.

    Conflicting facts auto-resolve: if you add ("auth", "algorithm",
    "RS256") and ("auth", "algorithm", "HS256") already exists, the
    old fact gets a valid_to timestamp. No manual cleanup needed.

    Args:
        subject: The entity — a server name, service, component.
        predicate: The relationship — what aspect of the subject.
        object: The current value.
        project: Project scope. Auto-detected if omitted.
        confidence: How certain you are (0.0-1.0). Default 1.0.
    """
    if not subject or not subject.strip():
        return {"error": "Subject cannot be empty", "stored": False}
    if not predicate or not predicate.strip():
        return {"error": "Predicate cannot be empty", "stored": False}
    if not object or not object.strip():
        return {"error": "Object cannot be empty", "stored": False}

    from lib.db import add_fact, get_or_create_project
    from lib.limits import (
        MAX_FACT_FIELD_BYTES,
        PayloadTooLarge,
        enforce_field_len,
    )

    try:
        enforce_field_len(subject, MAX_FACT_FIELD_BYTES, "subject")
        enforce_field_len(predicate, MAX_FACT_FIELD_BYTES, "predicate")
        enforce_field_len(object, MAX_FACT_FIELD_BYTES, "object")
    except PayloadTooLarge as exc:
        return {"error": str(exc), "stored": False}

    owner, project_dir, err = _tool_auth_and_project(
        project, {"stored": False}, writable=True,
    )
    if err:
        return err

    try:
        project_id = await get_or_create_project(project_dir, owner)
        result = await add_fact(
            project_id=project_id,
            subject=subject.strip(),
            predicate=predicate.strip(),
            obj=object.strip(),
            owner_user_id=owner,
            confidence=max(0.0, min(1.0, confidence)),
        )
    except Exception as exc:
        return _tool_error("memory_fact_add", exc, {"stored": False})

    return {"stored": True, **result}


@mcp.tool()
@instrumented("memory_fact_query")
async def memory_fact_query(
    subject: str,
    as_of: Optional[str] = None,
    project: Optional[str] = None,
) -> dict:
    """
    Look up what is (or was) true about an entity. Returns current
    facts by default, or historical facts at a specific point in time.

    WHEN to use:
    - Before making assumptions about infrastructure — "what model is
      the inference host running?" beats guessing from a memory that might be stale.
    - When debugging a regression — "what was the auth config on March
      15th?" lets you correlate changes with breakage.
    - When the user asks "what changed?" or "when did we switch?" —
      the temporal history shows exactly when facts were superseded.
    - To verify before acting — if a memory says "we use port 9432"
      but you're not sure it's current, check the fact graph.

    Use memory_search instead when you're looking for context,
    reasoning, or decisions — fact_query is for verifiable state.

    Args:
        subject: The entity to look up.
        as_of: ISO timestamp for time-travel. Omit for current state.
            Example: "2026-03-15T00:00:00Z"
        project: Project scope. Auto-detected if omitted.
            Pass empty string to search across all projects.
    """
    if not subject or not subject.strip():
        return {"error": "Subject cannot be empty", "facts": [], "count": 0}

    from lib.db import query_facts, get_or_create_project
    from lib.auth_context import current_user_id
    from datetime import datetime

    owner = current_user_id()
    if owner is None:
        return {"error": "authentication required", "facts": [], "count": 0}

    as_of_dt = None
    if as_of:
        try:
            as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except ValueError:
            return {"error": f"Invalid as_of timestamp: {as_of}", "facts": [], "count": 0}

    try:
        project_id = None
        if project is not None and project.strip():
            project_dir = _normalize_project(project)
            project_id = await get_or_create_project(project_dir, owner)

        facts = await query_facts(
            owner_user_id=owner,
            subject=subject.strip(),
            project_id=project_id,
            as_of=as_of_dt,
        )
    except Exception as exc:
        return _tool_error("memory_fact_query", exc,
                           {"facts": [], "count": 0})

    return {"facts": facts, "count": len(facts)}


@mcp.tool()
@instrumented("memory_project_configure")
async def memory_project_configure(
    domains: list[str],
    project: Optional[str] = None,
) -> dict:
    """
    Declare which shared domains a project pulls memories from. Without
    this, a local project sees only its own memories plus global memories.
    With domains declared, it also pulls from matching domain-scope
    projects — enabling cross-project knowledge sharing for language,
    tool, or platform specifics.

    WHEN to use:
    - First time the user mentions working in this project with a
      language or tool that has domain memories (Python, PowerShell,
      Docker, Postgres, etc.).
    - The user asks why a pattern they know about isn't showing up —
      the domain may need to be declared.
    - The user explicitly asks to share knowledge from X domain with
      this project.

    HOW the scope hierarchy works:
    - Store to project="_global" for universal memories (user
      preferences, coding style, communication rules).
    - Store to project="_domain_<name>" for category-level memories
      (e.g. _domain_python for Python patterns and gotchas).
    - Store to a normal path for project-specific memories (default).
    - Local projects automatically see globals; they see domains only
      if they're declared here.

    Args:
        domains: List of domain names to declare (e.g. ["python",
            "docker", "postgres"]). Must match the <name> suffix on
            existing _domain_<name> projects to have effect.
        project: Project to configure. Auto-detected if omitted.
            Only local-scope projects can declare domains.
    """
    from lib.db import (
        get_or_create_project, get_project_info, set_project_domains,
    )

    owner, project_dir, err = _tool_auth_and_project(
        project, {"configured": False},
    )
    if err:
        return err

    try:
        project_id = await get_or_create_project(project_dir, owner)
        info = await get_project_info(project_id, owner)
        if info and info["scope"] != "local":
            return {
                "error": f"Cannot set domains on a {info['scope']}-scope project",
                "configured": False,
                "project": info["name"],
                "scope": info["scope"],
            }
        updated = await set_project_domains(project_id, owner, domains)
    except Exception as exc:
        return _tool_error("memory_project_configure", exc,
                           {"configured": False})

    return {
        "configured": True,
        "project": info["name"] if info else project_dir,
        "domains": updated,
    }


@mcp.tool()
@instrumented("memory_context")
async def memory_context(
    project: Optional[str] = None,
    max_tokens: int = 500,
) -> dict:
    """
    Get your bearings quickly. Returns the most critical and actively
    relevant memories for the current project — no query needed. This
    is the "what do I need to know right now?" tool.

    WHEN to use:
    - Session start — call this first to recover your working set
      before doing anything else. Cheaper than a broad search.
    - After context compaction — you just lost detail. This gives
      you back the essentials: active constraints, critical decisions,
      hot preferences.
    - In hooks before lightweight operations (Bash, git) where a full
      semantic search would be overkill but you still need to respect
      constraints like "read-only review" or "never push to main."

    WHEN NOT to use:
    - You have a specific question — use memory_search instead.
      Context gives you the working set, not targeted answers.
    - You need to browse or audit — use memory_list.

    Results are ranked by importance first, then thermal activity —
    critical memories always surface, followed by whatever you've
    been actively working with.

    Scope behavior: automatically includes global memories and any
    domain memories matching the current project's declared domains,
    so cross-project preferences and shared patterns surface without
    needing to be stored in every project.

    Args:
        project: Project scope. Auto-detected if omitted.
        max_tokens: Token budget for the response (default 500,
            max 2000). Keeps injection lightweight.
    """
    from lib.db import get_context_memories, get_or_create_project

    owner, project_dir, err = _tool_auth_and_project(
        project, {"context": [], "count": 0},
    )
    if err:
        return err

    try:
        project_id = await get_or_create_project(project_dir, owner)
        results = await get_context_memories(
            project_id=project_id,
            owner_user_id=owner,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        return _tool_error("memory_context", exc,
                           {"context": [], "count": 0})

    return {"context": results, "count": len(results)}


_PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mcp-server.pid")

# When running as PID 1 we're the container's init process - the container
# runtime (not us) owns lifecycle. Writing a PID file there is actively
# harmful: after any restart, the new process sees the old file, finds
# "PID 1" still alive (it's itself), and aborts with "Server already
# running", crash-looping the container. /.dockerenv catches rootless or
# unusual setups where we somehow aren't PID 1 but are still in a container.
_IN_CONTAINER = os.getpid() == 1 or os.path.exists("/.dockerenv")


def _write_pid(port: int) -> None:
    """Write current PID and port to the PID file."""
    if _IN_CONTAINER:
        return
    with open(_PID_FILE, "w") as f:
        f.write(f"{os.getpid()}:{port}")


def _read_pid() -> tuple:
    """Read PID and port from PID file. Returns (pid, port) or (None, None)."""
    # In a container the PID file is meaningless (we're PID 1, file may be a
    # leftover from a pre-fix build's writable layer). Ignore it unconditionally.
    if _IN_CONTAINER:
        return None, None
    if not os.path.exists(_PID_FILE):
        return None, None
    try:
        with open(_PID_FILE, "r") as f:
            parts = f.read().strip().split(":")
            return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        print(f"Warning: malformed PID file: {_PID_FILE}", file=sys.stderr)
        return None, None


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID exists."""
    import subprocess
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        else:
            os.kill(pid, 0)
            return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _cleanup_pid() -> None:
    """Remove the PID file."""
    try:
        os.remove(_PID_FILE)
    except OSError as exc:
        print(f"Warning: could not remove PID file: {exc}", file=sys.stderr)


def _parse_port_from_args(skip_flags: tuple = ()) -> int:
    """Extract a port number from command-line arguments."""
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg in skip_flags or arg.startswith("--"):
            # Check if --http has a port argument after it
            if arg == "--http" and i + 1 < len(sys.argv):
                try:
                    return int(sys.argv[i + 1])
                except ValueError:
                    pass
            continue
        try:
            return int(arg)
        except ValueError:
            continue
    return _DEFAULT_HTTP_PORT


def _spawn_background(port: int) -> None:
    """Spawn the server as a detached background process."""
    import subprocess
    import time

    script = os.path.abspath(__file__)
    args = [sys.executable, script, str(port), "--foreground"]

    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        proc = subprocess.Popen(
            args,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        proc = subprocess.Popen(
            args,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    time.sleep(2)
    if proc.poll() is not None:
        print(f"Server failed to start (exit code {proc.returncode})")
        sys.exit(1)

    return proc


def _stop_running_server() -> tuple:
    """Stop a running server if one exists. Returns (was_running, port)."""
    pid, port = _read_pid()
    if pid and _is_process_alive(pid):
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"Stopped server (PID {pid})")
        except OSError as exc:
            print(f"Failed to stop PID {pid}: {exc}")
            sys.exit(1)
        _cleanup_pid()
        import time
        time.sleep(1)
        return True, port
    if pid:
        _cleanup_pid()
    return False, port


_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def _resolve_bind_host() -> str:
    """
    MEMORY_BIND_HOST defaults to 0.0.0.0 so existing installs that do
    not set it keep binding as they did before this change. New
    installs ship with the env var set explicitly (see install scripts)
    so the value is always visible to the operator.
    """
    return os.environ.get("MEMORY_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"


def _warn_insecure_bind(host: str) -> None:
    """
    Print a loud warning when the server is about to bind to a
    non-loopback interface without MEMORY_COOKIE_SECURE=1. The flag
    is our proxy for "operator has put this behind TLS" — without
    it, session cookies fly over plaintext and a network observer
    reads them.

    This is a warning, not a hard fail: some operators run behind a
    trusted TLS-terminating proxy and toggle COOKIE_SECURE separately.
    The message is verbose on purpose so it is impossible to miss.
    """
    if host in _LOOPBACK_HOSTS:
        return
    cookie_secure = os.environ.get("MEMORY_COOKIE_SECURE", "") in ("1", "true", "yes")
    if cookie_secure:
        return
    print("=" * 72, file=sys.stderr)
    print("  WARNING: binding to a non-loopback interface without TLS.", file=sys.stderr)
    print(f"  Bind host: {host}", file=sys.stderr)
    print("  Session cookies will travel in plaintext; anyone on the", file=sys.stderr)
    print("  network path can read them and impersonate logged-in users.", file=sys.stderr)
    print("", file=sys.stderr)
    print("  Fix: either restrict to loopback by setting", file=sys.stderr)
    print("    MEMORY_BIND_HOST=127.0.0.1", file=sys.stderr)
    print("  OR run behind a TLS-terminating reverse proxy and set", file=sys.stderr)
    print("    MEMORY_COOKIE_SECURE=1", file=sys.stderr)
    print("=" * 72, file=sys.stderr)


def _start_foreground(port: int) -> None:
    """Run the HTTP server in the foreground (attached to console)."""
    global _http_mode
    _http_mode = True
    bind_host = _resolve_bind_host()
    _warn_insecure_bind(bind_host)
    mcp.settings.host = bind_host
    mcp.settings.port = port
    mcp.settings.transport_security.enable_dns_rebinding_protection = False
    mcp.settings.transport_security.allowed_hosts = ["*"]
    mcp.settings.transport_security.allowed_origins = ["*"]

    # Build the ASGI app ourselves so we can layer middleware before
    # uvicorn starts. FastMCP's `mcp.run(transport="streamable-http")`
    # calls streamable_http_app() internally — we do the same but keep
    # the returned Starlette app around so BearerAuthMiddleware can
    # ride every incoming request.
    import uvicorn
    from lib.auth_middleware import BearerAuthMiddleware
    from lib.limits import BodySizeLimitMiddleware
    from lib.security_headers import SecurityHeadersMiddleware

    app = mcp.streamable_http_app()
    # Starlette composes middleware last-added-outermost. Order of
    # the `add_middleware` calls below, inner-to-outer:
    #
    #   BearerAuthMiddleware    — resolves identity for downstream.
    #   BodySizeLimitMiddleware — rejects oversize bodies BEFORE auth
    #                             spends scrypt cycles on them.
    #   SecurityHeadersMiddleware — outermost, so its headers land on
    #                             every response including the 413
    #                             from BodySizeLimit and the 401 from
    #                             BearerAuth.
    app.add_middleware(BearerAuthMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    # Bootstrap admin token: run before uvicorn so the operator sees
    # the banner in the terminal that started the server. Needs the
    # DB pool to be ready, which triggers the migration runner as a
    # side effect — so schema is current before we count admins.
    #
    # We run this in its own asyncio.run() and then CLOSE the pool so
    # uvicorn's event loop creates a fresh one. asyncpg pools are
    # bound to the event loop that created them; reusing the pre-
    # uvicorn pool inside uvicorn's loop would raise on first use.
    import asyncio
    from lib import admin_bootstrap, db as _db_module

    async def _bootstrap_check():
        try:
            await _db_module.get_pool()
            path = await admin_bootstrap.ensure_bootstrap_if_needed()
            if path:
                admin_bootstrap.log_bootstrap_banner(path)
        finally:
            await _db_module.close_pool()

    try:
        asyncio.run(_bootstrap_check())
    except Exception as exc:
        # Bootstrap check is best-effort: a DB blip here shouldn't
        # stop the server from starting. Log and continue; the next
        # restart will retry.
        print(f"admin bootstrap check skipped: {exc}", file=sys.stderr)

    _write_pid(port)
    print(f"NotNativeMemory MCP server starting on http://{bind_host}:{port} (foreground)")
    try:
        uvicorn.run(app, host=bind_host, port=port, log_level="info")
    finally:
        _cleanup_pid()


async def _cli_create_user(username: str) -> int:
    """
    Create a user from the command line. Prompts for the password on
    stdin (hidden input). Useful for solo-mode installs and for
    bootstrapping the first account on a multi-user deployment before
    opening HTTP registration.
    """
    import getpass
    from lib import auth_db
    import asyncpg

    password = getpass.getpass(f"Password for {username!r}: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.", file=sys.stderr)
        return 1

    try:
        user = await auth_db.create_user(username, password)
    except asyncpg.UniqueViolationError:
        print(f"Username {username!r} is already taken.", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    print(f"Created user {user['username']} ({user['id']}).")
    print("Next step: login to get a Bearer token.")
    print(f"  curl -X POST http://localhost:{_DEFAULT_HTTP_PORT}/auth/login "
          f"-H 'Content-Type: application/json' "
          f"-d '{{\"username\":\"{user['username']}\",\"password\":\"...\"}}'")
    return 0


async def _cli_reset_admin() -> int:
    """
    Clear the admin role on every user currently flagged, bump each of
    their token_generation counters so outstanding sessions die, and
    remove any stale bootstrap token file so the next server start
    regenerates a fresh one.

    No HTTP path toggles is_admin; this CLI and the claim-admin flow
    are the only writers. Running this always succeeds as a no-op
    when no admin exists (still cleans up a stale file if present).
    """
    from lib import admin_bootstrap, auth_db, db

    admin_ids = await auth_db.list_admin_ids()
    for uid in admin_ids:
        await auth_db.set_admin(uid, False)
        await auth_db.bump_token_generation(uid)

    # Remove any stale bootstrap file that the previous admin might
    # not have used, OR that was orphaned by a crash. Next startup
    # will regenerate one.
    file_removed = admin_bootstrap.delete_bootstrap_file()

    await db.close_pool()

    print(f"Demoted {len(admin_ids)} admin user(s).")
    for uid in admin_ids:
        print(f"  - {uid}")
    if file_removed:
        print("Removed stale admin bootstrap file.")
    print("On next server start, a fresh bootstrap token will be issued.")
    print("See state/admin_bootstrap.txt after startup.")
    return 0


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print("NotNativeMemory - MCP Memory Server")
        print()
        print("Usage:")
        print("  python server.py [PORT]                Start HTTP server (default, port 9500)")
        print("  python server.py --foreground          HTTP mode, attached to console")
        print("  python server.py --mcp                 stdio mode (for MCP client configs)")
        print("  python server.py --stop                Stop a running HTTP server")
        print("  python server.py --restart, -r         Stop and restart the HTTP server")
        print("  python server.py --status              Show server status")
        print("  python server.py --create-user NAME    Create a user (prompts for password)")
        print("  python server.py --reset-admin         Demote all admins; regen bootstrap file")
        print("  python server.py --help                Show this help")
        print()
        print("The default mode is HTTP (background). Use --mcp for stdio transport")
        print("in Claude Code / LM Studio MCP client configurations.")
        print()
        print("Environment:")
        print("  MEMORY_DB_HOST       Postgres host (default: localhost)")
        print("  MEMORY_DB_PORT       Postgres port (default: 5433)")
        print("  MEMORY_DB_NAME       Database name (default: notnative_memory)")
        print("  MEMORY_DB_USER       Database user (default: memory)")
        print("  MEMORY_DB_PASSWORD   Database password (required)")
        print("  MEMORY_MODEL_PATH    Path to embedding model")
        print("  MEMORY_DEFAULT_PROJECT  Default project scope")
        print()
        print("Configuration is loaded from .env in the server directory.")
        sys.exit(0)

    elif "--create-user" in sys.argv:
        idx = sys.argv.index("--create-user")
        if idx + 1 >= len(sys.argv):
            print("Usage: python server.py --create-user <username>", file=sys.stderr)
            sys.exit(2)
        import asyncio
        sys.exit(asyncio.run(_cli_create_user(sys.argv[idx + 1])))

    elif "--reset-admin" in sys.argv:
        import asyncio
        sys.exit(asyncio.run(_cli_reset_admin()))

    elif "--status" in sys.argv:
        pid, port = _read_pid()
        if pid and _is_process_alive(pid):
            print(f"NotNativeMemory server is running (PID {pid}, port {port})")
            print(f"  Endpoint: http://{_resolve_bind_host()}:{port}/mcp")
        elif pid:
            print(f"PID file exists (PID {pid}) but process is not running.")
            _cleanup_pid()
            print("  Cleaned up stale PID file.")
        else:
            print("NotNativeMemory server is not running.")
        sys.exit(0)

    elif "--restart" in sys.argv or "-r" in sys.argv:
        was_running, old_port = _stop_running_server()
        if not was_running:
            print("No running server found, starting fresh")
        restart_port = _parse_port_from_args(("--restart", "-r"))
        if not restart_port or restart_port == _DEFAULT_HTTP_PORT:
            restart_port = old_port or _DEFAULT_HTTP_PORT
        proc = _spawn_background(restart_port)
        print(f"NotNativeMemory server restarted (PID {proc.pid}, port {restart_port})")
        print(f"  Endpoint: http://{_resolve_bind_host()}:{restart_port}/mcp")
        sys.exit(0)

    elif "--stop" in sys.argv:
        was_running, _ = _stop_running_server()
        if not was_running:
            print("No running server found (no PID file)")
        sys.exit(0)

    elif "--mcp" in sys.argv:
        # stdio transport: launched as a child process by Claude Code / LM Studio.
        mcp.run()

    else:
        # Default: HTTP transport. --http accepted as alias.
        port = _parse_port_from_args()

        existing_pid, _ = _read_pid()
        if existing_pid and _is_process_alive(existing_pid):
            print(f"Server already running (PID {existing_pid})")
            sys.exit(1)

        if "--foreground" in sys.argv:
            _start_foreground(port)
        else:
            proc = _spawn_background(port)
            print(f"NotNativeMemory MCP server started (PID {proc.pid}, port {port})")
            print(f"  Endpoint: http://{_resolve_bind_host()}:{port}/mcp")
            print(f"  Stop:     python server.py --stop")
