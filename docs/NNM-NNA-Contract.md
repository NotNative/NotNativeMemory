# NNM <-> NNA Contract: Pending Work for the NNA Implementer

**Audience:** the developer (or LLM) implementing worker mode and the new hook surface in NotNativeAgent.
**Purpose:** spell out exactly what NNA must do so the corresponding NNM-side subscribers (already designed, partly built) can attach without ambiguity.

> **Posture:** NNM owns the storage contract. NNA conforms.
>
> The hooks NNM ships *are* the contract. Where this document specifies a payload field name, an event trigger, a tag convention, or a mode label, NNA must adhere verbatim. If a deviation seems necessary, raise it as a contract change against this doc, not as a one-off.

---

## 1. Mode Taxonomy

NNA must expose two distinct execution modes with the labels NNM expects:

| Mode label | Meaning |
|---|---|
| `TUI` | User-interactive session. Human types prompts; the model responds turn by turn. |
| `WORKER` | One-shot, NNO-driven workhorse. No human in the loop. Envelope in, output out. |

These labels appear verbatim in env vars (e.g. `HOOKS_TUI_INJECT_TOPK`, `HOOKS_WORKER_INJECT_TOPK`) so they must match exactly.

Any future modes (e.g. a streaming-agent mode) require a corresponding entry in this section before NNM grows env-prefix support for them.

---

## 2. Worker Hook Lifecycle

In `WORKER` mode, NNA must fire two new events alongside whatever it does today.

### 2.1 `WorkerStart`

**When:** at the start of a worker run, before the model sees the task envelope.

**Required payload (NNA must supply all of these):**

| Field | Type | Purpose |
|---|---|---|
| `missionId` | string | Mission identifier. Becomes the `mission:<id>` tag on all writes from this run. Also used as a retrieval filter. |
| `assignmentId` | string | Per-assignment identifier within the mission. Becomes the `assignment:<id>` tag on writes; surfaced for cross-assignment forensics. |
| `originUserId` | string (UUID) | The NNM user the mission was launched by. In single-user mode this is the open bucket; in multi-user mode this is the user whose scope the worker reads/writes. |
| `taskDescription` | string | Free-text description of what the worker is about to do. NNM uses this as the semantic query for injection. |
| `missionTags` | array of strings (optional) | Additional mission-level tags for cross-mission categorization (e.g. `vendor-scrape`, `pricing`). |
| `tokenBudget` | int | Soft cap on the size of the injection block returned. Workers running on small local models cannot absorb large preambles. Recommended default 1000, max 4000. |

**What NNM does:** calls the `memory_inject_for_task` MCP tool (or invokes `lib/db.get_inject_for_task` directly inside the hook subscriber) and returns an injection block for NNA to prepend to the worker's initial context. The block contains:

1. Every `critical`-importance memory in the user's visible scope.
2. Every `rule`-class memory in the user's visible scope.
3. Semantic top-K against `taskDescription`, optionally filtered by `mission:<missionId>` tag.
4. Deduplicated, truncated to `tokenBudget`.

This is already implemented; see `lib/db.py::get_inject_for_task` and `server.py::memory_inject_for_task`.

### 2.2 `WorkerEnd`

**When:** at the end of a worker run, after the model has produced its final output.

**Required payload:**

| Field | Type | Purpose |
|---|---|---|
| `missionId` | string | Same as WorkerStart. |
| `assignmentId` | string | Same as WorkerStart. |
| `originUserId` | string (UUID) | Same as WorkerStart. |
| `taskEnvelope` | string | The original task description (or the full envelope) as fed to the worker. |
| `workerOutput` | string | The worker's structured result or output transcript. May be empty for tool-result-only workers; in that case `WorkerEnd` is a no-op. |

**What NNM does:** calls `hook_bundles/nna/notnative-memory/_internal/turn_analysis_core.analyze_worker_run(...)` which:

1. Calls the configured analysis LLM with the worker-tuned prompt (`build_worker_analysis_prompt`). The prompt steers extraction toward vendor quirks, tool-result patterns, and operational gotchas, *not* toward user preferences.
2. Tags every extracted memory with `mission:<missionId>` and `assignment:<assignmentId>`.
3. Stores extracted facts as memories via `memory_store` with `source="model-inferred"`.
4. Stores the run summary in RAG with tags `["session-summary", "conv:<prefix>"]`.
5. (Until promise detection moves; see §5) optionally writes a pending-nudge memory for the next worker on the same mission.

This is already implemented in `hook_bundles/nna/notnative-memory/_internal/turn_analysis_core.py::analyze_worker_run`. NNA needs to ship the wrapper hook script that pulls payload from stdin in NNA's hook shape and calls this function.

