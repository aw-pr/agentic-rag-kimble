"""
VectorStore — LadybugDB native vector index, one index per dimension entity type.

Replaces the previous ChromaDB implementation. Single store, one lock domain,
native vector+graph in one engine. The public API is unchanged — callers
(semantic.py, semantic_tool.py, eval harness) are unaffected.

Architecture notes
------------------
- `connect()` opens a GraphDB (which auto-loads the VECTOR extension).
- `index_entities()` SETs description_embedding on each node then calls
  CREATE_VECTOR_INDEX. Idempotent — skips already-indexed nodes and swallows
  "index already exists" errors.
- `search()` embeds the query and runs QUERY_VECTOR_INDEX. Returns dicts with
  "name" and "score" keys (score = 1 - distance, so higher is more similar).
- `collection_count()` counts nodes where description_embedding IS NOT NULL.

LadybugDB quirks (from pass-12 build log)
------------------------------------------
- NULL STRING columns come back as float('nan'), not None — check isinstance(x, str).
- NULL FLOAT[N] columns may come back as nan-filled arrays or None — treat
  defensively; check IS NOT NULL in Cypher where possible.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from src.config import Config
from src.retrieval.embedder import Embedder

if TYPE_CHECKING:
    from src.graph.db import GraphDB

# Index names — entity-specific to avoid LadybugDB catalog collision on drop/recreate.
# Pass-26 history: the Algorithm index name went through two recovery renames
# after WAL phantoms from forcibly-killed backfills. Current canonical name is
# 'description_embedding_vec_idx_Algo_v2'. Must match VECTOR_INDEXES in schema.py.
_INDEX_NAMES: dict[str, str] = {
    "Algorithm": "description_embedding_vec_idx_Algo_v2",
    "Dataset": "description_embedding_vec_idx",
    "Task": "description_embedding_vec_idx",
    "AlgorithmFamily": "description_embedding_vec_idx",
}

# Primary-key field per entity type.
_PK_FIELD: dict[str, str] = {
    "Algorithm": "flow_id",
    "Dataset": "dataset_id",
    "Task": "task_id",
    "AlgorithmFamily": "family_id",
}

# Canonical "name" column per entity type (used as the "name" key in results).
_NAME_EXPR: dict[str, str] = {
    "Algorithm": "COALESCE(n.display_name, n.name)",
    "Dataset": "n.name",
    "Task": "n.task_type",
    "AlgorithmFamily": "n.display_name",
}

# Kept for backward-compat — callers that import COLLECTION_NAMES still work.
COLLECTION_NAMES: dict[str, str] = {
    "Algorithm": "algo_descriptions",
    "Dataset": "dataset_descriptions",
    "Task": "task_descriptions",
}


def _vec_literal(vec: list[float]) -> str:
    """Render a Python float list as a Cypher array literal."""
    return "[" + ",".join(str(v) for v in vec) + "]"


def _is_nan(value: Any) -> bool:
    """Return True if value is float('nan') — LadybugDB NULL sentinel."""
    try:
        return math.isnan(value)
    except (TypeError, ValueError):
        return False


class VectorStore:
    """LadybugDB-backed semantic store. One vector index per dimension entity type."""

    def __init__(self, config: Config, embedder: Embedder) -> None:
        self._config = config
        self._embedder = embedder
        self._db: GraphDB | None = None  # set by connect()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Open a GraphDB connection (which auto-loads the VECTOR extension)."""
        from src.graph.db import GraphDB

        self._db = GraphDB(self._config)
        self._db.connect()

    def _ensure_connected(self) -> GraphDB:
        assert self._db is not None, "Call connect() before using VectorStore."
        return self._db

    # ── Indexing ───────────────────────────────────────────────────────────

    def index_entities(
        self,
        entity_type: str,
        entities: list[dict],
    ) -> None:
        """
        Embed descriptions and SET description_embedding on matching nodes.

        After all nodes are updated, CREATE_VECTOR_INDEX is called (idempotent).

        Each entity dict must have at least:
          - "description"  — text to embed
          - <pk>           — primary key (flow_id / dataset_id / task_id)

        The "name" key is optional here; it is read back from the graph at
        search time.
        """
        db = self._ensure_connected()
        if not entities:
            return

        pk_field = _PK_FIELD[entity_type]
        idx_name = _index_name(entity_type)

        # LadybugDB forbids SET on a property that is currently indexed.
        # Pattern: drop the index, SET all embeddings, recreate the index.
        try:
            db._run(
                f"CALL DROP_VECTOR_INDEX('{entity_type}', '{idx_name}')"
            )
        except Exception:
            pass  # Index didn't exist yet — fine.

        for entity in entities:
            description = entity.get("description") or ""
            if not description:
                continue
            vec = self._embedder.embed_one(description)
            vec_lit = _vec_literal(vec)
            pk_val = entity[pk_field]
            # GraphDB.execute_write bypasses the read-only guard.
            db.execute_write(
                f"MATCH (n:{entity_type} {{{pk_field}: {pk_val}}}) "
                f"SET n.description_embedding = {vec_lit}"
            )

        # Recreate the vector index over all now-populated embeddings.
        try:
            db._run(
                f"CALL CREATE_VECTOR_INDEX('{entity_type}', '{idx_name}', 'description_embedding')"
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "already exist" in msg or "already been created" in msg:
                pass  # Should not happen after drop, but safe to swallow.
            else:
                raise

    # ── Search ─────────────────────────────────────────────────────────────

    def search(
        self,
        entity_type: str,
        query: str,
        top_k: int = 10,
    ) -> list[dict]:
        """
        Embed query, run QUERY_VECTOR_INDEX, return top_k results.

        Each result dict: {"name": str, "score": float, <pk>: int}
        Score = 1 - distance (higher is more similar).
        """
        db = self._ensure_connected()

        # Short-circuit if no embeddings exist yet.
        if self.collection_count(entity_type) == 0:
            return []

        pk_field = _PK_FIELD[entity_type]
        idx_name = _index_name(entity_type)
        name_expr = _NAME_EXPR[entity_type]
        query_vec = self._embedder.embed_one(query)
        vec_lit = _vec_literal(query_vec)

        rows = db._run(
            f"CALL QUERY_VECTOR_INDEX('{entity_type}', '{idx_name}', {vec_lit}, {top_k}) "
            f"RETURN node.{pk_field} AS pk, "
            f"{name_expr.replace('n.', 'node.')} AS entity_name, distance"
        )

        results: list[dict] = []
        for row in rows:
            raw_name = row.get("entity_name")
            # Guard against LadybugDB returning float('nan') for NULL strings.
            if raw_name is None or _is_nan(raw_name):
                raw_name = ""
            dist = float(row.get("distance") or 0.0)
            results.append(
                {
                    "name": str(raw_name),
                    "score": float(1.0 - dist),
                    pk_field: row.get("pk"),
                }
            )

        # Descending by score (highest similarity first).
        results.sort(key=lambda r: r["score"], reverse=True)
        return results

    # ── Helpers ────────────────────────────────────────────────────────────

    def collection_count(self, entity_type: str) -> int:
        """Return number of nodes with a non-null description_embedding."""
        db = self._ensure_connected()
        rows = db._run(
            f"MATCH (n:{entity_type}) "
            "WHERE n.description_embedding IS NOT NULL "
            "RETURN count(n) AS cnt"
        )
        if not rows:
            return 0
        val = rows[0].get("cnt", 0)
        if _is_nan(val):
            return 0
        return int(val)


def _index_name(entity_type: str) -> str:
    """Return the vector index name for the given entity type.

    Pass-26: Algorithm uses a distinct name after a WAL corruption event left
    the original 'description_embedding_vec_idx' name unrecoverable for that table.
    Dataset and Task retain the original name for backward compatibility.
    """
    return _INDEX_NAMES.get(entity_type, "description_embedding_vec_idx")
