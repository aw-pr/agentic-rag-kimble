#!/usr/bin/env bash
# backfill-openml-descriptions.sh — Pass 26: enrich Algorithm and Dataset
# descriptions with OpenML free-form text, then re-embed.
#
# Strategy (Option A — append to existing description column):
#   1. Drop Algorithm and Dataset vector indexes (catch "doesn't exist").
#   2. For each Algorithm row: fetch flow description from OpenML API.
#      If non-empty and not already appended: SET description = synthesised + openml_text,
#      SET description_embedding = NULL.
#   3. Same for Dataset rows.
#   4. Run scripts/build-index.sh to re-embed all NULL-embedding rows.
#
# Idempotency: rows whose description is already long (>= ALREADY_ENRICHED_LEN)
#   are assumed to have been enriched and are skipped.  Re-running is safe.
#
# Crash-safe resume (staged recovery): each write is durable the moment
#   execute_write returns (embedded LadybugDB auto-commits per statement). After
#   every 50-entity batch the script records the processed entity IDs to a
#   checkpoint file and fsyncs it — the explicit per-batch commit point. If the
#   process dies mid-run, re-running loads the checkpoint and skips every
#   already-processed ID, so no batch is re-fetched or re-written. The checkpoint
#   is deleted on a clean finish, so a present checkpoint always means an earlier
#   run did not complete.
#
# Progress: printed every 50 rows (Algorithm pass) and every 50 rows (Dataset pass),
#   plus a heartbeat every ~10 seconds so the watchdog stays happy.
#
# Usage:
#   scripts/backfill-openml-descriptions.sh
#
# Environment:
#   LADYBUG_DB_PATH       override default data/ladybug_db  (optional)
#   BACKFILL_CHECKPOINT   override the checkpoint file path  (optional;
#                         default: <db-path>.backfill-checkpoint.json)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Pass 26: backfill OpenML free-form descriptions ==="
echo ""

python3 -u <<'PY'
import asyncio
import json
import math
import os
import time

import httpx

from src.config import get_config
from src.graph.db import GraphDB
from src.graph.queries import SET_ALGORITHM_DESCRIPTION, SET_DATASET_DESCRIPTION
from src.ingestion.openml_fetch_async import (
    fetch_dataset_description_async,
    fetch_flow_description_async,
    reset_semaphore,
    _dataset_desc_cache,
    _flow_desc_cache,
)
from src.retrieval.vector_store import _index_name

cfg = get_config()

# Rows whose description is already this long are assumed already enriched.
# A plain synthesised Algorithm description is typically 80-160 chars; a
# Dataset description is ~130 chars.  A threshold of 300 comfortably exceeds
# both, so any row >= 300 chars already has OpenML text appended.
ALREADY_ENRICHED_LEN = 300

# Bounded concurrency for OpenML fetch calls (pass-14 finding: ~5-6 req/s safe).
# Held at 4 (down from 8) so the OpenML fetch fan-out stays well clear of the
# sustained-load conditions implicated in the pass-26 ingest segfault.
CONCURRENCY = 4
BATCH_SIZE = 50  # Process in batches; print progress at each batch boundary.

reset_semaphore(CONCURRENCY)

# ── Staged-recovery checkpoint ────────────────────────────────────────────────
# One JSON file records the entity IDs already processed in each pass. It is
# rewritten (and fsynced) at every batch boundary — the explicit per-batch
# commit — and deleted only on a clean finish of both passes. A checkpoint that
# is present on start therefore means a prior run crashed; its recorded IDs are
# skipped so no batch is re-fetched or re-written.
CHECKPOINT_PATH = os.environ.get("BACKFILL_CHECKPOINT") or (
    str(cfg.ladybug_db_path) + ".backfill-checkpoint.json"
)


def _load_checkpoint() -> dict:
    """Return the persisted progress, or an empty skeleton if none exists."""
    try:
        with open(CHECKPOINT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        return {"algorithm": [], "dataset": []}
    data.setdefault("algorithm", [])
    data.setdefault("dataset", [])
    return data


def _save_checkpoint(state: dict) -> None:
    """Atomically persist progress and fsync — the explicit commit point."""
    tmp = CHECKPOINT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CHECKPOINT_PATH)


