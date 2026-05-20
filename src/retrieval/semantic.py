"""
Thin wrapper called by the eval harness (src.eval.metrics._try_semantic_search)
and by the agent semantic_search tool (pass 06).

Signature: semantic_search(query, entity_type, top_k, config) -> list[dict]

Each result dict has at minimum {"name": str, "score": float}.
"""

from __future__ import annotations

from src.config import Config
from src.retrieval.embedder import Embedder
from src.retrieval.vector_store import VectorStore

# Module-level singletons so the model is loaded once per process.
_embedder: Embedder | None = None
_store: VectorStore | None = None
_store_config_path: str | None = None


def _get_store(config: Config) -> VectorStore:
    """Return a connected VectorStore, reusing the singleton if config matches."""
    global _embedder, _store, _store_config_path

    current_path = str(config.kuzu_db_path)
    if _store is None or _store_config_path != current_path:
        _embedder = Embedder(config)
        _store = VectorStore(config, _embedder)
        _store.connect()
        _store_config_path = current_path

    return _store


def semantic_search(
    query: str,
    entity_type: str,
    top_k: int,
    config: Config,
) -> list[dict]:
    """
    Semantic search via LadybugDB native HNSW vector index for the given entity_type.

    Parameters
    ----------
    query       : natural-language query string
    entity_type : one of "Algorithm", "Dataset", "Task"
    top_k       : number of results to return
    config      : Config (carries kuzu_db_path and embedding_model)

    Returns
    -------
    list of dicts, each with {"name": str, "score": float, ...entity_properties}
    """
    store = _get_store(config)
    return store.search(entity_type, query, top_k=top_k)
