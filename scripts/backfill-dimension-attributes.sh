#!/usr/bin/env bash
# backfill-dimension-attributes.sh
#
# Populates the new Kimball dimension columns added in Stage 1:
#   Dataset  : size_bucket, dim_bucket, imbalance_bucket
#   Algorithm: paradigm, is_ensemble, training_cost_class
#
# Prerequisites
# -------------
#   1. The DB schema must already include the new columns.
#      - Fresh-ingest (./scripts/ingest.sh) gets them automatically.
#      - Existing DBs: LadybugDB 0.16.1+ supports ALTER TABLE ADD COLUMN,
#        so the Python helper below runs the ALTER statements before writing
#        values. Run this script once after upgrading to Stage 1.
#
# Usage
# -----
#   cd /path/to/agentic-rag-kimble
#   ./scripts/backfill-dimension-attributes.sh
#
# The script does NOT re-ingest data and does NOT touch Run, Task, or
# relationship tables. It is safe to re-run — SET is idempotent.
#
# WARNING: Do NOT run against the live DB while an ingestion job is in
# progress. LadybugDB allows only one write transaction at a time.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "[backfill] Starting dimension-attribute backfill..."
python3 - <<'PYEOF'
import sys
sys.path.insert(0, ".")

from src.config import get_config
from src.graph.db import GraphDB
from src.ingestion.transform import (
    derive_size_bucket,
    derive_dim_bucket,
    derive_imbalance_bucket,
    derive_paradigm,
    derive_is_ensemble,
    derive_training_cost_class,
)

config = get_config()

NEW_DATASET_COLS = [
    ("size_bucket",      "STRING"),
    ("dim_bucket",       "STRING"),
    ("imbalance_bucket", "STRING"),
]

NEW_ALGORITHM_COLS = [
    ("paradigm",             "STRING"),
    ("is_ensemble",          "BOOLEAN"),
    ("training_cost_class",  "STRING"),
]


def _col_exists(db: GraphDB, table: str, col: str) -> bool:
    """Return True if *col* is already present in the node table schema."""
    rows = db.execute(f"CALL TABLE_INFO('{table}') RETURN name")
    return any(r["name"] == col for r in rows)


def _ensure_columns(db: GraphDB, table: str, col_defs: list[tuple[str, str]]) -> None:
    """Add columns that do not yet exist to *table* via ALTER TABLE ADD."""
    for col, ctype in col_defs:
        if not _col_exists(db, table, col):
            db.execute_write(f"ALTER TABLE {table} ADD {col} {ctype}")
            print(f"  [schema] Added {table}.{col} ({ctype})")
        else:
            print(f"  [schema] {table}.{col} already present — skipping ALTER")


with GraphDB(config) as db:
    print("[backfill] Ensuring schema columns exist...")
    _ensure_columns(db, "Dataset",   NEW_DATASET_COLS)
    _ensure_columns(db, "Algorithm", NEW_ALGORITHM_COLS)

    # ── Dataset backfill ─────────────────────────────────────────────────────
    print("[backfill] Loading Dataset rows...")
    datasets = db.execute(
        "MATCH (d:Dataset) "
        "RETURN d.dataset_id AS dataset_id, "
        "d.n_rows AS n_rows, "
        "d.n_features AS n_features, "
        "d.imbalance_ratio AS imbalance_ratio"
    )
    print(f"[backfill] Updating {len(datasets)} Dataset nodes...")
    for row in datasets:
        did        = row["dataset_id"]
        n_rows     = int(row["n_rows"] or 0)
        n_features = int(row["n_features"] or 0)
        imbalance  = float(row["imbalance_ratio"] or 1.0)

        size_b    = derive_size_bucket(n_rows).replace("'", "\\'")
        dim_b     = derive_dim_bucket(n_features).replace("'", "\\'")
        imbal_b   = derive_imbalance_bucket(imbalance).replace("'", "\\'")

        db.execute_write(
            f"MATCH (d:Dataset {{dataset_id: {did}}}) "
            f"SET d.size_bucket = '{size_b}', "
            f"    d.dim_bucket = '{dim_b}', "
            f"    d.imbalance_bucket = '{imbal_b}'"
        )
    print(f"[backfill] Dataset done — {len(datasets)} rows updated.")

    # ── Algorithm backfill ────────────────────────────────────────────────────
    #
    # LadybugDB segfaults on SET against an Algorithm node when the vector
    # index 'description_embedding_vec_idx_Algo_v2' is present (verified on
    # 2026-05-14: NodeTable::initUpdateState in the C++ stack).  The Dataset
    # vector index does not exhibit this — likely a residual corruption from
    # the pass-26 incident.  Workaround: drop, SET, recreate.
    from src.retrieval.vector_store import _index_name as _vidx_name
    algo_idx = _vidx_name("Algorithm")
    print(f"[backfill] Dropping Algorithm vector index '{algo_idx}' before SET ...")
    try:
        db._run(f"CALL DROP_VECTOR_INDEX('Algorithm', '{algo_idx}')")
        print("  dropped.")
    except Exception as exc:
        print(f"  drop failed (continuing): {exc}")

    print("[backfill] Loading Algorithm rows...")
    algorithms = db.execute(
        "MATCH (a:Algorithm) "
        "RETURN a.flow_id AS flow_id, "
        "a.name AS name, "
        "a.family AS family"
    )
    print(f"[backfill] Updating {len(algorithms)} Algorithm nodes...", flush=True)
    for i, row in enumerate(algorithms, 1):
        fid       = row["flow_id"]
        flow_name = str(row["name"] or "")
        family    = str(row["family"] or "other")

        paradigm   = derive_paradigm(flow_name, family).replace("'", "\\'")
        is_ens     = "true" if derive_is_ensemble(flow_name, family) else "false"
        cost_class = derive_training_cost_class(flow_name, family).replace("'", "\\'")

        db.execute_write(
            f"MATCH (a:Algorithm {{flow_id: {fid}}}) "
            f"SET a.paradigm = '{paradigm}', "
            f"    a.is_ensemble = {is_ens}, "
            f"    a.training_cost_class = '{cost_class}'"
        )
        if i % 200 == 0:
            print(f"  progress: {i}/{len(algorithms)}", flush=True)
    print(f"[backfill] Algorithm done — {len(algorithms)} rows updated.")

    print(f"[backfill] Recreating Algorithm vector index '{algo_idx}' ...")
    try:
        db._run(
            f"CALL CREATE_VECTOR_INDEX('Algorithm', '{algo_idx}', 'description_embedding')"
        )
        print("  recreated.")
    except Exception as exc:
        print(f"  recreate failed: {exc}")
        raise

print("[backfill] Dimension-attribute backfill complete.")
PYEOF
