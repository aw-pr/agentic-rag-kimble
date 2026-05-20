#!/usr/bin/env bash
# backfill-date-dimension.sh
#
# Populates the Date dimension and RUN_ON_DATE relationships for an existing
# LadybugDB that has Run nodes but no Date nodes (i.e. ingested before Stage 2).
#
# What it does
# ------------
#   1. Ensures the Date node table and RUN_ON_DATE relationship table exist
#      (idempotent CREATE ... IF NOT EXISTS via schema.create_schema).
#   2. Fetches upload_time for each Run via openml.runs.list_runs (uses the
#      local OpenML cache when warm; falls back to network otherwise).
#   3. Derives Date dimension rows (date_id in YYYYMMDD form, year, quarter,
#      month, month_name, day_of_week, is_weekend).
#   4. Upserts missing Date nodes and creates RUN_ON_DATE relationships.
#
# Network requirement
# -------------------
# If the local OpenML cache (~/.openml/org/openml.org/) is warm for the task
# IDs in your DB, no network calls are made. If the cache is cold, the script
# fetches run metadata from api.openml.org. A 10k-run DB will make O(tasks)
# API calls, not O(runs) — list_runs is batched by task.
#
# Usage
# -----
#   cd /path/to/agentic-rag-kimble
#   ./scripts/backfill-date-dimension.sh [--dry-run] [--batch-size N]
#
#   --dry-run       Print counts without writing to the DB.
#   --batch-size N  Runs to process per task (default: all). Use a small value
#                   (e.g. 500) for a first test pass.
#
# Safety
# ------
# The script is idempotent: it pre-fetches existing date_ids and existing
# RUN_ON_DATE rels so it can be re-run safely. Do NOT run while an ingestion
# job is in progress (LadybugDB: one write transaction at a time).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