### 2.3 What does NOT fire in worker mode

`UserPromptSubmit` and `PreCompact` do not fire in `WORKER` mode. Workers have no user prompts and one-shot workers do not compact. Long-running workers that genuinely need compaction are deferred until NNO produces such workers in practice.

---

## 3. Mode-Aware Hook Configuration

NNA must read mode-prefixed env vars from `hooks.env`. NNM-side defaults already exist; the prefix layer lets each mode tune injection volume and extraction behavior independently.

**Naming convention:** `HOOKS_<MODE>_<KEY>` where `<MODE>` is `TUI` or `WORKER` (matches §1) and `<KEY>` follows the existing hook-env pattern.

**Currently meaningful keys:**

| Key | Per-mode applicability | Effect |
|---|---|---|
| `INJECT_TOPK` | TUI, WORKER | Top-K for the semantic component of injection. |
| `EXTRACTION_ENABLED` | WORKER | If `false`, skip the end-of-run LLM extraction call (token budget too tight). |
| `EXTRACTION_BUDGET_TOKENS` | WORKER | Hard cap on the LLM call's max_tokens for extraction. |

Example `hooks.env`:

```
HOOKS_TUI_INJECT_TOPK=20
HOOKS_WORKER_INJECT_TOPK=5
HOOKS_WORKER_EXTRACTION_ENABLED=true
HOOKS_WORKER_EXTRACTION_BUDGET_TOKENS=2000
```

Defaults must apply when no override is set. Don't require all keys to exist.

---

## 4. New Hooks Beyond the Worker Lifecycle

NNA must fire the following events in addition to existing session hooks. NNM ships subscribers; the contract is the event payload.

### 4.1 `ToolCallPost`

**When:** after a tool call returns, in any mode.

**Required payload:**

| Field | Type | Purpose |
|---|---|---|
| `toolName` | string | Name of the tool invoked. |
| `toolInput` | dict | Sanitized input arguments (no secrets). |
| `toolOutput` | string | The tool's result, truncated to a reasonable size by NNA. |
| `isError` | bool | True if the tool errored. NNM-side handler can use this to extract failure patterns. |
| `cwd` | string | Working directory for project resolution. |

**What NNM does:** runs an extraction pass over the tool I/O to identify durable patterns (vendor quirks, output shapes, failure modes) and ingests them into RAG. Implementation pending; the extractor for this is a thin variant of the existing worker prompt.

**Explicitly rejected: `ToolCallPre`.** Do not fire a pre-call hook. By the time NNA is calling a tool, the decision is already made; pre-call memory injection arrives too late to influence anything and only burns context.

### 4.2 `MissionBoundary`

**When:** at NNO mission start and mission end.

**Required payload (start):**

| Field | Type | Purpose |
|---|---|---|
| `phase` | string | `"start"`. |
| `missionId` | string | Mission identifier. |
| `originUserId` | string (UUID) | User who launched the mission. |
| `missionDescription` | string | What the mission is for. Used as a semantic query for historical context injection. |

**Required payload (end):**

| Field | Type | Purpose |
|---|---|---|
| `phase` | string | `"end"`. |
| `missionId` | string | Mission identifier. |
| `originUserId` | string (UUID) | User who launched the mission. |
| `missionOutcome` | string | Free-text outcome summary from NNO. |

**What NNM does:**
- On `start`: pulls memories tagged `mission:<missionId>` from prior runs of the same mission name plus semantic top-K against `missionDescription`. Returns an injection block summarizing prior outcomes for NNO to feed into the orchestrator's preamble.
- On `end`: produces a mission-level summary and writes it as a RAG document tagged `mission-summary` and `mission:<missionId>`.

Implementation pending; depends on §4.2 firing.

---

## 5. Promise Detection Migration

**Status (2026-05-09):** the rule-based `promise_detector.py` has been removed from the NNM-side NNA bundle and the corresponding `tool.call:post` subscription dropped from `hook_bundles/nna/notnative-memory/manifest.json`. The NNA repo owns this logic going forward. The bundle's installer treats `promise_detector.py` as a retired script and removes it from the deployed dir on next install.

The LLM-judged promise tracking that lives inside `_internal/turn_analysis_core.py::analyze_turn` is still bundle-internal and still runs in NNM's NNA bundle today. Migrating that to a separate NNA-side LLM call remains pending; it does not block the worker lifecycle work.

**Why both move:** promise detection is an agent-loop concern (did the model commit to something and not deliver?), not a memory concern. NNM's job is to remember; NNA's job is to nudge.

