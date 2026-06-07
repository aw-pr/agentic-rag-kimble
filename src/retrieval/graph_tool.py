"""
graph_tool.py — read-only Cypher query tool for the Claude agent.

Validates that queries contain no write operations before executing them
against the LadybugDB graph database.
"""

from __future__ import annotations

from src.config import Config
from src.graph.db import _WRITE_KEYWORD_RE, GraphDB


def validate_cypher(query: str) -> None:
    """Raise PermissionError if query contains write operations.

    Uses the canonical whole-word regex from src.graph.db so both call sites
    stay in sync. Whole-word matching avoids false positives on identifiers
    like "dataset" or "created_at".
    """
    match = _WRITE_KEYWORD_RE.search(query)
    if match:
        kw = match.group(1).upper()
        raise PermissionError(
            f"Write operation '{kw}' not permitted in graph_query tool"
        )


def graph_query(cypher: str, explain: str, config: Config) -> list[dict]:
    """
    Execute a read-only Cypher query against the LadybugDB graph.

    Parameters
    ----------
    cypher  : A read-only Cypher query (no CREATE/MERGE/DELETE/SET/DROP).
    explain : One sentence explaining why this query answers the user's question.
    config  : Config carrying ladybug_db_path.

    Returns
    -------
    Up to 200 rows as a list of dicts.
    """
    validate_cypher(cypher)
    with GraphDB(config) as db:
        results = db.execute(cypher)
    return results[:200]


def graph_query_tool_schema() -> dict:
    """Return the Claude API tool definition for graph_query."""
    return {
        "name": "graph_query",
        "description": (
            "Execute a read-only Cypher query against the ML experiment knowledge graph. "
            "Use for exact lookups, relationship traversal, and filtering by known entity "
            "names or IDs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cypher": {
                    "type": "string",
                    "description": (
                        "A read-only Cypher query. "
                        "Must not contain CREATE, MERGE, DELETE, SET, or DROP."
                    ),
                },
                "explain": {
                    "type": "string",
                    "description": (
                        "One sentence explaining why this query answers the user's question."
                    ),
                },
            },
            "required": ["cypher", "explain"],
        },
    }
