"""
semantic_tool.py — semantic similarity search tool for the Claude agent.

Delegates to src.retrieval.semantic (the thin singleton wrapper built in pass 05)
so the embedding model is loaded once per process.
"""

from __future__ import annotations

from src.config import Config

_VALID_ENTITY_TYPES = frozenset(["Algorithm", "Dataset", "Task"])


def semantic_search(
    query: str,
    entity_type: str,
    top_k: int = 10,
    config: Config | None = None,
) -> list[dict]:
    """
    Find ML dimension entities (Algorithm, Dataset, Task) by semantic similarity.

    Parameters
    ----------
    query       : Natural-language description of what you are looking for.
    entity_type : One of "Algorithm", "Dataset", "Task".
    top_k       : Number of results to return (1–50).
    config      : Config instance (uses get_config() default if None).

    Returns
    -------
    list of dicts, each with {"name": str, "score": float, ...entity_properties}.
    """
    # MCP layer may forward top_k as a string — coerce defensively.
    top_k = int(top_k)

    if entity_type not in _VALID_ENTITY_TYPES:
        raise ValueError(
            f"entity_type must be one of {sorted(_VALID_ENTITY_TYPES)}, got '{entity_type}'"
        )
    if top_k < 1 or top_k > 50:
        raise ValueError(f"top_k must be between 1 and 50, got {top_k}")

    if config is None:
        from src.config import get_config
        config = get_config()

    # Delegate to the singleton-backed wrapper from pass 05.
    from src.retrieval.semantic import semantic_search as _semantic_search

    return _semantic_search(query=query, entity_type=entity_type, top_k=top_k, config=config)


def semantic_search_tool_schema() -> dict:
    """Return the Claude API tool definition for semantic_search."""
    return {
        "name": "semantic_search",
        "description": (
            "Find ML algorithms, datasets, or tasks by semantic similarity to a description. "
            "Use for 'similar to X', 'like Y', or when you don't know the exact name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language description of what you are looking for.",
                },
                "entity_type": {
                    "type": "string",
                    "enum": ["Algorithm", "Dataset", "Task"],
                    "description": "Which dimension entity type to search.",
                },
                "top_k": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Number of results to return.",
                },
            },
            "required": ["query", "entity_type"],
        },
    }
