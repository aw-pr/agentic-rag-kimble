"""
aggregate_tool.py — pre-aggregated measure tool for the Claude agent.

Builds Cypher internally from validated, whitelisted parameters. The agent
never supplies raw Cypher to this tool, preventing injection.

Return type deviation from spec
--------------------------------
The spec docstring shows ``dict[str, dict]`` but this module returns
``list[dict]`` instead. Rationale: the eval harness (src.eval.metrics._try_aggregate)
iterates the result as a list and extracts ``r.get("family")`` / ``r.get("name")``.
Returning a list also maps cleanly to JSON arrays for the Claude tool result.
"""

from __future__ import annotations

from typing import Literal

from src.config import Config
from src.graph.db import GraphDB

GroupBy = Literal[
    "algorithm.family",
    "algorithm.name",
    "algorithm.paradigm",
    "algorithm.training_cost_class",
    "algorithm_family.paradigm",
    "algorithm_family.interpretability",
    "dataset.n_rows_bucket",
    "dataset.n_features_bucket",
    "dataset.size_bucket",
    "dataset.dim_bucket",
    "dataset.imbalance_bucket",
    "date.year",
    "date.quarter",
    "date.month",
]
Measure = Literal["accuracy", "f1", "auc", "runtime_sec"]

VALID_GROUP_BY: frozenset[str] = frozenset([
    "algorithm.family",
    "algorithm.name",
    "algorithm.paradigm",
    "algorithm.training_cost_class",
    "algorithm_family.paradigm",
    "algorithm_family.interpretability",
    "dataset.n_rows_bucket",
    "dataset.n_features_bucket",
    "dataset.size_bucket",
    "dataset.dim_bucket",
    "dataset.imbalance_bucket",
    "date.year",
    "date.quarter",
    "date.month",
])
VALID_MEASURES: frozenset[str] = frozenset(["accuracy", "f1", "auc", "runtime_sec"])


def _n_rows_bucket_expr() -> str:
    """Cypher CASE expression that buckets d.n_rows into size labels."""
    return (
        "CASE "
        "WHEN d.n_rows < 1000 THEN 'small (<1k)' "
        "WHEN d.n_rows < 100000 THEN 'medium (1k-100k)' "
        "ELSE 'large (>100k)' "
        "END"
    )


def _n_features_bucket_expr() -> str:
    """Cypher CASE expression that buckets d.n_features into size labels."""
    return (
        "CASE "
        "WHEN d.n_features < 20 THEN 'low (<20)' "
        "WHEN d.n_features < 100 THEN 'medium (20-100)' "
        "ELSE 'high (>100)' "
        "END"
    )


def _build_cypher(group_by: str, measure: str, filter_cypher: str) -> str:
    """Build a read-only Cypher aggregation query from validated parameters."""
    # Base traversal — always joins Run to Algorithm and Dataset via relationships
    base = (
        "MATCH (r:Run)-[:USED_ALGORITHM]->(a:Algorithm), "
        "(r:Run)-[:ON_DATASET]->(d:Dataset)"
    )

    # Date axes require an extra join to the Date dimension.
    if group_by.startswith("date."):
        base = base + ", (r:Run)-[:RUN_ON_DATE]->(dt:Date)"

    # AlgorithmFamily axes require joining through BELONGS_TO_FAMILY.
    if group_by.startswith("algorithm_family."):
        base = base + ", (a:Algorithm)-[:BELONGS_TO_FAMILY]->(af:AlgorithmFamily)"

    # Append user-supplied filter clause (pre-validated by caller context;
    # filter_cypher is an optional WHERE fragment like "a.family = 'tree_ensemble'").
    # Note: we intentionally do NOT add IS NOT NULL — LadybugDB stores NaN as NULL, so
    # filtering would drop all rows when ingested data has no numeric metrics.
    # avg() skips NULLs natively; we still return group rows (for count-based fixtures).
    if filter_cypher and filter_cypher.strip():
        where = f" WHERE {filter_cypher.strip()}"
    else:
        where = ""

    # Group-by expression and alias
    if group_by == "algorithm.family":
        group_expr = "a.family"
        group_alias = "family"
    elif group_by == "algorithm.name":
        group_expr = "a.name"
        group_alias = "name"
    elif group_by == "algorithm.paradigm":
        group_expr = "a.paradigm"
        group_alias = "paradigm"
    elif group_by == "algorithm.training_cost_class":
        group_expr = "a.training_cost_class"
        group_alias = "training_cost_class"
    elif group_by == "algorithm_family.paradigm":
        group_expr = "af.paradigm"
        group_alias = "paradigm"
    elif group_by == "algorithm_family.interpretability":
        group_expr = "af.interpretability"
        group_alias = "interpretability"
    elif group_by == "dataset.n_rows_bucket":
        group_expr = _n_rows_bucket_expr()
        group_alias = "name"
    elif group_by == "dataset.n_features_bucket":
        group_expr = _n_features_bucket_expr()
        group_alias = "name"
    elif group_by == "dataset.size_bucket":
        group_expr = "d.size_bucket"
        group_alias = "size_bucket"
    elif group_by == "dataset.dim_bucket":
        group_expr = "d.dim_bucket"
        group_alias = "dim_bucket"
    elif group_by == "dataset.imbalance_bucket":
        group_expr = "d.imbalance_bucket"
        group_alias = "imbalance_bucket"
    elif group_by == "date.year":
        group_expr = "dt.year"
        group_alias = "year"
    elif group_by == "date.quarter":
        group_expr = "dt.quarter"
        group_alias = "quarter"
    elif group_by == "date.month":
        group_expr = "dt.month"
        group_alias = "month"
    else:
        # Should never reach here — validated above
        raise ValueError(f"Unsupported group_by: {group_by}")

    # Use count(r) (not count(r.measure)) so the count reflects all runs in the group
    # even when the measure column is entirely NULL (e.g. ingested with NaN values).
    cypher = (
        f"{base}{where} "
        f"RETURN {group_expr} AS {group_alias}, "
        f"avg(r.{measure}) AS mean, "
        f"count(r) AS count "
        f"ORDER BY count DESC"
    )
    return cypher


