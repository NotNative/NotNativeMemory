# Claude Code Hooks

Four hooks that extend Claude Code sessions with memory awareness at different points in the turn lifecycle. The install script wires them up automatically. This README is for anyone setting them up manually or adapting them for another platform.

## Why four hooks?

Each fires at a different moment and serves a different purpose:

| Hook | Fires | Purpose |
|------|-------|---------|
| `session_start.py` | session start / resume / post-compact | prime the fresh session with the hottest + most-critical memories for the project |
| `user_prompt_inject.py` | user sends a message | prime the turn with context relevant to the specific request |
| `compact_guard.py` | before context compaction | preserve critical rules + top memories through compression |
| `turn_analysis.py` | end of each turn (Stop event) | analyze the just-finished turn — extract learnable patterns into memory and flag unfulfilled promises |

`SessionStart` covers the cold-boot case (brand new window, resumed conversation, right after compaction). `UserPromptSubmit` covers every subsequent turn with request-specific context. `PreCompact` is the last chance to smuggle operational discipline through the compression event. `Stop` runs the harvest pass that grows the memory pool over time. Together they give coverage across the whole session lifecycle, with both forward (read) and backward (write) memory flow.

A previous `PreToolUse` hook (`memory_inject.py`) was retired 2026-04-19 because firing on every Edit/Write/Bash added noise without proportional signal — session-start priming plus prompt-time injection gave better coverage with fewer false positives. The install script auto-sweeps retired hook entries from `~/.claude/settings.json`.

## What they do

### `session_start.py` (SessionStart)

Fires at the very start of a session, when the user resumes a prior conversation, and immediately after context compaction. Calls `memory_context` (not a keyword search — the server ranks by importance + thermal activity) and emits the top picks as working-continuity context plus a reminder to ToolSearch-load the deferred MCP tools so `memory_store` / `memory_search` / etc. are callable without the user having to ask.

Output channel is plain stdout rather than the JSON envelope — `hookSpecificOutput` is not in the schema-approved shape for `SessionStart` in current Claude Code versions, and stdout is the universal injection channel the harness folds into session context.

### `user_prompt_inject.py` (UserPromptSubmit)

Fires when the user sends a message. Uses the prompt text as a semantic query and injects top matches as `additionalContext` so cross-cutting preferences (e.g. "no em dashes"), past decisions, and domain-level gotchas are in scope before the model starts reasoning.

Skips trivial prompts under 15 characters (configurable) to avoid noise on "ok" / "yes" / "continue." Uses a higher similarity floor than a tool-args query would because user messages are longer and more varied. Writes a telemetry line to `~/.claude/memory_prompt_hook.log` per invocation.

### `compact_guard.py` (PreCompact)

Fires right before Claude Code compresses the context window. Injects a short set of critical rules plus the top high-importance memories for the current project, so operational discipline survives compaction. The rules are baked into the script — customize `_CRITICAL_RULES` at the top of the file for your workflow.

### `turn_analysis.py` (Stop)

Fires at the end of each agent turn. Reads the session transcript JSONL at `transcript_path`, extracts the most recent `(user, assistant)` message pair, and sends it to a configured analysis LLM. The LLM returns two things in one call:

1. **Learnable patterns** — corrections, preferences, infrastructure facts, tool failures, explicit decisions. Each item lands in the MCP via `rag_ingest_text` so future sessions can recall it.
2. **Unfulfilled promises** — when the assistant said "I'll check X" but never did. A high-importance "pending nudge" memory is written so the next turn's `user_prompt_inject.py` surfaces it as a reminder.

Sharing one LLM call across both jobs means promise-detection costs zero extra tokens beyond extraction.

**This hook needs an LLM endpoint configured** in `hooks.env`. The defaults are deliberately unset so the hook short-circuits with a warning until you opt in. See **Analysis LLM endpoint** below.

The shared analysis logic lives in `hooks_shared/turn_analysis_core.py` so the nna and Claude Code hooks both use the same pipeline; only the input adapter (transcript JSONL parser here, direct stdin in nna) differs per harness.

