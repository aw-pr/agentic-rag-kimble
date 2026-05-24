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
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
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
    tool_calls: list[dict]    # [{name, input, result_preview, result, duration_ms}]
    citations: list[str]      # extracted from answer text
    total_tool_calls: int
    # Cost / usage telemetry from the SDK's ResultMessage. Defaults keep
    # synthetic AgentResponse fixtures in tests working unchanged.
    usage: dict[str, int] = field(default_factory=dict)
    num_turns: int = 0
    duration_ms: int = 0
    model_usage: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# In-process MCP tool definitions
# These wrap the existing tool implementations in tools.py / *_tool.py via
# dispatch_tool() so all NaN-sanitisation and validation logic is preserved.
# _config is set before each SDK call in _run_query_async().
# ---------------------------------------------------------------------------

# Per-task config reference — set via ContextVar in _run_query_async so that
# concurrent asyncio tasks (e.g. two Streamlit sessions) each see their own value.
_current_config: ContextVar[Config | None] = ContextVar("_current_config", default=None)


def _require_config() -> Config:
    """Return the per-task config; raise if _run_query_async hasn't set it."""
    cfg = _current_config.get()
    if cfg is None:
        raise RuntimeError(
            "_current_config is not set. _require_config() must only be called "
            "from within an active _run_query_async task."
        )
    return cfg


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
    token = _current_config.set(config)

    # Build in-process MCP server with our three tools
    mcp_server = create_sdk_mcp_server(
        name="agentic-rag-tools",
        version="1.0.0",
        tools=[_tool_graph_query, _tool_semantic_search, _tool_aggregate_measures],
    )

    # max_turns in the SDK counts every exchange (user + assistant), not just
    # tool calls. Allow 3 turns per tool call (model often interleaves a brief
    # reasoning turn between tool calls) plus 2 for the final answer.
    max_turns = config.agent_max_tool_calls * 3 + 2

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
    usage: dict[str, int] = {}
    num_turns = 0
    duration_ms = 0
    model_usage: dict[str, dict[str, Any]] = {}
    t0 = perf_counter()

    try:
        async for msg in sdk_query(prompt=user_query, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        total_tool_calls += 1
                        input_preview = json.dumps(block.input)[:200]
                        tool_calls_log.append({
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                            "result_preview": input_preview,
                            "result": None,
                            "t_start": perf_counter(),
                            "duration_ms": None,
                        })
                    elif isinstance(block, TextBlock):
                        answer_text = block.text

            elif isinstance(msg, UserMessage):
                for ublock in msg.content:
                    if isinstance(ublock, ToolResultBlock):
                        result_str = str(ublock.content)[:1000]
                        for tc in tool_calls_log:
                            if tc.get("id") == ublock.tool_use_id:
                                tc["result"] = result_str
                                t_start = tc.pop("t_start", None)
                                if t_start is not None:
                                    tc["duration_ms"] = int((perf_counter() - t_start) * 1000)
                                break

            elif isinstance(msg, ResultMessage):
                if msg.result:
                    answer_text = msg.result
                usage = dict(msg.usage) if msg.usage else {}
                num_turns = int(msg.num_turns or 0)
                duration_ms = int(msg.duration_ms or 0)
                model_usage = dict(msg.model_usage) if msg.model_usage else {}
    finally:
        _current_config.reset(token)

    # Fallback wall-clock if the SDK didn't surface duration_ms.
    if duration_ms == 0:
        duration_ms = int((perf_counter() - t0) * 1000)

    # Drop any lingering t_start keys before returning (tool result never arrived).
    for tc in tool_calls_log:
        tc.pop("t_start", None)

    response = AgentResponse(
        query=user_query,
        answer=answer_text,
        tool_calls=tool_calls_log,
        citations=extract_citations(answer_text),
        total_tool_calls=total_tool_calls,
        usage=usage,
        num_turns=num_turns,
        duration_ms=duration_ms,
        model_usage=model_usage,
    )

    # Best-effort cost log; never raise out of run_query for telemetry failure.
    try:
        from src.agent.cost_log import log_response
        log_response(response, model=config.claude_model)
    except OSError:
        pass

    return response


def _check_claude_code_available() -> None:
    """Friendly precondition check for the Claude Code OAuth session.

    The Claude Agent SDK invokes the `claude` CLI to use its OAuth session.
    If the CLI is missing or unauthenticated, the SDK raises deep into its
    own internals — this surfaces the problem early with an actionable line.
    """
    import shutil
    import sys

    if shutil.which("claude") is None:
        sys.stderr.write(
            "orchestrator: 'claude' CLI not found on PATH.\n"
            "  Install Claude Code from https://claude.com/code, then run\n"
            "  `claude /login` once to authenticate. The agent SDK uses\n"
            "  that OAuth session — no API key needed.\n"
        )
        sys.exit(2)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Agentic RAG query runner")
    parser.add_argument("--query", required=False, help="Single query to run")
    parser.add_argument("--interactive", action="store_true", help="Interactive REPL mode")
    parser.add_argument("--check-auth", action="store_true", help="Verify Claude Code is available, then exit")
    args = parser.parse_args()

    _check_claude_code_available()
    if args.check_auth:
        print("claude CLI present on PATH; SDK will use its OAuth session.")
        raise SystemExit(0)

    from src.config import get_config
    cfg = get_config()

    def _cost_line(resp: AgentResponse) -> str:
        u = resp.usage or {}
        in_tok = u.get("input_tokens", 0)
        out_tok = u.get("output_tokens", 0)
        cache_read = u.get("cache_read_input_tokens", 0)
        total = in_tok + out_tok
        parts = [
            f"{resp.total_tool_calls} tool calls",
            f"{in_tok:,} in / {out_tok:,} out tokens ({total:,} total)",
        ]
        if cache_read:
            parts.append(f"cache hit {cache_read:,}")
        parts.append(f"{resp.num_turns} turns")
        parts.append(f"{resp.duration_ms / 1000:.1f}s")
        # Per-model token breakdown when the SDK splits across models.
        if resp.model_usage:
            for model_id, agg in resp.model_usage.items():
                m_in = int(agg.get("input_tokens", 0) or 0)
                m_out = int(agg.get("output_tokens", 0) or 0)
                if m_in or m_out:
                    parts.append(f"{model_id}: {m_in:,} in / {m_out:,} out")
        return "[" + " | ".join(parts) + "]"

    if args.interactive:
        print("Agentic RAG — type 'quit' to exit")
        while True:
            q = input("\nQuery: ").strip()
            if q.lower() == "quit":
                break
            resp = run_query(q, cfg)
            print(f"\n{resp.answer}")
            print(f"\n{_cost_line(resp)}")
    elif args.query:
        resp = run_query(args.query, cfg)
        print(resp.answer)
        print(f"\n{_cost_line(resp)}")
    else:
        parser.print_help()
