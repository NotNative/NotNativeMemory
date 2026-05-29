# NotNativeMemory — Architecture Overview

**Status:** living document
**Audience:** external reviewers, integrators, contributors
**Last updated:** 2026-05-25

---

## 1. What NotNativeMemory Is

NotNativeMemory (NNM) is a **persistent, multi-user, semantic memory service for MCP-compatible AI agents** (Claude Code, LM Studio, Cline, Continue.dev, and others). It provides:

- Embeddings-backed recall that survives context compaction and session boundaries.
- Activity-driven thermal decay so memory value tracks actual use, not wall-clock recency.
- Disagreement-aware consolidation: similar memories merge; conflicting ones surface for human resolution.
- A temporal triple store (facts) for mutable state (ports, versions, configs) with point-in-time queries.
- Optional ambient injection via Claude Code / NNA / NNC hooks (SessionStart, UserPromptSubmit, PreCompact, end-of-turn analysis).
- A web UI for curation, conflict resolution, token management, and health introspection.
- Local-first: all embeddings on-CPU via sentence-transformers; no external LLM/API calls in the storage path.

The headline design goal is **agents that build and leverage long-term knowledge without explicit search overhead**. Hooks inject context before the model reasons; the model rarely has to "remember to remember."

---

## 2. Top-Level Layout

```
NotNativeMemory/
  server.py              FastMCP server (stdio + HTTP), 30+ MCP tools
  lib/                   Core engine (db, embeddings, retrieval, auth, RAG, classify, verbatim, web routes)
  config/                schema.sql + 22 advisory-lock-serialized migrations
  models/                Local embedding model (gte-large-en-v1.5, 1024-dim, fp16)
  hook_bundles/
    claude/notnative-memory/  Claude Code hook integration
    nna/notnative-memory/     NotNativeAgent hook integration
  templates/             Jinja2 web UI (memories, facts, tokens, conflicts)
  tests/                 25+ unit/integration tests
  docker/                docker-compose stack (mcp + postgres profiles)
  scripts/               Operational scripts
  install_windows.ps1    Windows installer (ASCII-only)
  install_linux.sh       Linux installer
  docs/                  Operator + integrator documentation
```

---

## 3. Storage Layer

### 3.1 Backend

- **Postgres 16+ with the `pgvector` extension.** No alternate backend; the schema and retrieval design assume Postgres-native features.
- Connection pooling via `asyncpg` (`lib/db.py`), conservative pool size (max 5).
- Migrations under `config/migrations/` are applied at startup, serialized by a Postgres advisory lock so multiple processes can boot safely.

### 3.2 Key Tables (see `config/schema.sql`)

| Table | Purpose |
|---|---|
| `memories` | Semantic memories. 1024-dim pgvector embedding, thermal state, importance (low/normal/high/critical), class (rule/preference/memory/NULL), source attribution, supersede chain. HNSW index on embedding; GIN on a generated `tsv` (full-text). |
| `projects` | Working directories mapped to UUIDs. Scope is one of {local, domain, global}; `domains[]` lists cross-project membership. |
| `facts` | Temporal triple store (subject, predicate, object) with `valid_from` / `valid_to`; supports `as_of` time-travel queries. |
| `documents`, `doc_chunks` | RAG storage. Chunks default 2000 chars / 250-char overlap; auto-embedded in the same vector space as memories. |
| `verbatim_chunks` | Raw per-turn transcript chunks captured by the NNA-side hook (turn:post / tool.call:post / user.prompt.submit). Same `vector(1024)` + `tsv` + HNSW + RRF shape as `memories`, but separate table — append-only, no dedup, no cap, no thermal. Carries label columns: `session_id`, `chunk_index`, `topic`, `agent`, `source_event`, `is_error`, `loaded_skills`, `mission_id`, `mission_type`, `outcome`. |
| `memory_conflicts` | Detected disagreements (similarity 0.75–0.91). Resolutions: keep_both, supersede_a/b, merged, dismissed. |
| `users`, `auth_tokens` | Bearer-token auth. Open registration. `token_generation` counter supports revoke-all. |
| `decay_stats`, `ingestion_jobs` | Telemetry and async RAG job tracking. |

