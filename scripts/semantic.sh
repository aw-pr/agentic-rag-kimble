#!/usr/bin/env bash
# Quick semantic search against ChromaDB.  Useful for ad-hoc exploration
# while the graph DB is locked by an in-flight ingest, or any time you
# want to test what the agent's semantic_search tool would surface.
#
# Usage:
#   ./scripts/semantic.sh Algorithm "ensemble methods using decision trees"
#   ./scripts/semantic.sh Dataset   "imbalanced binary classification" 10
#   ./scripts/semantic.sh Task      "predicting customer churn"
#
# Args: entity_type (Algorithm|Dataset|Task)  query  [top_k=5]
set -euo pipefail
cd "$(dirname "$0")/.."

ENTITY="${1:?entity_type required: Algorithm | Dataset | Task}"
QUERY="${2:?query string required}"
TOP_K="${3:-5}"

python3 -u - "$ENTITY" "$QUERY" "$TOP_K" <<'PY'
import sys
entity, query, top_k = sys.argv[1], sys.argv[2], int(sys.argv[3])

from src.config import get_config
from src.retrieval.embedder import Embedder
from src.retrieval.vector_store import VectorStore

cfg = get_config()
store = VectorStore(cfg, Embedder(cfg))
store.connect()

results = store.search(entity, query, top_k=top_k)
print(f"\n{entity}  ::  '{query}'")
print("-" * 70)
for r in results:
    name = r["name"] if isinstance(r["name"], str) else "<null>"
    print(f"  {r['score']:.3f}  {name}")
PY