## Analysis LLM endpoint

`turn_analysis.py` runs an LLM call **per turn**. Two paths to configure it, in `hooks.env`:

### Path A — local provider (strongly recommended)

LM Studio, Ollama (in OpenAI-compat mode), or vLLM all expose an OpenAI-shape `/v1/chat/completions` endpoint. Per-turn analysis is exactly the kind of "metadata extraction" workload a small local model handles well, and at zero marginal cost.

```
OPENAI_BASE_URL=http://YOUR-GPU-HOST:1234/v1
OPENAI_API_KEY=lm-studio
# MEMORY_EXTRACT_MODEL=your-loaded-model-id   # pin a specific loaded model; auto-discovers from /v1/models if unset
```

The hook calls `GET /v1/models` once per fire to pick the loaded model, so you don't have to set `MEMORY_EXTRACT_MODEL` unless you have several loaded simultaneously and want a specific one.

### Path B — Anthropic API (cloud)

Useful only when Claude Code is authenticated via a real `ANTHROPIC_API_KEY` (not OAuth/Pro/Max — the hook can't read OAuth tokens out of Claude Code's credential store). Defaults to `claude-haiku-4-5-20251001` (cheapest current model).

```
ANTHROPIC_API_KEY=sk-ant-...
# MEMORY_EXTRACT_MODEL=claude-haiku-4-5-20251001
```

**Cost warning:** every Claude Code turn now triggers an additional Anthropic API call. At Haiku 4.5 rates (~$1/MTok in, $5/MTok out) and a typical 8 KB input / 1 KB output per call, that's roughly **0.2¢ per turn** — about 20¢ across a 100-turn session. Acceptable for moderate use, less so for heavy use.

### What if neither is configured?

The hook fires but exits with a "no model resolved" warning to stderr and a no-op log row. Your session continues normally — there's just no harvest happening.

## Authentication

The MCP server requires auth since Phase 5. Hooks call `/mcp` over HTTP, so they need a way to authenticate. Two supported setups:

### Option 1 (recommended): Bearer token

1. Log into the web GUI, open **Tokens**, click **Create token** with a label like `claude-hooks`. Copy the raw value (shown once).
2. Paste it into `~/.claude/hooks/notnative-memory/hooks.env`:

   ```
   MEMORY_MCP_TOKEN=nnm_<lookup>.<secret>
   ```

3. Done. Hooks attach `Authorization: Bearer <token>` on every request.

Works for any deployment shape — loopback, LAN, public. The token can be rotated from the Tokens page at any time; just paste the new value and the next hook invocation picks it up.

### Option 2 (single-user local only): server-side bypass

For a solo developer running the server on their own box and wanting to skip token management:

1. In the **server's** `.env` (not `hooks.env`), set:

   ```
   MEMORY_AUTH_LOCALHOST_BYPASS=1
   MEMORY_AUTH_LOCALHOST_USER=<your username>
   ```

2. Leave `MEMORY_MCP_TOKEN` blank in `hooks.env`.
3. The server now auto-authenticates unauthenticated loopback requests as the named user. Only works when the server binds to loopback and the hooks run on the same host. Disable this in any shared / multi-user / network-exposed deployment.

If neither option is configured, hooks will see 401 responses and their injection silently falls back to empty context (the hook exits 1, the primary action still runs).

## Configuration

All hooks read from the deployed config at:

```
~/.claude/hooks/notnative-memory/hooks.env
```

This is created on first install by copying `hooks.env.example` from the repo and patching in the MCP URL you provided. Re-running the installer **preserves your hooks.env** — only the `.example` file gets refreshed from the repo. Edit the live file directly to change values.

Available keys (full reference is in `hooks.env.example`):

