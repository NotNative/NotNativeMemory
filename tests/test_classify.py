"""
Unit tests for lib/classify.py.

Covers the five auto-classification categories (decision, preference,
gotcha, correction, constraint), plus the augment_tags helper that
merges auto-detected tags with user-provided tags.

Usage:
    python tests/test_classify.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from lib.classify import classify, augment_tags


def run() -> int:
    failed = 0

    def check(label, cond):
        nonlocal failed
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failed += 1

    # -- decision category ---------------------------------------------------
    check("decision: 'decided to X'",
          "decision" in classify("decided to use asyncpg"))
    check("decision: 'chose X'",
          "decision" in classify("chose Postgres over SQLite"))
    check("decision: 'switched to X'",
          "decision" in classify("switched to pytest for this project"))
    check("decision: 'went with X'",
          "decision" in classify("went with a single-file module"))
    check("decision: 'settled on X'",
          "decision" in classify("settled on 500 as the cap"))
    check("decision: 'opting for X'",
          "decision" in classify("opting for the simpler approach"))

    # -- preference category -------------------------------------------------
    check("preference: 'prefer X'",
          "preference" in classify("I prefer tabs to spaces"))
    check("preference: 'always use X'",
          "preference" in classify("always use type hints"))
    check("preference: 'convention is X'",
          "preference" in classify("convention is snake_case"))
    check("preference: 'style is X'",
          "preference" in classify("style is terse comments only"))
    check("preference: 'likes to X'",
          "preference" in classify("user likes to see short responses"))
    check("preference: 'wants to X'",
          "preference" in classify("wants to avoid emoji"))

    # -- gotcha category -----------------------------------------------------
    check("gotcha: 'gotcha'",
          "gotcha" in classify("gotcha: pgvector needs the extension"))
    check("gotcha: 'watch out for X'",
          "gotcha" in classify("watch out for the \\b regex boundary"))
    check("gotcha: 'careful with X'",
          "gotcha" in classify("careful with the migration runner"))
    check("gotcha: 'subtle X'",
          "gotcha" in classify("subtle race in the counter throttle"))
    check("gotcha: 'caveat'",
          "gotcha" in classify("caveat: this only fires on loopback"))

    # -- correction category -------------------------------------------------
    check("correction: \"don't X\"",
          "correction" in classify("don't commit planning docs"))
    check("correction: 'stop doing X'",
          "correction" in classify("stop doing blanket excepts"))
    check("correction: 'changed my mind'",
          "correction" in classify("changed my mind about the cap"))
    check("correction: 'actually X'",
          "correction" in classify("actually, use app_conn here"))
    check("correction: 'was wrong'",
          "correction" in classify("I was wrong about that"))

    # -- constraint category -------------------------------------------------
    check("constraint: 'must not X'",
          "constraint" in classify("must not bypass RLS in tests"))
    check("constraint: 'cannot X'",
          "constraint" in classify("cannot touch production creds"))
    check("constraint: 'forbidden'",
          "constraint" in classify("forbidden from rebasing shared branches"))
    check("constraint: 'required to X'",
          "constraint" in classify("required to pin dependencies"))
    check("constraint: 'off-limits'",
          "constraint" in classify("the .env file is off-limits"))

    # -- case insensitivity --------------------------------------------------
    check("case-insensitive: DECIDED",
          "decision" in classify("DECIDED to use JSON"))
    check("case-insensitive: Prefer",
          "preference" in classify("Prefer type hints everywhere"))

    # -- word boundaries -----------------------------------------------------
    # "undecided" should not match "decided" (the regex uses \b)
    check("word boundary: 'undecided' not matched",
          "decision" not in classify("he remained undecided about the cap"))
    check("word boundary: 'pickles' not matched as 'picked'",
          "decision" not in classify("I enjoy pickles with dinner"))

    # -- no match ------------------------------------------------------------
    check("no match: ordinary prose",
          classify("The quick brown fox jumps over the lazy dog.") == [])
    check("no match: empty string",
          classify("") == [])

    # -- multiple categories in one message ----------------------------------
    tags = classify("decided to prefer tabs (don't use spaces)")
    check("multi: decision + preference + correction",
          "decision" in tags and "preference" in tags
          and "correction" in tags)

    # Classifier returns tags in the same fixed order as _CLASSIFIERS
    check("multi: order matches _CLASSIFIERS list",
          tags == ["decision", "preference", "correction"])

    # -- augment_tags: user tags come first ----------------------------------
    merged = augment_tags(["urgent", "backend"], "decided to switch to asyncpg")
    check("augment: user tags preserved in order",
          merged[:2] == ["urgent", "backend"])
    check("augment: auto tags appended",
          "decision" in merged)

    # -- augment_tags: dedup when user tag already matches auto tag ----------
    merged = augment_tags(["decision"], "decided to X")
    check("augment: no duplicate when user already has 'decision'",
          merged.count("decision") == 1)
    check("augment: user-provided 'decision' stays in original position",
          merged[0] == "decision")

    # -- augment_tags: empty user tags ---------------------------------------
    merged = augment_tags([], "I prefer short names")
    check("augment: empty user tags returns only auto tags",
          merged == ["preference"])

    # -- augment_tags: no auto match returns user tags unchanged -------------
    merged = augment_tags(["foo", "bar"], "The quick brown fox.")
    check("augment: no auto match preserves user tags verbatim",
          merged == ["foo", "bar"])

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