**What NNM keeps:** the bundle's own `_internal/turn_analysis_core.py::store_pending_nudge` writes a high-importance pending-nudge memory when called. NNA can call `memory_store` directly via MCP with the right tags and importance, or use the bundle-local helper if it ships its own bundle. Either way, the *decision* to write a nudge is NNA's.

---

## 6. Tag Conventions

These tags are load-bearing. NNA-side hooks must apply them consistently because NNM-side retrieval relies on them for filtering.

| Tag | Applied by | Meaning |
|---|---|---|
| `mission:<id>` | NNM (via `analyze_worker_run`) | Memory was written during a worker run for mission `<id>`. |
| `assignment:<id>` | NNM (via `analyze_worker_run`) | Memory was written during the specific assignment `<id>`. |
| `session-summary` | NNM (via `store_conversation_summary`) | RAG document is a compacted conversation digest, not source content. |
| `conv:<id-prefix>` | NNM (via `store_conversation_summary`) | All summaries from one session share the same `conv:<prefix>` tag. |
| `pending_nudge` | NNM (via `store_pending_nudge`) | Memory is a follow-up nudge for the next turn/run. |
| `mission-summary` | NNM (future, on `MissionBoundary` end) | RAG document is a mission-level outcome summary. |

If NNA writes additional tags via direct `memory_store` calls, prefer lowercase, colon-separated namespacing (e.g. `vendor:acme`, `tool:Bash`, `api:stripe`) to keep the tag space consistent.

---

## 7. Things NNA Must NOT Do

- **Do not invent new memory scopes.** The scope hierarchy is `local`/`domain`/`global` per user. There is no `business` or `mission` scope tier; mission scoping is tag-based only.
- **Do not narrow the MCP tool surface inside NNM.** If NNA wants to expose only a subset of NNM's tools to a worker (e.g. hide `memory_promote`/`memory_resolve_conflict` from automated workers), do that filtering at NNA's tool-routing layer. NNM provides every tool to every authenticated caller.
- **Do not fire `ToolCallPre`.** Explicitly rejected; see §4.1.
- **Do not call any NNM tool that performs an LLM call.** NNM is purely mechanical; if a tool ever appears that calls an LLM, treat that as an NNM bug.
- **Do not write run-specific transient data as memories.** Workers must not write "checked vendor X at 9am, no change" rows. Either suppress the write or have nothing to learn that turn. Memory pressure-cooling is not a backstop for noisy writers.

---

## 8. What NNM Has Built On Its Side

For the NNA implementer's reference; nothing here needs new NNM work.

- `lib/db.py::get_inject_for_task` — full implementation of the `WorkerStart` retrieval blend.
- `server.py::memory_inject_for_task` — MCP tool surface for the same operation.
- `hook_bundles/nna/notnative-memory/_internal/turn_analysis_core.py::analyze_worker_run` — full implementation of `WorkerEnd` extraction with mission tagging, summary write to RAG, and nudge write.
- `hook_bundles/nna/notnative-memory/_internal/turn_analysis_core.py::build_worker_analysis_prompt` — worker-tuned LLM prompt.
- `hook_bundles/nna/notnative-memory/_internal/turn_analysis_core.py::_attach_mission_tags` — pure-Python mission/assignment tag attachment with no-mutation, no-duplicate semantics.
- `hook_bundles/nna/notnative-memory/_internal/turn_analysis_core.py::store_conversation_summary` — session/run summary writer; lands in RAG with the right tags.
- Tests for all of the above in `tests/test_turn_analysis.py` and `tests/test_memory_inject.py`.

What NNA still needs to build on its side:

- The actual worker-mode entry point and runtime that fires `WorkerStart` / `WorkerEnd`.
- The mode-aware env-var reader so `HOOKS_TUI_*` / `HOOKS_WORKER_*` overrides take effect.
- `ToolCallPost` event firing.
- `MissionBoundary` event firing (or coordinate with NNO if it sources from there).
- The hook scripts that pull payloads from NNA's stdin shape and dispatch to the NNM helpers above.
- The promise-detection migration (both rule-based and LLM-judged).

---

## 9. Source Documents

This contract is distilled from the NNM direction work. Source material:

- `docs/NNM-Architecture.md` — full architectural overview of NNM as it stands today.
- `docs/NNM-direction.md` — the direction doc this contract is derived from. Items marked "Closed" are reflected here as obligations or implementations; items marked "Deferred" or "Rejected" are noted in §7 above.
- `docs/turn-analysis.md` — operator-facing turn-analysis configuration, env-var reference.
- `docs/api-auth.md` — auth model for any direct NNM API calls NNA wants to make.

If anything in this contract conflicts with `docs/NNM-direction.md`, this file wins; it is the canonical hand-off. The direction doc remains the design narrative for context.
