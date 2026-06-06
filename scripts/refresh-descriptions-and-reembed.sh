#!/usr/bin/env bash
# refresh-descriptions-and-reembed.sh — Pass 19 one-off migration.
#
# 1. Drop Algorithm and Dataset vector indexes (Task index untouched).
# 2. For each Algorithm row: re-synthesise description with family synonyms,
#    SET description + NULL embedding.
# 3. For each Dataset row: re-synthesise description with qualitative labels,
#    SET description + NULL embedding.
# 4. Print before/after sample descriptions for sanity-check.
# 5. Run build-index.sh to re-embed all rows with BGE-small-en-v1.5 and
#    recreate the vector indexes.
#
# Safe to re-run: always overwrites regardless of current state.
# Progress printed every 100 rows.
#
# Usage:
#   scripts/refresh-descriptions-and-reembed.sh
#
# Environment:
#   LADYBUG_DB_PATH   override default data/ladybug_db  (optional)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Pass 19: refresh descriptions and re-embed with BGE-small ==="
echo ""

python3 -u <<'PY'
import time
import math
from src.config import get_config
from src.graph.db import GraphDB
from src.ingestion.transform import (
    derive_algorithm_family,
    synthesise_description,
)
from src.retrieval.vector_store import _index_name

cfg = get_config()

