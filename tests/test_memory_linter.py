"""
Unit tests for lib.memory_linter.

Covers:
- Linter is advisory: long content always returns warnings, never raises.
- Long sentences detected past LONG_SENTENCE_WORDS.
- Meta-phrases detected case-insensitively.
- Rule-class memories require Why: and How to apply: anchors; non-rule
  classes do not.
- Disabled via MEMORY_LINT_ENABLED=0.

No DB or network. Pure function tests.

Usage:
    python tests/test_memory_linter.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


def run() -> int:
    # Force-enable for the test run regardless of host env.
    os.environ["MEMORY_LINT_ENABLED"] = "1"

    # Reload in case a prior import cached the disabled state.
    import importlib
    from lib import memory_linter
    importlib.reload(memory_linter)

    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    # Clean short sentence: no warnings.
    out = memory_linter.lint("Use snake_case for Python identifiers.")
    check("clean short content: no warnings", out == [])

    # Empty content: no warnings (callers should gate, but linter
    # must not crash).
    check("empty content: no warnings", memory_linter.lint("") == [])

    # Long sentence detected.
    long_sentence = (
        "This is a very long sentence " * 10
    ) + "and it just keeps going."
    out = memory_linter.lint(long_sentence)
    check("long sentence: warning emitted",
          any(w["code"] == "long_sentence" for w in out))

    # Boundary case: exactly LONG_SENTENCE_WORDS should not trigger.
    just_under = " ".join(["w"] * memory_linter.LONG_SENTENCE_WORDS) + "."
    out = memory_linter.lint(just_under)
    check(f"sentence at threshold ({memory_linter.LONG_SENTENCE_WORDS}): no warning",
          not any(w["code"] == "long_sentence" for w in out))

    just_over = " ".join(["w"] * (memory_linter.LONG_SENTENCE_WORDS + 1)) + "."
    out = memory_linter.lint(just_over)
    check(f"sentence one over threshold: warning",
          any(w["code"] == "long_sentence" for w in out))

    # Meta-phrase detection (case insensitive).
    out = memory_linter.lint("This is consistent with the rule we discussed.")
    check("meta-phrase 'this is consistent with': warning",
          any(w["code"] == "meta_phrase" for w in out))

    out = memory_linter.lint("AS NOTED ABOVE we already decided.")
    check("meta-phrase case-insensitive: warning",
          any(w["code"] == "meta_phrase" for w in out))

    # No meta-phrase: no meta_phrase warning.
    out = memory_linter.lint("Standalone fact about HS256 token hashing.")
    check("no meta-phrase: no warning",
          not any(w["code"] == "meta_phrase" for w in out))

    # Rule-class without Why: / How to apply: -> warnings.
    out = memory_linter.lint("Never force-push to main.", memory_class="rule")
    codes = {w["code"] for w in out}
    check("rule missing Why:", "rule_missing_why" in codes)
    check("rule missing How to apply:", "rule_missing_how" in codes)

    # Rule-class with both anchors: no rule_missing_* warnings.
    rule_ok = (
        "Never force-push to main.\n"
        "Why: rewrites history shared with the team.\n"
        "How to apply: always pull before pushing; use --force-with-lease."
    )
    out = memory_linter.lint(rule_ok, memory_class="rule")
    codes = {w["code"] for w in out}
    check("rule with Why: no missing-why warning",
          "rule_missing_why" not in codes)
    check("rule with How to apply: no missing-how warning",
          "rule_missing_how" not in codes)

    # Non-rule class doesn't require the anchors.
    out = memory_linter.lint("Just a preference.", memory_class="preference")
    codes = {w["code"] for w in out}
    check("preference doesn't require Why:",
          "rule_missing_why" not in codes)
    check("preference doesn't require How to apply:",
          "rule_missing_how" not in codes)

    # No class specified: no anchor warnings.
    out = memory_linter.lint("Just a memory.", memory_class=None)
    codes = {w["code"] for w in out}
    check("class=None: no missing-why warning",
          "rule_missing_why" not in codes)

    # Disabled via env: returns empty list even for offensive content.
    os.environ["MEMORY_LINT_ENABLED"] = "0"
    importlib.reload(memory_linter)
    out = memory_linter.lint(long_sentence, memory_class="rule")
    check("MEMORY_LINT_ENABLED=0: returns empty list", out == [])

    # Re-enable for trailing tests.
    os.environ["MEMORY_LINT_ENABLED"] = "1"
    importlib.reload(memory_linter)

    # Warning shape: dicts with code + message keys.
    out = memory_linter.lint(long_sentence)
    check("warning shape: has code + message",
          all(set(w.keys()) >= {"code", "message"} for w in out))

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
