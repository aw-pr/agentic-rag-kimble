#!/usr/bin/env bash
# Backfill `display_name` (and a re-synthesised `description`) on existing
# Algorithm nodes in the LadybugDB graph.
#
# Idempotent: rows whose display_name is already non-null are skipped.
# Run once after schema change adds the display_name column.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -u <<'PY'
from src.config import get_config
from src.graph.db import GraphDB
from src.ingestion.transform import derive_display_name, synthesise_description

cfg = get_config()
with GraphDB(cfg) as db:
    # Step 1 — ensure the column exists.  LadybugDB raises if it already does;
    # swallow that exception so the script remains idempotent.
    try:
        db.execute_write("ALTER TABLE Algorithm ADD display_name STRING DEFAULT NULL")
        print("Added display_name column.")
    except Exception as exc:
        msg = str(exc).lower()
        if any(s in msg for s in ("already exists", "duplicate", "already has property")):
            print("display_name column already present.")
        else:
            raise

    # Step 2 — read all algorithms.
    rows = db.execute("MATCH (a:Algorithm) RETURN a.flow_id AS flow_id, a.name AS name, a.family AS family, a.display_name AS existing")
    total = len(rows)
    print(f"Found {total} Algorithm rows.")

    updated = 0
    skipped = 0
    for i, row in enumerate(rows, 1):
        # LadybugDB returns NULL STRING columns as float('nan'), not None.
        # Only treat a real non-empty string as "already populated".
        existing = row.get("existing")
        if isinstance(existing, str) and existing:
            skipped += 1
            continue

        flow_id = int(row["flow_id"])
        fqcn = row["name"]
        family = row["family"]
        display = derive_display_name(fqcn)
        description = synthesise_description(
            "Algorithm",
            {"name": fqcn, "display_name": display, "family": family},
        )

        # Escape single quotes for Cypher literal.
        def _q(s: str) -> str:
            return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"

        db.execute_write(
            f"MATCH (a:Algorithm {{flow_id: {flow_id}}}) "
            f"SET a.display_name = {_q(display)}, a.description = {_q(description)}"
        )
        updated += 1
        if i % 100 == 0:
            print(f"  {i}/{total} processed ({updated} updated, {skipped} skipped)")

    print(f"\nBackfill complete: {updated} updated, {skipped} skipped, {total} total.")

    # Spot check
    sample = db.execute(
        "MATCH (a:Algorithm) "
        "RETURN a.name AS name, a.display_name AS display "
        "LIMIT 8"
    )
    print("\nSample:")
    for r in sample:
        d = r["display"] if isinstance(r["display"], str) else "<null>"
        print(f"  {d:30s} ← {r['name']}")
PY