def _q(s: str) -> str:
    """Escape single quotes for Cypher string literal."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"

def _safe_float(v):
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None

def _safe_int(v, default=0):
    f = _safe_float(v)
    return default if f is None else int(f)

with GraphDB(cfg) as db:

    # ═══════════════════════════════════════════════════════════════════════
    # ALGORITHM REFRESH
    # ═══════════════════════════════════════════════════════════════════════
    print("── Algorithm pass ──")

    # Step 1 — drop Algorithm vector index
    idx_algo = _index_name("Algorithm")
    try:
        db._run(f"CALL DROP_VECTOR_INDEX('Algorithm', '{idx_algo}')")
        print(f"  Dropped Algorithm vector index '{idx_algo}'.")
    except Exception as exc:
        msg = str(exc).lower()
        if any(k in msg for k in ("not exist", "does not exist", "no such")):
            print(f"  Algorithm vector index '{idx_algo}' not found — continuing.")
        else:
            print(f"  Warning: DROP_VECTOR_INDEX raised: {exc}")

    # Step 2 — read all Algorithm rows
    rows = db.execute(
        "MATCH (a:Algorithm) "
        "RETURN a.flow_id AS flow_id, a.name AS name, "
        "       a.display_name AS display_name, a.description AS old_description"
    )
    total = len(rows)
    print(f"  Found {total} Algorithm rows.")

    # Print before sample
    sample_row = next((r for r in rows if "IBk" in str(r.get("name", ""))), rows[0] if rows else None)
    if sample_row:
        print(f"\n  BEFORE sample ({sample_row.get('name', '')}):")
        print(f"    {sample_row.get('old_description', '(no description)')}")

    updated = 0
    start = time.time()
    for i, row in enumerate(rows, 1):
        flow_id = int(row["flow_id"])
        fqcn = row["name"]
        raw_display = row.get("display_name")
        display_name = raw_display if isinstance(raw_display, str) and raw_display else fqcn

        new_family = derive_algorithm_family(fqcn)
        new_desc = synthesise_description(
            "Algorithm",
            {"name": fqcn, "display_name": display_name, "family": new_family},
        )

        db.execute_write(
            f"MATCH (a:Algorithm {{flow_id: {flow_id}}}) "
            f"SET a.family = {_q(new_family)}, "
            f"    a.description = {_q(new_desc)}, "
            f"    a.description_embedding = NULL"
        )
        updated += 1

        if i % 100 == 0 or i == total:
            elapsed = time.time() - start
            print(f"    {i}/{total} rows updated ({elapsed:.1f}s elapsed)", flush=True)

    print(f"\n  Algorithm refresh complete: {updated}/{total} rows updated.")

    # Print after sample
    after_rows = db.execute(
        "MATCH (a:Algorithm) WHERE a.name CONTAINS 'IBk' "
        "RETURN a.description AS descr LIMIT 1"
    )
    if after_rows:
        print(f"\n  AFTER sample (IBk):")
        print(f"    {after_rows[0]['descr']}")

    # Print family distribution
    dist = db.execute(
        "MATCH (a:Algorithm) RETURN a.family AS family, count(a) AS cnt ORDER BY cnt DESC"
    )
    print("\n  Family distribution after refresh:")
    for r in dist:
        print(f"    {r['family']:25s}: {r['cnt']}")

    # ═══════════════════════════════════════════════════════════════════════
    # DATASET REFRESH
    # ═══════════════════════════════════════════════════════════════════════
    print("\n── Dataset pass ──")

    # Step 1 — drop Dataset vector index
    idx_ds = _index_name("Dataset")
    try:
        db._run(f"CALL DROP_VECTOR_INDEX('Dataset', '{idx_ds}')")
        print(f"  Dropped Dataset vector index '{idx_ds}'.")
    except Exception as exc:
        msg = str(exc).lower()
        if any(k in msg for k in ("not exist", "does not exist", "no such")):
            print(f"  Dataset vector index '{idx_ds}' not found — continuing.")
        else:
            print(f"  Warning: DROP_VECTOR_INDEX raised: {exc}")

    # Step 2 — read all Dataset rows
    ds_rows = db.execute(
        "MATCH (d:Dataset) "
        "RETURN d.dataset_id AS dataset_id, d.name AS name, "
        "       d.n_rows AS n_rows, d.n_features AS n_features, "
        "       d.n_classes AS n_classes, d.imbalance_ratio AS imbalance_ratio, "
        "       d.description AS old_description"
    )
    total_ds = len(ds_rows)
    print(f"  Found {total_ds} Dataset rows.")

    # Print before sample (find iris or first small dataset)
    sample_ds = next(
        (r for r in ds_rows if str(r.get("name", "")).lower() == "iris"),
        ds_rows[0] if ds_rows else None
    )
    if sample_ds:
        print(f"\n  BEFORE sample ({sample_ds.get('name', '')}):")
        print(f"    {sample_ds.get('old_description', '(no description)')}")

    updated_ds = 0
    start_ds = time.time()
    for i, row in enumerate(ds_rows, 1):
        dataset_id = int(row["dataset_id"])
        props = {
            "name": str(row["name"]),
            "n_rows": _safe_int(row.get("n_rows"), 0),
            "n_features": _safe_int(row.get("n_features"), 0),
            "n_classes": _safe_int(row.get("n_classes"), 0),
            "imbalance_ratio": _safe_float(row.get("imbalance_ratio")) or 1.0,
        }
        new_desc = synthesise_description("Dataset", props)

        db.execute_write(
            f"MATCH (d:Dataset {{dataset_id: {dataset_id}}}) "
            f"SET d.description = {_q(new_desc)}, "
            f"    d.description_embedding = NULL"
        )
        updated_ds += 1

        if i % 100 == 0 or i == total_ds:
            elapsed = time.time() - start_ds
            print(f"    {i}/{total_ds} rows updated ({elapsed:.1f}s elapsed)", flush=True)

    print(f"\n  Dataset refresh complete: {updated_ds}/{total_ds} rows updated.")

    # Print after sample
    after_ds = db.execute(
        "MATCH (d:Dataset) WHERE d.name = 'iris' "
        "RETURN d.description AS descr LIMIT 1"
    )
    if after_ds:
        print(f"\n  AFTER sample (iris):")
        print(f"    {after_ds[0]['descr']}")

print()
print("=== Description refresh done. Running build-index.sh to re-embed with BGE-small... ===")
print()
PY

bash scripts/build-index.sh

echo ""
echo "=== Pass 19 refresh complete ==="
echo "Run './scripts/run-eval.sh' to evaluate recall@10."
