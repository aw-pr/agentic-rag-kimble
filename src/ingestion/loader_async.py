"""
loader_async.py — Async orchestrator: fetch → transform → write into LadybugDB.

Architecture
------------
- Fetches (HTTP) use httpx.AsyncClient with a bounded semaphore (default 8)
  inside openml_fetch_async.
- The **outer** dataset loop is now fully concurrent: all datasets are
  dispatched via asyncio.gather, gated by a Semaphore(openml_max_concurrent_datasets).
- Writes use a single ladybug.AsyncConnection serialised through an
  asyncio.Queue + dedicated writer task.

Rationale for serialised writes
--------------------------------
ladybug.AsyncConnection rejects concurrent write transactions
("Only one write transaction at a time is allowed"). Serial async writes still
beat the sync loader because HTTP fetch latency is fully overlapped:
while one dataset's writes are flushing, other datasets' fetches are in flight.

Idempotency
-----------
Existing PKs are pre-loaded into Python sets at startup (same strategy as
loader.py). PK check-and-add is protected by an asyncio.Lock (one per set)
to prevent duplicate inserts when datasets run concurrently.

Fallback
--------
The original sync loader (loader.py / run_ingestion) is preserved unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
import ladybug

from src.config import Config, get_config
from src.graph.db import GraphDB
from src.ingestion.family_metadata import FAMILY_METADATA, get_family_row
from src.ingestion.openml_fetch_async import (
    fetch_dataset_async,
    fetch_dataset_description_async,
    fetch_flow_async,
    fetch_flow_description_async,
    fetch_runs_for_dataset_async,
    fetch_task_async,
    list_dataset_ids_async,
    reset_semaphore,
)
from src.ingestion.transform import (
    derive_date_row,
    transform_dataset,
    transform_run,
    transform_task,
)

logger = logging.getLogger(__name__)

# Sentinel written to the write queue to signal the writer task to stop.
_STOP = object()


# ── Cypher helpers (mirror loader.py) ────────────────────────────────────────


def _quote_str(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _cypher_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return _quote_str(str(value))


def _create_node_cypher(table: str, props: dict) -> str:
    kv = ", ".join(
        f"{k}: {_cypher_value(v)}"
        for k, v in props.items()
        if v is not None
    )
    return f"CREATE (n:{table} {{{kv}}})"


def _create_rel_cypher(
    rel: str,
    from_table: str, from_pk_col: str, from_pk: int,
    to_table: str, to_pk_col: str, to_pk: int,
) -> str:
    return (
        f"MATCH (a:{from_table} {{{from_pk_col}: {from_pk}}}), "
        f"(b:{to_table} {{{to_pk_col}: {to_pk}}}) "
        f"CREATE (a)-[:{rel}]->(b)"
    )


# ── Shared ingestion state ────────────────────────────────────────────────────


@dataclass
class IngestState:
    """Shared mutable state across concurrent _process_dataset coroutines.

    The asyncio.Lock protects the check-and-add on each PK set to prevent
    duplicate inserts when datasets run concurrently.  The critical section
    is intentionally tiny — just the membership check and set.add(); the
    DB-write enqueue happens outside the lock.
    """
    existing_runs: set[int] = field(default_factory=set)
    existing_algorithms: set[int] = field(default_factory=set)
    existing_datasets: set[int] = field(default_factory=set)
    existing_tasks: set[int] = field(default_factory=set)
    existing_dates: set[int] = field(default_factory=set)
    # flow_ids that already have a BELONGS_TO_FAMILY relationship.
    existing_family_rels: set[int] = field(default_factory=set)
    pk_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    counters: dict = field(default_factory=lambda: {
        "runs": 0,
        "skipped": 0,
        "algorithms": 0,
        "datasets": 0,
        "tasks": 0,
        "dates": 0,
    })
    counters_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ── Writer task ───────────────────────────────────────────────────────────────


async def _writer(aconn: ladybug.AsyncConnection, queue: asyncio.Queue) -> None:
    """Drain the write queue, executing each query serially on aconn."""
    while True:
        item = await queue.get()
        if item is _STOP:
            queue.task_done()
            return
        query: str = item
        try:
            await aconn.execute(query)
        except Exception as exc:
            logger.warning("Write failed: %s | query: %.120s", exc, query)
        queue.task_done()


# ── Pre-fetch PKs (sync, before event loop) ──────────────────────────────────


def _existing_pks(db: GraphDB, table: str, pk_col: str) -> set[int]:
    rows = db.execute(f"MATCH (n:{table}) RETURN n.{pk_col} AS pk")
    return {int(r["pk"]) for r in rows}


# ── Per-dataset processor ─────────────────────────────────────────────────────


async def _process_dataset(
    client: httpx.AsyncClient,
    dataset_id: int,
    queue: asyncio.Queue,
    state: IngestState,
    config: Config,
    max_runs_per_dataset: int | None,
) -> None:
    """Fetch and enqueue writes for one dataset. Per-task error handling.

    Uses state.pk_lock around every check-and-add to prevent duplicate inserts
    when multiple _process_dataset coroutines run concurrently.
    """
    logger.debug("_process_dataset START dataset_id=%d", dataset_id)
    try:
        # ── Dataset node ─────────────────────────────────────────────────────
        async with state.pk_lock:
            if dataset_id in state.existing_datasets:
                ds_is_new = False
            else:
                state.existing_datasets.add(dataset_id)
                ds_is_new = True

        if ds_is_new:
            fetched_ds = await fetch_dataset_async(client, dataset_id)
            if fetched_ds is None:
                logger.warning("Skipping dataset %d — fetch failed", dataset_id)
                # Roll back the reservation so a future retry can try again
                async with state.pk_lock:
                    state.existing_datasets.discard(dataset_id)
                return
            ds_props = transform_dataset(fetched_ds)

            # Append OpenML free-form description to enrich the embedding text.
            openml_ds_desc = await fetch_dataset_description_async(client, dataset_id)
            if openml_ds_desc:
                ds_props["description"] = ds_props["description"] + " " + openml_ds_desc

            await queue.put(_create_node_cypher("Dataset", ds_props))
            async with state.counters_lock:
                state.counters["datasets"] += 1

        # ── Runs ─────────────────────────────────────────────────────────────
        runs = await fetch_runs_for_dataset_async(
            client, dataset_id, config.openml_task_type,
            max_runs=max_runs_per_dataset,
        )

        if len(runs) < config.openml_min_runs_per_dataset:
            logger.debug(
                "Dataset %d: only %d runs (min=%d), skipping.",
                dataset_id, len(runs), config.openml_min_runs_per_dataset,
            )
            return

        # Collect unique flow_ids and task_ids we need to fetch
        async with state.pk_lock:
            new_flow_ids = {
                r.flow_id for r in runs
                if r.flow_id not in state.existing_algorithms
            }
            new_task_ids = {
                r.task_id for r in runs
                if r.task_id not in state.existing_tasks
            }
            # Reserve them to prevent parallel datasets from re-fetching
            state.existing_algorithms.update(new_flow_ids)
            state.existing_tasks.update(new_task_ids)

        # ── Fetch flows in parallel ───────────────────────────────────────────
        flow_map: dict[int, dict] = {}
        if new_flow_ids:
            flow_results = await asyncio.gather(
                *[fetch_flow_async(client, fid) for fid in new_flow_ids],
                return_exceptions=True,
            )
            for fid, result in zip(new_flow_ids, flow_results):
                if isinstance(result, BaseException) or result is None:
                    flow_map[fid] = {"flow_id": fid, "name": f"unknown_flow_{fid}"}
                else:
                    flow_map[fid] = result

        # ── Fetch tasks in parallel ───────────────────────────────────────────
        task_map: dict[int, dict] = {}
        if new_task_ids:
            task_results = await asyncio.gather(
                *[fetch_task_async(client, tid) for tid in new_task_ids],
                return_exceptions=True,
            )
            for tid, result in zip(new_task_ids, task_results):
                if isinstance(result, BaseException) or result is None:
                    pass
                else:
                    task_map[tid] = result

        # ── Enqueue writes for tasks ──────────────────────────────────────────
        for tid, task_info in task_map.items():
            task_props = transform_task(
                task_id=task_info["task_id"],
                task_type=task_info["task_type"],
                target_feature=task_info["target_feature"],
                evaluation_measure=task_info["evaluation_measure"],
            )
            await queue.put(_create_node_cypher("Task", task_props))
            async with state.counters_lock:
                state.counters["tasks"] += 1
            # PART_OF_TASK relationship
            if dataset_id in state.existing_datasets:
                await queue.put(_create_rel_cypher(
                    "PART_OF_TASK",
                    "Dataset", "dataset_id", dataset_id,
                    "Task", "task_id", tid,
                ))

        # ── Enqueue writes for algorithms ─────────────────────────────────────
        for fid, flow_data in flow_map.items():
            _, algo_props = transform_run(
                next(r for r in runs if r.flow_id == fid),
                flow_data["name"],
            )

            # Append OpenML free-form description to enrich the embedding text.
            openml_flow_desc = await fetch_flow_description_async(client, fid)
            if openml_flow_desc:
                algo_props["description"] = algo_props["description"] + " " + openml_flow_desc

            await queue.put(_create_node_cypher("Algorithm", algo_props))
            async with state.counters_lock:
                state.counters["algorithms"] += 1

            # ── BELONGS_TO_FAMILY relationship ────────────────────────────────
            async with state.pk_lock:
                rel_is_new = fid not in state.existing_family_rels
                if rel_is_new:
                    state.existing_family_rels.add(fid)
            if rel_is_new:
                family_slug = algo_props.get("family", "other")
                family_id = get_family_row(family_slug)["family_id"]
                await queue.put(
                    f"MATCH (a:Algorithm {{flow_id: {fid}}}), "
                    f"(f:AlgorithmFamily {{family_id: {family_id}}}) "
                    f"CREATE (a)-[:BELONGS_TO_FAMILY]->(f)"
                )

        # ── Enqueue writes for runs ───────────────────────────────────────────
        for run in runs:
            async with state.pk_lock:
                if run.run_id in state.existing_runs:
                    is_new_run = False
                else:
                    state.existing_runs.add(run.run_id)
                    is_new_run = True

            if not is_new_run:
                async with state.counters_lock:
                    state.counters["skipped"] += 1
                continue

            # Flow name (may be from cache or just-fetched map)
            flow_data = flow_map.get(run.flow_id) or {"name": f"unknown_flow_{run.flow_id}"}
            flow_name = flow_data["name"]

            run_dict, _ = transform_run(run, flow_name)
            await queue.put(_create_node_cypher("Run", run_dict))
            async with state.counters_lock:
                state.counters["runs"] += 1

            # Relationships
            await queue.put(_create_rel_cypher(
                "USED_ALGORITHM",
                "Run", "run_id", run.run_id,
                "Algorithm", "flow_id", run.flow_id,
            ))
            if dataset_id in state.existing_datasets:
                await queue.put(_create_rel_cypher(
                    "ON_DATASET",
                    "Run", "run_id", run.run_id,
                    "Dataset", "dataset_id", dataset_id,
                ))
            if run.task_id in state.existing_tasks:
                await queue.put(_create_rel_cypher(
                    "FOR_TASK",
                    "Run", "run_id", run.run_id,
                    "Task", "task_id", run.task_id,
                ))

            # ── Date dimension ────────────────────────────────────────────────
            if run.upload_date:
                date_row = derive_date_row(run.upload_date)
                if date_row is not None:
                    date_id = date_row["date_id"]
                    date_is_new = False
                    async with state.pk_lock:
                        if date_id not in state.existing_dates:
                            state.existing_dates.add(date_id)
                            date_is_new = True
                    if date_is_new:
                        await queue.put(_create_node_cypher("Date", date_row))
                        async with state.counters_lock:
                            state.counters["dates"] += 1
                    await queue.put(_create_rel_cypher(
                        "RUN_ON_DATE",
                        "Run", "run_id", run.run_id,
                        "Date", "date_id", date_id,
                    ))

            async with state.counters_lock:
                total = state.counters["runs"] + state.counters["skipped"]
            if total % 1000 == 0 and total > 0:
                async with state.counters_lock:
                    c = state.counters.copy()
                logger.info(
                    "Progress: %d runs inserted, %d skipped, "
                    "%d algorithms, %d datasets, %d tasks",
                    c["runs"], c["skipped"],
                    c["algorithms"], c["datasets"], c["tasks"],
                )

    except Exception as exc:
        logger.error(
            "Unhandled error processing dataset %d: %s", dataset_id, exc,
            exc_info=True,
        )

    logger.debug("_process_dataset END dataset_id=%d", dataset_id)


# ── Main entry point ──────────────────────────────────────────────────────────


async def run_ingestion_async(
    config: Config,
    max_runs_per_dataset: int | None = None,
    concurrency: int = 8,
    concurrent_datasets: int | None = None,
) -> None:
    """Async version of run_ingestion.

    Fetches are concurrent both within each dataset (bounded by `concurrency`
    semaphore slots in openml_fetch_async) and across datasets (bounded by
    `concurrent_datasets` Semaphore, default from config).

    Writes are serialised through a single ladybug.AsyncConnection.

    Idempotent: pre-loads existing PKs and skips already-present nodes.
    """
    max_ds_concurrency = (
        concurrent_datasets
        if concurrent_datasets is not None
        else config.openml_max_concurrent_datasets
    )

    # The inner HTTP semaphore needs enough slots to serve all concurrent
    # dataset coroutines without stalling each other.  Scale it by the number
    # of concurrent datasets so the per-dataset fetch concurrency is preserved.
    # Cap at 64 to avoid overwhelming the OpenML API.
    inner_concurrency = min(concurrency * max_ds_concurrency, 64)
    reset_semaphore(inner_concurrency)

    # ── Open sync DB, pre-load PKs, initialise schema ─────────────────────────
    with GraphDB(config) as db:
        db.initialise_schema()

        # Upsert all 9 AlgorithmFamily nodes before the async ingest loop.
        existing_family_names_sync: set[str] = set()
        for r in db.execute("MATCH (f:AlgorithmFamily) RETURN f.family_name AS fn"):
            v = r.get("fn")
            if v is not None and isinstance(v, str):
                existing_family_names_sync.add(v)
        for slug in FAMILY_METADATA:
            if slug not in existing_family_names_sync:
                fprops = get_family_row(slug)
                kv = ", ".join(
                    f"{k}: {_cypher_value(v)}"
                    for k, v in fprops.items()
                    if v is not None
                )
                try:
                    db.execute_write(f"CREATE (n:AlgorithmFamily {{{kv}}})")
                    existing_family_names_sync.add(slug)
                    logger.info("Inserted AlgorithmFamily node: %s", slug)
                except Exception as exc:
                    logger.warning("Failed to insert AlgorithmFamily %s: %s", slug, exc)

        state = IngestState(
            existing_runs=_existing_pks(db, "Run", "run_id"),
            existing_algorithms=_existing_pks(db, "Algorithm", "flow_id"),
            existing_datasets=_existing_pks(db, "Dataset", "dataset_id"),
            existing_tasks=_existing_pks(db, "Task", "task_id"),
            existing_dates=_existing_pks(db, "Date", "date_id"),
            existing_family_rels={
                int(r["flow_id"])
                for r in db.execute(
                    "MATCH (a:Algorithm)-[:BELONGS_TO_FAMILY]->(:AlgorithmFamily) "
                    "RETURN a.flow_id AS flow_id"
                )
            },
        )

        logger.info(
            "Pre-loaded PKs: %d runs, %d algorithms, %d datasets, %d tasks, %d dates",
            len(state.existing_runs), len(state.existing_algorithms),
            len(state.existing_datasets), len(state.existing_tasks),
            len(state.existing_dates),
        )

        # Keep reference to the underlying ladybug.Database for async connection
        _lb_db = db._db  # noqa: SLF001
        assert _lb_db is not None, "GraphDB not connected — db._db is None."

    # ── Set up async write connection ─────────────────────────────────────────
    aconn = ladybug.AsyncConnection(_lb_db)
    write_queue: asyncio.Queue = asyncio.Queue()

    async with httpx.AsyncClient() as client:
        # Fetch dataset ID list
        dataset_ids = await list_dataset_ids_async(
            client,
            task_type=config.openml_task_type,
            max_datasets=config.openml_max_datasets,
            min_runs=config.openml_min_runs_per_dataset,
        )
        logger.info(
            "Found %d candidate datasets (max_concurrent_datasets=%d)",
            len(dataset_ids), max_ds_concurrency,
        )

        # Start the writer task
        writer_task = asyncio.create_task(_writer(aconn, write_queue))

        # Semaphore limits how many datasets are processed in parallel
        ds_sem = asyncio.Semaphore(max_ds_concurrency)

        async def _gated(ds_id: int) -> None:
            async with ds_sem:
                await _process_dataset(
                    client=client,
                    dataset_id=ds_id,
                    queue=write_queue,
                    state=state,
                    config=config,
                    max_runs_per_dataset=max_runs_per_dataset,
                )

        # Fan-out: all datasets start concurrently, gated by ds_sem.
        # return_exceptions=True ensures one failure doesn't abort the rest.
        results = await asyncio.gather(
            *[_gated(ds_id) for ds_id in dataset_ids],
            return_exceptions=True,
        )

        # Log any unexpected exceptions returned from _gated
        for ds_id, result in zip(dataset_ids, results):
            if isinstance(result, Exception):
                logger.error(
                    "Dataset %d raised an unhandled exception: %s",
                    ds_id, result, exc_info=result,
                )

        # Drain the write queue then stop the writer
        await write_queue.join()
        write_queue.put_nowait(_STOP)
        await writer_task

    c = state.counters
    summary = (
        f"\nAsync ingestion complete:\n"
        f"  Runs:       {c['runs']} inserted, {c['skipped']} skipped\n"
        f"  Algorithms: {c['algorithms']} inserted\n"
        f"  Datasets:   {c['datasets']} inserted\n"
        f"  Tasks:      {c['tasks']} inserted\n"
        f"  Dates:      {c['dates']} inserted\n"
    )
    print(summary)
    logger.info(summary)


# ── CLI entry point ───────────────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Async ingest OpenML data into LadybugDB."
    )
    parser.add_argument("--max-datasets", type=int, default=None)
    parser.add_argument("--max-runs-per-dataset", type=int, default=None)
    parser.add_argument(
        "--concurrency", type=int, default=8,
        help="Number of concurrent HTTP fetch slots (within-dataset)",
    )
    parser.add_argument(
        "--concurrent-datasets", type=int, default=None,
        help="Number of datasets processed in parallel (default: from config)",
    )
    args = parser.parse_args()

    cfg = get_config()
    if args.max_datasets:
        cfg.openml_max_datasets = args.max_datasets

    asyncio.run(
        run_ingestion_async(
            cfg,
            max_runs_per_dataset=args.max_runs_per_dataset,
            concurrency=args.concurrency,
            concurrent_datasets=args.concurrent_datasets,
        )
    )
