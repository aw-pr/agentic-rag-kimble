#!/usr/bin/env bash
# Build the LadybugDB native vector index from dimension node descriptions.
#
# For each entity table (Algorithm, Dataset, Task):
#   1. SELECT rows where description IS NOT NULL and description_embedding IS NULL
#   2. Embed each description with sentence-transformers (local CPU)
#   3. SET description_embedding on each node
#   4. CALL CREATE_VECTOR_INDEX (idempotent — skips if already exists)
#
# Idempotent: re-running skips rows that already have an embedding.
# The VECTOR extension is loaded automatically by GraphDB.connect().
#
# Usage:
#   scripts/build-index.sh
#
# Environment:
#   KUZU_DB_PATH   override default data/kuzu_db  (optional)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Building LadybugDB native vector index..."
python3 - <<'PYEOF'
import math

from src.config import get_config
from src.graph.db import GraphDB
from src.retrieval.embedder import Embedder
from src.retrieval.vector_store import _index_name, _PK_FIELD, _NAME_EXPR

cfg = get_config()
embedder = Embedder(cfg)

ENTITIES = [
    ("Algorithm", "flow_id"),
    ("Dataset",   "dataset_id"),
    ("Task",      "task_id"),
]

with GraphDB(cfg) as db:
    for entity_type, pk in ENTITIES:
        name_expr = _NAME_EXPR[entity_type].replace("n.", "n.")

        # ── 1. Count total nodes with descriptions ──────────────────────
        total_rows = db._run(
            f"MATCH (n:{entity_type}) WHERE n.description IS NOT NULL "
            "RETURN count(n) AS cnt"
        )
        total = int(total_rows[0]["cnt"]) if total_rows else 0

        # ── 2. Fetch rows that still need embedding ─────────────────────
        rows = db._run(
            f"MATCH (n:{entity_type}) "
            f"WHERE n.description IS NOT NULL AND n.description_embedding IS NULL "
            f"RETURN n.{pk} AS pk, n.description AS descr "
            "LIMIT 100000"
        )

        # LadybugDB returns NULL strings as float('nan') — filter them out.
        def is_valid_str(v):
            if v is None:
                return False
            try:
                return not math.isnan(float(v))
            except (TypeError, ValueError):
                return True

        rows = [r for r in rows if is_valid_str(r.get("descr")) and isinstance(r.get("descr"), str)]
        to_embed = len(rows)

        print(f"  {entity_type}: {total} nodes with descriptions, {to_embed} needing embedding")

        # ── 3. Drop existing index before SET (LadybugDB constraint) ───────
        # LadybugDB forbids SET on a property that is currently indexed.
        # Drop → SET all → recreate is the required pattern.
        idx_name = _index_name(entity_type)
        try:
            db._run(f"CALL DROP_VECTOR_INDEX('{entity_type}', '{idx_name}')")
            print(f"  {entity_type}: dropped index '{idx_name}' for backfill")
        except Exception:
            pass  # Index didn't exist yet.

        # ── 4. Embed and SET ────────────────────────────────────────────
        for i, row in enumerate(rows, 1):
            vec = embedder.embed_one(row["descr"])
            vec_lit = "[" + ",".join(str(v) for v in vec) + "]"
            pk_val = row["pk"]
            db.execute_write(
                f"MATCH (n:{entity_type} {{{pk}: {pk_val}}}) "
                f"SET n.description_embedding = {vec_lit}"
            )
            if i % 50 == 0 or i == to_embed:
                print(f"    {entity_type}: {i}/{to_embed} embedded", flush=True)

        # ── 5. Recreate vector index ────────────────────────────────────
        try:
            db._run(
                f"CALL CREATE_VECTOR_INDEX('{entity_type}', '{idx_name}', 'description_embedding')"
            )
            print(f"  {entity_type}: vector index '{idx_name}' created")
        except Exception as exc:
            msg = str(exc).lower()
            if "already exist" in msg or "already been created" in msg:
                print(f"  {entity_type}: vector index '{idx_name}' already exists — skipped")
            else:
                raise

        # ── 6. Report final count ───────────────────────────────────────
        count_rows = db._run(
            f"MATCH (n:{entity_type}) WHERE n.description_embedding IS NOT NULL "
            "RETURN count(n) AS cnt"
        )
        indexed = int(count_rows[0]["cnt"]) if count_rows else 0
        print(f"  {entity_type}: {indexed}/{total} nodes now have embeddings")

print()
print("Index complete.")
print("Run 'python3 -m src.eval.metrics' to evaluate recall@10.")
PYEOF
