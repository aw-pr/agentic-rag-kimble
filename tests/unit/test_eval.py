"""Unit tests for the eval harness (pass 04, extended in pass 20)."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.eval.fixtures import FIXTURES
from src.eval.judge import _parse_judge_response, score_response
from src.eval.metrics import recall_at_k, run_retrieval_eval

# ── recall_at_k ─────────────────────────────────────────────────────────────

def test_recall_at_k_hit():
    assert recall_at_k(["RandomForest", "XGBoost"], ["RandomForest"], k=5) == 1.0


def test_recall_at_k_miss():
    assert recall_at_k(["SVM", "KNN"], ["RandomForest"], k=5) == 0.0


def test_recall_at_k_beyond_k():
    # Expected item is at position 11 — should miss at k=10
    retrieved = [f"item_{i}" for i in range(15)]
    assert recall_at_k(retrieved, ["item_11"], k=10) == 0.0


def test_recall_at_k_exact_boundary():
    # Item at position 9 (0-indexed) is within k=10
    retrieved = [f"item_{i}" for i in range(15)]
    assert recall_at_k(retrieved, ["item_9"], k=10) == 1.0


def test_recall_at_k_multiple_expected_any_match():
    assert recall_at_k(["SVM", "KNN"], ["RandomForest", "KNN"], k=5) == 1.0


def test_recall_at_k_empty_retrieved():
    assert recall_at_k([], ["RandomForest"], k=5) == 0.0


# ── Fixtures ─────────────────────────────────────────────────────────────────

def test_fixtures_count():
    assert len(FIXTURES) == 20


def test_fixtures_have_valid_tool_hints():
    valid = {"graph", "semantic", "aggregate"}
    for f in FIXTURES:
        assert f.tool_hint in valid, f"Invalid tool_hint '{f.tool_hint}' in fixture: {f.query}"


def test_fixtures_have_valid_entity_types():
    valid = {"Algorithm", "Dataset", "Task"}
    for f in FIXTURES:
        assert f.expected_entity_type in valid


def test_fixtures_have_nonempty_expected_names():
    for f in FIXTURES:
        assert len(f.expected_entity_names) > 0, f"Empty expected_entity_names for: {f.query}"


def test_fixtures_tool_hint_distribution():
    from collections import Counter

    counts = Counter(f.tool_hint for f in FIXTURES)
    assert counts["semantic"] == 8
    assert counts["graph"] == 7
    assert counts["aggregate"] == 5


# ── Judge stub ───────────────────────────────────────────────────────────────

def test_judge_stub_returns_placeholder():
    score = score_response("test query", "context", "response", api_key=None)
    assert score.overall == 0.0
    assert "stub" in score.flags[0]


def test_judge_stub_all_scores_zero():
    score = score_response("q", "ctx", "resp", api_key=None)
    assert score.grounding == 0
    assert score.reasoning == 0
    assert score.completeness == 0


def test_judge_stub_verdict():
    score = score_response("q", "ctx", "resp", api_key=None)
    assert isinstance(score.verdict, str)
    assert len(score.verdict) > 0


# ── _parse_judge_response — three input shapes ───────────────────────────────

def test_parse_judge_json_tag():
    """Accept <json>{...}</json> wrapper."""
    raw = (
        '<json>{"grounding": 4, "reasoning": 3, "completeness": 5, '
        '"flags": [], "verdict": "Good."}</json>'
    )
    score = _parse_judge_response(raw)
    assert score.grounding == 4
    assert score.reasoning == 3
    assert score.completeness == 5
    assert score.overall == round((4 + 3 + 5) / 3, 1)
    assert score.verdict == "Good."


def test_parse_judge_fenced_json():
    """Accept ```json\\n{...}\\n``` fence."""
    raw = (
        '```json\n{"grounding": 5, "reasoning": 5, "completeness": 4, '
        '"flags": ["minor gap"], "verdict": "Solid."}\n```'
    )
    score = _parse_judge_response(raw)
    assert score.grounding == 5
    assert score.reasoning == 5
    assert score.completeness == 4
    assert score.flags == ["minor gap"]


def test_parse_judge_raw_json():
    """Accept raw {...} when Claude skips the wrapper."""
    raw = '{"grounding": 3, "reasoning": 3, "completeness": 3, "flags": [], "verdict": "Adequate."}'
    score = _parse_judge_response(raw)
    assert score.grounding == 3
    assert score.verdict == "Adequate."


def test_parse_judge_failure_returns_degraded_score():
    """Parse failure must produce a degraded JudgeScore, not raise."""
    raw = "This is not JSON at all."
    score = _parse_judge_response(raw)
    assert score.grounding == 0
    assert score.reasoning == 0
    assert score.completeness == 0
    assert score.overall == 0.0
    assert "parse failure" in score.flags
    # verdict contains the start of the raw text
    assert "not JSON" in score.verdict


def test_parse_judge_empty_response_returns_degraded_score():
    """Empty response must produce a degraded JudgeScore."""
    score = _parse_judge_response("")
    assert "parse failure" in score.flags


# ── _build_judge_context includes tool results (pass-27) ─────────────────────

def test_build_judge_context_includes_tool_result():
    """When tool_calls[*].result is set, _build_judge_context includes it."""
    from src.agent.orchestrator import AgentResponse
    from src.eval.metrics import _build_judge_context

    agent_resp = AgentResponse(
        query="Which families?",
        answer="Tree ensembles are best.",
        tool_calls=[{
            "id": "tu_1",
            "name": "aggregate_measures",
            "input": {"group_by": "algorithm.family", "measure": "accuracy"},
            "result_preview": '{"group_by": "algorithm.family"}',
            "result": '[{"family": "tree_ensemble", "mean_accuracy": 0.91}]',
        }],
        citations=[],
        total_tool_calls=1,
    )
    context = _build_judge_context(agent_resp, retrieved=[])
    assert "aggregate_measures" in context
    assert "tree_ensemble" in context
    assert "0.91" in context


def test_build_judge_context_no_result_omits_arrow():
    """When result is None, the arrow line is not added."""
    from src.agent.orchestrator import AgentResponse
    from src.eval.metrics import _build_judge_context

    agent_resp = AgentResponse(
        query="test",
        answer="answer",
        tool_calls=[{
            "id": "tu_1",
            "name": "graph_query",
            "input": {"cypher": "MATCH (n) RETURN n LIMIT 1", "explain": "test"},
            "result_preview": "",
            "result": None,
        }],
        citations=[],
        total_tool_calls=1,
    )
    context = _build_judge_context(agent_resp, retrieved=[])
    assert "→" not in context


# ── SDK mock: score_response with live config ─────────────────────────────────

def _make_async_gen(items):
    """Helper: wrap a list into an async generator for mocking sdk_query."""
    async def _gen():
        for item in items:
            yield item
    return _gen()


def test_score_response_with_sdk_mock():
    """score_response calls _score_async and parses a ResultMessage payload."""
    from src.config import Config

    valid_json_response = (
        '<json>{"grounding": 4, "reasoning": 4, "completeness": 3, '
        '"flags": [], "verdict": "Response well-grounded."}</json>'
    )

    cfg = Config()

    with patch("src.eval.judge._score_async", new=AsyncMock(return_value=valid_json_response)):
        score = score_response("test query", "some context", "some response", config=cfg)

    assert score.grounding == 4
    assert score.reasoning == 4
    assert score.completeness == 3
    assert score.overall == round((4 + 4 + 3) / 3, 1)
    assert score.verdict == "Response well-grounded."
    assert score.flags == []


def test_score_response_sdk_parse_error_does_not_raise():
    """SDK returns unparseable text → degraded JudgeScore, no exception."""
    from src.config import Config

    cfg = Config()

    with patch("src.eval.judge._score_async", new=AsyncMock(return_value="garbage output")):
        score = score_response("q", "ctx", "resp", config=cfg)

    assert score.grounding == 0
    assert "parse failure" in score.flags


# ── run_retrieval_eval against empty/missing DB ──────────────────────────────

def test_metrics_runs_without_db():
    """run_retrieval_eval must not crash when DB is empty or tools not yet available."""
    from src.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(
            kuzu_db_path=Path(tmp) / "kuzu",

        )
        result = run_retrieval_eval(cfg)
        assert "recall_at_10" in result


def test_metrics_empty_db_recall_is_zero():
    from src.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(
            kuzu_db_path=Path(tmp) / "kuzu",

        )
        result = run_retrieval_eval(cfg)
        assert result["recall_at_5"] == 0.0
        assert result["recall_at_10"] == 0.0


def test_metrics_empty_db_returns_expected_keys():
    from src.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(
            kuzu_db_path=Path(tmp) / "kuzu",

        )
        result = run_retrieval_eval(cfg)
        for key in (
            "recall_at_5", "recall_at_10", "per_tool", "failures", "n_fixtures", "db_populated"
        ):
            assert key in result, f"Missing key: {key}"


def test_metrics_empty_db_populated_false():
    from src.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(
            kuzu_db_path=Path(tmp) / "kuzu",

        )
        result = run_retrieval_eval(cfg)
        assert result["db_populated"] is False


def test_metrics_n_fixtures_correct():
    from src.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(
            kuzu_db_path=Path(tmp) / "kuzu",

        )
        result = run_retrieval_eval(cfg)
        assert result["n_fixtures"] == 20
