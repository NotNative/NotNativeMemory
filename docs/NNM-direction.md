# NNM — Gaps and Direction

> **Audience:** Claude Code working on NotNativeMemory.
> **Purpose:** Identify what's missing to make NNM a robust memory backend for smaller local LLMs running under NNA, with NNO sitting on top. **This is not a full re-spec — the existing architecture doc remains authoritative for what's built. This doc identifies gaps and prescribes direction.**
>
> **Constraints that frame all decisions below:**
> - **Locally-hosted LLMs only**, forever. No cloud inference path.
> - **Single-instance-per-business**, forever. No multi-tenant-in-one-instance designs.
> - **NNM is a standalone product.** It must continue to work without NNA or NNO present. All NNA/NNO integration is opt-in via host adapters.

---

## 1. The Role NNM Plays in the Stack

NNM is the persistence and recall layer for agent intelligence. NNA reasons; NNM remembers. The headline value NNM provides — and the value that gets sharper when serving smaller local LLMs — is **ambient context injection**: putting the right memory in front of the model *before* it reasons, so a 30B model doesn't need 70B-class judgment to know what it should remember.

Two things follow from this framing:

1. **Quality of injection matters more than quantity of storage.** A 70B model can recover from sloppy context; a 30B model cannot. Every gap below should be evaluated against "does this make injection more accurate or more relevant for a small model?"
2. **NNM's hooks are a public contract, not internal scaffolding.** Multiple host integrations live in this repo (Claude Code, NNA, NNC). They are NNM's *adapters*, shipped from NNM because their dependency is NNM's MCP surface. This is the right shape; do not move them into the host repos.

---

## 2. The Principle That Decides Where Code Lives

A component lives in NNM if and only if its **interesting dependency** is NNM's storage, embeddings, scope model, or retrieval. If a component runs inside NNA's lifecycle but does NNM-shaped work (extraction, ingestion, scope-aware injection), it belongs in NNM as an NNA adapter. If a component runs inside NNA's lifecycle and does NNA-shaped work (deciding whether to nudge the loop, classifying tool calls, watching the byte stream), it belongs in NNA, even if NNM was historically where hook infrastructure existed.

**Apply this rule to:**

- **Promise detector.** Currently in NNM (`promise_detector.py`). Its inputs are conversation turns and tool calls; its output is an agent-loop signal. It does not need NNM. **Move to NNA.** Keep an NNM-side hook that *consumes* NNA's promise events if useful for memory (e.g., persisting unfulfilled promises as high-importance memories), but the detection itself is NNA's.
- **Turn analysis core.** Stays in NNM. Its job is "given a turn transcript, extract structured knowledge and write to NNM." The dependency is NNM's RAG ingestion and scope model. The host-side hook is the thin trigger; the work is NNM's.
- **Future: anything that makes a content decision about an agent action.** That belongs in NNA. NNM's job is to remember what happened, not to evaluate it.

---

## 3. Gaps That Block Robust Use Under Small Local LLMs

These are the gaps I'd close before declaring NNM ready as the brains for an NNA running a 30B–70B local model.

### 3.1 Shared-Knowledge Deployments Use Single-User Mode

**Not a gap — a clarification.** An earlier draft of this doc proposed a fourth scope tier (`business`) visible to all authenticated users of an instance, intended to support shared business knowledge across multiple humans (e.g., owner, manager, assistant in a restaurant deployment).

That proposal is **withdrawn**. The deployment pattern instead is:

- **Trusted small-team / single-business deployments → single-user mode** (no auth, open instance). All humans and all workers hit the same bucket. The existing `local`/`domain`/`global` scope hierarchy already covers per-project vs. cross-project knowledge sharing within that bucket. No code change required.
- **Mixed-trust or isolation-required deployments → multi-user mode** (auth on, RLS already active gating rows by `owner_user_id`). Each user is fully siloed. There is no shared cross-user scope, by design.

A "shared bucket inside multi-user mode" would require both genuine isolation between users *and* a deliberate cross-user spillway — a contradictory shape that's better served by switching to single-user mode for the trusted-team case. If real deployment feedback ever surfaces a need for both at once, revisit then.

**Implication for the rest of this doc:** wherever an earlier draft said "business scope," substitute "the open bucket in single-user mode" or "the originating user's scope in multi-user mode." Workers, conflict resolution, and curation all flow accordingly.

### 3.2 Worker-Aware Hook Lifecycle