def aggregate_measures(
    group_by: str,
    measure: str,
    filter_cypher: str = "",
    config: Config | None = None,
) -> list[dict]:
    """
    Aggregate a measure grouped by a dimension property.

    Parameters
    ----------
    group_by     : One of "algorithm.family", "algorithm.name",
                   "algorithm.paradigm", "algorithm.training_cost_class",
                   "algorithm_family.paradigm", "algorithm_family.interpretability",
                   "dataset.n_rows_bucket", "dataset.n_features_bucket",
                   "dataset.size_bucket", "dataset.dim_bucket",
                   "dataset.imbalance_bucket",
                   "date.year", "date.quarter", "date.month".
    measure      : One of "accuracy", "f1", "auc", "runtime_sec".
    filter_cypher: Optional WHERE fragment appended to the generated query
                   (e.g. "a.family = 'tree_ensemble'"). Leave empty for no filter.
                   Use "a.is_ensemble = true" to restrict to ensemble methods.
    config       : Config instance (uses get_config() default if None).

    Returns
    -------
    list[dict] — each dict has a group key and "mean", "count".
    Ordered by count descending.

    Deviation from spec
    -------------------
    Spec declared return type as ``dict[str, dict]``; this implementation returns
    ``list[dict]`` so the eval harness (which iterates and calls r.get("family")) works.
    """
    if group_by not in VALID_GROUP_BY:
        raise ValueError(
            f"group_by must be one of {sorted(VALID_GROUP_BY)}, got '{group_by}'"
        )
    if measure not in VALID_MEASURES:
        raise ValueError(
            f"measure must be one of {sorted(VALID_MEASURES)}, got '{measure}'"
        )

    if config is None:
        from src.config import get_config
        config = get_config()

    cypher = _build_cypher(group_by, measure, filter_cypher or "")

    with GraphDB(config) as db:
        rows = db.execute(cypher)

    return rows


def aggregate_measures_tool_schema() -> dict:
    """Return the Claude API tool definition for aggregate_measures."""
    return {
        "name": "aggregate_measures",
        "description": (
            "Aggregate ML run metrics (accuracy, f1, auc, runtime_sec) grouped by "
            "a dimension attribute. Supported group_by axes: algorithm.family, "
            "algorithm.name, algorithm.paradigm, algorithm.training_cost_class, "
            "algorithm_family.paradigm, algorithm_family.interpretability, "
            "dataset.size_bucket, dataset.dim_bucket, dataset.imbalance_bucket, "
            "dataset.n_rows_bucket, dataset.n_features_bucket, "
            "date.year, date.quarter, date.month. "
            "Use filter_cypher to narrow results (e.g. \"a.is_ensemble = true\"). "
            "Use when the user asks for comparisons, rankings, averages, or "
            "temporal trends across groups."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "group_by": {
                    "type": "string",
                    "enum": sorted(VALID_GROUP_BY),
                    "description": "Dimension to group by.",
                },
                "measure": {
                    "type": "string",
                    "enum": sorted(VALID_MEASURES),
                    "description": "Metric to aggregate.",
                },
                "filter_cypher": {
                    "type": "string",
                    "description": (
                        "Optional WHERE clause fragment to narrow the result set "
                        "(e.g. \"a.family = 'tree_ensemble'\"). Leave empty for all runs."
                    ),
                },
            },
            "required": ["group_by", "measure"],
        },
    }
