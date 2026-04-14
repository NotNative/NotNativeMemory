# Project Instructions

You have access to a persistent memory system via MCP tools. Use it to maintain context across sessions and survive context compaction.

## Memory Tools

Eight tools are available:

| Tool | Purpose |
|------|---------|
| `memory_store` | Save a memory (supports `verbatim` flag to preserve full reasoning) |
| `memory_search` | Find relevant memories by semantic similarity |
| `memory_list` | Browse stored memories for audit or curation |
| `memory_forget` | Remove outdated or wrong memories |
| `memory_context` | Pull the hottest / most-critical memories for the current project in one call. Use at session start and after compaction. |
| `memory_fact_add` | Record a fact triple (subject, predicate, object) — state that changes over time |
| `memory_fact_query` | Look up current or historical facts about an entity (supports `as_of` time-travel) |
| `memory_project_configure` | Declare which shared domains this project pulls from |

## Memory vs Fact

- **Memories** capture observations that were true in their original context (decisions, preferences, gotchas). They're always valid — a decision made in March is still a valid memory of that March decision.
- **Facts** capture mutable state (what model runs where, what port a service uses, what algorithm auth uses). When state changes, `memory_fact_add` automatically invalidates the old fact with a timestamp rather than deleting it.

Use memories for reasoning and preferences. Use facts for "what is true right now."

## Scope Hierarchy

Every memory lives in one of three scopes:

- **local** (default) — specific to this project directory
- **global** — stored to `project="_global"`, surfaces in every search
- **domain** — stored to `project="_domain_<name>"` (e.g. `_domain_python`, `_domain_powershell`), surfaces in any project that declares that domain via `memory_project_configure`

When you learn something portable — a language gotcha, a user preference, a coding pattern — prefer a broader scope. Don't trap cross-cutting knowledge in one project.

## When to Store

Store memories when you learn something that would hurt to lose:

- **Decisions**: "Chose HS256 for JWT signing because single-tenant" → local or global depending on portability
- **User preferences**: "No em dashes", "Use local timestamps" → `_global`
- **Language/tool patterns**: "PowerShell $LASTEXITCODE resets every command" → `_domain_powershell`
- **Project constraints**: "Read-only review, do not edit files" → local, importance=critical
- **Corrections**: When the user corrects your approach, store what you learned with enough context to avoid repeating the mistake
- **Architecture choices**: "Inter-service calls go through the service bus, never direct HTTP" → local

Tags are auto-classified (decision, preference, gotcha, correction, constraint), so don't agonize over tagging — explicit tags supplement, they don't replace.

## When to Search

Search proactively:

- **Session start**: Call `memory_context` first for a lightweight working-set recovery
- **After compaction**: Search for what you were working on
- **Before assumptions**: If you're about to make a decision, check if it was discussed before
- **User references past work**: "remember when we…" → search for it
- **Uncertain about a convention**: Search before guessing

## When to Record a Fact

Use `memory_fact_add` when you learn state that can change later:

- `(inference-host, runs_model, Llama-3.1-70B)`
- `(auth, algorithm, HS256)`
- `(app-postgres, port, 5433)`

When the state changes, add the new fact — the old one is auto-invalidated.

## When to Forget

Use `memory_forget` sparingly — only when a stored memory is wrong, reversed, or actively harmful to keep. Thermal decay handles normal aging. For facts that changed, use `memory_fact_add` (which preserves history); don't `memory_forget` them.

## What NOT to Store

- Ephemeral task state (what file you're editing right now) — that's working context
- Things already in the codebase — read the code instead
- Obvious patterns anyone could derive from standard docs
- Full file contents — store the decision about the file, not the file
