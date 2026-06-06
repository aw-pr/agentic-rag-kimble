"""Unit tests for the three agent tools (pass 06)."""

from __future__ import annotations

import pytest

from src.retrieval.aggregate_tool import aggregate_measures
from src.retrieval.graph_tool import validate_cypher
from src.retrieval.semantic_tool import semantic_search
from src.retrieval.tools import dispatch_tool

# ── validate_cypher ──────────────────────────────────────────────────────────

def test_validate_cypher_allows_match():
    validate_cypher("MATCH (n:Algorithm) RETURN n.name LIMIT 10")  # must not raise


def test_validate_cypher_allows_match_with_where():
    validate_cypher("MATCH (n:Algorithm) WHERE n.family = 'tree_ensemble' RETURN n.name")


def test_validate_cypher_allows_starts_with_set_in_value():
    # Property value contains the word "set" — should NOT trip the guard.
    validate_cypher("MATCH (n:Algorithm) WHERE n.name STARTS WITH 'setosa' RETURN n.name")


def test_validate_cypher_blocks_create():
    with pytest.raises(PermissionError):
        validate_cypher("CREATE (n:Algorithm {name: 'evil'})")


def test_validate_cypher_blocks_delete():
    with pytest.raises(PermissionError):
        validate_cypher("MATCH (n) DELETE n")


def test_validate_cypher_blocks_merge():
    with pytest.raises(PermissionError):
        validate_cypher("MERGE (n:Algorithm {name: 'x'})")


def test_validate_cypher_blocks_set():
    with pytest.raises(PermissionError):
        validate_cypher("MATCH (n:Algorithm) SET n.name = 'x'")


def test_validate_cypher_blocks_drop():
    with pytest.raises(PermissionError):
        validate_cypher("DROP TABLE Algorithm")


def test_validate_cypher_blocks_create_case_insensitive():
    with pytest.raises(PermissionError):
        validate_cypher("create (n:Algorithm {name: 'evil'})")


# ── aggregate_measures validation ────────────────────────────────────────────

def test_aggregate_rejects_invalid_group_by():
    with pytest.raises(ValueError, match="group_by"):
        aggregate_measures("invalid.field", "accuracy")


def test_aggregate_rejects_invalid_measure():
    with pytest.raises(ValueError, match="measure"):
        aggregate_measures("algorithm.family", "not_a_metric")


# ── new group_by axes in VALID_GROUP_BY ──────────────────────────────────────

def test_new_algorithm_axes_in_valid_group_by():
    from src.retrieval.aggregate_tool import VALID_GROUP_BY
    assert "algorithm.paradigm" in VALID_GROUP_BY
    assert "algorithm.training_cost_class" in VALID_GROUP_BY


def test_new_dataset_axes_in_valid_group_by():
    from src.retrieval.aggregate_tool import VALID_GROUP_BY
    assert "dataset.size_bucket" in VALID_GROUP_BY
    assert "dataset.dim_bucket" in VALID_GROUP_BY
    assert "dataset.imbalance_bucket" in VALID_GROUP_BY


def test_is_ensemble_not_a_group_by_axis():
    """is_ensemble is BOOL — intentionally excluded from group_by."""
    from src.retrieval.aggregate_tool import VALID_GROUP_BY
    assert "algorithm.is_ensemble" not in VALID_GROUP_BY


def test_new_axes_resolve_to_correct_column_expressions():
    """_build_cypher must not raise for each new axis and must reference the right column."""
    from src.retrieval.aggregate_tool import _build_cypher
    checks = {
        "algorithm.paradigm":             "a.paradigm",
        "algorithm.training_cost_class":  "a.training_cost_class",
        "dataset.size_bucket":            "d.size_bucket",
        "dataset.dim_bucket":             "d.dim_bucket",
        "dataset.imbalance_bucket":       "d.imbalance_bucket",
    }
    for group_by, expected_col in checks.items():
        cypher = _build_cypher(group_by, "accuracy", "")
        assert expected_col in cypher, (
            f"Expected '{expected_col}' in generated Cypher for group_by={group_by!r}. "
            f"Got: {cypher!r}"
        )


def test_aggregate_accepts_new_algorithm_paradigm():
    import tempfile
    from pathlib import Path

    from src.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(ladybug_db_path=Path(tmp) / "ladybug")
        try:
            result = aggregate_measures("algorithm.paradigm", "accuracy", config=cfg)
            assert isinstance(result, list)
        except Exception as exc:
            assert "ValueError" not in type(exc).__name__
            assert "PermissionError" not in type(exc).__name__


def test_aggregate_accepts_dataset_size_bucket():
    import tempfile
    from pathlib import Path

    from src.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(ladybug_db_path=Path(tmp) / "ladybug")
        try:
            result = aggregate_measures("dataset.size_bucket", "f1", config=cfg)
            assert isinstance(result, list)
        except Exception as exc:
            assert "ValueError" not in type(exc).__name__
            assert "PermissionError" not in type(exc).__name__


def test_aggregate_accepts_valid_group_by_and_measure():
    # Validation only — no DB needed; error raised by GraphDB, not by validation
    # (passes through validation without ValueError/PermissionError)
    import tempfile
    from pathlib import Path

    from src.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(ladybug_db_path=Path(tmp) / "ladybug")
        # With an empty (but initialised) DB, aggregate_measures should return []
        # rather than raising a validation error.
        try:
            result = aggregate_measures("algorithm.family", "accuracy", config=cfg)
            assert isinstance(result, list)
        except Exception as exc:
            # A DB-level error is acceptable here — we only care that validation passed.
            assert "ValueError" not in type(exc).__name__
            assert "PermissionError" not in type(exc).__name__


