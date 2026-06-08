"""
Local embedding model wrapper for NotNativeMemory.

Loads gte-large-en-v1.5 (1024-dim) on first use and keeps it in memory.
Runs on CPU in fp16: ~870MB on disk and ~1GB resident after the half()
cast. Halving the dtype halves both footprints at a negligible quality
cost for cosine-similarity retrieval. No GPU needed, no external API
calls.
"""

import json
import os
import threading
from typing import List, Optional

# Output dimensionality of the configured embedding model. Used by the
# DB schema (vector(EMBEDDING_DIM)) and by tests that construct fake
# vectors with the right shape. Change here + the schema migration +
# re-embed; nowhere else should hardcode the number.
EMBEDDING_DIM = 1024

# Lazy-loaded model instance. Stays in memory after first embed() call
# so subsequent calls are fast (no reload).
_model = None
_model_path: Optional[str] = None

# Guards concurrent first-callers of _load_model(). SentenceTransformer
# instantiation takes seconds; without this, two threads racing on a
# cold cache would both load, one would overwrite the other, and the
# loser's instance would leak until GC. threading.Lock (not asyncio)
# because embed() is a synchronous, potentially-cross-thread API.
_model_lock = threading.Lock()


def _is_complete_model_dir(path: str) -> bool:
    """Return True when the local SentenceTransformer snapshot is usable."""
    required_files = (
        "config.json",
        "modules.json",
        "config_sentence_transformers.json",
        "tokenizer.json",
    )
    if not os.path.isdir(path):
        return False
    if any(not os.path.isfile(os.path.join(path, name)) for name in required_files):
        return False
    try:
        with open(os.path.join(path, "config.json"), "r", encoding="utf-8") as fh:
            if not json.load(fh).get("model_type"):
                return False
    except (OSError, json.JSONDecodeError):
        return False
    return any(
        os.path.isfile(os.path.join(path, name))
        for name in ("model.safetensors", "pytorch_model.bin")
    )


def _get_model_path() -> str:
    """Resolve the embedding model path from env or default."""
    global _model_path
    if _model_path:
        return _model_path

    from dotenv import load_dotenv
    load_dotenv()

    relative = os.environ.get("MEMORY_MODEL_PATH", "models/gte-large-en-v1.5")
    # Resolve relative to project root (parent of lib/)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _model_path = os.path.join(project_root, relative)
    return _model_path


def _load_model():
    """Load the sentence-transformers model. Called once on first embed().

    Thread-safe: the fast path is lock-free (module-level _model read),
    and the slow path acquires _model_lock with a double-check so only
    one thread runs SentenceTransformer().
    """
    global _model
    if _model is not None:
        return _model

    with _model_lock:
        # Re-check under the lock: a thread that was blocked here may
        # have already loaded the model.
        if _model is not None:
            return _model

        from sentence_transformers import SentenceTransformer

        path = _get_model_path()
        if not _is_complete_model_dir(path):
            raise FileNotFoundError(
                f"Embedding model not found or incomplete at {path}. "
                f"Run the install script to download it. "
                f"If this path exists, remove it or re-run the installer so "
                f"the model can be repaired."
            )

        _model = SentenceTransformer(path, trust_remote_code=True)
        # Cast to fp16 unconditionally so RAM stays ~1GB regardless of
        # on-disk dtype. The install script saves fp16 checkpoints, so
        # the cast is a no-op there, but defends against an fp32 model
        # directory left over from a prior install.
        _model = _model.half()
        return _model


def embed(text: str) -> List[float]:
    """
    Embed a text string into a 1024-dimensional vector.

    Args:
        text: The text to embed. Should be non-empty.

    Returns:
        List of 1024 floats representing the text embedding.

    Raises:
        ValueError: If text is empty.
        FileNotFoundError: If the model hasn't been downloaded.
    """
    if not text or not text.strip():
        raise ValueError("Cannot embed empty text")

    model = _load_model()
    # encode() returns a numpy array; convert to plain list for Postgres
    vector = model.encode(text, normalize_embeddings=True)
    return vector.tolist()


def embed_batch(texts: List[str]) -> List[List[float]]:
    """
    Embed multiple texts in a single batch (more efficient than calling embed() in a loop).

    Args:
        texts: List of non-empty text strings.

    Returns:
        List of embedding vectors, one per input text.
    """
    if not texts:
        return []

    model = _load_model()
    vectors = model.encode(texts, normalize_embeddings=True)
    return [v.tolist() for v in vectors]
