"""
Unit tests for the AlgorithmFamily dimension (Stage 3).

Covers:
- src/ingestion/family_metadata.py  (FAMILY_METADATA, get_family_row)
- src/ingestion/transform.py        (transform_algorithm_family)
- src/graph/schema.py               (AlgorithmFamily table, BELONGS_TO_FAMILY rel)
- src/retrieval/aggregate_tool.py   (algorithm_family.* group_by axes, Cypher generation)
"""

from __future__ import annotations

import pytest

from src.graph.schema import NODE_TABLES, REL_TABLES, VECTOR_INDEXES
from src.ingestion.family_metadata import FAMILY_METADATA, _family_id, get_family_row
from src.ingestion.transform import transform_algorithm_family
from src.retrieval.aggregate_tool import VALID_GROUP_BY, _build_cypher

# ── FAMILY_METADATA shape ──────────────────────────────────────────────────────

EXPECTED_SLUGS = {
    "tree_ensemble", "decision_tree", "gradient_boosting",
    "linear", "svm", "knn", "neural", "bayes", "other",
}

REQUIRED_KEYS = {"display_name", "paradigm", "interpretability", "typical_use_case", "description"}
VALID_INTERPRETABILITY = {"high", "medium", "low"}


def test_family_metadata_has_all_expected_slugs():
    assert EXPECTED_SLUGS == set(FAMILY_METADATA.keys()), (
        f"Unexpected difference: {EXPECTED_SLUGS.symmetric_difference(FAMILY_METADATA.keys())}"
    )


@pytest.mark.parametrize("slug", list(EXPECTED_SLUGS))
def test_family_metadata_entry_has_required_keys(slug):
    entry = FAMILY_METADATA[slug]
    missing = REQUIRED_KEYS - entry.keys()
    assert not missing, f"FAMILY_METADATA['{slug}'] is missing keys: {missing}"


@pytest.mark.parametrize("slug", list(EXPECTED_SLUGS))
def test_family_metadata_interpretability_is_valid(slug):
    interp = FAMILY_METADATA[slug]["interpretability"]
    assert interp in VALID_INTERPRETABILITY, (
        f"FAMILY_METADATA['{slug}'].interpretability = '{interp}', "
        f"expected one of {VALID_INTERPRETABILITY}"
    )


@pytest.mark.parametrize("slug", list(EXPECTED_SLUGS))
def test_family_metadata_description_is_non_empty(slug):
    desc = FAMILY_METADATA[slug]["description"]
    assert isinstance(desc, str) and len(desc) > 20, (
        f"FAMILY_METADATA['{slug}'].description is empty or too short"
    )


@pytest.mark.parametrize("slug", list(EXPECTED_SLUGS))
def test_family_metadata_typical_use_case_is_non_empty(slug):
    use_case = FAMILY_METADATA[slug]["typical_use_case"]
    assert isinstance(use_case, str) and len(use_case) > 10, (
        f"FAMILY_METADATA['{slug}'].typical_use_case is empty or too short"
    )


# ── _family_id stability ───────────────────────────────────────────────────────

def test_family_id_is_positive():
    for slug in EXPECTED_SLUGS:
        assert _family_id(slug) > 0, f"_family_id('{slug}') is not positive"


def test_family_id_is_stable():
    """Same slug must always return the same id."""
    for slug in EXPECTED_SLUGS:
        assert _family_id(slug) == _family_id(slug)


def test_family_id_is_unique():
    ids = [_family_id(slug) for slug in EXPECTED_SLUGS]
    assert len(ids) == len(set(ids)), "family_id collision detected across slugs"


def test_family_id_fits_signed_int64():
    max_int64 = (2 ** 63) - 1
    for slug in EXPECTED_SLUGS:
        fid = _family_id(slug)
        assert 0 < fid <= max_int64, f"_family_id('{slug}') = {fid} is out of INT64 range"


# ── get_family_row ─────────────────────────────────────────────────────────────

EXPECTED_ROW_KEYS = {"family_id", "family_name", "display_name", "paradigm",
                     "interpretability", "typical_use_case", "description"}


@pytest.mark.parametrize("slug", list(EXPECTED_SLUGS))
def test_get_family_row_returns_expected_keys(slug):
    row = get_family_row(slug)
    assert EXPECTED_ROW_KEYS == row.keys(), (
        f"get_family_row('{slug}') has unexpected keys: {row.keys() ^ EXPECTED_ROW_KEYS}"
    )


@pytest.mark.parametrize("slug", list(EXPECTED_SLUGS))
def test_get_family_row_family_name_matches_slug(slug):
    row = get_family_row(slug)
    assert row["family_name"] == slug


@pytest.mark.parametrize("slug", list(EXPECTED_SLUGS))
def test_get_family_row_family_id_is_deterministic(slug):
    row1 = get_family_row(slug)
    row2 = get_family_row(slug)
    assert row1["family_id"] == row2["family_id"]


