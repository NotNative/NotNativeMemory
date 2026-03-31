"""
Local embedding model wrapper for NotNativeMemory.

Loads gte-base-en-v1.5 (768-dim) on first use and keeps it in memory.
Runs on CPU - the model is ~130MB and fast enough for single queries.
No GPU needed, no external API calls.
"""

import os
from typing import List, Optional

# Lazy-loaded model instance. Stays in memory after first embed() call
# so subsequent calls are fast (no reload).
_model = None
_model_path: Optional[str] = None


def _get_model_path() -> str:
    """Resolve the embedding model path from env or default."""
    global _model_path
    if _model_path:
        return _model_path

    from dotenv import load_dotenv
    load_dotenv()

    relative = os.environ.get("MEMORY_MODEL_PATH", "models/gte-base-en-v1.5")
    # Resolve relative to project root (parent of lib/)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _model_path = os.path.join(project_root, relative)
    return _model_path


def _load_model():
    """Load the sentence-transformers model. Called once on first embed()."""
    global _model
    if _model is not None:
        return _model

    from sentence_transformers import SentenceTransformer

    path = _get_model_path()
    if not os.path.isdir(path):
        raise FileNotFoundError(
            f"Embedding model not found at {path}. "
            f"Run the install script to download it."
        )

    _model = SentenceTransformer(path, trust_remote_code=True)
    return _model


def embed(text: str) -> List[float]:
    """
    Embed a text string into a 768-dimensional vector.

    Args:
        text: The text to embed. Should be non-empty.

    Returns:
        List of 768 floats representing the text embedding.

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
