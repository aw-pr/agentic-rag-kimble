"""
Embedder — sentence-transformers wrapper for local CPU inference.

Loads the model once (lazy singleton per instance). Returns plain Python
lists compatible with LadybugDB's native HNSW vector index.
"""

from __future__ import annotations

import numpy as np
from sentence_transformers import SentenceTransformer

from src.config import Config


class Embedder:
    """Wraps sentence-transformers for local CPU embedding. Loads model once."""

    def __init__(self, config: Config) -> None:
        self._model: SentenceTransformer | None = None
        self._model_name: str = config.embedding_model  # "BAAI/bge-small-en-v1.5"
        self._batch_size: int = config.embedding_batch_size

    # ── Private ────────────────────────────────────────────────────────────

    def _load(self) -> SentenceTransformer:
        if self._model is None:
            self._model = SentenceTransformer(self._model_name)
        return self._model

    # ── Public API ─────────────────────────────────────────────────────────

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns list of 384-dim float lists."""
        model = self._load()
        vectors = model.encode(
            texts,
            batch_size=self._batch_size,
            show_progress_bar=False,
        )
        # Convert numpy arrays to plain Python lists (required by LadybugDB VECTOR extension)
        if isinstance(vectors, np.ndarray):
            return vectors.tolist()
        return [v.tolist() if isinstance(v, np.ndarray) else list(v) for v in vectors]

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text. Returns a 384-dim float list."""
        result = self.embed([text])
        return result[0]

    @property
    def dimension(self) -> int:
        return 384
