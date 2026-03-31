"""
Auto-classification for memory content.

Regex-based classifier that detects memory types from content and
augments user-provided tags. Never removes user tags, only adds.

Runs in the store path — zero-cost improvement to tag consistency
without requiring the AI to get tagging right every time.
"""

import re
from typing import List

_CLASSIFIERS = [
    ("decision",   re.compile(
        r"\b(decided|chose|switched to|went with|picked|selected|"
        r"using .+ instead of|settled on|opting for)\b", re.I)),
    ("preference", re.compile(
        r"\b(prefer|always use|never do|convention is|style is|"
        r"likes? to|wants? to|standard is)\b", re.I)),
    ("gotcha",     re.compile(
        r"\b(gotcha|watch out|careful|pitfall|subtle|non-obvious|"
        r"took .+ to debug|easy to miss|trap|caveat)\b", re.I)),
    ("correction", re.compile(
        r"\b(don't|stop doing|not that|wrong approach|changed my mind|"
        r"actually|was wrong|should have|mistake)\b", re.I)),
    ("constraint", re.compile(
        r"\b(must not|cannot|do not|never|forbidden|required to|"
        r"read.only|observation only|off.limits)\b", re.I)),
]


def classify(content: str) -> List[str]:
    """
    Detect memory type tags from content text.

    Returns a list of auto-detected tag strings. May return an empty
    list if no patterns match.
    """
    return [tag for tag, pattern in _CLASSIFIERS if pattern.search(content)]


def augment_tags(user_tags: List[str], content: str) -> List[str]:
    """
    Augment user-provided tags with auto-detected tags.

    Auto-detected tags are unioned with user tags. Duplicates are
    removed while preserving user tag order.

    Args:
        user_tags: Tags provided by the caller.
        content: Memory content to classify.

    Returns:
        Combined tag list (user tags first, then any new auto-detected).
    """
    auto_tags = classify(content)
    existing = set(user_tags)
    merged = list(user_tags)
    for tag in auto_tags:
        if tag not in existing:
            merged.append(tag)
            existing.add(tag)
    return merged