# ── semantic_search validation ───────────────────────────────────────────────

def test_semantic_search_rejects_invalid_entity_type():
    with pytest.raises(ValueError, match="entity_type"):
        semantic_search("query", "NotAnEntity")


def test_semantic_search_rejects_top_k_too_large():
    with pytest.raises(ValueError, match="top_k"):
        semantic_search("query", "Algorithm", top_k=100)


def test_semantic_search_rejects_top_k_zero():
    with pytest.raises(ValueError, match="top_k"):
        semantic_search("query", "Algorithm", top_k=0)


def test_semantic_search_rejects_top_k_negative():
    with pytest.raises(ValueError, match="top_k"):
        semantic_search("query", "Algorithm", top_k=-5)


# ── dispatch_tool ─────────────────────────────────────────────────────────────

def test_dispatch_tool_routes_graph_query(mocker):
    mocker.patch("src.retrieval.tools.graph_query", return_value=[{"name": "RF"}])
    result = dispatch_tool(
        "graph_query",
        {"cypher": "MATCH (n) RETURN n", "explain": "test"},
        config=None,
    )
    assert "RF" in result


def test_dispatch_tool_routes_semantic_search(mocker):
    mocker.patch(
        "src.retrieval.tools.semantic_search",
        return_value=[{"name": "RandomForest", "score": 0.95}],
    )
    result = dispatch_tool(
        "semantic_search",
        {"query": "tree ensemble", "entity_type": "Algorithm"},
        config=None,
    )
    assert "RandomForest" in result


def test_dispatch_tool_routes_aggregate_measures(mocker):
    mocker.patch(
        "src.retrieval.tools.aggregate_measures",
        return_value=[{"family": "tree_ensemble", "mean": 0.91, "count": 200}],
    )
    result = dispatch_tool(
        "aggregate_measures",
        {"group_by": "algorithm.family", "measure": "accuracy"},
        config=None,
    )
    assert "tree_ensemble" in result


def test_dispatch_tool_raises_on_unknown():
    with pytest.raises(ValueError, match="Unknown tool"):
        dispatch_tool("nonexistent_tool", {}, config=None)


def test_dispatch_tool_returns_json_string(mocker):
    mocker.patch("src.retrieval.tools.graph_query", return_value=[{"name": "SVM"}])
    result = dispatch_tool(
        "graph_query",
        {"cypher": "MATCH (n) RETURN n", "explain": "test"},
        config=None,
    )
    import json
    parsed = json.loads(result)
    assert isinstance(parsed, list)
    assert parsed[0]["name"] == "SVM"


# ── Tool schemas ──────────────────────────────────────────────────────────────

def test_get_tool_schemas_returns_three():
    from src.retrieval.tools import get_tool_schemas
    schemas = get_tool_schemas()
    assert len(schemas) == 3


def test_tool_schemas_have_required_keys():
    from src.retrieval.tools import get_tool_schemas
    for schema in get_tool_schemas():
        assert "name" in schema
        assert "description" in schema
        assert "input_schema" in schema


def test_tool_schema_names():
    from src.retrieval.tools import get_tool_schemas
    names = {s["name"] for s in get_tool_schemas()}
    assert names == {"graph_query", "semantic_search", "aggregate_measures"}


# ── Date dimension group_by axes ──────────────────────────────────────────────

def test_date_axes_in_valid_group_by():
    from src.retrieval.aggregate_tool import VALID_GROUP_BY
    assert "date.year" in VALID_GROUP_BY
    assert "date.quarter" in VALID_GROUP_BY
    assert "date.month" in VALID_GROUP_BY


def test_date_axes_produce_correct_cypher():
    """_build_cypher must include the Date join and correct column references."""
    from src.retrieval.aggregate_tool import _build_cypher

    checks = {
        "date.year":    ("dt.year",    "RUN_ON_DATE"),
        "date.quarter": ("dt.quarter", "RUN_ON_DATE"),
        "date.month":   ("dt.month",   "RUN_ON_DATE"),
    }
    for group_by, (expected_col, expected_rel) in checks.items():
        cypher = _build_cypher(group_by, "accuracy", "")
        assert expected_col in cypher, (
            f"Expected '{expected_col}' in Cypher for group_by={group_by!r}. Got: {cypher!r}"
        )
        assert expected_rel in cypher, (
            f"Expected '{expected_rel}' in Cypher for group_by={group_by!r}. Got: {cypher!r}"
        )


def test_date_axes_do_not_inject_date_join_for_non_date_axes():
    """Non-date group_by axes must not include the Date join."""
    from src.retrieval.aggregate_tool import _build_cypher
    cypher = _build_cypher("algorithm.family", "accuracy", "")
    assert "RUN_ON_DATE" not in cypher
    assert "dt:" not in cypher


def test_date_year_axis_accepted_by_aggregate_measures():
    import tempfile
    from pathlib import Path

    from src.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(ladybug_db_path=Path(tmp) / "ladybug")
        try:
            result = aggregate_measures("date.year", "accuracy", config=cfg)
            assert isinstance(result, list)
        except Exception as exc:
            assert "ValueError" not in type(exc).__name__
            assert "PermissionError" not in type(exc).__name__
