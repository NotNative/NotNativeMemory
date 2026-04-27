# Turn Analysis — Combined Learning + Promise Detection

**Version:** 2.0
**Last Updated:** 2026-04-26
**Status:** Production
**Renamed from:** `turn-extractor.md` (file: `turn_extractor.py` → `turn_analysis.py`) on 2026-04-26 to reflect broader scope (extraction + promise detection in one LLM call).

---

## Overview

The **Turn Analysis** hook (`turn_analysis.py`) fires on `user.prompt.submit:post` after the model responds. A single LLM call analyzes the turn for two things at once:

1. **Learnable patterns** — corrections, preferences, tool gotchas, decisions. Stored to NotNativeMemory via RAG ingestion.
2. **Unfulfilled promises** — "I'll look up X" with no follow-through, tools called but no substantive answer delivered. Stored as a high-importance `pending_nudge` memory.

Both extractions share the same LLM call, so promise detection costs zero extra inference.

The next turn's `user_prompt_inject.py` picks up the nudge via the existing high-importance similarity threshold (default 0.35) — no new event wiring required.

---

## Architecture

```
User Prompt
    |
    v
[NNA processes turn]
    |
    v
Model Response
    |
    v
turn_analysis.py (post phase)
  - Reads: prompt + model_response
  - Calls LM Studio once with combined prompt
  - Stores extracted facts (rag_ingest_text)
  - If shouldNudge: stores nudgeText as high-importance memory
    tagged "pending_nudge"
    |
    v
Next turn: user_prompt_inject.py (pre phase)
  - Semantic search picks up relevant memories AND pending nudges
  - Injects via additionalContext
```

**Payload:** stdin receives `{prompt, model_response, cwd}` from NNA's hook system.

---

## LLM Output Schema

The LLM is instructed to return ONE JSON object with both sections:

```json
{
  "results": [
    {
      "type": "behavioral|operational|gotcha|decision",
      "category": "correction|preference|frustration|tool-failure|policy|...",
      "key": "short_identifier",
      "value": "distilled rule or fact",
      "confidence": "high|medium|low"
    }
  ],
  "unfulfilledPromises": [
    { "promise": "...", "reason": "tools called but no results delivered" }
  ],
  "shouldNudge": false,
  "nudgeText": ""
}
```

Missing keys are coerced to safe defaults (`[]`, `false`, `""`) by the parser, so partial LLM responses don't crash the hook.

---

## Configuration (`hooks.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_EXTRACT_MIN_LENGTH` | 30 | Minimum conversation chars (×10 multiplier) to trigger analysis |
| `MEMORY_EXTRACT_TEMP` | 0.1 | LLM temperature (low = deterministic) |
| `MEMORY_EXTRACT_MAX_RESULTS` | 5 | Max extracted facts stored per turn |
| `MEMORY_EXTRACT_TIMEOUT` | 10 | LLM call timeout (seconds) |
| `MEMORY_EXTRACT_LLM_URL` | derived | Override chat completion endpoint; defaults to `MCP_URL` with `/mcp` → `/v1/chat/completions` |
| `MEMORY_EXTRACT_LOG` | `~/.nna/turn_analysis.log` | Telemetry log path |

---

## Hook Manifest

```json
{
  "event": "user.prompt.submit",
  "phase": "post",
  "command": "python turn_analysis.py",
  "blocking": false,
  "timeout_ms": 15000
}
```

`blocking: false` — analysis failures NEVER affect user interaction.

---

## Storage Format

### Extracted facts

Stored via `rag_ingest_text`:

- **Title:** `{type}:{key}:{conversation_id[:8]}` (deduplication anchor)
- **Tags:** `[type, "cat:{category}"]`
- **Importance:** `high` if confidence=high else `normal`

### Pending nudges

Stored via `rag_ingest_text` with:

- **Title:** `pending_nudge:{conversation_id[:8]}`
- **Tags:** `["pending_nudge", "cat:promise"]`
- **Importance:** `high`
- **Content:** `[PENDING NUDGE] An earlier turn made a commitment that was not delivered.\nSuggested follow-up: ...`

The high importance lets `user_prompt_inject.py` surface the nudge at its lower `MEMORY_PROMPT_HIGH_THRESHOLD` (default 0.35) — so even a topic shift on the next turn still raises the nudge if it's at all relevant.

---

## Migration Notes (from `turn_extractor.py`)

When `merge_hooks.py` installs the renamed script it:

1. Copies `turn_analysis.py` to `~/.nna/hooks/notnative-memory/`
2. Removes the obsolete `turn_extractor.py` from the install dir (clean rename)
3. Updates `manifest.json` to invoke `python turn_analysis.py`
4. Writes `hooks.env` with the same `MEMORY_EXTRACT_*` variable names (config-compatible)

On first run after the rename, `turn_analysis.py` deletes the legacy `~/.nna/turn_extractor.log` so operators don't accumulate stale logs.

---

## Test Coverage

`tests/test_turn_analysis.py` — 17 tests covering:

- Markdown fence stripping (3 variants)
- Combined prompt construction (both sections present, length caps enforced)
- LLM response shape parsing (full shape, missing keys, short-conversation skip, LLM-unreachable)
- Nudge storage (empty-text skip, correct tags + importance)
- Fact storage (malformed-item skip, max-extractions cap, confidence → importance mapping)
- Legacy log cleanup (delete on first run, no-op when absent)
- Log path uses renamed file

Run: `python tests/test_turn_analysis.py`

---

## Failure Modes

| Failure | Behavior |
|---------|----------|
| LLM unreachable | Returns empty analysis shape; logs `[WARN]` to stderr |
| Invalid JSON from LLM | Same as unreachable — empty shape |
| RAG ingestion fails | Logs `[ERROR]` to stderr per item; continues remaining items |
| Missing `prompt` or `model_response` in stdin | Logs `[ERROR]`, exits 1 (non-blocking; NNA continues) |
| Trivial conversation (<300 chars) | Skipped silently |

The hook never blocks NNA. All failures are recoverable — next turn retries automatically.

---

## Related

- `user_prompt_inject.py` — pre-phase semantic search + injection (consumer of `pending_nudge` memories)
- `compact_guard.py` — compaction-time persistent context injection
- `merge_hooks.py` — installer that copies hooks into NNA's drop-in directory
- NNA design doc: `D:/ProjectRepo/NotNativeAgent/docs/planning/turn-analysis-self-learning-system.md`
- NNA implementation status: `D:/ProjectRepo/NotNativeAgent/docs/planning/turn-analysis-implementation-status.md`
