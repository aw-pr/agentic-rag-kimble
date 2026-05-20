"""
aggregate.py — re-export shim for backward compatibility with the eval harness.

The eval harness (src.eval.metrics._try_aggregate) imports aggregate_measures
from this module and calls it with group_by="family" (a short alias).  This
shim translates the alias to the canonical "algorithm.family" before delegating
to aggregate_tool.aggregate_measures.
"""

from __future__ import annotations

from src.config import Config
from src.retrieval.aggregate_tool import aggregate_measures as _aggregate_measures

# Alias map: eval harness short names → canonical group_by values
_GROUP_BY_ALIASES: dict[str, str] = {
    "family": "algorithm.family",
    "name": "algorithm.name",
    "n_rows_bucket": "dataset.n_rows_bucket",
    "n_features_bucket": "dataset.n_features_bucket",
}


def aggregate_measures(
    group_by: str,
    measure: str,
    filter_cypher: str | None = "",
    config: Config | None = None,
) -> list[dict]:
    """
    Wrapper around aggregate_tool.aggregate_measures that accepts both canonical
    group_by values ("algorithm.family") and short aliases ("family").
    """
    canonical = _GROUP_BY_ALIASES.get(group_by, group_by)
    return _aggregate_measures(
        group_by=canonical,
        measure=measure,
        filter_cypher=filter_cypher or "",
        config=config,
    )
