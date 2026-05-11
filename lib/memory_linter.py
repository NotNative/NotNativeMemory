"""
Write-time linter for memory content.

Runs on `memory_store` after a successful insert and returns advisory
warnings on the response. Never rejects the write — the linter
suggests, the user (or the model on the user's behalf) decides.

The rules target the same audience the memory itself targets: any
model that consumes this MCP, down to 20-30B local models. The plan's
style guide ("HOW to write" block in `memory_store`'s description) is
the source of truth; this linter checks the cheap mechanical bits of
it.

Checks:
- Long sentences (> LONG_SENTENCE_WORDS words).
- Cross-memory meta-phrases ("this coexists with...", "see also...").
- For class='rule' memories: missing `Why:` or `How to apply:` anchor.

Disabled by setting MEMORY_LINT_ENABLED=0 in the server's environment.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

# Words per sentence threshold. The style guide targets 15-25, so 40
# is the "you've blown past the target" alarm rather than the target.
LONG_SENTENCE_WORDS = 40

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Phrases that read as cross-memory composition — discouraged by the
# style guide because each memory should stand alone (smaller models
# can't reliably reason across stored memories).
_META_PHRASES = (
    "this coexists with",
    "this is consistent with",
    "see also memory",
    "as noted above",
    "as discussed earlier",
    "the previous memory",
    "the other memory",
)


def _is_enabled() -> bool:
    val = os.environ.get("MEMORY_LINT_ENABLED", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


def lint(content: str, memory_class: Optional[str] = None) -> List[dict]:
    """Inspect a memory's content and return advisory warnings.

    Returns an empty list when linting is disabled or the content
    passes all checks. Each warning is a dict with `code` and
    `message` keys so callers can render or aggregate them.
    """
    if not _is_enabled() or not content:
        return []

    warnings: List[dict] = []
    stripped = content.strip()

    sentences = [s for s in _SENTENCE_SPLIT.split(stripped) if s.strip()]
    for i, sentence in enumerate(sentences, start=1):
        word_count = len(sentence.split())
        if word_count > LONG_SENTENCE_WORDS:
            warnings.append({
                "code": "long_sentence",
                "message": (
                    f"Sentence {i} is {word_count} words; style guide "
                    f"targets 15-25 (alarm at {LONG_SENTENCE_WORDS})."
                ),
            })

    lower = stripped.lower()
    for phrase in _META_PHRASES:
        if phrase in lower:
            warnings.append({
                "code": "meta_phrase",
                "message": (
                    f"Phrase '{phrase}' references another memory. "
                    f"Each memory should stand alone."
                ),
            })

    if memory_class == "rule":
        if "Why:" not in content:
            warnings.append({
                "code": "rule_missing_why",
                "message": (
                    "Rule-class memory should include a 'Why:' line "
                    "explaining the motivation. Smaller models latch onto "
                    "this anchor when applying the rule."
                ),
            })
        if "How to apply:" not in content:
            warnings.append({
                "code": "rule_missing_how",
                "message": (
                    "Rule-class memory should include a 'How to apply:' "
                    "line describing when the rule kicks in."
                ),
            })

    return warnings
