"""
tools.py — tool registry for the Claude agent.

Exposes get_tool_schemas() for passing to the Anthropic SDK and
dispatch_tool() for routing tool-use responses back to implementations.
"""

from __future__ import annotations

import json
import math
from typing import Any

from src.config import Config
from src.retrieval.aggregate_tool import aggregate_measures, aggregate_measures_tool_schema
from src.retrieval.graph_tool import graph_query, graph_query_tool_schema
from src.retrieval.semantic_tool import semantic_search, semantic_search_tool_schema


def _sanitise_nan(obj: Any) -> Any:
    """
    Recursively replace NaN / ±Inf float values with None.

    json.dumps() by default emits the bare token `NaN` for float('nan'),
    which is invalid JSON (RFC 8259). The smoke DB has NaN in many metric
    columns, so this guard prevents strict JSON consumers (including the
    Anthropic SDK) from rejecting tool results.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitise_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitise_nan(v) for v in obj]
    return obj


def get_tool_schemas() -> list[dict]:
    """Return all three tool schemas in Claude API format."""
    return [
        graph_query_tool_schema(),
        semantic_search_tool_schema(),
        aggregate_measures_tool_schema(),
    ]


def dispatch_tool(tool_name: str, tool_input: dict, config: Config) -> str:
    """
    Route a tool-use call from the Claude agent to the correct implementation.

    Parameters
    ----------
    tool_name  : The name field from the Claude tool-use block.
    tool_input : The input dict from the Claude tool-use block.
    config     : Config instance passed through to each tool.

    Returns
    -------
    JSON string — the tool result to send back to Claude.
    NaN / ±Inf values are sanitised to null before serialisation.
    """
    if tool_name == "graph_query":
        result = graph_query(
            cypher=tool_input["cypher"],
            explain=tool_input["explain"],
            config=config,
        )
    elif tool_name == "semantic_search":
        result = semantic_search(
            query=tool_input["query"],
            entity_type=tool_input["entity_type"],
            top_k=tool_input.get("top_k", 10),
            config=config,
        )
    elif tool_name == "aggregate_measures":
        result = aggregate_measures(
            group_by=tool_input["group_by"],
            measure=tool_input["measure"],
            filter_cypher=tool_input.get("filter_cypher", ""),
            config=config,
        )
    else:
        raise ValueError(f"Unknown tool: {tool_name!r}")

    clean = _sanitise_nan(result)
    return json.dumps(clean, indent=2, default=str)
