"""
Regression checks for local embedding model directory validation.

Usage:
    python tests/test_embedding_model_validation.py
"""

import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from lib.embeddings import _is_complete_model_dir  # noqa: E402


def write(path: str, content: str = "{}") -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def main() -> int:
    failed = 0

    def check(label: str, cond: bool) -> None:
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, "missing")
        check("missing path is incomplete", not _is_complete_model_dir(missing))

        partial = os.path.join(tmp, "partial")
        os.makedirs(partial)
        write(os.path.join(partial, "config.json"))
        check("partial path is incomplete", not _is_complete_model_dir(partial))

        missing_model_type = os.path.join(tmp, "missing-model-type")
        os.makedirs(missing_model_type)
        for filename in (
            "config.json",
            "modules.json",
            "config_sentence_transformers.json",
            "tokenizer.json",
            "model.safetensors",
        ):
            write(os.path.join(missing_model_type, filename))
        check(
            "config without model_type is incomplete",
            not _is_complete_model_dir(missing_model_type),
        )

        complete = os.path.join(tmp, "complete")
        os.makedirs(complete)
        write(os.path.join(complete, "config.json"), '{"model_type": "new"}')
        for filename in (
            "modules.json",
            "config_sentence_transformers.json",
            "tokenizer.json",
            "model.safetensors",
        ):
            write(os.path.join(complete, filename))
        check("complete path is accepted", _is_complete_model_dir(complete))

    print("---")
    print(f"{4 - failed}/4 passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
