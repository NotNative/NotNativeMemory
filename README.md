# NotNativeMemory

**Multi-user persistent memory for MCP-capable AI agents. Run it solo on localhost, or share one server across a team with per-user auth and full memory isolation.**

- **Persistent semantic memory** — embeddings-backed recall that survives context compaction and session boundaries.
- **Document RAG** — ingest text blobs or files; chunks are auto-embedded and stored alongside memories. `rag_search` retrieves raw source text, `recall` fuses memory + document hits into one ranked list.
- **Multi-user by design** — open self-registration, Bearer-token auth, per-user isolation enforced at the database (Postgres RLS). Every user sees only their own memories, including their own global and domain scopes.
- **Facts with history** — record assertions as triples with automatic supersession and `as_of` time-travel, alongside the free-form memory store.
- **Hybrid retrieval** — opt-in BM25 + vector fusion via Reciprocal Rank Fusion. Surfaces exact-keyword matches the embedder alone misses (names, acronyms, identifiers).
- **Ambient via hooks** — shipped hook bundles for **Claude Code**, **NotNativeAgent**, and **Codex** inject relevant memory and capture useful lifecycle telemetry. The model doesn't have to remember to search.
- **Web GUI** — curate memories, facts, and API tokens in a browser; same auth as the MCP.

