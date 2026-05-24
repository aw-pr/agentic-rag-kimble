"""
Embedder — sentence-transformers wrapper.

Loads the model once (lazy singleton per instance). Returns plain Python
lists compatible with LadybugDB's native HNSW vector index.

Device selection: defaults to "auto" which picks MPS (Apple Metal) when
available, then CUDA, then CPU. Override via Config.embedding_device or
the EMBEDDING_DEVICE env var.
"""

from __future__ import annotations

import logging

import numpy as np
from sentence_transformers import SentenceTransformer

from src.config import Config

logger = logging.getLogger(__name__)


def _resolve_device(requested: str) -> str:
    """Resolve 'auto' to the best available backend; pass through explicit choices."""
    if requested != "auto":
        return requested
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class Embedder:
    """Wraps sentence-transformers. Loads model once on the configured device."""

    def __init__(self, config: Config) -> None:
        self._model: SentenceTransformer | None = None
        self._model_name: str = config.embedding_model  # "BAAI/bge-small-en-v1.5"
        self._batch_size: int = config.embedding_batch_size
        self._device: str = _resolve_device(config.embedding_device)

    # ── Private ────────────────────────────────────────────────────────────

    def _load(self) -> SentenceTransformer:
        if self._model is None:
            logger.info(
                "Loading embedding model %s on device=%s",
                self._model_name, self._device,
            )
            self._model = SentenceTransformer(self._model_name, device=self._device)
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
    def device(self) -> str:
        """The resolved compute device (cpu | mps | cuda) being used."""
        return self._device

    @property
    def dimension(self) -> int:
        return 384
