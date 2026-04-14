# NotNativeMemory

Persistent, vector-backed memory for any MCP-compatible AI agent platform. Stores memories with semantic embeddings so they survive context compaction, session boundaries, and model changes.

## What This Does

When you work with an AI coding assistant, the conversation context has a limited window. Long sessions get compacted or truncated, and important decisions, preferences, and constraints get lost. NotNativeMemory gives the AI a persistent memory it can read and write to, so the things that matter survive.

The AI stores memories as it works (decisions, corrections, preferences) and searches for them when context is thin (session start, after compaction). You never have to manually manage it.

Built and tested with **Claude Code** and **LM Studio**, but works with any platform that speaks [MCP](https://modelcontextprotocol.io) — Cline, Continue.dev, Cursor custom modes, self-hosted agents, etc. For platforms without a CLAUDE.md-style instruction file, paste [`docs/memory-persona.md`](memory-persona.md) into your system prompt so the model knows when and how to use the tools.

## Prerequisites

- **Python 3.11+**
- **Docker** (only if running the database locally)
- An MCP-compatible AI agent (Claude Code, LM Studio, Cline, Continue.dev, etc.)

## Install

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File install_windows.ps1
```

### Linux / macOS

```bash
bash install_linux.sh
```

The install script will:

1. Ask if your database is local (Docker) or remote (existing Postgres on your network)
2. Set up the database (or connect to your remote one)
3. Install Python dependencies
4. Download the embedding model (~130MB, runs on CPU)
5. Run a self-test to verify everything works
6. Print the configuration to add to your AI tools

## Start the Server

```bash
python server.py --http        # Start as network service (recommended)
python server.py --stop        # Stop the server
python server.py --http --foreground  # Stay attached (debugging)
```

HTTP mode is recommended. The server runs on port 9500 and any machine on your network can connect to it. No local install needed on client machines.

## Configure Your AI Tools

### Option A: HTTP Mode (recommended - works from any machine)

Start the server on the host machine, then on any client:

**Claude Code:**
```bash
claude mcp add --transport http memory --scope user http://YOUR_SERVER:9500/mcp
```

**LM Studio:**
Add to `~/.lmstudio/mcp.json`:
```json
{
  "memory": {
    "type": "http",
    "url": "http://YOUR_SERVER:9500/mcp"
  }
}
```

### Option B: stdio Mode (this machine only)

Claude Code launches the server automatically per session. No need to start it manually.

**Claude Code:**
```bash
claude mcp add --transport stdio memory --scope user -- python server.py
```

**LM Studio:**
Add to `~/.lmstudio/mcp.json`:
```json
{
  "memory": {
    "command": "python",
    "args": ["/path/to/NotNativeMemory/server.py"],
    "env": {
      "MEMORY_DB_HOST": "localhost",
      "MEMORY_DB_PORT": "5433",
      "MEMORY_DB_NAME": "notnative_memory",
      "MEMORY_DB_USER": "memory",
      "MEMORY_DB_PASSWORD": "your-password-here",
      "MEMORY_MODEL_PATH": "/path/to/NotNativeMemory/models/gte-base-en-v1.5"
    }
  }
}
```

The install script generates a `SETUP_COMPLETE.md` with your actual values filled in.

## Add Memory Instructions to Your Agent

The AI needs to know the memory tools exist and when to use them. Pick the option that matches your platform:

**Claude Code — new project (no CLAUDE.md yet):**
Copy `claude/CLAUDE.md` to your project root.

**Claude Code — existing project (has its own CLAUDE.md):**
Append the contents of `claude/memory-instructions.md` to your existing CLAUDE.md.

**Any other MCP-compatible platform (LM Studio, Cline, Continue.dev, Cursor, custom agents):**
Paste [`docs/memory-persona.md`](memory-persona.md) into the system prompt / persona / custom instructions field. It's platform-neutral and covers all eight tools, when to use each, and the scope hierarchy.

## Optional: Claude Code Hooks

The repo ships with three Claude Code hooks under `claude/hooks/` that make memory ambient — the model doesn't have to remember to search, relevant context just shows up:

- **`user_prompt_inject.py`** (UserPromptSubmit) — fires when the user sends a message, searches memory using the prompt text, and injects matches so the model has relevant context *before* it starts reasoning. Catches decisions at the moment they're framed.
- **`memory_inject.py`** (PreToolUse) — fires before Edit/Write/Bash, searches memory using tool-specific context (file extension, command keywords), and injects action-specific gotchas and preferences.
- **`compact_guard.py`** (PreCompact) — fires before context compaction, injects critical rules and top memories so operational discipline survives compression.

Together they cover the three points in a turn where memory matters most: when the user states intent, when the model takes action, and when the window is about to shrink.

The install script wires them up automatically. For manual setup, tuning, or adapting them for another platform, see [`claude/hooks/README.md`](../claude/hooks/README.md).

These hooks use Claude Code's hook protocol specifically, but the core logic (query building, MCP search, formatting) is platform-agnostic — adapters for other platforms with equivalent hook systems are straightforward.

## Multi-Machine Setup

Run the server on one machine, connect from everywhere. No local install needed on client machines.

1. **Server machine:** Run the install script, start with `python server.py --http`
2. **Client machines:** Just add the HTTP config pointing to the server's hostname

## Verify It Works

Open your MCP-configured agent and try:

1. "Store a test memory: the sky is blue"
2. "Search your memories for sky"

You should see the stored memory come back with a similarity score.

## Available Tools

The server exposes eight MCP tools:

| Tool | Purpose |
|------|---------|
| `memory_store` | Save a memory (with optional `verbatim` flag for unsummarized text) |
| `memory_search` | Semantic recall by natural-language query |
| `memory_list` | Browse stored memories for audit or curation |
| `memory_forget` | Delete a memory by ID |
| `memory_context` | Return the hottest/most-critical memories within a token budget, no query needed |
| `memory_fact_add` | Record a fact triple (subject, predicate, object) with automatic invalidation of superseded values |
| `memory_fact_query` | Look up current or historical facts about an entity (supports `as_of` time-travel) |
| `memory_project_configure` | Declare which shared domains the current project pulls from |

Tags are auto-classified from content (decision, preference, gotcha, correction, constraint), so the AI doesn't need to tag perfectly.

## Memory Scoping

Memories live in one of three scopes:

- **local** (default) — tied to a specific project directory. What you get when you pass a real path to `memory_store`.
- **global** — stored in the reserved `_global` project, surfaced in every search and context call. Use for user preferences, formatting rules, communication style.
- **domain** — stored in `_domain_<name>` projects (e.g. `_domain_python`, `_domain_powershell`). Local projects pull from specific domains by calling `memory_project_configure(domains=["python", "powershell"])`.

Cross-project knowledge (gotchas, language patterns, style rules) no longer has to be trapped in the project where it was discovered.

## How It Works

- **Storage:** Memories are embedded into 768-dimensional vectors using a local model (gte-base-en-v1.5) and stored in Postgres with pgvector.
- **Search:** Queries are embedded the same way, then matched by cosine similarity with importance weighting. Searching from a local project automatically includes globals plus declared domains.
- **Thermal decay:** Each memory carries a temperature. Access reheats it; storing new memories in the same project cools existing ones (displacement cooling). Critical memories never cool.
- **Eviction:** Each project has a 500-memory cap. When exceeded, the coldest memories are evicted — importance is the primary tiebreaker, so critical memories survive.
- **Deduplication:** Storing a semantically similar memory (cosine similarity ≥ 0.92) merges into the existing one rather than creating a duplicate.
- **Facts vs memories:** Memories are observations that were true in their original context (always valid). Facts are assertions about current state that get superseded with timestamps when they change, preserving history.
- **Migrations:** The server self-bootstraps — pending SQL migrations in `config/migrations/` apply automatically on first tool call after deploy.
- **No daemons:** The MCP server is stateless between calls. Cleanup piggybacks on normal operations.

## Bulk Import from Transcripts

`mine.py` retroactively imports Claude Code JSONL transcripts into memory:

```bash
python mine.py path/to/session.jsonl
python mine.py path/to/session.jsonl --project /path/to/your/project
```

Each user/assistant exchange becomes a memory, auto-classified and deduplicated against existing ones.

## Troubleshooting

**"Docker not found"** - Install Docker Desktop (Windows/macOS) or Docker Engine (Linux).

**"Python not found"** - Install Python 3.11+ and make sure it's on your PATH.

**"Cannot connect to remote database"** - Check the host, port, and credentials. Make sure the Postgres server allows connections from your machine.

**"Model download failed"** - Check your internet connection. The model is downloaded from Hugging Face (~130MB).

**"Schema creation failed"** - The database might already have the tables (safe to ignore) or the pgvector extension isn't available (make sure you're using the pgvector/pgvector Docker image).

**Memory tools don't appear in Claude Code** - Check that the MCP config in `~/.claude/settings.json` is valid JSON. Restart Claude Code after editing settings.

**Memory tools don't appear in LM Studio** - Check `~/.lmstudio/mcp.json`. Save the file and LM Studio will auto-reload.

## Uninstall

```bash
# Stop and remove the Docker container (local DB only)
docker compose -f docker/docker-compose.yml down -v

# Remove MCP config from Claude Code settings
# Edit ~/.claude/settings.json and remove the "memory" block

# Remove MCP config from LM Studio
# Edit ~/.lmstudio/mcp.json and remove the "memory" block

# Delete the project folder
# rm -rf /path/to/NotNativeMemory
```

## File Structure

```
server.py               - MCP server (run by any MCP-compatible agent)
mine.py                 - CLI for bulk-importing conversation transcripts
lib/
    db.py               - Database operations, migration runner, scope resolution
    embeddings.py       - Local embedding model wrapper
    classify.py         - Auto-classification of memory content into tag types
config/
    schema.sql          - Fresh-install database schema
    migrations/         - Incremental schema changes, applied on first tool call
docker/
    docker-compose.yml  - Postgres + pgvector + MCP server containers
docs/
    README.md           - This file
    memory-persona.md   - Platform-neutral system prompt for any MCP agent
claude/
    CLAUDE.md           - Memory instructions for new Claude Code projects
    memory-instructions.md - Short version to append to an existing CLAUDE.md
    claude-code-config.json - Claude Code MCP config template
    lmstudio-config.json    - LM Studio MCP config template
    hooks/
        README.md               - Hook setup and adaptation guide
        user_prompt_inject.py   - UserPromptSubmit hook (context on every prompt)
        memory_inject.py        - PreToolUse hook (action-specific gotchas)
        compact_guard.py        - PreCompact hook (rules + top memories)
        merge_hooks.py          - Idempotent installer for ~/.claude/settings.json
        hooks-config.json       - Hook registration snippet template
models/
    gte-base-en-v1.5/   - Embedding model (downloaded by install script)
```

## Acknowledgments

Built on ideas from the broader memory-for-LLMs community. Specific inspirations are too many to enumerate reliably, and selectively crediting one would be unfair to the rest.
