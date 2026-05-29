# Turn Analysis - Learning Extraction

**Version:** 3.0
**Last Updated:** 2026-05-29
**Status:** Production

## Overview

The `turn_analysis.py` hook runs after an agent turn and asks a local or configured LLM to extract durable learning. It is non-blocking: analyzer failures must never stop the agent.

The analyzer now has two write channels:

- `state_assertions` go to `memory_fact_add` for mutable current state.
- `results` go to `memory_store` for durable rules, preferences, decisions, and background memories.

Conversation summaries are stored through RAG as session-summary documents. Promise/nudge detection moved to NNA and is no longer owned by NNM turn analysis.

## Output Schema

The LLM is instructed to return a single JSON object:

```json
{
  "state_assertions": [
    {
      "subject": "service-or-component",
      "predicate": "current-aspect",
      "object": "current-value",
      "confidence": 0.9
    }
  ],
  "results": [
    {
      "fact": "One standalone memory sentence.",
      "tags": ["short", "keywords"],
      "confidence": "high",
      "source": "user-stated",
      "memory_class": "rule"
    }
  ],
  "summary": "Optional 1-3 sentence dialogue summary."
}
```

`memory_class` is `rule`, `preference`, or `memory`. If the model omits it or emits an invalid value, deterministic fallback infers a conservative class from tags and wording.

## Reliability Guards

The analyzer does not trust model formatting blindly:

- Markdown fences are stripped.
- If the response contains prose around the JSON, the first balanced `{...}` object is recovered.
- Missing or wrong top-level fields are coerced to the safe empty shape.
- Irreparable output is logged and written to a quarantine JSONL file for review.
- Malformed memory/fact items are skipped item-by-item instead of failing the whole turn.

Default quarantine path is derived from `MEMORY_EXTRACT_LOG` as `<log-root>.quarantine.jsonl`. Override with `MEMORY_EXTRACT_QUARANTINE`.

## Storage

`results` are stored with:

- `content`: the fact text verbatim.
- `tags`: cleaned tag list.
- `importance`: `high`, `normal`, or `low` from confidence.
- `source`: `user-stated`, `tool-result`, or `model-inferred`; unknown values fall back to `model-inferred`.
- `memory_class`: valid LLM value or deterministic fallback.

`state_assertions` are stored as fact triples:

- `subject`
- `predicate`
- `object`
- `confidence`

This keeps mutable state out of semantic memories so stale facts can be superseded cleanly.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MEMORY_EXTRACT_MIN_LENGTH` | `40` | Minimum combined conversation chars before analysis runs. |
| `MEMORY_EXTRACT_TEMP` | `0.1` | Analyzer LLM temperature. |
| `MEMORY_EXTRACT_MAX_RESULTS` | `50` | Runaway hedge for extracted items. |
| `MEMORY_EXTRACT_TIMEOUT` | `300` | LLM/socket timeout in seconds. |
| `MEMORY_EXTRACT_LLM_URL` | derived | Explicit chat completion endpoint. |
| `MEMORY_EXTRACT_MODEL` | unset | Optional pinned model; otherwise OpenAI-compatible mode can discover. |
| `MEMORY_EXTRACT_PROJECT` | `_global` | Write scope for analyzer memories/facts. |
| `MEMORY_EXTRACT_LOG` | harness default | Failure and outcome log path. |
| `MEMORY_EXTRACT_QUARANTINE` | derived | JSONL path for irreparable analyzer responses. |

## Tests

Run:

```powershell
python tests/test_turn_analysis.py
```

The suite covers prompt contracts, JSON recovery, quarantine logging, fact-vs-memory routing, memory class fallback, MCP wire shape, and both Claude/NNA adapter behavior.