def _clear_checkpoint() -> None:
    """Remove the checkpoint after a clean finish so the tree stays tidy."""
    try:
        os.remove(CHECKPOINT_PATH)
    except FileNotFoundError:
        pass


def _safe_str(v) -> str:
    """Return v as str, or '' if it's nan / None."""
    if v is None:
        return ""
    try:
        if math.isnan(float(v)):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v)


async def backfill_algorithms(
    db: GraphDB, client: httpx.AsyncClient, checkpoint: dict
) -> dict:
    """Fetch OpenML flow descriptions and append to Algorithm descriptions."""
    print("── Algorithm pass ──")

    done_ids = set(checkpoint["algorithm"])
    if done_ids:
        print(f"  Resuming: {len(done_ids)} Algorithm row(s) already checkpointed.")

    # Drop vector index first (LadybugDB constraint: can't SET indexed column).
    idx = _index_name("Algorithm")
    try:
        db._run(f"CALL DROP_VECTOR_INDEX('Algorithm', '{idx}')")
        print(f"  Dropped Algorithm vector index '{idx}'.")
    except Exception as exc:
        msg = str(exc).lower()
        if any(k in msg for k in ("not exist", "does not exist", "no such")):
            print(f"  Algorithm vector index '{idx}' not found — continuing.")
        else:
            print(f"  Warning: DROP_VECTOR_INDEX raised: {exc}")

    rows = db.execute(
        "MATCH (a:Algorithm) "
        "RETURN a.flow_id AS flow_id, a.name AS name, a.description AS description"
    )
    total = len(rows)
    print(f"  Found {total} Algorithm rows.")

    updated = 0
    skipped_already_enriched = 0
    skipped_no_desc = 0
    skipped_errors = 0
    skipped_checkpointed = 0
    start = time.time()
    last_print = start

    for batch_start in range(0, total, BATCH_SIZE):
        batch = rows[batch_start: batch_start + BATCH_SIZE]

        # Fetch descriptions only for rows not already committed by a prior run.
        pending = [(r, int(r["flow_id"])) for r in batch if int(r["flow_id"]) not in done_ids]
        skipped_checkpointed += len(batch) - len(pending)
        descs = await asyncio.gather(
            *[fetch_flow_description_async(client, fid) for _, fid in pending],
            return_exceptions=True,
        )

        for (row, fid), openml_desc in zip(pending, descs):
            if isinstance(openml_desc, Exception):
                logger_info = f"    flow {fid}: fetch raised {openml_desc}"
                print(logger_info, flush=True)
                skipped_errors += 1
                continue

            existing_desc = _safe_str(row.get("description"))

            # Skip if already enriched (description is long enough).
            if len(existing_desc) >= ALREADY_ENRICHED_LEN:
                skipped_already_enriched += 1
                done_ids.add(fid)
                continue

            if not openml_desc:
                skipped_no_desc += 1
                done_ids.add(fid)
                continue

            new_desc = existing_desc + " " + openml_desc
            db.execute_write(
                SET_ALGORITHM_DESCRIPTION,
                {"flow_id": fid, "description": new_desc},
            )
            updated += 1
            done_ids.add(fid)

        # Explicit per-batch commit point: persist and fsync progress so a crash
        # after this line never re-fetches or re-writes this batch on resume.
        checkpoint["algorithm"] = sorted(done_ids)
        _save_checkpoint(checkpoint)

        # Print progress at each batch boundary.
        now = time.time()
        processed_so_far = min(batch_start + BATCH_SIZE, total)
        elapsed = now - start
        print(
            f"    {processed_so_far}/{total} rows processed "
            f"({updated} updated, {elapsed:.1f}s)",
            flush=True,
        )
        last_print = now

    print(
        f"\n  Algorithm backfill complete: "
        f"{updated} updated, "
        f"{skipped_already_enriched} already enriched, "
        f"{skipped_no_desc} no OpenML desc, "
        f"{skipped_errors} errors, "
        f"{skipped_checkpointed} resumed-skip."
    )
    return {
        "updated": updated,
        "already_enriched": skipped_already_enriched,
        "no_desc": skipped_no_desc,
        "errors": skipped_errors,
        "checkpointed": skipped_checkpointed,
        "total": total,
    }


