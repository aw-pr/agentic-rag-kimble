"""
orchestrator.py — Claude Agent SDK client, agentic tool loop, citation extraction.

Auth: The Claude Agent SDK authenticates automatically via the Claude Code
OAuth session — no env vars or API keys required. Auth is handled by the
claude-agent-sdk package (claude-agent-sdk on PyPI, v0.1.81+).

Tools are registered as in-process MCP tools via the @tool decorator and
exposed to the Claude agent through create_sdk_mcp_server(). The SDK handles
the tool-use loop internally; we observe results via the async message stream.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk import (
    query as sdk_query,
)

from src.agent.prompts import SYSTEM_PROMPT
from src.config import Config
from src.retrieval.tools import dispatch_tool


@dataclass
class AgentResponse:
    query: str
    answer: str
    tool_calls: list[dict]    # [{name, input, result_preview, result}]
    citations: list[str]      # extracted from answer text
    total_tool_calls: int


# ---------------------------------------------------------------------------
# In-process MCP tool definitions
# These wrap the existing tool implementations in tools.py / *_tool.py via
# dispatch_tool() so all NaN-sanitisation and validation logic is preserved.
# _config is set before each SDK call in _run_query_async().
# ---------------------------------------------------------------------------

# Module-level config reference — set per-call in _run_query_async
_current_config: Config | None = None


def _require_config() -> Config:
    """Return the per-call config, asserting it was set by _run_query_async."""
    assert _current_config is not None, "Config not set before tool dispatch."
    return _current_config


@tool(
    name="graph_query",
    description=(
        "Execute a read-only Cypher query against the ML experiment knowledge graph. "
        "Use for exact lookups, relationship traversal, and filtering by known entity names or IDs."
    ),
    input_schema={
        "cypher": (
            str,
            "A read-only Cypher query. Must not contain CREATE, MERGE, DELETE, SET, or DROP.",
        ),
        "explain": (str, "One sentence explaining why this query answers the user's question."),
    },
)
async def _tool_graph_query(args: dict[str, Any]) -> dict[str, Any]:
    result_str = dispatch_tool("graph_query", args, _require_config())
    return {"content": [{"type": "text", "text": result_str}]}


@tool(
    name="semantic_search",
    description=(
        "Find ML algorithms, datasets, or tasks by semantic similarity to a description. "
        "Use for 'similar to X', 'like Y', or when you don't know the exact name."
    ),
    input_schema={
        "query": (str, "Natural-language description of what you are looking for."),
        "entity_type": (str, "Which dimension entity type to search: Algorithm, Dataset, or Task."),
        "top_k": (int, "Number of results to return (1-50, default 10)."),
    },
)
async def _tool_semantic_search(args: dict[str, Any]) -> dict[str, Any]:
    result_str = dispatch_tool("semantic_search", args, _require_config())
    return {"content": [{"type": "text", "text": result_str}]}


@tool(
    name="aggregate_measures",
    description=(
        "Aggregate ML run metrics (accuracy, f1, auc, runtime_sec) grouped by algorithm family, "
        "algorithm name, or dataset size bucket. Use when the user asks for comparisons, "
        "rankings, or averages."
    ),
    input_schema={
        "group_by": (
            str,
            "Dimension to group by: algorithm.family, algorithm.name, "
            "dataset.n_features_bucket, or dataset.n_rows_bucket.",
        ),
        "measure": (str, "Metric to aggregate: accuracy, auc, f1, or runtime_sec."),
        "filter_cypher": (
            str,
            "Optional WHERE clause fragment to narrow the result set. "
            "Leave empty for all runs.",
        ),
    },
)
async def _tool_aggregate_measures(args: dict[str, Any]) -> dict[str, Any]:
    result_str = dispatch_tool("aggregate_measures", args, _require_config())
    return {"content": [{"type": "text", "text": result_str}]}


# Citation pattern: [Type: value] — colon form, consistent with SYSTEM_PROMPT
_CITATION_RE = re.compile(r'\[(?:Algorithm|Dataset|Task|Run): ?[^\]]+\]')


def extract_citations(text: str) -> list[str]:
    """Extract [Algorithm: X], [Dataset: X], [Task: X], [Run: X] citations."""
    return _CITATION_RE.findall(text)


def run_query(query: str, config: Config) -> AgentResponse:
    """
    Run a single user query through the agentic tool loop.

    Synchronous facade over the async SDK call. Keeps the existing
    UI / eval contract: run_query(query, config) -> AgentResponse.

    The Claude Agent SDK handles auth automatically via the Claude Code
    OAuth session. No env vars or API keys required.
    """
    return asyncio.run(_run_query_async(query, config))


async def _run_query_async(user_query: str, config: Config) -> AgentResponse:
    """Async implementation — called by run_query()."""
    global _current_config
    _current_config = config

    # Build in-process MCP server with our three tools
    mcp_server = create_sdk_mcp_server(
        name="agentic-rag-tools",
        version="1.0.0",
        tools=[_tool_graph_query, _tool_semantic_search, _tool_aggregate_measures],
    )

    # max_turns in the SDK counts every exchange (user + assistant), not just
    # tool calls. Allow 2 turns per tool call plus 2 for the final answer.
    max_turns = config.agent_max_tool_calls * 2 + 2

    options = ClaudeAgentOptions(
        model=config.claude_model,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"agentic-rag-tools": mcp_server},
        max_turns=max_turns,
        permission_mode="bypassPermissions",
    )

    tool_calls_log: list[dict] = []
    answer_text = ""
    total_tool_calls = 0

    async for msg in sdk_query(prompt=user_query, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    total_tool_calls += 1
                    # Capture a preview of the tool input for the log.
                    # result_preview kept for UI backward compat; result filled below.
                    input_preview = json.dumps(block.input)[:200]
                    tool_calls_log.append({
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                        "result_preview": input_preview,
                        "result": None,
                    })
                elif isinstance(block, TextBlock):
                    # Accumulate text — the final assistant turn text is what
                    # we want; overwrite so we end up with the last one
                    answer_text = block.text

        elif isinstance(msg, UserMessage):
            # UserMessage carries ToolResultBlocks — the actual tool outputs
            # the model saw. Correlate by tool_use_id to fill result.
            for ublock in msg.content:
                if isinstance(ublock, ToolResultBlock):
                    result_str = str(ublock.content)[:1000]
                    for tc in tool_calls_log:
                        if tc.get("id") == ublock.tool_use_id:
                            tc["result"] = result_str
                            break

        elif isinstance(msg, ResultMessage):
            # ResultMessage.result holds the final text output
            if msg.result:
                answer_text = msg.result

    return AgentResponse(
        query=user_query,
        answer=answer_text,
        tool_calls=tool_calls_log,
        citations=extract_citations(answer_text),
        total_tool_calls=total_tool_calls,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Agentic RAG query runner")
    parser.add_argument("--query", required=False, help="Single query to run")
    parser.add_argument("--interactive", action="store_true", help="Interactive REPL mode")
    args = parser.parse_args()

    from src.config import get_config
    cfg = get_config()

    if args.interactive:
        print("Agentic RAG — type 'quit' to exit")
        while True:
            q = input("\nQuery: ").strip()
            if q.lower() == "quit":
                break
            resp = run_query(q, cfg)
            print(f"\n{resp.answer}")
            print(f"\n[{resp.total_tool_calls} tool calls | {len(resp.citations)} citations]")
    elif args.query:
        resp = run_query(args.query, cfg)
        print(resp.answer)
        if resp.tool_calls:
            print(f"\n[{resp.total_tool_calls} tool calls]")
    else:
        parser.print_help()