### 3.2.1 Two-Layer Stack: Closets and Drawers

NNM models the same drawers/closets split that MemPalace introduced. `memories` is the **closets** layer — curated, dedup'd, thermal, capped — the distilled rules/decisions/preferences a future session needs to see. `verbatim_chunks` is the **drawers** layer — raw per-turn transcript chunks streamed in by the NNA-side hook.

The two layers are deliberately separate tables:

- Verbatim writes never trigger memories-side dedup, displacement cooling, or cap enforcement. One chatty session would otherwise blow the project cap and evict load-bearing rules.
- `memory_search` rankings stay clean — only distilled rows surface, not transcript noise.
- Retention is managed independently per layer (verbatim is TTL/archive-friendly; memories use the thermal model).

Three MCP tools own the verbatim path: `verbatim_capture` (append, idempotent on `(session_id, chunk_index)`), `verbatim_search` (RRF hybrid by default, with `session_id`/`topic`/`mission_id`/`is_error`/`source_events`/`outcomes` filters), and `verbatim_stamp_outcome` (mark a session's chunks `success` / `failure` / `aborted` / `unknown` after the fact, used by the dreaming loop's curator and skill-induction passes).

### 3.3 Embeddings

- Single model across memories and RAG chunks: **gte-large-en-v1.5** (1024-dim, fp16, ~1 GB resident).
- Loaded on first `embed()` call, cached in-process.
- L2-normalized, so dot product equals cosine similarity.
- Fully local; no network egress in the storage or retrieval path.

---

## 4. Memory Lifecycle

### 4.0 Write-Time Linter

`memory_store` runs a cheap advisory pass via `lib/memory_linter.py`
after a successful insert. It flags long sentences (>40 words),
cross-memory meta-phrases ("this coexists with…", "as noted above"),
and rule-class memories missing `Why:` / `How to apply:` anchors.
Warnings ride back on the response payload as `warnings: [...]`;
nothing is rejected. Disable by setting `MEMORY_LINT_ENABLED=0` in
the server's environment.

### 4.1 Storage and Deduplication

On `memory_store`:

1. Embed the content.
2. Search the visible scope for nearest neighbors.
3. **If cosine similarity ≥ 0.92**, treat as a duplicate: increment `access_count`, reheat the existing row (+10 temperature), do not insert.
4. **If 0.75 ≤ similarity < 0.92**, insert the new memory **and** record a row in `memory_conflicts`. The user (or an agent) resolves it later via `memory_resolve_conflict` or the web UI.
5. Otherwise insert a fresh memory at initial temperature 70.0 with the requested importance/class.

### 4.2 Thermal Decay (Activity-Driven, Not Time-Based)

- **Reheat** (+10) on each search hit or dedup merge.
- **Displacement cooling** (−0.5) on every new memory stored in the same project; an additional −0.5 of pressure cooling kicks in once the project exceeds 80% of its scope cap (500 local / 1000 domain / 1000 global by default; override via `MEMORY_PROJECT_CAP`, `MEMORY_DOMAIN_CAP`, `MEMORY_GLOBAL_CAP`).
- **Importance multiplier** on cooling: critical 0× (never cools), high 0.25×, normal 1×, low 2×.
- **Eviction** at the per-project cap: coldest first, importance as tiebreaker.

The intent is that *use* keeps memory alive; the system self-prunes around what an agent actually exercises.

### 4.3 Three-Class Taxonomy and Promotion

