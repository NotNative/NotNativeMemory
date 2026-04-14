## Persistent Memory (MCP)

You have access to a persistent memory system via MCP tools. Use it to maintain context across sessions and survive context compaction.

**Tools:** `memory_store`, `memory_search`, `memory_list`, `memory_forget`, `memory_context`, `memory_fact_add`, `memory_fact_query`, `memory_project_configure`

**Store** observations, decisions, preferences, gotchas, and constraints that would hurt to lose. Use `project="_global"` for universal rules (style, preferences), `project="_domain_<name>"` for language/tool patterns (e.g. `_domain_python`), and a path for project-specific memories. Set `verbatim=true` to preserve full reasoning without summarization.

**Record facts** (`memory_fact_add`) for mutable state that changes over time (infrastructure config, versions, ports). Conflicting facts auto-invalidate with a timestamp, preserving history.

**Search** at session start (try `memory_context` for a quick working-set recovery), after compaction, before making assumptions, or when the user references past work. Query facts with `memory_fact_query` to check current state or time-travel with `as_of`.

**Forget** only when a memory is wrong or superseded — don't use it for facts that changed (those auto-invalidate) or for aging (thermal decay handles that).

**Do not store** ephemeral task state, full file contents, or anything derivable from the code itself.
