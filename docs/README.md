# NotNativeMemory

Persistent, vector-backed memory for any MCP-compatible AI agent platform. Stores memories with semantic embeddings so they survive context compaction, session boundaries, and model changes.

## What This Does

When you work with an AI coding assistant, the conversation context has a limited window. Long sessions get compacted or truncated, and important decisions, preferences, and constraints get lost. NotNativeMemory gives the AI a persistent memory it can read and write to, so the things that matter survive.

The AI stores memories as it works (decisions, corrections, preferences) and searches for them when context is thin (session start, after compaction). You never have to manually manage it.

Built and tested with **Claude Code** and **LM Studio**, but works with any platform that speaks [MCP](https://modelcontextprotocol.io) — Cline, Continue.dev, Cursor custom modes, self-hosted agents, etc. For platforms without a CLAUDE.md-style instruction file, paste [`docs/memory-persona.md`](memory-persona.md) into your system prompt so the model knows when and how to use the tools.

## Prerequisites

- **Docker** — required for full install, recommended for server-only install, not needed for client-only install
- **Python 3.11+** — required on any machine that runs the server as a host process (server-only without Docker) or the hook scripts (any mode with hooks enabled)
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

The installer asks which of three modes you want:

1. **Full** — Postgres + MCP server both run as Docker containers on this machine. No Python required on the host. Auto-starts on boot.
2. **Server only** — MCP server runs here, Postgres is on another machine. You choose whether the server runs as a Docker container (recommended, auto-restarts) or as a host Python process. The installer falls back to Python automatically if Docker isn't available.
3. **Client only** — no server on this machine, just hooks + MCP config pointing at a remote server.

Depending on the mode, the installer also:

- Starts the Docker stack (full / server-docker) or installs Python deps (server-python).
- Writes `.env` with your DB credentials.
- Downloads the embedding model (~130MB, CPU-only).
- Applies the schema to your remote DB if server mode.
- Runs a self-test against the live server.
- Detects `claude` and/or `nnc` on your PATH and auto-wires the hook bundle and MCP registration for whichever is present.
- Emits `SETUP_COMPLETE.md` with the commands and paths for your specific install.

## Managing the Server

### Full install (Docker, local Postgres)

```bash
docker compose -f docker/docker-compose.yml --profile full up -d      # start
docker compose -f docker/docker-compose.yml --profile full down       # stop
docker compose -f docker/docker-compose.yml logs mcp                   # view logs
```

Auto-restarts on boot (`restart: unless-stopped`). Reachable on port 9500.

### Server only, Docker backend (remote Postgres)

```bash
docker compose -f docker/docker-compose.yml --profile server up -d mcp    # start
docker compose -f docker/docker-compose.yml --profile server down         # stop
docker compose -f docker/docker-compose.yml logs mcp                       # view logs
```

Same auto-restart behavior. The `server` Compose profile runs just the `mcp` container; Postgres connection details come from `.env`.

### Server only, Python backend (remote Postgres)

```bash
python server.py              # HTTP mode (default), backgrounded
python server.py --foreground # stay attached (debugging)
python server.py --stop       # stop a running HTTP server
python server.py --status     # check status
python server.py --mcp        # stdio mode (for MCP clients that launch the server themselves)
```

No reboot-survival unless you wire up a service manager (systemd, launchd, Task Scheduler).

### Client only

No server on this machine — the hooks talk to the remote MCP URL configured at install time.

## Web GUI

The server ships a browser UI for curating your own memories, facts,
and API tokens. It runs on the same port as the MCP (default 9500)
and uses the same auth layer; sign in once and you see only your own
data.

Open the server URL in a browser and log in:

```
http://localhost:9500/            # auto-redirects to /login
http://localhost:9500/memories    # browse, filter, edit, bulk delete
http://localhost:9500/facts       # triple store browser
http://localhost:9500/tokens      # mint and revoke API tokens
```

What's there:

- **Memory list** with full-text search, filters (scope, tag,
  importance, project), sort (created, accessed, temperature,
  importance), and pagination. Every change updates the URL so
  views are bookmarkable.
- **Memory detail** lets you edit content (re-embeds on save),
  tags, importance, and scope (rescope works by looking up or
  creating the target project for your user).
- **Bulk select + delete** with an all-visible toggle.
- **Facts browser** with the same filter conventions. History is
  hidden by default; check "Include superseded facts" to see the
  temporal chain.
- **Token management**: list your tokens (label, created, last
  used, revoked state), mint new ones with a label, revoke.
  New tokens are shown exactly once at creation; copy before
  reloading the page.

Auth is Bearer tokens under the hood. The browser stores the token
in an HttpOnly session cookie; all state-changing requests are
CSRF-protected with a double-submit cookie.

## Authentication

The server supports Bearer-token auth with open self-registration.
No admin concept: every user sees only their own memories, including
their own `_global` and `_domain_*` scopes.

Two operational modes picked at install time:

- **Solo mode** (`MEMORY_AUTH_LOCALHOST_BYPASS=1` +
  `MEMORY_AUTH_LOCALHOST_USER=<username>`): loopback callers are
  implicitly authenticated as the named user. Hooks and on-host
  agents work without a token. Explicit Bearer headers still win,
  so a second user with their own token can still use the server
  from the same machine without being silently overridden.
- **Multi-user mode**: bypass off, every caller must present a
  valid Bearer token. Registration, login, and token management
  go through `POST /auth/register`, `POST /auth/login`, and
  `GET|POST|DELETE /auth/tokens`.

Full API reference including curl examples for login, token
management, and client setup: [`docs/api-auth.md`](api-auth.md).

## Configure Your AI Tools

**In most cases you don't need to.** The installer detects `claude` and `nnc` on your PATH and wires both the hook bundle (under `~/.claude/` and/or `~/.nnc/`) and the MCP server registration automatically. Skip ahead to [Add Memory Instructions to Your Agent](#add-memory-instructions-to-your-agent) if you used the installer.

If you installed an agent CLI after running the installer, rerun it — it's idempotent, and the detection step will pick up the new CLI without reconfiguring existing ones.

### Manual MCP registration (reference)

**Claude Code — HTTP transport (server running remotely or locally on port 9500):**
```bash
claude mcp add --transport http memory --scope user http://YOUR_SERVER:9500/mcp
```

**Claude Code — stdio transport (launch the server per session on this machine, Python backend only):**
```bash
claude mcp add --transport stdio memory --scope user -- python server.py --mcp
```

**LM Studio and other HTTP-capable clients** — add to `~/.lmstudio/mcp.json` (or equivalent):
```json
{
  "memory": {
    "type": "http",
    "url": "http://YOUR_SERVER:9500/mcp"
  }
}
```

The installer writes `SETUP_COMPLETE.md` with the exact commands for your specific install (hostname, port, paths filled in).

## Add Memory Instructions to Your Agent

The AI needs to know the memory tools exist and when to use them. Pick the option that matches your platform:

**Claude Code — new project (no CLAUDE.md yet):**
Copy `claude/CLAUDE.md` to your project root.

**Claude Code — existing project (has its own CLAUDE.md):**
Append the contents of `claude/memory-instructions.md` to your existing CLAUDE.md.

**Any other MCP-compatible platform (LM Studio, Cline, Continue.dev, Cursor, custom agents):**
Paste [`docs/memory-persona.md`](memory-persona.md) into the system prompt / persona / custom instructions field. It's platform-neutral and covers all eight tools, when to use each, and the scope hierarchy.

## Optional: Ambient Memory via Hooks

The repo ships hook bundles for two agent platforms that make memory ambient — the model doesn't have to remember to search, relevant context just shows up. Three hooks per platform, each firing at a different moment in the turn:

- **UserPromptSubmit** — fires when the user sends a message, searches memory using the prompt text, and injects matches *before* the model reasons about the request. Catches decisions at the moment they're framed.
- **PreToolUse** — fires before file edits or shell commands, searches with tool-specific context (file extension, command keywords), and injects action-specific gotchas.
- **PreCompact** — fires before context compaction, injects critical rules and top memories so operational discipline survives compression.

Together they cover the three points in a turn where memory matters most: when the user states intent, when the model takes action, and when the window is about to shrink.

### Claude Code

Located at `claude/hooks/`. Claude Code's install script wires them up automatically. For manual setup or tuning, see [`claude/hooks/README.md`](../claude/hooks/README.md).

Tool matcher: `Edit|Write|Bash`.

### NotNativeCoder (NNC)

Located at `nnc/hooks/`. Install manually:

```bash
python nnc/hooks/merge_hooks.py /absolute/path/to/NotNativeMemory http://localhost:9500/mcp
```

The installer idempotently writes to `~/.nnc/settings.json` and generates `nnc/hooks/hooks.env`. See [`nnc/hooks/README.md`](../nnc/hooks/README.md) for payload format, tuning, and the exact settings it produces.

Tool matcher: `edit_file|write_file|read_file|bash`.

### Other platforms

The hook logic (query building, MCP search over HTTP, response formatting) is platform-agnostic. Porting to a platform with an equivalent hook system is mostly adjusting the stdin payload field names and the config file shape.

## Multi-Machine Setup

Run the server on one machine, connect from everywhere. No local install needed on client machines.

1. **Server machine:** Run the install script, start with `python server.py --http`
2. **Client machines:** Just add the HTTP config pointing to the server's hostname

## Deployment Shapes

Pick the shape that matches where the server runs. The two env vars that control network posture are `MEMORY_BIND_HOST` (which interface uvicorn listens on) and `MEMORY_COOKIE_SECURE` (whether session and CSRF cookies carry the Secure attribute). Both live in `.env`.

**Loopback-only (safest default; one-user-on-one-machine).**
No other machine can reach the server over the network. Suitable when only agents running on the same box hit the MCP, or when you access the web GUI from a browser on the same host.
```
MEMORY_BIND_HOST=127.0.0.1
MEMORY_COOKIE_SECURE=
```

**LAN behind a trusted reverse proxy (TLS on the proxy).**
Expose `nnm.example.internal` via nginx / Caddy / Traefik terminating TLS, proxying plain HTTP to the MCP. Session cookies now carry `Secure`, so they only flow over TLS.
```
MEMORY_BIND_HOST=0.0.0.0
MEMORY_COOKIE_SECURE=1
```

**Public via reverse proxy.**
Same as LAN behind a proxy, plus: the proxy should set `Strict-Transport-Security` with an appropriate `max-age`, and you should strongly consider firewalling the raw MCP port so only the proxy can reach it.

**Plain HTTP on a non-loopback interface (NOT RECOMMENDED).**
Leaving `MEMORY_BIND_HOST=0.0.0.0` without `MEMORY_COOKIE_SECURE=1` makes the server print a loud warning at startup and keep running. Session cookies then travel in plaintext; anyone on the network path can read them and impersonate logged-in users. Fine only for throwaway dev on a fully trusted LAN.

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

The uninstaller reads `.install-manifest.json` and only removes what was installed, so it's safe whatever mode you picked.

```bash
bash uninstall_linux.sh             # stop containers, remove hooks + MCP registration
bash uninstall_linux.sh --full      # also delete the Postgres data directory (full install only)
```

```powershell
powershell -ExecutionPolicy Bypass -File uninstall_windows.ps1
powershell -ExecutionPolicy Bypass -File uninstall_windows.ps1 -Full
```

The `.env` file and project directory are preserved — remove them manually if you want. LM Studio / other MCP clients aren't auto-cleaned; edit their config files (`~/.lmstudio/mcp.json`, etc.) to drop the `memory` block.

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
    api-auth.md         - Bearer-token auth API reference
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
nnc/
    hooks/
        README.md               - NNC hook setup and payload reference
        user_prompt_inject.py   - UserPromptSubmit hook (context on every prompt)
        memory_inject.py        - PreToolUse hook (edit_file/write_file/read_file/bash)
        compact_guard.py        - PreCompact hook (rules + top memories)
        merge_hooks.py          - Idempotent installer for ~/.nnc/settings.json
        hooks-config.json       - Hook registration snippet template
models/
    gte-base-en-v1.5/   - Embedding model (downloaded by install script)
```

## Acknowledgments

Built on ideas from the broader memory-for-LLMs community. Specific inspirations are too many to enumerate reliably, and selectively crediting one would be unfair to the rest.