- `lib/classify.py` does regex-based auto-tagging (decision, preference, gotcha, correction, constraint).
- A memory's `class` is one of `rule` (never decay), `preference` (decay-resistant), `memory` (normal decay), or `NULL` (unclassified, normal decay).
- `memory_promotion_candidates` surfaces memories with ≥3 accesses as upgrade candidates.
- `memory_promote(id, "rule" | "preference")` hardens them. Promotion is explicit and user-driven, not automatic.

### 4.4 Source Attribution

Every memory carries `source_kind` (user-stated, tool-result, model-inferred) and `source_session_id`. Used today for audit and `memory_health` introspection; available for retrieval filtering.

### 4.5 Supersede

`memory_supersede(old_id, new_id)` chains an updated memory over a stale one. The web UI exposes this directly; conflict resolution can also produce a supersede.

---

## 5. Retrieval

Three composable layers (`lib/db.py`, `lib/retrieval.py`):

1. **Vector similarity** — cosine, HNSW-indexed nearest-neighbor.
2. **Importance calibration** — additive bias: critical +0.15, high +0.10, normal 0, low −0.05.
3. **Hybrid (opt-in)** — Postgres full-text BM25 (`ts_rank_cd` over the generated `tsv` column) fused with vector via Reciprocal Rank Fusion, k=60.

Defaults:

- `memory_search`, `rag_search`: hybrid **off** by default.
- `recall` (the composed entry point): hybrid **on** by default. Recall fuses memory and RAG results in one ranked stream.

Retrieval is always scoped: a query for project P sees `local` memories of P, all `domain` memories whose domain set contains P's domain, and all `global` memories. Cross-user isolation is enforced at the application layer today; Row-Level Security is scaffolded but inert (see §9.3).

---

## 6. RAG Subsystem

- **Ingestion paths:** `rag_ingest_text` (inline) and `rag_ingest_file` (queued in HTTP mode).
- **Chunking:** 2000 chars with 250-char overlap; same embedding model as memories.
- **Async worker:** `install_rag_worker_lifespan` runs inside the HTTP server's lifespan, draining `ingestion_jobs`. Stdio mode ingests inline (no daemon).
- **Search:** `rag_search` over `doc_chunks`. `recall` fuses RAG hits with memory hits via RRF.
- **Status:** `rag_ingestion_status` reports queue depth, in-flight jobs, recent failures.

A reranker pass was investigated and **deferred**.

---

## 7. MCP Server

`server.py` is a single FastMCP entry point with two transports:

- **stdio** — launched per session by the MCP client; stateless; RAG ingest is inline.
- **http** — long-lived process on port 9500. Starlette + Uvicorn; hosts the web UI and the RAG async worker. Operator surface: `--foreground`, `--status`, `--stop`.

### 7.1 Exposed Tools (27)

| Group | Tools |
|---|---|
| Memory CRUD | `memory_store`, `memory_search`, `memory_forget`, `memory_list`, `memory_update`, `memory_context`, `memory_inject_for_task` |
| Facts | `memory_fact_add`, `memory_fact_query`, `memory_fact_update`, `memory_fact_forget` |
| Project | `memory_project_configure`, `memory_project_list`, `memory_project_delete` |
| Promotion | `memory_promotion_candidates`, `memory_promote`, `memory_health` |
| Conflicts | `memory_conflicts`, `memory_resolve_conflict`, `memory_supersede` |
| RAG | `rag_ingest_text`, `rag_ingest_file`, `rag_search`, `rag_ingestion_status`, `rag_list`, `rag_forget` |
| Composed | `recall` |

Every tool runs through `_tool_auth_and_project()` (bearer token or localhost bypass) and `_tool_error()` (structured error wrapping). Write tools whose project arg fails `_validate_writable_scope` (e.g. the legacy `general` sentinel, bare names, relative paths) **raise** `ToolError` rather than returning a structured failure dict, so the MCP envelope reports `isError: true` and clients that only inspect the envelope cannot mistake a rejected write for a successful one. The detail message is still surfaced via `content[0].text` for clients that read the inner shape.

---

## 8. Web UI

