#!/usr/bin/env bash
# Refresh Algorithm `family` and `description` columns using updated _FAMILY_RULES,
# then null out `description_embedding` so build-index.sh re-embeds from scratch.
#
# Safe to re-run: idempotent (recalculates and overwrites regardless).
#
# Sequence:
#   1. DROP Algorithm vector index (so SET is not blocked)
#   2. For each Algorithm row: recompute family + description, SET both + null embedding
#   3. Print family distribution as a sanity check
#   4. Run build-index.sh to re-embed all Algorithm rows and recreate the index
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Pass 18: refresh Algorithm families + descriptions ==="

python3 -u <<'PY'
import time
from src.config import get_config
from src.graph.db import GraphDB
from src.ingestion.transform import derive_algorithm_family, synthesise_description
from src.retrieval.vector_store import _index_name

cfg = get_config()

def _q(s: str) -> str:
    """Escape single quotes for Cypher string literal."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"

with GraphDB(cfg) as db:
    # Step 1 — drop the Algorithm vector index so SET is allowed
    idx_name = _index_name("Algorithm")
    try:
        db._run(f"CALL DROP_VECTOR_INDEX('Algorithm', '{idx_name}')")
        print(f"Dropped Algorithm vector index '{idx_name}'.")
    except Exception as exc:
        msg = str(exc).lower()
        if "not exist" in msg or "does not exist" in msg or "no such" in msg:
            print(f"Algorithm vector index '{idx_name}' not found — continuing.")
        else:
            print(f"Warning: DROP_VECTOR_INDEX raised unexpected: {exc}")

    # Step 2 — read all Algorithm rows
    rows = db.execute(
        "MATCH (a:Algorithm) "
        "RETURN a.flow_id AS flow_id, a.name AS name, a.display_name AS display_name"
    )
    total = len(rows)
    print(f"Found {total} Algorithm rows to refresh.")

    updated = 0
    start = time.time()
    for i, row in enumerate(rows, 1):
        flow_id = int(row["flow_id"])
        fqcn = row["name"]
        # LadybugDB returns NULL STRING as float('nan')
        raw_display = row.get("display_name")
        display_name = raw_display if isinstance(raw_display, str) and raw_display else fqcn

        new_family = derive_algorithm_family(fqcn)
        new_description = synthesise_description(
            "Algorithm",
            {"name": fqcn, "display_name": display_name, "family": new_family},
        )

        db.execute_write(
            f"MATCH (a:Algorithm {{flow_id: {flow_id}}}) "
            f"SET a.family = {_q(new_family)}, "
            f"    a.description = {_q(new_description)}, "
            f"    a.description_embedding = NULL"
        )
        updated += 1

        elapsed = time.time() - start
        if i % 100 == 0 or elapsed - (i // 100 - 1) * 0 >= 10:
            # Print progress every 100 rows
            if i % 100 == 0:
                print(f"  {i}/{total} rows refreshed ({elapsed:.1f}s elapsed)", flush=True)

    print(f"\nRefresh complete: {updated}/{total} rows updated.")

    # Step 3 — print family distribution
    dist = db.execute(
        "MATCH (a:Algorithm) RETURN a.family AS family, count(a) AS cnt ORDER BY cnt DESC"
    )
    print("\nFamily distribution AFTER refresh:")
    for r in dist:
        print(f"  {r['family']:25s}: {r['cnt']}")

PY

echo ""
echo "=== Running build-index.sh to re-embed Algorithm rows and recreate index ==="
bash scripts/build-index.sh

echo ""
echo "=== Refresh complete. Run 'python3 -m src.eval.metrics' to re-evaluate. ==="
