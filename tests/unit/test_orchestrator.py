"""
Unit tests for src/agent/orchestrator.py and the NaN sanitisation helper.

All Claude Agent SDK calls are mocked — no live API calls.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import pytest

from src.agent.orchestrator import (
    AgentResponse,
    extract_citations,
    run_query,
)
from src.agent.prompts import SYSTEM_PROMPT
from src.config import Config
from src.retrieval.tools import _sanitise_nan, dispatch_tool

# ── extract_citations ─────────────────────────────────────────────────────────

def test_extract_citations_finds_algorithm():
    text = "Based on [Algorithm: RandomForest] and [Dataset: iris]..."
    citations = extract_citations(text)
    assert "[Algorithm: RandomForest]" in citations


def test_extract_citations_finds_dataset():
    text = "See [Dataset: iris] for the benchmark."
    assert "[Dataset: iris]" in extract_citations(text)


def test_extract_citations_finds_task():
    text = "This [Task: Supervised Classification] result..."
    assert "[Task: Supervised Classification]" in extract_citations(text)


def test_extract_citations_finds_run_colon_form():
    """Run citations use colon form consistently — [Run: 123456]."""
    text = "[Run: 123456] achieved 0.94 accuracy"
    assert "[Run: 123456]" in extract_citations(text)


def test_extract_citations_multiple():
    text = "[Algorithm: SVM] on [Dataset: digits] via [Run: 99]"
    citations = extract_citations(text)
    assert len(citations) == 3


def test_extract_citations_empty_text():
    assert extract_citations("No citations here.") == []


def test_extract_citations_ignores_hash_form():
    """[Run #123456] (no colon) should NOT match — old format is retired."""
    assert extract_citations("[Run #123456] old format") == []


# ── run_query shape and tool-call tracking ────────────────────────────────────

def _make_async_iter(messages: list):
    """Helper: create an async iterator from a list of message objects."""
    async def _gen() -> AsyncIterator:
        for msg in messages:
            yield msg
    return _gen()


def test_run_query_returns_agent_response(mocker):
    """run_query returns a properly populated AgentResponse (no tool calls)."""
    from claude_agent_sdk import ResultMessage

    result_msg = ResultMessage(
        subtype="success",
        duration_ms=500,
        duration_api_ms=400,
        is_error=False,
        num_turns=1,
        session_id="test-session",
        stop_reason="end_turn",
        total_cost_usd=0.001,
        usage={},
        result="The answer is [Algorithm: RandomForest].",
        structured_output=None,
        model_usage=None,
        permission_denials=None,
        deferred_tool_use=None,
        errors=None,
        api_error_status=None,
        uuid=None,
    )

    mocker.patch(
        "src.agent.orchestrator.sdk_query",
        return_value=_make_async_iter([result_msg]),
    )

    cfg = Config()
    resp = run_query("Which algorithms are best?", cfg)

    assert isinstance(resp, AgentResponse)
    assert resp.query == "Which algorithms are best?"
    assert "RandomForest" in resp.answer
    assert resp.total_tool_calls == 0
    assert "[Algorithm: RandomForest]" in resp.citations


def test_run_query_captures_tool_calls(mocker):
    """Tool calls in AssistantMessage blocks are logged in tool_calls_log."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

    tool_block = ToolUseBlock(
        id="tu_abc",
        name="semantic_search",
        input={"query": "imbalanced datasets", "entity_type": "Dataset"},
    )
    assistant_msg = AssistantMessage(
        content=[tool_block],
        model="claude-haiku-4-5",
        parent_tool_use_id=None,
        error=None,
        usage=None,
        message_id=None,
        stop_reason="tool_use",
        session_id="test-session",
        uuid=None,
    )
    result_msg = ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=2,
        session_id="test-session",
        stop_reason="end_turn",
        total_cost_usd=0.002,
        usage={},
        result="[Dataset: credit-g] shows class imbalance.",
        structured_output=None,
        model_usage=None,
        permission_denials=None,
        deferred_tool_use=None,
        errors=None,
        api_error_status=None,
        uuid=None,
    )

    mocker.patch(
        "src.agent.orchestrator.sdk_query",
        return_value=_make_async_iter([assistant_msg, result_msg]),
    )

    cfg = Config()
    resp = run_query("Find imbalanced datasets", cfg)

    assert resp.total_tool_calls == 1
    assert resp.tool_calls[0]["name"] == "semantic_search"
    assert "[Dataset: credit-g]" in resp.citations


def test_run_query_accumulates_multiple_tool_calls(mocker):
    """Multiple ToolUseBlocks across assistant messages are all counted."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

    block1 = ToolUseBlock(
        id="tu_1",
        name="graph_query",
        input={"cypher": "MATCH (n) RETURN n LIMIT 1", "explain": "test"},
    )
    block2 = ToolUseBlock(
        id="tu_2",
        name="aggregate_measures",
        input={"group_by": "algorithm.family", "measure": "accuracy"},
    )

    assistant_msg1 = AssistantMessage(
        content=[block1],
        model="claude-haiku-4-5",
        parent_tool_use_id=None,
        error=None, usage=None, message_id=None,
        stop_reason="tool_use", session_id="s", uuid=None,
    )
    assistant_msg2 = AssistantMessage(
        content=[block2],
        model="claude-haiku-4-5",
        parent_tool_use_id=None,
        error=None, usage=None, message_id=None,
        stop_reason="tool_use", session_id="s", uuid=None,
    )
    result_msg = ResultMessage(
        subtype="success", duration_ms=0, duration_api_ms=0,
        is_error=False, num_turns=3, session_id="s",
        stop_reason="end_turn", total_cost_usd=0.003, usage={},
        result="Two tools were called.", structured_output=None,
        model_usage=None, permission_denials=None, deferred_tool_use=None,
        errors=None, api_error_status=None, uuid=None,
    )

    mocker.patch(
        "src.agent.orchestrator.sdk_query",
        return_value=_make_async_iter([assistant_msg1, assistant_msg2, result_msg]),
    )

    cfg = Config()
    resp = run_query("Run two tools", cfg)

    assert resp.total_tool_calls == 2
    assert len(resp.tool_calls) == 2


def test_run_query_answer_from_result_message(mocker):
    """When ResultMessage.result is set, it becomes resp.answer."""
    from claude_agent_sdk import ResultMessage

    result_msg = ResultMessage(
        subtype="success", duration_ms=100, duration_api_ms=90,
        is_error=False, num_turns=1, session_id="s",
        stop_reason="end_turn", total_cost_usd=0.001, usage={},
        result="Final answer text here.",
        structured_output=None, model_usage=None,
        permission_denials=None, deferred_tool_use=None,
        errors=None, api_error_status=None, uuid=None,
    )

    mocker.patch(
        "src.agent.orchestrator.sdk_query",
        return_value=_make_async_iter([result_msg]),
    )

    cfg = Config()
    resp = run_query("test query", cfg)
    assert resp.answer == "Final answer text here."


# ── ToolResultBlock correlation (pass-27) ────────────────────────────────────

def test_run_query_tool_result_correlated_by_id(mocker):
    """ToolResultBlocks in UserMessage are matched to ToolUseBlocks by id."""
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    tool_block = ToolUseBlock(
        id="tu_xyz",
        name="aggregate_measures",
        input={"group_by": "algorithm.family", "measure": "accuracy", "filter_cypher": ""},
    )
    assistant_msg = AssistantMessage(
        content=[tool_block],
        model="claude-haiku-4-5",
        parent_tool_use_id=None,
        error=None, usage=None, message_id=None,
        stop_reason="tool_use", session_id="s", uuid=None,
    )
    result_block = ToolResultBlock(
        tool_use_id="tu_xyz",
        content='[{"family": "tree_ensemble", "mean_accuracy": 0.91}]',
        is_error=False,
    )
    user_msg = UserMessage(
        content=[result_block],
        uuid=None,
        parent_tool_use_id=None,
        tool_use_result=None,
    )
    final_msg = ResultMessage(
        subtype="success", duration_ms=0, duration_api_ms=0,
        is_error=False, num_turns=2, session_id="s",
        stop_reason="end_turn", total_cost_usd=0.001, usage={},
        result="Tree ensembles top the list.",
        structured_output=None, model_usage=None,
        permission_denials=None, deferred_tool_use=None,
        errors=None, api_error_status=None, uuid=None,
    )

    mocker.patch(
        "src.agent.orchestrator.sdk_query",
        return_value=_make_async_iter([assistant_msg, user_msg, final_msg]),
    )

    cfg = Config()
    resp = run_query("Which families are best?", cfg)

    assert resp.total_tool_calls == 1
    tc = resp.tool_calls[0]
    assert tc["name"] == "aggregate_measures"
    assert tc["result"] is not None
    assert "tree_ensemble" in tc["result"]


def test_run_query_unmatched_tool_result_is_ignored(mocker):
    """A ToolResultBlock with an unknown id does not raise and leaves result=None."""
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    tool_block = ToolUseBlock(
        id="tu_known",
        name="graph_query",
        input={"cypher": "MATCH (a:Algorithm) RETURN a LIMIT 1", "explain": "test"},
    )
    assistant_msg = AssistantMessage(
        content=[tool_block],
        model="claude-haiku-4-5",
        parent_tool_use_id=None,
        error=None, usage=None, message_id=None,
        stop_reason="tool_use", session_id="s", uuid=None,
    )
    orphan_block = ToolResultBlock(
        tool_use_id="tu_unknown",
        content="orphan result",
        is_error=False,
    )
    user_msg = UserMessage(
        content=[orphan_block],
        uuid=None,
        parent_tool_use_id=None,
        tool_use_result=None,
    )
    final_msg = ResultMessage(
        subtype="success", duration_ms=0, duration_api_ms=0,
        is_error=False, num_turns=2, session_id="s",
        stop_reason="end_turn", total_cost_usd=0.001, usage={},
        result="Done.", structured_output=None, model_usage=None,
        permission_denials=None, deferred_tool_use=None,
        errors=None, api_error_status=None, uuid=None,
    )

    mocker.patch(
        "src.agent.orchestrator.sdk_query",
        return_value=_make_async_iter([assistant_msg, user_msg, final_msg]),
    )

    cfg = Config()
    resp = run_query("test", cfg)

    # The known tool call should have result=None (no matching result block)
    assert resp.tool_calls[0]["result"] is None


# ── NaN sanitisation (tools.py helper) ───────────────────────────────────────

def test_sanitise_nan_replaces_nan():
    assert _sanitise_nan(float("nan")) is None


def test_sanitise_nan_replaces_inf():
    assert _sanitise_nan(float("inf")) is None


def test_sanitise_nan_replaces_neg_inf():
    assert _sanitise_nan(float("-inf")) is None


def test_sanitise_nan_preserves_valid_float():
    assert _sanitise_nan(0.95) == pytest.approx(0.95)


def test_sanitise_nan_preserves_zero():
    assert _sanitise_nan(0.0) == 0.0


def test_sanitise_nan_nested_dict():
    data = {"accuracy": float("nan"), "name": "RF", "f1": 0.91}
    result = _sanitise_nan(data)
    assert result["accuracy"] is None
    assert result["name"] == "RF"
    assert result["f1"] == pytest.approx(0.91)


def test_sanitise_nan_nested_list():
    data = [{"score": float("nan")}, {"score": 0.88}]
    result = _sanitise_nan(data)
    assert result[0]["score"] is None
    assert result[1]["score"] == pytest.approx(0.88)


def test_sanitise_nan_passthrough_string():
    assert _sanitise_nan("hello") == "hello"


def test_sanitise_nan_passthrough_none():
    assert _sanitise_nan(None) is None


def test_dispatch_tool_nan_produces_valid_json(mocker):
    """dispatch_tool must produce strict-JSON-valid output even with NaN in results."""
    mocker.patch(
        "src.retrieval.tools.aggregate_measures",
        return_value=[
            {"family": "tree_ensemble", "mean_accuracy": float("nan"), "count": 42}
        ],
    )
    result = dispatch_tool(
        "aggregate_measures",
        {"group_by": "algorithm.family", "measure": "accuracy"},
        config=None,
    )
    # Must parse without error
    parsed = json.loads(result)
    assert parsed[0]["mean_accuracy"] is None
    assert parsed[0]["count"] == 42


def test_dispatch_tool_inf_produces_valid_json(mocker):
    """±Inf values in tool results must become null in JSON output."""
    mocker.patch(
        "src.retrieval.tools.graph_query",
        return_value=[{"runtime_sec": float("inf"), "name": "SVM"}],
    )
    result = dispatch_tool(
        "graph_query",
        {"cypher": "MATCH (n) RETURN n", "explain": "test"},
        config=None,
    )
    parsed = json.loads(result)
    assert parsed[0]["runtime_sec"] is None


# ── SYSTEM_PROMPT consistency ─────────────────────────────────────────────────

def test_system_prompt_uses_colon_citation_form():
    """SYSTEM_PROMPT must use [Type: value] colon form, not [Run #X] hash form."""
    assert "[Run:" in SYSTEM_PROMPT
    assert "[Run #" not in SYSTEM_PROMPT


def test_system_prompt_mentions_three_tools():
    for tool_name in ("graph_query", "semantic_search", "aggregate_measures"):
        assert tool_name in SYSTEM_PROMPT


def test_system_prompt_mentions_max_tool_calls():
    assert "5 tool calls" in SYSTEM_PROMPT
