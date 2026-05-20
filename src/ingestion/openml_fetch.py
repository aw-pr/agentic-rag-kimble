"""
openml_fetch.py — Thin wrapper around the OpenML Python client.

All access is read-only; no API key is required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

import openml
import pandas as pd
from openml.tasks import TaskType

# Read-only access — no key required
openml.config.apikey = ""

logger = logging.getLogger(__name__)


@dataclass
class FetchedDataset:
    dataset_id: int
    name: str
    qualities: dict


@dataclass
class FetchedRun:
    run_id: int
    setup_id: int
    flow_id: int
    dataset_id: int
    task_id: int
    evaluations: dict  # metric_name → value
    upload_date: str | None = None  # ISO date string from OpenML upload_time column


def list_dataset_ids(
    task_type: str, max_datasets: int, min_runs: int
) -> list[int]:
    """Return dataset IDs that have at least min_runs classification runs.

    Uses task_type_id=1 (supervised classification) regardless of task_type
    string — the string is retained for compatibility with Config but OpenML's
    list_tasks API uses an integer discriminant.
    """
    try:
        tasks = openml.tasks.list_tasks(
            task_type=TaskType.SUPERVISED_CLASSIFICATION,
            output_format="dataframe",
        )
    except Exception as exc:
        logger.error("Failed to list OpenML tasks: %s", exc)
        return []

    assert isinstance(tasks, pd.DataFrame)
    if tasks.empty:
        return []

    # Count tasks per dataset; a dataset with multiple tasks likely has many runs
    if "did" not in tasks.columns:
        logger.warning("Unexpected tasks dataframe columns: %s", list(tasks.columns))
        return []

    dataset_counts = tasks.groupby("did").size()
    # Proxy: datasets with more tasks tend to have more runs; filter ≥ 1 task
    # (real min_runs filtering happens after we fetch actual runs)
    eligible = dataset_counts[dataset_counts >= 1].index.tolist()
    # Sort descending by task count as a heuristic for active datasets
    eligible = sorted(
        eligible,
        key=lambda did: dataset_counts[did],
        reverse=True,
    )
    return [int(did) for did in eligible[:max_datasets]]


def fetch_dataset(dataset_id: int) -> FetchedDataset | None:
    """Fetch one dataset's metadata and qualities. Returns None on failure."""
    try:
        ds = openml.datasets.get_dataset(
            dataset_id,
            download_data=False,
            download_qualities=True,
            download_features_meta_data=False,
        )
        qualities = ds.qualities if ds.qualities else {}
        assert ds.dataset_id is not None
        return FetchedDataset(
            dataset_id=int(ds.dataset_id),
            name=str(ds.name),
            qualities=qualities,
        )
    except Exception as exc:
        logger.warning("Skipping dataset %d: %s", dataset_id, exc)
        return None


def fetch_runs_for_dataset(
    dataset_id: int, task_type: str, max_runs: int | None = None
) -> list[FetchedRun]:
    """Fetch runs for a dataset's classification tasks.

    Parameters
    ----------
    max_runs:
        If set, cap the total number of runs returned across all tasks.
        Useful for smoke ingestion to keep runtimes bounded.
    """
    try:
        # Get tasks for this dataset (supervised classification only)
        tasks = openml.tasks.list_tasks(
            task_type=TaskType.SUPERVISED_CLASSIFICATION,
            data_id=dataset_id,
            output_format="dataframe",
        )
    except Exception as exc:
        logger.warning("Failed to list tasks for dataset %d: %s", dataset_id, exc)
        return []

    assert isinstance(tasks, pd.DataFrame)
    if tasks.empty or "tid" not in tasks.columns:
        return []

    task_ids = tasks["tid"].tolist()
    fetched_runs: list[FetchedRun] = []

    for task_id in task_ids:
        if max_runs is not None and len(fetched_runs) >= max_runs:
            break
        try:
            # Use size= to cap API response when max_runs is set
            size_arg = max_runs - len(fetched_runs) if max_runs is not None else None
            if size_arg is not None:
                runs_df = openml.runs.list_runs(
                    task=[int(task_id)], output_format="dataframe", size=size_arg
                )
            else:
                runs_df = openml.runs.list_runs(
                    task=[int(task_id)], output_format="dataframe"
                )
        except Exception as exc:
            logger.warning(
                "Failed to list runs for task %d (dataset %d): %s",
                task_id, dataset_id, exc,
            )
            continue

        assert isinstance(runs_df, pd.DataFrame)
        if runs_df.empty:
            continue

        for _, row in runs_df.iterrows():
            try:
                run_id = int(row["run_id"])
                flow_id = int(row["flow_id"])
                setup_id = int(row.get("setup_id", 0) or 0)

                # Build evaluations dict from columns where available
                evaluations: dict = {}
                for metric in ("predictive_accuracy", "accuracy", "f1", "area_under_roc_curve"):
                    if metric in row and row[metric] is not None:
                        try:
                            evaluations[metric] = float(row[metric])
                        except (TypeError, ValueError):
                            pass

                # OpenML returns upload_time as "YYYY-MM-DD HH:MM:SS"
                upload_date: str | None = None
                if "upload_time" in row and row["upload_time"] is not None:
                    raw = str(row["upload_time"]).strip()
                    if raw:
                        # Keep only the date part for the dimension key
                        upload_date = raw[:10]

                fetched_runs.append(
                    FetchedRun(
                        run_id=run_id,
                        setup_id=setup_id,
                        flow_id=flow_id,
                        dataset_id=int(dataset_id),
                        task_id=int(task_id),
                        evaluations=evaluations,
                        upload_date=upload_date,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Skipping run row for task %d: %s", task_id, exc
                )

    return fetched_runs


@lru_cache(maxsize=2000)
def fetch_flow(flow_id: int) -> dict:
    """Fetch flow (algorithm) metadata. Returns dict with flow_id and name.

    LRU-cached because flows repeat heavily across runs.
    """
    try:
        flow = openml.flows.get_flow(flow_id)
        assert flow.flow_id is not None
        return {
            "flow_id": int(flow.flow_id),
            "name": str(flow.name),
        }
    except Exception as exc:
        logger.warning("Failed to fetch flow %d: %s", flow_id, exc)
        return {
            "flow_id": flow_id,
            "name": f"unknown_flow_{flow_id}",
        }