Implemented in `lib/web_routes.py` over Starlette, sharing the bearer-token model with MCP. Sessions use HttpOnly cookies with CSRF double-submit on all state-changing requests.

**Routes:**

- `/login`, `/register` — auth.
- `/memories` — list/filter/sort/search; inline HTMX edits; bulk delete; rescope between projects/scopes. Filters: project, scope, tag, importance floor, source kind (`user-stated` / `tool-result` / `model-inferred`), free-text content search.
- `/facts` — temporal browser with an "include superseded" toggle.
- `/tokens` — mint/label/revoke bearer tokens.
- `/conflicts` — side-by-side conflict resolution.
- `/admin/audit`, `/admin/metrics` — admin-only.

**Single- vs multi-user mode.** With no admin user provisioned, the GUI redirects from `/` to `/memories` and shows a banner explaining how to enable multi-user mode. The Dockerfile includes `templates/` so rendering works out of the box.

---

## 9. Authentication, Authorization, Multi-Tenancy

### 9.1 Auth

- Bearer tokens issued via `/tokens` and stored hashed in `auth_tokens`.
- `token_generation` per user supports a fast revoke-all.
- Localhost bypass is a configurable convenience for single-user installs.

### 9.2 Tenancy Model

- Each user has their own scope hierarchy: `local` (one project), `domain` (a named cross-project bucket), `global` (all of that user's projects).
- Application-layer filters resolve `visible_project_ids` for every query.

### 9.3 RLS

Postgres Row-Level Security is **scaffolded but inert** (see `lib/rls.py`, `docs/rls-activation.md`). The dual-role wiring exists; policies can be activated without a schema change. Today's enforcement is application-layer; RLS is the planned defense-in-depth layer.

---

## 10. Hook Integration

NNM ships hooks for two host platforms: Claude Code (`hook_bundles/claude/notnative-memory/`), NotNativeAgent (`hook_bundles/nna/notnative-memory/`). Both share the same lifecycle:

| Hook | Behavior |
|---|---|
| `SessionStart` | Pull top-K critical/hot memories for the project and inject them as preamble. |
| `UserPromptSubmit` | Search with the user's prompt; inject matches before the model sees the prompt. |
| `PreCompact` | Inject rules + critical memories so operational discipline survives context compression. |
| `Stop` (turn-analysis) | End-of-turn LLM extraction: stores mutable state through `memory_fact_add`, durable observations through `memory_store`, and compact turn summaries through RAG. Malformed analyzer output is logged and quarantined for review. |
| `PreToolUse` (safety gate) | Opt-in (`MEMORY_SAFETY_GATE_ENABLED=1`). Refuses a small baseline of destructive ops — `git push --force`, `rm -rf /`, `git reset --hard origin/...`, `DROP DATABASE` — by exiting 2 before dispatch. Disabled by default. |

Each bundle ships its own `_internal/turn_analysis_core.py`; the agent-facing hook scripts in the bundle are thin adapters. Configuration lives in `hooks.env`, resolved by `_internal/env_loader.py::load_hooks_env` which reads `~/.claude/hooks/notnative-memory/hooks.env` (or the NNA equivalent) and merges into `os.environ` via `setdefault` — so the inherited process environment always wins over file values, useful for tests and one-off overrides.

Installer-generated defaults seed the analyzer with a working local LLM endpoint and safe write target so a fresh install runs end-to-end without manual editing:

| Key | Default | Purpose |
|---|---|---|
| `MEMORY_MCP_URL` | `http://127.0.0.1:9500/mcp` (or installer arg) | NNM MCP endpoint the hooks POST to. |
| `OPENAI_BASE_URL` | `http://127.0.0.1:1234/v1` (LM Studio) | OpenAI-compat LLM endpoint for turn analysis. |
| `OPENAI_API_KEY` | `lm-studio` | Placeholder accepted by LM Studio; real key for cloud backends. |
| `MEMORY_EXTRACT_MODEL` | unset | Auto-discovered via `/v1/models` when blank; pin a specific id when multiple are loaded. |
| `MEMORY_EXTRACT_PROJECT` | `_global` | NNM scope analyzer writes to. Must be a writable scope (`_global`, `_domain_<name>`, or absolute path). The server-side `general` default is rejected. |
| `MEMORY_EXTRACT_DISABLE_REASONING` | `1` | When set, OpenAI-compat body adds `chat_template_kwargs={"enable_thinking": false}` so reasoning models (Qwen3-think, DeepSeek-R1) skip the hidden `<think>` phase that wastes the subprocess timeout on a classifier prompt. No-op on the Anthropic Messages backend. |
| `MEMORY_EXTRACT_QUARANTINE` | derived from `MEMORY_EXTRACT_LOG` | JSONL review file for irreparable analyzer responses that could not be parsed or repaired deterministically. |

LM Studio (OpenAI-compatible) and the Anthropic Messages API are both supported. Bundles do not share helpers across agents — each agent's bundle is developed independently with its own dev.

---

## 11. Deployment

Three install profiles (see `install_windows.ps1`, `install_linux.sh`, README):

1. **Full** — Docker stack: `postgres` (pgvector image, host port 5433) + `mcp` (FastMCP HTTP server, port 9500). Auto-start on boot.
2. **Server only** — MCP server (Docker or native Python), points at a remote Postgres.
3. **Client only** — no server; just hooks and an MCP client config pointing at a remote NNM.

`docker/docker-compose.yml` defines two profiles (`full`, `server`) so the same file covers all stack shapes.

Configuration is `.env`-driven: `MEMORY_DB_*`, `MEMORY_MODEL_PATH`, `MCP_PORT`, `MEMORY_DEFAULT_PROJECT`, `MEMORY_AUTH_LOCALHOST_BYPASS`, `MEMORY_BIND_HOST`, `MEMORY_COOKIE_SECURE`.

The Windows installer is constrained to pure ASCII so legacy console codepages render cleanly.

---

## 12. Tests

`tests/` covers, among others:

- Thermal decay, dedup, eviction, cross-scope retrieval (`test_memory_thermal.py`, `test_memory_dedup.py`, `test_memory_eviction.py`, `test_memory_cross_scope.py`).
- Hybrid search (`test_memory_search_hybrid.py`).
- Conflicts and supersede (`test_conflicts.py`).
- Facts, including update/temporal semantics (`test_memory_facts.py`, `test_fact_update.py`).
- Auth primitives, rate limits, request limits (`test_auth_primitives.py`, `test_rate_limit.py`, `test_limits.py`).
- HTTP server lifespan / RAG worker (`test_http_lifespan.py`).
- Memory classification (`test_memory_class.py`).
- Turn analysis (`test_turn_analysis.py`).

Project policy: every code change ships with a test.

---

## 13. End-to-End Data Flow

A representative round trip:

1. **User opens a Claude Code session.**
   `SessionStart` hook calls `memory_context` over MCP. NNM resolves the project's visible scope, returns top-K critical/hot memories. The hook injects them as preamble.
2. **User submits a prompt.**
   `UserPromptSubmit` runs `recall` (hybrid memory + RAG). Hits are embedded into the prompt as inline context. The model reasons with that context already present.
3. **Model produces output, possibly using tools.**
   No NNM activity unless the agent explicitly stores something via `memory_store`.
4. **Turn ends.**
   `Stop` hook (NNA) runs turn analysis: extracts learnable patterns via the configured LLM and writes them as RAG documents and/or as a high-importance promise nudge.
5. **Storage path.**
   Each `memory_store` embeds, checks for dedup at ≥0.92, conflicts at 0.75–0.91, then inserts. Displacement cooling fires across the project; if over the scope-appropriate cap (500 local, 1000 domain, 1000 global by default), the coldest non-critical row is evicted.
6. **Curation.**
   The user opens the web UI to resolve conflicts, promote frequently-accessed memories to `rule`/`preference`, supersede stale rows, or revoke tokens.
7. **Next session.**
   Hot memories survive; cold ones have decayed or been evicted; rules and preferences persist. The cycle repeats.

---

## 14. Architectural Decisions and Tradeoffs

1. **Postgres + pgvector over a vector-only store.** Buys relational integrity, RLS, hybrid full-text, transactional migrations. Cost: an extra service to operate, and embedding indexes (HNSW) are less tunable than a dedicated vector DB.
2. **Activity-driven thermal decay, not time-based.** Aligns memory value with use; pressure cooling lets the system self-prune. Cost: a fresh-but-unused memory cools quickly; counteracted by `importance=critical` and explicit promotion.
3. **Dedup at 0.92, conflicts at 0.75–0.91.** Auto-merge reduces noise; disagreements surface for resolution rather than corrupting silently. Thresholds are tuned empirically and may need revisiting per embedding model.
4. **Three-scope hierarchy (local / domain / global), per user.** Cross-project sharing without centralized admin. Domains are user-defined sets, not org-wide constructs.
5. **Hooks-first ambient injection.** The model is rarely asked to remember to retrieve; context arrives before reasoning starts. Cost: every hook invocation adds latency to session start and prompt submit; mitigated by HNSW and small top-K.
6. **Hybrid retrieval defaults: search off, recall on.** Search is "I know what I want"; recall is "compose everything you have." Different defaults match different intent.
7. **Async RAG worker only in HTTP mode.** Stdio stays stateless and per-session-isolated; HTTP is trusted infrastructure where a daemon is appropriate.
8. **Single embedding model across memories and RAG.** Simplifies fusion and avoids cross-space rerank gymnastics. Cost: cannot specialize embeddings per content type without a migration.
9. **App-layer tenancy now, RLS scaffolded for later.** Pragmatic posture: keep complexity low until the multi-tenant deployment story justifies the operational weight of RLS policies.
10. **Local embeddings only.** No network egress in the hot path; works on an air-gapped host. Cost: a ~1 GB resident model and CPU embedding latency.

---

## 15. Known Limits and Open Questions

- **Single embedding model.** Switching models requires a re-embed migration.
- **Per-scope memory caps (500 local / 1000 domain / 1000 global).** Each scope has its own env-tunable cap. No per-importance cap; a flood of `low` memories can pressure-cool `normal` ones until eviction.
- **Conflict thresholds are fixed (0.75 / 0.92).** They are correct for the current embedding model; they would need re-tuning if the model changes.
- **RLS not yet active.** Multi-tenant isolation today is application-layer only.
- **Reranker deferred.** Hybrid RRF is the current ceiling on retrieval quality; a cross-encoder reranker is a known future option.
- **Turn analysis is NNA-only today.** Claude Code and NNC ship the lifecycle hooks but not the LLM-driven extraction step.
- **Consolidation beyond conflict resolution is intentionally deferred.** Broader memory consolidation is treated as an agent-level "dreaming" behavior, not a storage-layer concern.

---

## 16. Repository Pointers for Reviewers

- Schema: `config/schema.sql`, `config/migrations/`
- Engine: `lib/db.py`, `lib/embeddings.py`, `lib/retrieval.py`, `lib/classify.py`
- RAG: `lib/rag/`
- Auth + RLS: `lib/auth*.py`, `lib/rls.py`, `docs/api-auth.md`, `docs/rls-activation.md`
- Web: `lib/web_routes.py`, `templates/`
- MCP server: `server.py`
- Hooks: `hook_bundles/claude/notnative-memory/`, `hook_bundles/nna/notnative-memory/`
- Tests: `tests/`
- Operator docs: `docs/api-auth.md`, `docs/incident-response.md`, `docs/turn-analysis.md`, `docs/memory-persona.md`
