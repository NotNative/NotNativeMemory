"""
Verbatim transcript adapter.

Reads append-only JSONL transcripts written by NNA's verbatim writer
(under ~/.nna/transcripts/<session-id>.jsonl). Exposes a substring
search that the dreaming loop (hygiene + conflict resolution) uses as
primary-source ground truth.

Embedding-backed search is a Phase 6 follow-up; the baseline here is
intentionally cheap: substring match in ASCII-lowered content.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator, Optional


def _default_dir() -> Path:
    override = os.environ.get("NNA_VERBATIM_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".nna" / "transcripts"


def list_sessions(directory: Optional[Path] = None) -> list[str]:
    """Return session IDs (filename stems) for every JSONL transcript
    present in `directory`. Empty list when the directory doesn't exist.
    """
    base = directory or _default_dir()
    if not base.exists():
        return []
    return [p.stem for p in base.glob("*.jsonl")]


def iter_session(
    session_id: str, directory: Optional[Path] = None,
) -> Iterator[dict]:
    """Yield JSONL entries from a session in append order. Malformed
    lines are skipped rather than raised; verbatim writes never
    block the agent, so the reader tolerates partial corruption."""
    base = directory or _default_dir()
    path = base / f"{session_id}.jsonl"
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def search_sessions(
    query: str,
    limit: int = 10,
    directory: Optional[Path] = None,
) -> list[dict]:
    """Substring search across every JSONL transcript. Returns matched
    entries with `session_id` attached. Cheap baseline; embedding search
    deferred to Phase 6."""
    if not query:
        return []
    needle = query.lower()
    base = directory or _default_dir()
    matches: list[dict] = []
    for session_id in list_sessions(base):
        for entry in iter_session(session_id, base):
            content = str(entry.get("content", "")).lower()
            if needle in content:
                matches.append({**entry, "session_id": session_id})
                if len(matches) >= limit:
                    return matches
    return matches
