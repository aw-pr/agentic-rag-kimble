"""
loader.py — Orchestrates fetch → transform → write for the Kimball graph.

Idempotency strategy (LadybugDB has no MERGE):
  - At ingestion start, prefetch existing PKs per node type into Python sets.
  - Only CREATE nodes whose PKs are not already in the set.
  - Relationships are only created for newly inserted Run nodes.

All writes go through GraphDB.execute_write(). No direct ladybug calls here.
"""

from __future__ import annotations

import logging
from typing import Any

from src.config import Config, get_config
from src.graph.db import GraphDB
from src.ingestion.family_metadata import FAMILY_METADATA, get_family_row
from src.ingestion.openml_fetch import (
    FetchedRun,
    fetch_dataset,
    fetch_flow,
    fetch_runs_for_dataset,
    list_dataset_ids,
)
from src.ingestion.transform import (
    derive_date_row,
    transform_dataset,
    transform_run,
    transform_task,
)

logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _existing_pks(db: GraphDB, table: str, pk_col: str) -> set[int]:
    """Return the set of existing primary-key values for a node table."""
    rows = db.execute(f"MATCH (n:{table}) RETURN n.{pk_col} AS pk")
    return {int(r["pk"]) for r in rows}


def _existing_string_values(db: GraphDB, table: str, col: str) -> set[str]:
    """Return the set of existing string values for a column in a node table."""
    rows = db.execute(f"MATCH (n:{table}) RETURN n.{col} AS val")
    result: set[str] = set()
    for r in rows:
        v = r.get("val")
        if v is not None and isinstance(v, str):
            result.add(v)
    return result


def _quote_str(value: str) -> str:
    """Escape single quotes and wrap in single quotes for Cypher literals."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _cypher_value(value: Any) -> str:
    """Render a Python value as a Cypher literal fragment."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return _quote_str(str(value))


def _create_node(db: GraphDB, table: str, props: dict) -> None:
    """Issue a CREATE statement for a single node."""
    kv = ", ".join(
        f"{k}: {_cypher_value(v)}"
        for k, v in props.items()
        if v is not None  # skip None — avoids type conflicts on typed cols
    )
    # description_embedding is intentionally omitted (None → skipped above)
    db.execute_write(f"CREATE (n:{table} {{{kv}}})")


def _create_rel(db: GraphDB, rel: str, from_table: str, from_pk_col: str,
                from_pk: int, to_table: str, to_pk_col: str, to_pk: int) -> None:
    """Create a relationship between two existing nodes."""
    db.execute_write(
        f"MATCH (a:{from_table} {{{from_pk_col}: {from_pk}}}), "
        f"(b:{to_table} {{{to_pk_col}: {to_pk}}}) "
        f"CREATE (a)-[:{rel}]->(b)"
    )


# ── Task fetching helpers ────────────────────────────────────────────────────

def _get_task_info(task_id: int) -> dict | None:
    """Fetch task metadata from OpenML. Returns None on failure."""
    try:
        import openml
        task = openml.tasks.get_task(task_id, download_data=False)
        task_type = (
            str(task.task_type)
            if hasattr(task, "task_type")
            else "Supervised Classification"
        )
        target_feature = (
            str(task.target_name)
            if hasattr(task, "target_name") and task.target_name
            else ""
        )
        evaluation_measure = (
            str(task.evaluation_measure)
            if hasattr(task, "evaluation_measure") and task.evaluation_measure
            else ""
        )
        return {
            "task_id": int(task_id),
            "task_type": task_type,
            "target_feature": target_feature,
            "evaluation_measure": evaluation_measure,
        }
    except Exception as exc:
        logger.warning("Failed to fetch task %d: %s", task_id, exc)
        return None


# ── Main pipeline ─────────────────────────────────────────────────────────────