**Gap:** All four existing hook events (`SessionStart`, `UserPromptSubmit`, `PreCompact`, `Stop`) assume a person is opening a session and submitting prompts. NNA worker mode (one-shot, no human, envelope-driven) does not match this lifecycle. A worker that fires `SessionStart` will pull memories for whichever user happens to be configured, which may be wrong (worker pulls another user's prefs) or useless (worker pulls personal context for an automated price scrape).

**Contract ownership:** NNM owns the worker-hook payload contract because NNM owns the storage semantics. NNA conforms. The hooks NNM ships *are* the contract; NNA implementers read these signatures and adhere to them. This is the same posture as for the existing session-mode hooks.

**New hooks:**

| Hook | Replaces | NNM-Side Behavior |
|---|---|---|
| `WorkerStart` | `SessionStart` | Build a task-scoped injection block and return it for NNA to inject into the worker's initial context. |
| `WorkerEnd` | `Stop` | Run end-of-run extraction (the same `analyze_turn`-shaped LLM call as session-mode, with a worker-tuned prompt) and persist results as memories tagged with the mission. |

`UserPromptSubmit` and `PreCompact` do not fire in worker mode. Workers have no user prompts, and one-shot workers do not compact. Long-running workers that genuinely need compaction are deferred until NNO produces such workers in practice.

**Required `WorkerStart` payload (NNA must supply all of these):**

| Field | Purpose |
|---|---|
| `missionId` | Mission identifier; used as `mission:<id>` tag for write scoping and as a retrieval filter. |
| `assignmentId` | Per-assignment identifier within the mission. Stored on writes for cross-assignment forensics. |
| `originUserId` | The user who launched the mission. In single-user mode this is the open bucket; in multi-user mode it is the user whose scope the worker reads/writes. |
| `taskDescription` | Free-text description of what the worker is about to do. Used as the semantic query for injection. |
| `missionTags` | Optional array of additional mission-level tags. Surface for cross-mission categorization (e.g. `vendor-scrape`, `pricing`). |
| `tokenBudget` | Soft cap on the size of the injection block returned. Workers running on small local models cannot absorb large preambles. |

**Required `WorkerEnd` payload:** `missionId`, `assignmentId`, `originUserId`, the worker's task envelope (or a reference to it), and the worker's structured result or output transcript. Workers that produce only tool output (no LLM exchange) make this a no-op rather than an error.

**Retrieval at `WorkerStart` is hybrid, not hot-list.** Sessions get a generic "top-K critical/hot for the project" preamble because there is no semantic query yet. Workers have a task description, which is a much richer signal. The strategy:

1. Always include `critical`-importance memories scoped to the user.
2. Always include `rule`-class memories scoped to the user.
3. Plus semantic top-K against `taskDescription`, scoped to the user and filtered/boosted by `mission:<id>` tag if present.
4. Truncate to `tokenBudget`.

This is the same operation as the proposed `memory_inject_for_task` MCP tool in §4. They share an implementation; the difference is who triggers it (`WorkerStart` is hook-driven; `memory_inject_for_task` is tool-driven, for callers that want explicit control).

**Mission scoping is tag-based, not a new scope tier.** Worker writes carry `mission:<id>` and (when applicable) `assignment:<id>` tags. Retrieval can filter or boost on those tags. We do not introduce a new scope column or scope tier; the existing `local`/`domain`/`global` hierarchy stays intact, and tag-based scoping handles the cross-worker-within-mission case naturally.

**Decision rule:** Worker writes go to whichever bucket the worker was launched against (open bucket in single-user mode; `originUserId`'s scope in multi-user mode), tagged with the mission. Chat-session writes default to `local` for the active project, with promotion to `global` available via the web UI as today. No new scope tier is introduced.

**Worker extraction uses the same shared core, with a worker-tuned prompt.** `hooks_shared/turn_analysis_core.py` already does extraction; the worker variant adds a sibling prompt builder whose rules emphasize vendor quirks, tool-result patterns, and operational gotchas over user preferences. The LLM call path, schema, storage helpers, and source attribution (`model-inferred`) are shared.

**Edge cases noted, not built:**

- **Long-running workers and compaction.** Defer until NNO produces workers that need it.
- **Tool-result-only workers** (no LLM output). `WorkerEnd` is a no-op rather than an error.
- **Mission spanning multiple workers.** Handled by tag-based retrieval; revisit if tag-leakage becomes a real problem in deployment.

### 3.3 Source-Aware Retrieval Filtering (Closed — Trust the Extractor)

**Resolution:** No source-aware machinery is being built. After review:

- The proposed retrieval bias (`±0.05` by `source_kind`) was too small relative to importance multipliers (`±0.15`) and acted on the wrong table (extraction writes to RAG today, not memories).
- The proposed `confirmed` flag duplicated thermal/decay mechanisms that already weed out unused or wrong memories.
- The `(unverified)` injection label depended on extending `source_kind` to RAG content, which neither side saw a clear purpose for.

**What did change** (separate but related): the turn-analysis extractor now writes to **memories** instead of RAG (`memory_store_call` in `hooks_shared/turn_analysis_core.py`), with `source="model-inferred"` set on every extraction. Source attribution exists structurally and is queryable for future curation/analysis without any retrieval-side bias machinery.

If confabulation becomes a visible problem in deployment, revisit. Until then, the tightened extractor (§above) plus existing thermal decay are trusted to keep low-quality memories from accruing.

### 3.4 Per-Importance Cap (Deferred — Workflow, Not Memory)

**Gap:** §15 of the existing doc notes "no per-importance cap; a flood of `low` memories can pressure-cool `normal` ones until eviction."

**Resolution:** Deferred. The motivating scenario in earlier drafts ("workers writing 'checked vendor X, no change' every hour") is a **workflow problem, not a memory problem** — that data should never reach NNM in the first place. A worker that observes no change has nothing learnable to store; suppressing the write is the right fix and lives upstream in NNA, not in NNM eviction policy.

If a different floodscenario surfaces later (some legitimate but high-volume class of low-importance writes that genuinely belongs in memory), revisit this. Until then the existing per-project total cap with thermal eviction is sufficient.

### 3.5 Hooks Configuration Per Mode

**Gap:** Hook behavior is configured globally per-host. NNA is being reshaped to expose distinct execution modes (TUI = user-interactive; worker = NNO-driven workhorse). The hook layer needs to follow.

**Direction:** Hook configuration becomes mode-aware. `hooks.env` (or equivalent) accepts mode-prefixed overrides keyed to NNA's mode taxonomy:

```
HOOKS_TUI_INJECT_TOPK=20
HOOKS_WORKER_INJECT_TOPK=5
HOOKS_WORKER_EXTRACTION_ENABLED=true
HOOKS_WORKER_EXTRACTION_BUDGET_TOKENS=2000
```

Defaults stay sensible without overrides. The driver is NNA's TUI/worker split: a chat session and a one-shot worker want very different injection volumes, very different extraction budgets, and in some cases a different decision about whether the LLM-driven post-turn extractor should run at all. Mode-prefixed env vars let the same `hooks_shared` core serve both shapes without code paths branching at runtime.

**Coordination with NNA:** the mode names (`TUI`, `WORKER`) must match NNA's mode-routing taxonomy verbatim. Owned by NNA's direction doc; this section follows that contract.

### 3.6 Hooks That Don't Yet Exist

The existing four hooks cover the conversation lifecycle. Two more align with NNA's new mode roles:

| New Hook | Trigger | NNM-Side Behavior |
|---|---|---|
| `ToolCallPost` | After NNA's tool returns | Optional extraction of tool-result patterns into RAG. Especially valuable for scraping/integration tools where vendor-specific quirks accrue over time. |
| `MissionBoundary` | NNO mission start/end | Inject mission-historical context (prior runs, prior outcomes) at start; consolidate cross-assignment learnings at end. |

These are NNA-fired (or NNO-fired for `MissionBoundary`); NNM ships subscribers. Same shape as the existing hooks.

**`ToolCallPre` is explicitly rejected.** By the time NNA is calling a tool, the decision of whether and how to call it has already been made. Pre-call memory injection arrives too late to influence the choice and only burns context for a model that has already committed. If tool-specific knowledge needs to reach the model earlier, it does so via the existing `SessionStart` / `UserPromptSubmit` injection — not via a new pre-call hook.

### 3.7 Compacted Conversation Summaries Ingested into RAG

**Gap:** NNM stores extracted facts and individual memories. The conversation itself is only a `source_session_id` foreign key. When a model later asks "what did we decide last week," extraction-only loses the *shape* of the discussion.

**Direction:** At end-of-turn (or end-of-session), produce a compacted summary of the conversation and ingest it into RAG via the existing `rag_ingest_text` path. Tag it as a conversation summary so retrieval can route or filter it as needed.

**Scope of the summary:**

- **Include:** the user/assistant dialogue, distilled into a short prose summary that captures decisions made, problems discussed, and conclusions reached.
- **Exclude:** tool calls and tool results. Those are mechanical artifacts of how the work happened, not what was discussed. They balloon the summary, dilute the signal, and rarely help future recall.

**Why RAG and not a new table:** the `documents` / `doc_chunks` infrastructure already does what's needed (embed, chunk, retrieve, rank-fuse with memories via RRF). A separate `conversations` table would duplicate that machinery for marginal benefit. Ingesting summaries into RAG keeps the storage surface uniform and lets the existing `recall` path surface them alongside memories without new code paths.

**Composition:** the compacted summary is produced by the same end-of-turn LLM that runs extraction. One LLM call, two outputs: discrete facts (the existing extraction path, now tightened per §3.3-adjacent work) plus a single conversation summary. The summary writes to RAG with tags that mark it as a session digest so it can be filtered out of casual searches when appropriate.

---

## 4. The MCP Tool Surface: What Should Be Exposed

NNM currently exposes 15 MCP tools. The set is good for an interactive agent. **NNM is purely mechanical — no LLM calls inside any tool.** The only adjustment that matters from NNM's side is exposing what workers need to do their work; NNA decides which subset of tools to surface in any given mode.

**Add a `memory_inject_for_task(task_description, scope_filter, max_tokens)` tool.** Workers don't have a session to populate context from; they have a task envelope. A worker (or NNA on a worker's behalf) should be able to ask NNM "given this task, what memories should I have in context?" and receive a pre-formatted injection block. This is the worker analog to `SessionStart` injection but explicit and tool-driven rather than hook-driven.

**Tool exposure per mode is NNA's concern, not NNM's.** NNM provides every tool to every authenticated caller; NNM has no notion of "which agent is calling" and should not grow one. NNA picks which tools to surface per mode (TUI vs. worker), keeping NNM mechanical and reusable across hosts.

---

## 5. What's NOT a Gap

To save Claude Code from chasing things that don't matter:

- **The conflict threshold tuning.** The 0.75/0.92 thresholds are model-specific and currently correct for `gte-large-en-v1.5`. Don't touch them. If you swap embedding models, re-tune them then.
- **The reranker.** Hybrid RRF is the ceiling. A cross-encoder reranker is on the future-options list but not blocking. Defer.
- **The web UI.** It's adequate for v1. The curation surface (`/conflicts`, `/memories`, `/admin/audit`) does what it needs to. Polish later.
- **Multi-embedding-model support.** Single model is correct. Don't introduce per-content-type embeddings; the cross-space retrieval problem is not worth solving for the deployment scale.
- **Time-based decay.** Activity-driven decay is the right design and should not be supplemented with time-based decay. Resist the temptation.

---

## 6. Order of Operations

If Claude Code is going to take this on incrementally, the order I'd recommend:

1. **Move LLM-based promise detection to NNA.** Smallest change, clears a conceptual debt. Includes the rule-based `promise_detector.py` already living in `nna/hooks/` and the LLM-judged promise tracking currently entangled in `hooks_shared/turn_analysis_core.py`.
2. **Add worker-mode hook lifecycle (`WorkerStart`, `WorkerEnd`).** Required for NNO worker integration. Coordinates with NNA's mode taxonomy.
3. **Add `memory_inject_for_task` MCP tool.** Required for clean worker injection.
4. **Add mode-aware hook configuration (TUI vs. worker).** Coordinates with NNA's mode-routing work; lets the same `hooks_shared` core serve both shapes via env-prefixed overrides.
5. **Compacted conversation summaries into RAG.** Same extraction LLM call produces a session digest alongside discrete facts; ingested into RAG, dialogue only, no tool calls or results.
6. **Source-aware retrieval bias / `confirmed` flag.** Open — see §3.3; recommendation pending decision. Small-model reliability lifeline.
7. **`ToolCallPost` hook.** Tool-result pattern extraction into RAG.
8. **`MissionBoundary` hook.** NNO mission start/end; mission-historical context injection and cross-assignment consolidation.

Items 1–3 are required for the restaurant deployment. 4–5 align with the NNA/NNO refactor. 6 is a deferred reliability improvement. 7–8 are future expansion driven by NNA/NNO maturity.

**Explicitly deferred / rejected:** per-importance cap (§3.4 — workflow concern, not memory); `ToolCallPre` hook (§3.6 — too late in the loop to influence tool-call decisions); business scope (§3.1 — single-user mode covers shared-team deployments).

---

## 7. Cross-References to Other Docs

- The shape of NNA's hook events that NNM consumes is owned by **NNA's direction doc, §3 (Operator Presence) and §5 (Hook Surface)**.
- The mission/worker lifecycle that NNM's worker-mode hooks must fire against is owned by **NNO's direction doc, §4 (Mission and Worker Contract)**.
- The decision rule for "where does this code live" is shared with NNA and NNO docs and applies symmetrically.

---

*End of NNM direction doc.*