async def backfill_datasets(
    db: GraphDB, client: httpx.AsyncClient, checkpoint: dict
) -> dict:
    """Fetch OpenML dataset descriptions and append to Dataset descriptions."""
    print("\n── Dataset pass ──")

    done_ids = set(checkpoint["dataset"])
    if done_ids:
        print(f"  Resuming: {len(done_ids)} Dataset row(s) already checkpointed.")

    idx = _index_name("Dataset")
    try:
        db._run(f"CALL DROP_VECTOR_INDEX('Dataset', '{idx}')")
        print(f"  Dropped Dataset vector index '{idx}'.")
    except Exception as exc:
        msg = str(exc).lower()
        if any(k in msg for k in ("not exist", "does not exist", "no such")):
            print(f"  Dataset vector index '{idx}' not found — continuing.")
        else:
            print(f"  Warning: DROP_VECTOR_INDEX raised: {exc}")

    rows = db.execute(
        "MATCH (d:Dataset) "
        "RETURN d.dataset_id AS dataset_id, d.name AS name, d.description AS description"
    )
    total = len(rows)
    print(f"  Found {total} Dataset rows.")

    updated = 0
    skipped_already_enriched = 0
    skipped_no_desc = 0
    skipped_errors = 0
    skipped_checkpointed = 0
    start = time.time()

    for batch_start in range(0, total, BATCH_SIZE):
        batch = rows[batch_start: batch_start + BATCH_SIZE]

        pending = [
            (r, int(r["dataset_id"]))
            for r in batch
            if int(r["dataset_id"]) not in done_ids
        ]
        skipped_checkpointed += len(batch) - len(pending)
        descs = await asyncio.gather(
            *[fetch_dataset_description_async(client, did) for _, did in pending],
            return_exceptions=True,
        )

        for (row, did), openml_desc in zip(pending, descs):
            if isinstance(openml_desc, Exception):
                print(f"    dataset {did}: fetch raised {openml_desc}", flush=True)
                skipped_errors += 1
                continue

            existing_desc = _safe_str(row.get("description"))

            if len(existing_desc) >= ALREADY_ENRICHED_LEN:
                skipped_already_enriched += 1
                done_ids.add(did)
                continue

            if not openml_desc:
                skipped_no_desc += 1
                done_ids.add(did)
                continue

            new_desc = existing_desc + " " + openml_desc
            db.execute_write(
                SET_DATASET_DESCRIPTION,
                {"dataset_id": did, "description": new_desc},
            )
            updated += 1
            done_ids.add(did)

        # Explicit per-batch commit point (see backfill_algorithms).
        checkpoint["dataset"] = sorted(done_ids)
        _save_checkpoint(checkpoint)

        processed_so_far = min(batch_start + BATCH_SIZE, total)
        elapsed = time.time() - start
        print(
            f"    {processed_so_far}/{total} rows processed "
            f"({updated} updated, {elapsed:.1f}s)",
            flush=True,
        )

    print(
        f"\n  Dataset backfill complete: "
        f"{updated} updated, "
        f"{skipped_already_enriched} already enriched, "
        f"{skipped_no_desc} no OpenML desc, "
        f"{skipped_errors} errors, "
        f"{skipped_checkpointed} resumed-skip."
    )
    return {
        "updated": updated,
        "already_enriched": skipped_already_enriched,
        "no_desc": skipped_no_desc,
        "errors": skipped_errors,
        "checkpointed": skipped_checkpointed,
        "total": total,
    }


