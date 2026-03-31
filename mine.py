"""
Conversation mining CLI for NotNativeMemory.

Parses Claude Code JSONL transcripts into exchange pairs and bulk-stores
them as memories. Useful for retroactive capture of important context
from sessions where the model didn't proactively use memory tools.

Usage:
    python mine.py <transcript_path> [--project <name>]
    python mine.py conversations/session-2026-04-07.jsonl
    python mine.py conversations/session.jsonl --project "/path/to/your/project"
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import List, Tuple

from dotenv import load_dotenv


# Minimum combined length (chars) for an exchange to be worth storing
_MIN_EXCHANGE_LENGTH = 50


def parse_claude_code_jsonl(path: Path) -> List[Tuple[str, str]]:
    """
    Parse a Claude Code JSONL transcript into exchange pairs.

    Each line in the JSONL is a message object with 'role' and 'content'.
    Groups consecutive user+assistant messages into pairs.

    Returns:
        List of (user_message, assistant_message) tuples.
    """
    messages = []
    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                print(f"  Skipping malformed line {line_num}", file=sys.stderr)
                continue

            role = msg.get("role", "")
            content = msg.get("content", "")

            # Content can be a string or a list of content blocks
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, str):
                        text_parts.append(block)
                    elif isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                content = "\n".join(text_parts)

            if role in ("user", "assistant", "human") and content.strip():
                messages.append((role, content.strip()))

    # Pair up: find user->assistant sequences
    pairs = []
    i = 0
    while i < len(messages):
        if messages[i][0] in ("user", "human"):
            user_text = messages[i][1]
            # Collect the next assistant response
            if i + 1 < len(messages) and messages[i + 1][0] == "assistant":
                assistant_text = messages[i + 1][1]
                pairs.append((user_text, assistant_text))
                i += 2
                continue
        i += 1

    return pairs


async def mine_transcript(
    path: Path,
    project_dir: str,
) -> dict:
    """
    Mine a transcript file and store exchanges as memories.

    Returns stats dict with counts of processed, stored, duplicates, skipped.
    """
    from lib.embeddings import embed
    from lib.db import store_memory, get_or_create_project
    from lib.classify import augment_tags

    pairs = parse_claude_code_jsonl(path)
    if not pairs:
        return {"processed": 0, "stored": 0, "duplicates": 0, "skipped": 0}

    project_id = await get_or_create_project(project_dir)

    stored = 0
    duplicates = 0
    skipped = 0

    for user_msg, assistant_msg in pairs:
        combined = f"{user_msg}\n{assistant_msg}"

        # Skip trivial exchanges
        if len(combined) < _MIN_EXCHANGE_LENGTH:
            skipped += 1
            continue

        # Format as a memory
        content = f"User: {user_msg}\nAssistant: {assistant_msg}"

        # Auto-classify and add mined tag
        tags = augment_tags(["mined"], content)

        embedding = embed(content)

        try:
            await store_memory(
                content=content,
                embedding=embedding,
                project_id=project_id,
                tags=tags,
                importance="normal",
            )
            stored += 1
        except Exception as exc:
            # store_memory handles dedup internally — merged counts as stored
            print(f"  Error storing exchange: {exc}", file=sys.stderr)
            skipped += 1

    # Duplicates are handled silently by store_memory's merge logic,
    # so we report stored count (which includes merges)
    return {
        "processed": len(pairs),
        "stored": stored,
        "skipped": skipped,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Mine Claude Code transcripts into NotNativeMemory",
    )
    parser.add_argument(
        "transcript",
        type=Path,
        help="Path to JSONL transcript file",
    )
    parser.add_argument(
        "--project",
        type=str,
        default=None,
        help="Project directory path (auto-detected from cwd if omitted)",
    )
    args = parser.parse_args()

    if not args.transcript.exists():
        print(f"Error: file not found: {args.transcript}", file=sys.stderr)
        sys.exit(1)

    load_dotenv()

    import os
    project_dir = args.project or os.path.abspath(os.getcwd())

    print(f"Mining: {args.transcript}")
    print(f"Project: {project_dir}")

    stats = asyncio.run(mine_transcript(args.transcript, project_dir))

    print(f"\nResults:")
    print(f"  Exchanges found:  {stats['processed']}")
    print(f"  Memories stored:  {stats['stored']}")
    print(f"  Skipped (trivial): {stats['skipped']}")


if __name__ == "__main__":
    main()
