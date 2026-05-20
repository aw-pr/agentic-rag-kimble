#!/usr/bin/env bash
# rebuild-algorithm-table.sh
#
# Recovers the corrupt Algorithm node table (pass-26 storage corruption:
# SET on any Algorithm row segfaults in lbug::storage::NodeTable::initUpdateState,
# independent of the vector index).
#
# Reads are safe, so all data is salvaged and the table rebuilt clean:
#
#   Phase A  Export Algorithm rows + USED_ALGORITHM edges to JSONL (resumable;
#            skipped if export files already exist).
#   Phase B  Drop vector index, USED_ALGORITHM, BELONGS_TO_FAMILY, Algorithm;
#            checkpoint; initialise_schema (recreates clean tables + index defs).
#   Phase C  Reload 1,588 Algorithm nodes (parameterised CREATE — no escaping bugs).
#   Phase D  Relink USED_ALGORITHM edges via UNWIND batches, checkpoint every 20k.
#   Phase E  Recompute Algorithm description embeddings + recreate vector index.
#
# After this, Stage 1 (Algo half) / Stage 3 / Stage 4 backfills SET cleanly.
#
# Idempotent-ish: Phase A is resumable. Phases B-E are not — they assume a
# corrupt-but-readable Algorithm table as input. Always snapshot first.
#
# Usage:  ./scripts/rebuild-algorithm-table.sh

set -euo pipefail
cd "$(dirname "$0")/.."

EXPORT_DIR="runs/backfill-2026-05-14/export"
mkdir -p "$EXPORT_DIR"

python3 -u - <<'PY'
import json
import os
import sys
import time

from src.config import get_config
from src.graph.db import GraphDB
from src.retrieval.vector_store import _index_name

EXPORT_DIR = "runs/backfill-2026-05-14/export"
ALGOS_FILE = os.path.join(EXPORT_DIR, "algos.jsonl")
EDGES_FILE = os.path.join(EXPORT_DIR, "used_algo.jsonl")

ALGO_IDX = _index_name("Algorithm")  # description_embedding_vec_idx_Algo_v2

cfg = get_config()


def log(msg: str) -> None:
    print(f"[rebuild] {time.strftime('%F %T')} {msg}", flush=True)