async def print_before_after_samples(db: GraphDB, client: httpx.AsyncClient) -> None:
    """Print before/after description samples for the two target fixtures."""
    print("\n── Before / After samples ──")

    # HistGradientBoostingClassifier — find by name substring
    histgb_rows = db.execute(
        "MATCH (a:Algorithm) WHERE a.name CONTAINS 'HistGradientBoosting' "
        "RETURN a.name AS name, a.description AS description LIMIT 1"
    )
    if histgb_rows:
        r = histgb_rows[0]
        print(f"\n  HistGradientBoostingClassifier:")
        print(f"    {r['description']}")
    else:
        print("\n  HistGradientBoostingClassifier: not found in DB")

    # connect-4 dataset
    c4_rows = db.execute(
        "MATCH (d:Dataset) WHERE d.name = 'connect-4' "
        "RETURN d.name AS name, d.description AS description LIMIT 1"
    )
    if c4_rows:
        r = c4_rows[0]
        print(f"\n  connect-4 dataset:")
        print(f"    {r['description']}")
    else:
        print("\n  connect-4 dataset: not found in DB")


async def main():
    t0 = time.time()

    # Use a 10-second total timeout for the backfill — OpenML's /flow and /data
    # endpoints are normally <2s; a 30s timeout (the _get default) causes the
    # whole batch to stall when OpenML is slow.  httpx.Timeout(10, connect=5)
    # lets us fail fast and skip rather than stall.
    _client_timeout = httpx.Timeout(10.0, connect=5.0)

    checkpoint = _load_checkpoint()
    if checkpoint["algorithm"] or checkpoint["dataset"]:
        print(
            f"── Resuming from checkpoint {CHECKPOINT_PATH} "
            f"({len(checkpoint['algorithm'])} algo / "
            f"{len(checkpoint['dataset'])} dataset already done) ──\n"
        )

    with GraphDB(cfg) as db:
        async with httpx.AsyncClient(timeout=_client_timeout) as client:
            # Print before samples
            print("── Pre-backfill samples ──")
            histgb_pre = db.execute(
                "MATCH (a:Algorithm) WHERE a.name CONTAINS 'HistGradientBoosting' "
                "RETURN a.name AS name, a.description AS description LIMIT 1"
            )
            if histgb_pre:
                print(f"  BEFORE HistGradientBoostingClassifier:")
                print(f"    {histgb_pre[0]['description']}")
            c4_pre = db.execute(
                "MATCH (d:Dataset) WHERE d.name = 'connect-4' "
                "RETURN d.name AS name, d.description AS description LIMIT 1"
            )
            if c4_pre:
                print(f"  BEFORE connect-4:")
                print(f"    {c4_pre[0]['description']}")
            print()

            algo_stats = await backfill_algorithms(db, client, checkpoint)
            ds_stats = await backfill_datasets(db, client, checkpoint)

            # Print after samples
            await print_before_after_samples(db, client)

    # Both passes finished cleanly — drop the checkpoint so a later run starts
    # fresh (a lingering checkpoint always signals an incomplete prior run).
    _clear_checkpoint()

    wall = time.time() - t0
    total_api_calls = (
        algo_stats["total"] - algo_stats["already_enriched"] +
        ds_stats["total"] - ds_stats["already_enriched"]
    )
    print(f"\n── Summary ──")
    print(f"  Algorithms: {algo_stats['updated']} updated / {algo_stats['total']} total")
    print(f"    already enriched: {algo_stats['already_enriched']}")
    print(f"    no OpenML desc:   {algo_stats['no_desc']}")
    print(f"    errors:           {algo_stats['errors']}")
    print(f"  Datasets: {ds_stats['updated']} updated / {ds_stats['total']} total")
    print(f"    already enriched: {ds_stats['already_enriched']}")
    print(f"    no OpenML desc:   {ds_stats['no_desc']}")
    print(f"    errors:           {ds_stats['errors']}")
    print(f"  Total API calls (approx): {total_api_calls}")
    print(f"  Wall time: {wall:.1f}s")


asyncio.run(main())
PY

echo ""
echo "=== Backfill done. Running build-index.sh to re-embed... ==="
echo ""

bash scripts/build-index.sh

echo ""
echo "=== Pass 26 backfill complete ==="
echo "Run './scripts/run-eval.sh' to evaluate recall@10."