```
MEMORY_MCP_URL=http://localhost:9500/mcp
MEMORY_MCP_TOKEN=                 # see Authentication section above

# UserPromptSubmit (user_prompt_inject.py)
MEMORY_PROMPT_THRESHOLD=0.45      # similarity floor for normal memories
MEMORY_PROMPT_HIGH_THRESHOLD=0.35 # floor for high/critical importance
MEMORY_PROMPT_MAX_RESULTS=3
MEMORY_PROMPT_MIN_CHARS=15        # skip trivial acknowledgements

# SessionStart (session_start.py)
MEMORY_SESSION_MAX_TOKENS=600

# PreCompact (compact_guard.py)
MEMORY_COMPACT_MAX_RESULTS=5

# Stop / TurnAnalysis (turn_analysis.py) — NOT configured by default.
# See "Analysis LLM endpoint" above. Uncomment ONE of:
#   OPENAI_BASE_URL=http://YOUR-GPU-HOST:1234/v1   (recommended)
#   OPENAI_API_KEY=lm-studio
# OR
#   ANTHROPIC_API_KEY=sk-ant-...                    (incurs cost per turn)
# MEMORY_EXTRACT_MODEL=...        # pin a model; auto-discovers from /v1/models if unset
# MEMORY_EXTRACT_TEMP=0.1
# MEMORY_EXTRACT_TIMEOUT=15
# MEMORY_EXTRACT_MAX_RESULTS=5
# MEMORY_EXTRACT_MIN_LENGTH=30    # x10 = 300 chars combined min before analysis fires
```

All values can be overridden by setting the corresponding environment variable before Claude Code launches.

## Manual installation

If you're not using the install script, run:

```bash
python claude/hooks/merge_hooks.py /absolute/path/to/NotNativeMemory http://your-mcp-host:9500/mcp
```

That copies the Python hooks + `hooks_shared/` package + `hooks.env.example` into `~/.claude/hooks/notnative-memory/`, creates `hooks.env` from the template (if it doesn't already exist), and registers the hook entries in `~/.claude/settings.json`. Safe to re-run; updates existing entries in place and preserves your `hooks.env` edits.

The deploy layout:

```
~/.claude/hooks/notnative-memory/
    compact_guard.py          # PreCompact hook
    session_start.py          # SessionStart hook
    turn_analysis.py          # Stop hook
    user_prompt_inject.py     # UserPromptSubmit hook
    hooks_shared/             # shared modules (env loader, analysis core)
    hooks.env                 # your config — preserved across re-installs
    hooks.env.example         # canonical template — refreshed from repo each install
    VERSION                   # install timestamp + source repo path
```

## Adapting for other platforms

Claude Code's hook protocol (stdin JSON in, stdout JSON or plain text out, event names like `SessionStart` / `UserPromptSubmit` / `PreCompact`) is Claude-Code-specific. If your agent platform has equivalent hook events, the core Python in each hook is platform-agnostic — the query building, HTTP call to the MCP server, and response formatting all work anywhere. You'll just need to adapt the stdin/stdout contract to whatever your platform expects. See `nnc/hooks/` for an example port to a different hook system.

## What gets injected

**SessionStart output** (plain stdout; `hookSpecificOutput` isn't accepted for this event):
```
[Session Start] Memory MCP tools are deferred by the harness. Call ToolSearch with ...
[Session Start] Working-continuity memories for this project:
  1. [high|global] no em dashes
  2. [critical|local] read-only review, do not edit files
  ...
```

**UserPromptSubmit output** (if matches above threshold):
```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "[Memory Hook] Context from previous sessions relevant to this request:\n  1. [high|global|0.58] ...\n  2. ..."
  }
}
```

**PreCompact output** (always, since critical rules always inject):
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreCompact",
    "additionalContext": "[Compact Guard] Critical rules preserved across context compaction:\n- ...\n\n[Compact Guard] High-priority memories from previous sessions:\n- ..."
  }
}
```

All hooks exit with code `0` on success and `1` on non-fatal error (the session / prompt / compaction proceeds regardless). A 401 from the MCP server (missing or invalid auth; see Authentication above) logs to stderr and is treated as a non-fatal error — Claude Code keeps running, just without memory injection for that event.