with GraphDB(cfg) as db:

    # ── Phase A — export (safe reads) ────────────────────────────────────────
    if os.path.exists(ALGOS_FILE) and os.path.exists(EDGES_FILE):
        log(f"Phase A skipped — export files already exist in {EXPORT_DIR}")
    else:
        log("Phase A — exporting Algorithm rows ...")
        algos = db.execute(
            "MATCH (a:Algorithm) "
            "RETURN a.flow_id AS flow_id, a.name AS name, a.family AS family, "
            "a.description AS description, a.display_name AS display_name"
        )
        with open(ALGOS_FILE, "w") as fh:
            for r in algos:
                fh.write(json.dumps(r) + "\n")
        log(f"  wrote {len(algos)} Algorithm rows -> {ALGOS_FILE}")

        log("Phase A — exporting USED_ALGORITHM edges ...")
        edges = db.execute(
            "MATCH (r:Run)-[:USED_ALGORITHM]->(a:Algorithm) "
            "RETURN r.run_id AS run_id, a.flow_id AS flow_id"
        )
        with open(EDGES_FILE, "w") as fh:
            for r in edges:
                fh.write(json.dumps(r) + "\n")
        log(f"  wrote {len(edges)} USED_ALGORITHM edges -> {EDGES_FILE}")

    # Load exports into memory.
    with open(ALGOS_FILE) as fh:
        algos = [json.loads(line) for line in fh if line.strip()]
    with open(EDGES_FILE) as fh:
        edges = [json.loads(line) for line in fh if line.strip()]
    log(f"Loaded {len(algos)} algos, {len(edges)} edges from export.")

    if not algos:
        log("ERROR: no Algorithm rows exported — aborting before destructive phase.")
        sys.exit(1)

    # ── Phase B — drop & recreate structure ──────────────────────────────────
    log("Phase B — dropping Algorithm vector index ...")
    try:
        db._run(f"CALL DROP_VECTOR_INDEX('Algorithm', '{ALGO_IDX}')")
        log("  index dropped")
    except Exception as exc:
        log(f"  index drop skipped: {exc}")

    log("Phase B — dropping USED_ALGORITHM / BELONGS_TO_FAMILY / Algorithm ...")
    db._run("DROP TABLE IF EXISTS USED_ALGORITHM")
    db._run("DROP TABLE IF EXISTS BELONGS_TO_FAMILY")
    db._run("DROP TABLE IF EXISTS Algorithm")
    db._run("CHECKPOINT")
    log("  dropped + checkpointed")

    log("Phase B — recreating schema ...")
    db.initialise_schema()
    assert db.node_count("Algorithm") == 0, "Algorithm not empty after recreate"
    log("  schema recreated, Algorithm table empty and clean")

    # ── Phase C — reload Algorithm nodes (parameterised) ─────────────────────
    log(f"Phase C — reloading {len(algos)} Algorithm nodes ...")
    for i, a in enumerate(algos, 1):
        db.execute_write(
            "CREATE (n:Algorithm {flow_id:$flow_id, name:$name, family:$family, "
            "description:$description, display_name:$display_name})",
            {
                "flow_id": int(a["flow_id"]),
                "name": a.get("name"),
                "family": a.get("family"),
                "description": a.get("description"),
                "display_name": a.get("display_name"),
            },
        )
        if i % 500 == 0:
            log(f"  nodes {i}/{len(algos)}")
    db._run("CHECKPOINT")
    log(f"  reloaded {db.node_count('Algorithm')} Algorithm nodes")

    # ── Phase D — relink USED_ALGORITHM (UNWIND batches) ─────────────────────
    log(f"Phase D — relinking {len(edges)} USED_ALGORITHM edges ...")
    BATCH = 2000
    done = 0
    for start in range(0, len(edges), BATCH):
        chunk = edges[start:start + BATCH]
        rows = [
            {"rid": int(e["run_id"]), "fid": int(e["flow_id"])}
            for e in chunk
        ]
        db.execute_write(
            "UNWIND $rows AS row "
            "MATCH (r:Run {run_id:row.rid}), (a:Algorithm {flow_id:row.fid}) "
            "CREATE (r)-[:USED_ALGORITHM]->(a)",
            {"rows": rows},
        )
        done += len(chunk)
        if (start // BATCH) % 10 == 0:
            db._run("CHECKPOINT")
            log(f"  edges {done}/{len(edges)} (checkpointed)")
    db._run("CHECKPOINT")
    final_edges = db.execute(
        "MATCH (:Run)-[x:USED_ALGORITHM]->(:Algorithm) RETURN count(x) AS c"
    )[0]["c"]
    log(f"  relinked {final_edges} USED_ALGORITHM edges")
    if final_edges != len(edges):
        log(f"  WARNING: edge count {final_edges} != exported {len(edges)}")

    # ── Phase E — re-embed + recreate vector index ───────────────────────────
    log("Phase E — recomputing Algorithm description embeddings ...")
    from src.retrieval.embedder import Embedder
    from src.retrieval.vector_store import VectorStore

    embedder = Embedder(cfg)
    store = VectorStore(cfg, embedder)
    store.connect()
    embeddable = [
        {"flow_id": int(a["flow_id"]), "description": a["description"]}
        for a in algos
        if a.get("description")
    ]
    store.index_entities("Algorithm", embeddable)
    log(f"  embedded {len(embeddable)} Algorithm nodes + recreated vector index")

    # ── Final sanity: SET must now succeed ───────────────────────────────────
    probe_fid = int(algos[0]["flow_id"])
    db.execute_write(
        f"MATCH (a:Algorithm {{flow_id:{probe_fid}}}) SET a.paradigm='__rebuild_probe__'"
    )
    db.execute_write(
        f"MATCH (a:Algorithm {{flow_id:{probe_fid}}}) SET a.paradigm=NULL"
    )
    db._run("CHECKPOINT")
    log("  SET probe on rebuilt Algorithm table SUCCEEDED — corruption cleared")

log("[rebuild] DONE — Algorithm table rebuilt clean.")
PY