Works with any [MCP](https://modelcontextprotocol.io)-compatible client (Claude Code, LM Studio, Cline, Continue.dev, Cursor custom modes, self-hosted agents). Tested extensively with Claude Code and LM Studio.

## Solo or shared

Two operational modes, picked at install time:

- **Solo** — loopback-only, `MEMORY_AUTH_LOCALHOST_BYPASS=1` lets on-host agents and hooks work without tokens. One user, one machine, zero auth ceremony.
- **Shared** — server listens on a routable interface (typically behind a reverse proxy); every client presents a Bearer token. Registration, login, and token management via the web GUI or the auth API. Per-user isolation holds whether or not Postgres RLS is actively enforcing it.

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
- Downloads the embedding model (gte-large-en-v1.5, ~870MB on disk in fp16, ~1GB resident, CPU-only).
- Applies the schema to your remote DB if server mode.
- Runs a self-test against the live server.
- Detects `claude`, `nna`, and/or Codex on your machine and auto-wires the matching hook bundle for whichever is present.
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

Details for the two modes introduced in [Solo or shared](#solo-or-shared).

- **Solo** — set `MEMORY_AUTH_LOCALHOST_BYPASS=1` and
  `MEMORY_AUTH_LOCALHOST_USER=<username>`. Loopback callers are
  implicitly authenticated as the named user. Explicit Bearer
  headers still win, so a second user with their own token can
  use the server from the same machine without being silently
  overridden.
- **Shared** — bypass off; every caller presents a Bearer token.
  Endpoints: `POST /auth/register`, `POST /auth/login`,
  `GET|POST|DELETE /auth/tokens`. No admin concept — every user
  sees only their own memories, including their own `_global`
  and `_domain_*` scopes.

Full API reference including curl examples for login, token
management, and client setup: [`docs/api-auth.md`](docs/api-auth.md).

## Configure Your AI Tools

**In most cases you don't need to.** The installer detects `claude`, `nna`, and Codex on your machine and wires the matching hook bundle automatically. Skip ahead to [Add Memory Instructions to Your Agent](#add-memory-instructions-to-your-agent) if you used the installer.

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
Paste [`docs/memory-persona.md`](docs/memory-persona.md) into the system prompt / persona / custom instructions field. It's platform-neutral and covers all eight tools, when to use each, and the scope hierarchy.

## Optional: Ambient Memory via Hooks

The repo ships hook bundles for supported agent platforms that make memory ambient — the model doesn't have to remember to search, relevant context just shows up. Each bundle is tailored to that agent's hook contract:

- **UserPromptSubmit** — fires when the user sends a message, searches memory using the prompt text, and injects matches *before* the model reasons about the request. Catches decisions at the moment they're framed.
- **PreToolUse** — fires before file edits or shell commands, searches with tool-specific context (file extension, command keywords), and injects action-specific gotchas.
- **PreCompact** — fires before context compaction, injects critical rules and top memories so operational discipline survives compression.

Together they cover the three points in a turn where memory matters most: when the user states intent, when the model takes action, and when the window is about to shrink.

### Claude Code

Located at `hook_bundles/claude/notnative-memory/`. Claude Code's install script wires them up automatically. For manual setup or tuning, see [`hook_bundles/claude/notnative-memory/README.md`](hook_bundles/claude/notnative-memory/README.md).

Tool matcher: `Edit|Write|Bash`.


### Codex

Located at `hook_bundles/codex/notnative-memory/`. The installer deploys Codex-specific hooks under `~/.codex/hooks/notnative-memory/` and merges registrations into `~/.codex/hooks.json`. Codex may ask you to trust the hooks with `/hooks` before they run.

The first Codex bundle is intentionally conservative: `UserPromptSubmit` injects memory and captures user prompts, `SessionStart` provides a small working set, and `PostToolUse` / `Stop` passively capture telemetry. It does not try to govern Codex tool approval.

### NotNativeAgent

Located at `hook_bundles/nna/notnative-memory/`. The installer deploys the bundle under `~/.nna/hooks/notnative-memory/` where NNA discovers it through its native manifest.

### Other platforms

The hook intent is shared, but hook shape is platform-specific. Porting to a platform with an equivalent hook system means writing a new adapter for that platform's stdin payload, output envelope, and install config.

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

MCP tools grouped by purpose.

### Memory

| Tool | Purpose |
|------|---------|
| `memory_store` | Save a memory (with optional `verbatim` flag for unsummarized text) |
| `memory_search` | Semantic recall by natural-language query (opt-in `hybrid=True` adds BM25 fusion) |
| `memory_list` | Browse stored memories for audit or curation |
| `memory_forget` | Delete a memory by ID |
| `memory_update` | Edit a memory in place (content re-embeds, tags/importance/scope change) without resetting thermal state |
| `memory_context` | Return the hottest/most-critical memories within a token budget, no query needed |
| `memory_fact_add` | Record a fact triple (subject, predicate, object) with automatic invalidation of superseded values |
| `memory_fact_query` | Look up current or historical facts about an entity (supports `as_of` time-travel) |
| `memory_project_configure` | Declare which shared domains the current project pulls from |

Tags are auto-classified from content (decision, preference, gotcha, correction, constraint), so the AI doesn't need to tag perfectly.

### RAG

| Tool | Purpose |
|------|---------|
| `rag_ingest_text` | Ingest a text blob; chunked, embedded, stored. Sync by default; pass `async_mode=True` to return immediately and let a background worker fill in embeddings |
| `rag_ingest_file` | Read a UTF-8 text file from disk and ingest it. Infers title and content type from the filename |
| `rag_search` | Semantic search over ingested chunks (opt-in `hybrid=True` adds BM25 fusion). Returns document title, source URI, and character offsets for citation |
| `rag_ingestion_status` | Poll the most recent ingestion_job for a document. Useful after async ingest to check completion |

RAG content is deduplicated per-user by sha256 of the content. Re-ingesting identical content is a no-op that returns the existing document ID.

### Composed

| Tool | Purpose |
|------|---------|
| `recall` | Unified retrieval across memories and RAG documents, fused via Reciprocal Rank Fusion. Every returned row carries a `kind` field (`"memory"` or `"doc"`) so downstream code can route. Default `hybrid=True` since fusion is where the BM25 side pays off most |

### Verbatim

| Tool | Purpose |
|------|---------|
| `verbatim_capture` | Append raw transcript/tool chunks from agent hooks into the append-only verbatim layer |
| `verbatim_search` | Search verbatim chunks by semantic or hybrid vector/text retrieval for audit and curator grounding |
| `verbatim_recent` | Retrieve recent chunks from one session so low-signal prompts like "please proceed" can recover the active topic |
| `verbatim_stamp_outcome` | Stamp a session as `success`, `failure`, `aborted`, or `unknown` for later curation |
| `verbatim_skill_candidates` | Summarize success-stamped `loaded_skills` evidence so skill curators can decide what workflow guidance to refine |

## Memory Scoping

Memories live in one of three scopes:

- **local** (default) — tied to a specific project directory. What you get when you pass a real path to `memory_store`.
- **global** — stored in the reserved `_global` project, surfaced in every search and context call. Use for user preferences, formatting rules, communication style.
- **domain** — stored in `_domain_<name>` projects (e.g. `_domain_python`, `_domain_powershell`). Local projects pull from specific domains by calling `memory_project_configure(domains=["python", "powershell"])`.

Cross-project knowledge (gotchas, language patterns, style rules) no longer has to be trapped in the project where it was discovered.

## Document RAG

Memories are for curated knowledge; RAG is for raw source text the AI might need to consult verbatim. Typical examples: internal docs, specs, transcripts, markdown files.

**Ingestion:**

```python
rag_ingest_text(
    title="Onboarding playbook",
    content="...",                 # full UTF-8 text
    project="_global",             # or absolute path / _domain_*
)

rag_ingest_file(
    path="/opt/docs/runbook.md",
    project="/absolute/project/path",
)
```

Content is hashed (sha256) per user; ingesting the same bytes twice is a cheap no-op that returns the existing `document_id`. Documents are chunked at 2000 characters with 250 overlap in v1 (no sentence-boundary detection). Binary formats (PDF, docx) are not supported — extract text first.

**Async ingestion** (`async_mode=True`): chunks are persisted immediately with NULL embeddings; a background worker backfills. The tool call returns within milliseconds instead of seconds-to-minutes for large files. Use `rag_ingestion_status(document_id)` to poll completion. The worker starts automatically in HTTP mode; stdio mode ingests inline.

**Retrieval:**

- `rag_search(query)` returns document chunks ranked by cosine similarity. Each hit carries `document_title`, `source_uri`, `chunk_index`, and `char_start/end` for citation.
- `recall(query)` runs both `memory_search` and `rag_search` under the hood and fuses the results so you don't have to pick. Each row has `kind: "memory"|"doc"` so downstream code can route.

**Hybrid mode (`hybrid=True`):** adds a Postgres full-text ranking (`ts_rank_cd`) alongside the vector similarity and fuses them via Reciprocal Rank Fusion. Better recall on queries with specific tokens (product names, code identifiers, acronyms) that pure cosine similarity can miss. Off by default on `memory_search` and `rag_search`; on by default in `recall` where the fusion payoff is highest.

## How It Works

- **Storage:** Memories are embedded into 1024-dimensional vectors using a local model (gte-large-en-v1.5, fp16) and stored in Postgres with pgvector. Document chunks use the same model and a parallel `doc_chunks` table.
- **Search:** Queries are embedded the same way, then matched by cosine similarity with importance weighting on memories. Searching from a local project automatically includes globals plus declared domains. Opt-in `hybrid=True` adds a Postgres full-text ranking and fuses both via Reciprocal Rank Fusion (k=60); a generated `tsvector` column backs the full-text side and stays in sync automatically.
- **Thermal decay:** Each memory carries a temperature. Access reheats it; storing new memories in the same project cools existing ones (displacement cooling). Critical memories never cool.
- **Eviction:** Each project has a 500-memory cap. When exceeded, the coldest memories are evicted — importance is the primary tiebreaker, so critical memories survive.
- **Deduplication:** Storing a semantically similar memory (cosine similarity ≥ 0.92) merges into the existing one rather than creating a duplicate. RAG documents dedup by sha256 of content per user.
- **Async ingestion:** `rag_ingest_*(async_mode=True)` persists chunks with NULL embeddings and returns immediately. A background worker (started automatically in HTTP mode) drains the queue, recovers stranded jobs on boot, and marks completion in `ingestion_jobs`.
- **Facts vs memories:** Memories are observations that were true in their original context (always valid). Facts are assertions about current state that get superseded with timestamps when they change, preserving history.
- **Migrations:** The server self-bootstraps — pending SQL migrations in `config/migrations/` apply automatically on first tool call after deploy.
- **No daemons:** The MCP server (in stdio mode) is stateless between calls. HTTP mode runs one long-lived process with the RAG worker attached. Cleanup piggybacks on normal operations.

## Bulk Import from Transcripts

`scripts/mine.py` retroactively imports Claude Code JSONL transcripts into memory:

```bash
python scripts/mine.py path/to/session.jsonl
python scripts/mine.py path/to/session.jsonl --project /path/to/your/project
```

Each user/assistant exchange becomes a memory, auto-classified and deduplicated against existing ones.

## Troubleshooting

**"Docker not found"** - Install Docker Desktop (Windows/macOS) or Docker Engine (Linux).

**"Python not found"** - Install Python 3.11+ and make sure it's on your PATH.

**"Cannot connect to remote database"** - Check the host, port, and credentials. Make sure the Postgres server allows connections from your machine.

**"Model download failed"** - Check your internet connection. The model is downloaded from Hugging Face (~870MB fp16).

**"Schema creation failed"** - The database might already have the tables (safe to ignore) or the pgvector extension isn't available (make sure you're using the pgvector/pgvector Docker image).

**Memory tools don't appear in Claude Code** - Check that the MCP config in `~/.claude/settings.json` is valid JSON. Restart Claude Code after editing settings.

**Memory tools don't appear in LM Studio** - Check `~/.lmstudio/mcp.json`. Save the file and LM Studio will auto-reload.

**Hook calls hang / time out when the MCP is on the same machine (Windows)** - Windows' default IPv6 prefix-policy table prefers `::1` over `127.0.0.1` when a client resolves `localhost`. If the MCP server only binds to IPv4 (the default), same-host clients land on `::1`, the SYN goes nowhere, and the call eventually times out. Symptoms include 401-with-no-server-side-hit, mysterious analyzer drops, and hook subprocesses that finish at exactly their configured timeout. Fix from an **elevated** PowerShell prompt:

```
netsh interface ipv6 set prefixpolicy ::ffff:0:0/96 60 4
```

This bumps the precedence of IPv4-mapped addresses so IPv4 wins. System-wide change — revert with `netsh interface ipv6 set prefixpolicy ::ffff:0:0/96 35 4`. The Windows installer surfaces this advice automatically when the MCP URL is loopback AND the in-installer connection test cannot reach it — i.e. exactly the situation where this is the likely culprit.

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
README.md               - This file
docs/SECURITY.md        - Security posture, threat model, deployment guidance
server.py               - MCP server (run by any MCP-compatible agent)
scripts/
    mine.py             - CLI for bulk-importing conversation transcripts
    selftest.py         - Post-install store/search/forget verification
lib/
    db.py               - Database operations, migration runner, scope resolution
    embeddings.py       - Local embedding model wrapper (gte-large-en-v1.5, fp16)
    classify.py         - Auto-classification of memory content into tag types
    retrieval.py        - Composed recall: fuses memory + RAG hits via RRF
    rag/
        ingest.py       - Document ingestion (sync + async_mode)
        search.py       - doc_chunks semantic search (vector + optional hybrid)
        chunking.py     - Character-based chunker with overlap
        worker.py       - Background worker that drains the ingestion queue
config/
    schema.sql          - Fresh-install database schema
    migrations/         - Incremental schema changes, applied on first tool call
docker/
    docker-compose.yml  - Postgres + pgvector + MCP server containers
docs/
    api-auth.md         - Bearer-token auth API reference
    memory-persona.md   - Platform-neutral system prompt for any MCP agent
    incident-response.md - Runbook for suspected compromise
claude/
    CLAUDE.md           - Memory instructions for new Claude Code projects
    memory-instructions.md - Short version to append to an existing CLAUDE.md
    claude-code-config.json - Claude Code MCP config template
    lmstudio-config.json    - LM Studio MCP config template
hook_bundles/
    claude/notnative-memory/
        README.md               - Hook setup and adaptation guide
        session_start.py        - SessionStart hook (working-continuity at session start)
        user_prompt_inject.py   - UserPromptSubmit hook (context on every prompt)
        compact_guard.py        - PreCompact hook (rules + top memories)
        turn_analysis.py        - Stop hook (end-of-turn extraction)
        merge_hooks.py          - Idempotent installer for ~/.claude/settings.json
        hooks-config.json       - Hook registration snippet template
    codex/notnative-memory/
        README.md               - Codex-specific hook setup notes
        session_start.py        - SessionStart hook (small working set)
        user_prompt_submit.py   - UserPromptSubmit hook (context + prompt capture)
        post_tool_use.py        - PostToolUse hook (tool telemetry capture)
        stop.py                 - Stop hook (assistant response capture)
        merge_hooks.py          - Idempotent installer for ~/.codex/hooks.json
    nna/notnative-memory/
        session_start.py        - session.start subscriber
        user_prompt_inject.py   - user.prompt.submit:pre subscriber
        compact_guard.py        - compaction:pre and session.end:pre subscriber
        turn_analysis.py        - user.prompt.submit:post subscriber (extraction)
        merge_hooks.py          - Idempotent installer for ~/.nna/hooks/notnative-memory/
models/
    gte-large-en-v1.5/  - Embedding model (downloaded by install script)
```

## Acknowledgments

Built on ideas from the broader memory-for-LLMs community. Specific inspirations are too many to enumerate reliably, and selectively crediting one would be unfair to the rest.