def test_get_family_row_unknown_slug_falls_back_to_other_meta():
    row = get_family_row("completely_unknown_xyz")
    assert row["family_name"] == "completely_unknown_xyz"
    # Should use 'other' metadata for all other fields
    other_meta = FAMILY_METADATA["other"]
    assert row["display_name"] == other_meta["display_name"]
    assert row["paradigm"] == other_meta["paradigm"]
    assert row["interpretability"] == other_meta["interpretability"]


def test_get_family_row_unknown_slug_has_distinct_id_from_other():
    unknown_row = get_family_row("completely_unknown_xyz")
    other_row = get_family_row("other")
    # Both are valid unique IDs derived from their respective slugs
    assert unknown_row["family_id"] != other_row["family_id"]


def test_get_family_row_does_not_include_embedding():
    """description_embedding must NOT be present — computed downstream."""
    for slug in EXPECTED_SLUGS:
        row = get_family_row(slug)
        assert "description_embedding" not in row, (
            f"get_family_row('{slug}') unexpectedly includes description_embedding"
        )


# ── transform_algorithm_family ────────────────────────────────────────────────

@pytest.mark.parametrize("slug", list(EXPECTED_SLUGS))
def test_transform_algorithm_family_known_slug(slug):
    row = transform_algorithm_family(slug)
    assert row["family_name"] == slug
    assert "family_id" in row
    assert "description" in row


def test_transform_algorithm_family_unknown_slug_fallback():
    row = transform_algorithm_family("not_a_real_family")
    assert row["family_name"] == "not_a_real_family"
    # Should still have all required fields from 'other' metadata
    assert "display_name" in row
    assert "paradigm" in row
    assert "description" in row


def test_transform_algorithm_family_no_embedding_column():
    row = transform_algorithm_family("linear")
    assert "description_embedding" not in row


# ── schema: AlgorithmFamily node table ────────────────────────────────────────

def test_schema_has_algorithm_family_table():
    assert "AlgorithmFamily" in NODE_TABLES, "AlgorithmFamily missing from NODE_TABLES"


def test_algorithm_family_table_has_required_columns():
    cols = {name for name, _ in NODE_TABLES["AlgorithmFamily"]}
    required = {
        "family_id", "family_name", "display_name", "paradigm",
        "interpretability", "typical_use_case", "description", "description_embedding",
    }
    missing = required - cols
    assert not missing, f"AlgorithmFamily is missing columns: {missing}"


def test_algorithm_family_pk_is_int64():
    first_col_name, first_col_type = NODE_TABLES["AlgorithmFamily"][0]
    assert first_col_name == "family_id"
    assert first_col_type == "INT64"


def test_schema_has_belongs_to_family_rel():
    rel_names = {r[0] for r in REL_TABLES}
    assert "BELONGS_TO_FAMILY" in rel_names, "BELONGS_TO_FAMILY missing from REL_TABLES"


def test_belongs_to_family_rel_direction():
    rel = {r[0]: (r[1], r[2]) for r in REL_TABLES}
    assert rel["BELONGS_TO_FAMILY"] == ("Algorithm", "AlgorithmFamily"), (
        f"BELONGS_TO_FAMILY direction wrong: {rel['BELONGS_TO_FAMILY']}"
    )


def test_vector_indexes_include_algorithm_family():
    indexed = {(t, c) for t, _, c, _ in VECTOR_INDEXES}
    assert ("AlgorithmFamily", "description_embedding") in indexed, (
        "No vector index registered for AlgorithmFamily.description_embedding"
    )


# ── aggregate_tool: algorithm_family.* axes ───────────────────────────────────

def test_valid_group_by_includes_algorithm_family_paradigm():
    assert "algorithm_family.paradigm" in VALID_GROUP_BY


def test_valid_group_by_includes_algorithm_family_interpretability():
    assert "algorithm_family.interpretability" in VALID_GROUP_BY


def test_build_cypher_algorithm_family_paradigm_joins_belongs_to_family():
    cypher = _build_cypher("algorithm_family.paradigm", "accuracy", "")
    assert "BELONGS_TO_FAMILY" in cypher, (
        "Cypher for algorithm_family.paradigm should join through BELONGS_TO_FAMILY"
    )
    assert "af.paradigm" in cypher, "Cypher should reference af.paradigm"


def test_build_cypher_algorithm_family_interpretability_joins_belongs_to_family():
    cypher = _build_cypher("algorithm_family.interpretability", "f1", "")
    assert "BELONGS_TO_FAMILY" in cypher
    assert "af.interpretability" in cypher


def test_build_cypher_algorithm_family_paradigm_returns_correct_alias():
    cypher = _build_cypher("algorithm_family.paradigm", "accuracy", "")
    assert "AS paradigm" in cypher


def test_build_cypher_algorithm_family_interpretability_returns_correct_alias():
    cypher = _build_cypher("algorithm_family.interpretability", "auc", "")
    assert "AS interpretability" in cypher


def test_build_cypher_algorithm_family_includes_filter():
    cypher = _build_cypher("algorithm_family.paradigm", "accuracy", "af.interpretability = 'high'")
    assert "af.interpretability = 'high'" in cypher
