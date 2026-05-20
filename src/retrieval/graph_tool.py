"""
graph_tool.py — read-only Cypher query tool for the Claude agent.

Validates that queries contain no write operations before executing them
against the LadybugDB graph database.
"""

from __future__ import annotations

import re

from src.config import Config
from src.graph.db import GraphDB

# Whole-word regex so "dataset.name STARTS WITH 'set'" does not trip.
_WRITE_PATTERN = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|DROP|REMOVE)\b",
    re.IGNORECASE,
)


def validate_cypher(query: str) -> None:
    """Raise PermissionError if query contains write operations.

    Uses whole-word matching so property values containing write keywords
    (e.g. 'dataset') do not cause false positives.
    """
    if _WRITE_PATTERN.search(query):
        match = _WRITE_PATTERN.search(query)
        kw = match.group(1).upper() if match else "write"
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
    config  : Config carrying kuzu_db_path.

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