def run_ingestion(
    config: Config,
    dry_run: bool = False,
    max_runs_per_dataset: int | None = None,
) -> None:
    """Full pipeline: fetch → transform → load into LadybugDB.

    Idempotent: pre-fetches existing PKs to skip already-loaded nodes.
    Logs progress every 1000 runs.

    Parameters
    ----------
    max_runs_per_dataset:
        Optional cap on runs fetched per dataset. Useful for smoke tests.
    """
    dataset_ids = list_dataset_ids(
        task_type=config.openml_task_type,
        max_datasets=config.openml_max_datasets,
        min_runs=config.openml_min_runs_per_dataset,
    )
    logger.info("Found %d candidate datasets", len(dataset_ids))

    if dry_run:
        logger.info("Dry run: %d candidate dataset IDs — no writes.", len(dataset_ids))
        print(f"Dry run: {len(dataset_ids)} candidate dataset IDs — no writes.")
        return

    with GraphDB(config) as db:
        db.initialise_schema()

        # ── AlgorithmFamily dimension nodes (upsert all 9 before ingestion) ────
        existing_family_names: set[str] = _existing_string_values(
            db, "AlgorithmFamily", "family_name"
        )
        for slug in FAMILY_METADATA:
            if slug not in existing_family_names:
                family_props = get_family_row(slug)
                try:
                    _create_node(db, "AlgorithmFamily", family_props)
                    existing_family_names.add(slug)
                    logger.info("Inserted AlgorithmFamily node: %s", slug)
                except Exception as exc:
                    logger.warning("Failed to insert AlgorithmFamily %s: %s", slug, exc)

        # Pre-fetch existing PKs
        existing_runs: set[int] = _existing_pks(db, "Run", "run_id")
        existing_algorithms: set[int] = _existing_pks(db, "Algorithm", "flow_id")
        existing_datasets: set[int] = _existing_pks(db, "Dataset", "dataset_id")
        existing_tasks: set[int] = _existing_pks(db, "Task", "task_id")
        existing_dates: set[int] = _existing_pks(db, "Date", "date_id")
        # Track which (flow_id, family_slug) BELONGS_TO_FAMILY rels already exist.
        existing_family_rels: set[int] = {
            int(r["flow_id"])
            for r in db.execute(
                "MATCH (a:Algorithm)-[:BELONGS_TO_FAMILY]->(:AlgorithmFamily) "
                "RETURN a.flow_id AS flow_id"
            )
        }

        inserted_runs = 0
        inserted_algorithms = 0
        inserted_datasets = 0
        inserted_tasks = 0
        inserted_dates = 0
        skipped_runs = 0

        for i, dataset_id in enumerate(dataset_ids):
            logger.info(
                "Processing dataset %d/%d (id=%d)",
                i + 1, len(dataset_ids), dataset_id,
            )

            # ── Dataset node ────────────────────────────────────────────────
            if dataset_id not in existing_datasets:
                fetched_ds = fetch_dataset(dataset_id)
                if fetched_ds is None:
                    continue
                ds_props = transform_dataset(fetched_ds)
                try:
                    _create_node(db, "Dataset", ds_props)
                    existing_datasets.add(dataset_id)
                    inserted_datasets += 1
                except Exception as exc:
                    logger.warning(
                        "Failed to insert dataset %d: %s", dataset_id, exc
                    )
                    continue

            # ── Runs for this dataset ───────────────────────────────────────
            runs: list[FetchedRun] = fetch_runs_for_dataset(
                dataset_id,
                config.openml_task_type,
                max_runs=max_runs_per_dataset,
            )

            if len(runs) < config.openml_min_runs_per_dataset:
                logger.debug(
                    "Dataset %d has only %d runs (min=%d), skipping.",
                    dataset_id, len(runs), config.openml_min_runs_per_dataset,
                )
                continue

            for run in runs:
                if run.run_id in existing_runs:
                    skipped_runs += 1
                    continue

                # ── Task node ───────────────────────────────────────────────
                if run.task_id not in existing_tasks:
                    task_info = _get_task_info(run.task_id)
                    if task_info is not None:
                        task_props = transform_task(
                            task_id=task_info["task_id"],
                            task_type=task_info["task_type"],
                            target_feature=task_info["target_feature"],
                            evaluation_measure=task_info["evaluation_measure"],
                        )
                        try:
                            _create_node(db, "Task", task_props)
                            existing_tasks.add(run.task_id)
                            inserted_tasks += 1
                            # PART_OF_TASK: Dataset → Task
                            if dataset_id in existing_datasets:
                                try:
                                    _create_rel(
                                        db, "PART_OF_TASK",
                                        "Dataset", "dataset_id", dataset_id,
                                        "Task", "task_id", run.task_id,
                                    )
                                except Exception as exc:
                                    logger.warning(
                                        "PART_OF_TASK rel failed (dataset=%d task=%d): %s",
                                        dataset_id, run.task_id, exc,
                                    )
                        except Exception as exc:
                            logger.warning(
                                "Failed to insert task %d: %s", run.task_id, exc
                            )

                # ── Algorithm node ──────────────────────────────────────────
                flow_data = fetch_flow(run.flow_id)
                flow_name = flow_data["name"]

                if run.flow_id not in existing_algorithms:
                    run_dict, algo_props = transform_run(run, flow_name)
                    try:
                        _create_node(db, "Algorithm", algo_props)
                        existing_algorithms.add(run.flow_id)
                        inserted_algorithms += 1
                    except Exception as exc:
                        logger.warning(
                            "Failed to insert algorithm %d: %s", run.flow_id, exc
                        )
                    # ── BELONGS_TO_FAMILY relationship ──────────────────────
                    if run.flow_id not in existing_family_rels:
                        family_slug = algo_props.get("family", "other")
                        family_id = get_family_row(family_slug)["family_id"]
                        try:
                            db.execute_write(
                                f"MATCH (a:Algorithm {{flow_id: {run.flow_id}}}), "
                                f"(f:AlgorithmFamily {{family_id: {family_id}}}) "
                                f"CREATE (a)-[:BELONGS_TO_FAMILY]->(f)"
                            )
                            existing_family_rels.add(run.flow_id)
                        except Exception as exc:
                            logger.warning(
                                "BELONGS_TO_FAMILY rel failed (flow=%d family=%s): %s",
                                run.flow_id, family_slug, exc,
                            )
                else:
                    run_dict, _ = transform_run(run, flow_name)

                # ── Run node ────────────────────────────────────────────────
                try:
                    _create_node(db, "Run", run_dict)
                    existing_runs.add(run.run_id)
                    inserted_runs += 1
                except Exception as exc:
                    logger.warning(
                        "Failed to insert run %d: %s", run.run_id, exc
                    )
                    continue

                # ── Relationships ───────────────────────────────────────────
                try:
                    _create_rel(
                        db, "USED_ALGORITHM",
                        "Run", "run_id", run.run_id,
                        "Algorithm", "flow_id", run.flow_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "USED_ALGORITHM rel failed (run=%d flow=%d): %s",
                        run.run_id, run.flow_id, exc,
                    )

                if dataset_id in existing_datasets:
                    try:
                        _create_rel(
                            db, "ON_DATASET",
                            "Run", "run_id", run.run_id,
                            "Dataset", "dataset_id", dataset_id,
                        )
                    except Exception as exc:
                        logger.warning(
                            "ON_DATASET rel failed (run=%d dataset=%d): %s",
                            run.run_id, dataset_id, exc,
                        )

                if run.task_id in existing_tasks:
                    try:
                        _create_rel(
                            db, "FOR_TASK",
                            "Run", "run_id", run.run_id,
                            "Task", "task_id", run.task_id,
                        )
                    except Exception as exc:
                        logger.warning(
                            "FOR_TASK rel failed (run=%d task=%d): %s",
                            run.run_id, run.task_id, exc,
                        )

                # ── Date dimension ───────────────────────────────────────────
                if run.upload_date:
                    date_row = derive_date_row(run.upload_date)
                    if date_row is not None:
                        date_id = date_row["date_id"]
                        if date_id not in existing_dates:
                            try:
                                _create_node(db, "Date", date_row)
                                existing_dates.add(date_id)
                                inserted_dates += 1
                            except Exception as exc:
                                logger.warning(
                                    "Failed to insert date %d: %s", date_id, exc
                                )
                        if date_id in existing_dates:
                            try:
                                _create_rel(
                                    db, "RUN_ON_DATE",
                                    "Run", "run_id", run.run_id,
                                    "Date", "date_id", date_id,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "RUN_ON_DATE rel failed (run=%d date=%d): %s",
                                    run.run_id, date_id, exc,
                                )

                if (inserted_runs + skipped_runs) % 1000 == 0 and inserted_runs + skipped_runs > 0:
                    logger.info(
                        "Progress: %d runs inserted, %d skipped, "
                        "%d algorithms, %d datasets, %d tasks",
                        inserted_runs, skipped_runs,
                        inserted_algorithms, inserted_datasets, inserted_tasks,
                    )

        # ── Summary ─────────────────────────────────────────────────────────
        summary = (
            f"\nIngestion complete:\n"
            f"  Runs:       {inserted_runs} inserted, {skipped_runs} skipped\n"
            f"  Algorithms: {inserted_algorithms} inserted\n"
            f"  Datasets:   {inserted_datasets} inserted\n"
            f"  Tasks:      {inserted_tasks} inserted\n"
            f"  Dates:      {inserted_dates} inserted\n"
        )
        print(summary)
        logger.info(summary)

        # Final node counts from DB
        for table in ("Run", "Algorithm", "Dataset", "Task", "Date", "AlgorithmFamily"):
            count = db.node_count(table)
            print(f"  {table} total in DB: {count}")


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Ingest OpenML data into the LadybugDB graph.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Fetch candidate IDs only; no DB writes."
    )
    parser.add_argument("--max-datasets", type=int, default=None)
    parser.add_argument(
        "--max-runs-per-dataset",
        type=int,
        default=None,
        help="Cap runs fetched per dataset (useful for smoke tests).",
    )
    args = parser.parse_args()

    cfg = get_config()
    if args.max_datasets:
        cfg.openml_max_datasets = args.max_datasets

    run_ingestion(cfg, dry_run=args.dry_run, max_runs_per_dataset=args.max_runs_per_dataset)
