"""
tests/eval/test_fixtures_coverage.py

Static checks on the FIXTURES list — no DB required.
"""

from __future__ import annotations

from src.eval.fixtures import FIXTURES, EvalFixture


def test_fixture_count() -> None:
    assert len(FIXTURES) == 20, f"Expected 20 fixtures, got {len(FIXTURES)}"


def test_all_fixtures_are_eval_fixture_instances() -> None:
    for f in FIXTURES:
        assert isinstance(f, EvalFixture), f"Not an EvalFixture: {f!r}"


def test_tool_hints_are_valid() -> None:
    valid = {"graph", "semantic", "aggregate"}
    for f in FIXTURES:
        assert f.tool_hint in valid, f"Invalid tool_hint '{f.tool_hint}' in fixture: {f.query!r}"


def test_entity_types_are_valid() -> None:
    valid = {"Algorithm", "Dataset", "Task"}
    for f in FIXTURES:
        assert f.expected_entity_type in valid, (
            f"Invalid entity type '{f.expected_entity_type}' in fixture: {f.query!r}"
        )


def test_expected_names_non_empty() -> None:
    for f in FIXTURES:
        assert len(f.expected_entity_names) >= 1, (
            f"Fixture has no expected names: {f.query!r}"
        )


def test_queries_non_empty() -> None:
    for f in FIXTURES:
        assert f.query.strip(), "Fixture has empty query"


def test_tool_hint_distribution() -> None:
    """Ensure all three tool types are represented."""
    hints = {f.tool_hint for f in FIXTURES}
    assert "graph" in hints
    assert "semantic" in hints
    assert "aggregate" in hints


def test_aggregate_fixtures_use_family_names() -> None:
    """Aggregate fixtures must include at least one known family name in expected_entity_names."""
    known_families = {
        "tree_ensemble", "gradient_boosting", "neural",
        "linear", "svm", "knn", "other",
    }
    for f in FIXTURES:
        if f.tool_hint == "aggregate":
            overlap = set(f.expected_entity_names) & known_families
            assert overlap, (
                f"Aggregate fixture has no family names in expected_entity_names: {f.query!r}"
            )