exec python3 - "$@" <<'PYEOF'
"""
Backfill helper — invoked by backfill-date-dimension.sh.

Reads every Run's upload_date via openml.runs.list_runs, derives Date rows,
and inserts any missing Date nodes plus RUN_ON_DATE relationships.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("backfill-date-dim")

# ── CLI args ──────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Backfill Date dimension.")
parser.add_argument("--dry-run", action="store_true")
parser.add_argument("--batch-size", type=int, default=None)
args = parser.parse_args()

# ── Imports (after arg parse so --help works without heavy deps) ──────────────

import openml
from openml.tasks import TaskType

from src.config import get_config
from src.graph.db import GraphDB
from src.ingestion.transform import derive_date_row

config = get_config()

# ── Open DB, ensure schema ────────────────────────────────────────────────────

with GraphDB(config) as db:
    db.initialise_schema()  # CREATE ... IF NOT EXISTS — safe to call on old DB

    # Pre-fetch existing date_ids and existing RUN_ON_DATE rels.
    existing_date_ids: set[int] = {
        int(r["pk"])
        for r in db.execute("MATCH (dt:Date) RETURN dt.date_id AS pk")
    }
    existing_run_date_rels: set[int] = {
        int(r["run_id"])
        for r in db.execute(
            "MATCH (r:Run)-[:RUN_ON_DATE]->(:Date) RETURN r.run_id AS run_id"
        )
    }

    # Fetch all run_ids from the graph.
    all_run_ids: list[int] = [
        int(r["run_id"])
        for r in db.execute("MATCH (r:Run) RETURN r.run_id AS run_id")
    ]

    # Identify runs that still need a RUN_ON_DATE relationship.
    pending_run_ids = [rid for rid in all_run_ids if rid not in existing_run_date_rels]

    logger.info(
        "Runs total: %d | already linked: %d | pending: %d",
        len(all_run_ids),
        len(existing_run_date_rels),
        len(pending_run_ids),
    )

    if not pending_run_ids:
        logger.info("Nothing to do — all runs already have a RUN_ON_DATE link.")
        sys.exit(0)

    if args.dry_run:
        logger.info("Dry run: would process %d runs.", len(pending_run_ids))
        sys.exit(0)

    # ── Interleaved fetch + write, per chunk ─────────────────────────────────
    #
    # Rewritten 2026-05-15 after the original buffered-all-then-write design
    # hung 9h on a timeout-less OpenML call and lost the entire run.
    #
    # Properties of this version:
    #   * Per-chunk HTTP timeout (ThreadPoolExecutor.result(timeout=...)).  A
    #     stalled OpenML request can no longer freeze the whole job — the chunk
    #     is skipped and retried on the next run.
    #   * Writes Date nodes + RUN_ON_DATE rels immediately after each chunk's
    #     fetch, then CHECKPOINTs periodically.  A crash/hang loses at most one
    #     chunk; everything already written survives.
    #   * Resumable: pending_run_ids already excludes runs that have a
    #     RUN_ON_DATE rel, so simply re-running the script continues where it
    #     stopped.  Smaller chunk size = finer resumability granularity.
    #   * Parameterised Cypher writes (no string interpolation / escaping bugs).

    import concurrent.futures

    CHUNK = args.batch_size or 100
    FETCH_TIMEOUT_S = 90        # per-chunk OpenML deadline
    CHECKPOINT_EVERY_CHUNKS = 20

    inserted_dates = 0
    inserted_rels = 0
    skipped_no_date = 0
    timed_out_chunks = 0
    total = len(pending_run_ids)

    _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def _fetch_chunk(ids: list[int]):
        df = openml.runs.list_runs(id=ids, output_format="dataframe")
        out: dict[int, str | None] = {}
        if not df.empty and "run_id" in df.columns:
            for _, row in df.iterrows():
                rid = int(row["run_id"])
                raw = str(row.get("upload_time") or "").strip()
                out[rid] = raw[:10] if raw else None
        return out

    n_chunks = (total + CHUNK - 1) // CHUNK
    for ci, start in enumerate(range(0, total, CHUNK), 1):
        chunk = pending_run_ids[start : start + CHUNK]

        fut = _pool.submit(_fetch_chunk, chunk)
        try:
            run_upload = fut.result(timeout=FETCH_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            timed_out_chunks += 1
            logger.warning(
                "chunk %d/%d timed out after %ds — skipping (will retry on resume)",
                ci, n_chunks, FETCH_TIMEOUT_S,
            )
            fut.cancel()
            continue
        except Exception as exc:
            logger.warning("chunk %d/%d fetch failed: %s — skipping", ci, n_chunks, exc)
            continue

        for rid in chunk:
            upload_date = run_upload.get(rid)
            if not upload_date:
                skipped_no_date += 1
                continue
            date_row = derive_date_row(upload_date)
            if date_row is None:
                skipped_no_date += 1
                continue
            date_id = int(date_row["date_id"])

            if date_id not in existing_date_ids:
                cols = ", ".join(f"{k}: ${k}" for k in date_row)
                db.execute_write(
                    f"CREATE (dt:Date {{{cols}}})", dict(date_row)
                )
                existing_date_ids.add(date_id)
                inserted_dates += 1

            db.execute_write(
                "MATCH (r:Run {run_id: $rid}), (dt:Date {date_id: $did}) "
                "CREATE (r)-[:RUN_ON_DATE]->(dt)",
                {"rid": int(rid), "did": date_id},
            )
            inserted_rels += 1

        if ci % CHECKPOINT_EVERY_CHUNKS == 0:
            db._run("CHECKPOINT")
            logger.info(
                "progress: chunk %d/%d | dates+%d rels+%d skipped %d timeouts %d (checkpointed)",
                ci, n_chunks, inserted_dates, inserted_rels,
                skipped_no_date, timed_out_chunks,
            )

    db._run("CHECKPOINT")
    _pool.shutdown(wait=False, cancel_futures=True)

    logger.info(
        "Done. Date nodes inserted: %d | RUN_ON_DATE rels created: %d | "
        "runs skipped (no date): %d | chunks timed out: %d",
        inserted_dates, inserted_rels, skipped_no_date, timed_out_chunks,
    )
    if timed_out_chunks:
        logger.info(
            "%d chunk(s) timed out — re-run this script to pick them up "
            "(resumable: already-linked runs are skipped).",
            timed_out_chunks,
        )

PYEOF
