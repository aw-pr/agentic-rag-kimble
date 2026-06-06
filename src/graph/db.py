"""
GraphDB — LadybugDB connection and lifecycle manager.

Provides a single connection per process lifetime, schema initialisation,
and a read-only execute() that rejects write operations.

Backed by LadybugDB (Cypher-compatible embedded property graph).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import ladybug

from src.config import Config
from src.graph.schema import create_schema, drop_schema

# Words that indicate a write operation. Matched as whole words so that
# identifiers like "created_at" do not trigger a false positive.
# Single authoritative source — imported by src.retrieval.graph_tool.
_WRITE_KEYWORD_RE = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|DROP|REMOVE)\b",
    re.IGNORECASE,
)


class GraphDB:
    """Manages a single LadybugDB connection for the lifetime of a process."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._db: ladybug.Database | None = None
        self._conn: ladybug.Connection | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Open the LadybugDB instance at config.ladybug_db_path (creates parent dir)."""
        db_path: Path = self._config.ladybug_db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = ladybug.Database(str(db_path))
        self._conn = ladybug.Connection(self._db)
        self._load_vector_extension()

    def _load_vector_extension(self) -> None:
        """Install and load the VECTOR extension. Idempotent — safe to call on every connect."""
        assert self._conn is not None
        try:
            self._conn.execute("INSTALL VECTOR")
        except Exception:
            # Already installed — swallow "already installed" and similar errors.
            pass
        self._conn.execute("LOAD EXTENSION VECTOR")

    def close(self) -> None:
        """Close the connection and release the database handle."""
        self._conn = None
        self._db = None

    def __enter__(self) -> "GraphDB":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── Query execution ────────────────────────────────────────────────────

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Run a read-only query and return rows as list of dicts.

        Raises PermissionError if the query contains any write keyword
        (CREATE, MERGE, DELETE, SET, DROP — whole-word match, case-insensitive).
        """
        if _WRITE_KEYWORD_RE.search(query):
            raise PermissionError(
                "Read-only execute() called with write query. "
                "Use execute_write() for mutations."
            )
        return self._run(query, params)

    def execute_write(self, query: str, params: dict[str, Any] | None = None) -> None:
        """Run a write query (CREATE/MERGE/COPY). Used by ingestion only."""
        self._run(query, params)

    def _run(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict]:
        """Internal: execute query, return rows as list of plain dicts."""
        assert self._conn is not None, "Call connect() before executing queries."
        if params:
            result = self._conn.execute(query, parameters=params)
        else:
            result = self._conn.execute(query)
        # A single Cypher statement yields one QueryResult (a list is only
        # returned for multi-statement queries, which _run never issues).
        assert not isinstance(result, list), (
            "Multi-statement query passed to _run; expected a single result."
        )
        df = result.get_as_df()
        if df.empty:
            return []
        return df.to_dict(orient="records")

    # ── Schema management ──────────────────────────────────────────────────

    def initialise_schema(self) -> None:
        """Create all node tables, rel tables, and vector indexes if they don't exist.

        Idempotent: safe to call on a database that already has the schema.
        """
        assert self._conn is not None, "Call connect() before initialise_schema()."
        try:
            create_schema(self._conn)
        except RuntimeError:
            # Belt-and-braces: create_schema already handles idempotency internally,
            # but catch any unexpected RuntimeError at this level too.
            pass

    def reset_schema(self) -> None:
        """Drop and recreate all tables. Destructive — ingestion use only."""
        assert self._conn is not None, "Call connect() before reset_schema()."
        drop_schema(self._conn)
        create_schema(self._conn)

    # ── Helpers ────────────────────────────────────────────────────────────

    def node_count(self, table: str) -> int:
        """Return count of nodes in the given table."""
        rows = self.execute(f"MATCH (n:{table}) RETURN count(n) AS cnt")
        if not rows:
            return 0
        return int(rows[0]["cnt"])
