#!/usr/bin/env bash
# backfill-algorithm-families.sh
#
# Promotes Algorithm.family into first-class AlgorithmFamily nodes for an
# existing LadybugDB that was ingested before Stage 3 (the AlgorithmFamily
# outrigger dimension).
#
# What it does
# ------------
#   1. Ensures the AlgorithmFamily node table and BELONGS_TO_FAMILY relationship
#      table exist (idempotent CREATE ... IF NOT EXISTS via schema.create_schema).
#   2. Upserts the 9 curated AlgorithmFamily nodes from FAMILY_METADATA.
#   3. For each Algorithm node whose flow_id does not yet have a
#      BELONGS_TO_FAMILY relationship, creates one based on Algorithm.family.
#   4. Computes description embeddings for all AlgorithmFamily nodes that lack
#      a description_embedding and writes them via the LadybugDB vector store.
#
# Usage
# -----
#   cd /path/to/agentic-rag-kimble
#   ./scripts/backfill-algorithm-families.sh [--dry-run]
#
#   --dry-run   Print counts and actions without writing to the DB.
#
# Safety
# ------
# The script is idempotent: it pre-checks existing AlgorithmFamily nodes and
# existing BELONGS_TO_FAMILY relationships before writing.  Re-running it will
# only fill in gaps.  Do NOT run while an ingestion job is in progress
# (LadybugDB: one write transaction at a time).
#
# Network requirement
# -------------------
# No network access is required.  All family metadata is curated in
# src/ingestion/family_metadata.py.  Embedding computation uses the local
# sentence-transformers model (BAAI/bge-small-en-v1.5, ~130 MB).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

exec python3 - "$@" <<'PYEOF'
"""
Backfill helper — invoked by backfill-algorithm-families.sh.

Upserts AlgorithmFamily nodes, creates BELONGS_TO_FAMILY relationships, and
computes description embeddings for the new family nodes.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("backfill-algorithm-families")

# ── CLI args ──────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Backfill AlgorithmFamily dimension.")
parser.add_argument("--dry-run", action="store_true")
args = parser.parse_args()

# ── Imports ───────────────────────────────────────────────────────────────────

from src.config import get_config
from src.graph.db import GraphDB
from src.ingestion.family_metadata import FAMILY_METADATA, get_family_row

config = get_config()


def _cypher_value(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


# ── Open DB, ensure schema ────────────────────────────────────────────────────

with GraphDB(config) as db:
    db.initialise_schema()  # CREATE ... IF NOT EXISTS — safe on existing DB

    # ── 1. Upsert AlgorithmFamily nodes ───────────────────────────────────────

    existing_family_names: set[str] = set()
    for r in db.execute("MATCH (f:AlgorithmFamily) RETURN f.family_name AS fn"):
        v = r.get("fn")
        if v is not None and isinstance(v, str):
            existing_family_names.add(v)

    logger.info(
        "AlgorithmFamily nodes: %d existing, %d in FAMILY_METADATA",
        len(existing_family_names), len(FAMILY_METADATA),
    )

    families_to_insert = [s for s in FAMILY_METADATA if s not in existing_family_names]
    if families_to_insert:
        logger.info("Will insert %d new AlgorithmFamily nodes: %s", len(families_to_insert), families_to_insert)
        if not args.dry_run:
            for slug in families_to_insert:
                fprops = get_family_row(slug)
                kv = ", ".join(
                    f"{k}: {_cypher_value(v)}"
                    for k, v in fprops.items()
                    if v is not None
                )
                try:
                    db.execute_write(f"CREATE (n:AlgorithmFamily {{{kv}}})")
                    logger.info("  Inserted: %s", slug)
                except Exception as exc:
                    logger.warning("  Failed to insert %s: %s", slug, exc)
    else:
        logger.info("All AlgorithmFamily nodes already present.")

    # ── 2. Create BELONGS_TO_FAMILY relationships ─────────────────────────────

    existing_rels: set[int] = {
        int(r["flow_id"])
        for r in db.execute(
            "MATCH (a:Algorithm)-[:BELONGS_TO_FAMILY]->(:AlgorithmFamily) "
            "RETURN a.flow_id AS flow_id"
        )
    }

    all_algorithms = db.execute(
        "MATCH (a:Algorithm) RETURN a.flow_id AS flow_id, a.family AS family"
    )

    pending_algos = [
        r for r in all_algorithms
        if int(r["flow_id"]) not in existing_rels
    ]

    logger.info(
        "Algorithms: %d total | %d already have BELONGS_TO_FAMILY | %d pending",
        len(all_algorithms),
        len(existing_rels),
        len(pending_algos),
    )

    if not pending_algos:
        logger.info("Nothing to do — all algorithms already have a BELONGS_TO_FAMILY link.")
    elif not args.dry_run:
        inserted_rels = 0
        for row in pending_algos:
            flow_id = int(row["flow_id"])
            family_raw = row.get("family")
            family_slug = family_raw if isinstance(family_raw, str) else "other"
            fam_id = get_family_row(family_slug)["family_id"]
            try:
                db.execute_write(
                    f"MATCH (a:Algorithm {{flow_id: {flow_id}}}), "
                    f"(f:AlgorithmFamily {{family_id: {fam_id}}}) "
                    f"CREATE (a)-[:BELONGS_TO_FAMILY]->(f)"
                )
                inserted_rels += 1
            except Exception as exc:
                logger.warning("BELONGS_TO_FAMILY failed (flow=%d): %s", flow_id, exc)
        logger.info("Created %d BELONGS_TO_FAMILY relationships.", inserted_rels)

    # ── 3. Compute embeddings for AlgorithmFamily nodes ───────────────────────

    if args.dry_run:
        logger.info("Dry run: skipping embedding computation.")
        sys.exit(0)

    logger.info("Computing description embeddings for AlgorithmFamily nodes ...")

    from src.retrieval.embedder import Embedder
    from src.retrieval.vector_store import VectorStore

    embedder = Embedder(config)
    store = VectorStore(config, embedder)
    store.connect()

    family_entities = db.execute(
        "MATCH (f:AlgorithmFamily) "
        "RETURN f.family_id AS family_id, f.description AS description"
    )

    embeddable = [
        {"family_id": int(r["family_id"]), "description": r["description"]}
        for r in family_entities
        if r.get("description") and isinstance(r["description"], str)
    ]

    if embeddable:
        store.index_entities("AlgorithmFamily", embeddable)
        logger.info("Embedded %d AlgorithmFamily nodes.", len(embeddable))
    else:
        logger.info("No AlgorithmFamily nodes with descriptions found to embed.")

logger.info("Backfill complete.")

PYEOF
