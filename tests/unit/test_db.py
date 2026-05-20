"""
Integration tests for src/graph/db.py.

Uses pytest's tmp_path fixture for an isolated DB per test.
Does NOT touch data/kuzu_db/.
"""

from __future__ import annotations

import pytest

from src.config import Config
from src.graph.db import GraphDB


def _make_db(tmp_path) -> GraphDB:
    """Helper: return a GraphDB configured to use tmp_path."""
    cfg = Config(kuzu_db_path=tmp_path / "kuzu_test")
    return GraphDB(cfg)


# ── Tests ──────────────────────────────────────────────────────────────────


def test_schema_initialises_cleanly(tmp_path):
    """After initialise_schema(), every node table should exist and be empty."""
    with _make_db(tmp_path) as db:
        db.initialise_schema()
        for table in ("Run", "Algorithm", "Dataset", "Task"):
            assert db.node_count(table) == 0, (
                f"Expected 0 nodes in {table} after fresh init"
            )


def test_schema_is_idempotent(tmp_path):
    """Calling initialise_schema() twice must not raise."""
    with _make_db(tmp_path) as db:
        db.initialise_schema()
        db.initialise_schema()  # must not raise


def test_execute_rejects_write_queries(tmp_path):
    """execute() must raise PermissionError for any query with a write keyword."""
    write_queries = [
        "CREATE (n:Run {run_id: 1})",
        "MERGE (n:Algorithm {flow_id: 1})",
        "MATCH (n:Run) DELETE n",
        "MATCH (n:Run) SET n.accuracy = 0.9",
        "DROP TABLE Run",
    ]
    with _make_db(tmp_path) as db:
        db.initialise_schema()
        for q in write_queries:
            with pytest.raises(PermissionError):
                db.execute(q)


def test_execute_rejects_write_keywords_case_insensitive(tmp_path):
    """Write-keyword check must be case-insensitive and word-boundary aware."""
    with _make_db(tmp_path) as db:
        db.initialise_schema()
        # Lower-case write keyword
        with pytest.raises(PermissionError):
            db.execute("create (n:Run {run_id: 1})")
        # Identifier containing keyword substring should NOT be rejected
        # e.g. a property name like "created_at" — but we test a safe MATCH here
        rows = db.execute("MATCH (n:Algorithm) RETURN n.flow_id AS flow_id")
        assert isinstance(rows, list)


def test_execute_write_creates_node(tmp_path):
    """execute_write() should insert a node; node_count should reflect it."""
    with _make_db(tmp_path) as db:
        db.initialise_schema()
        db.execute_write(
            "CREATE (a:Algorithm {flow_id: 42, name: 'TestAlgo', "
            "family: 'tree', description: 'A test algorithm', "
            "description_embedding: null})"
        )
        assert db.node_count("Algorithm") == 1


def test_context_manager_closes_cleanly(tmp_path):
    """Using GraphDB as a context manager should not raise on exit."""
    db = _make_db(tmp_path)
    with db:
        db.initialise_schema()
    # After __exit__, internal handles should be None
    assert db._conn is None
    assert db._db is None


def test_vector_extension_loads_on_connect(tmp_path):
    """After connect(), the VECTOR extension should be available.

    We verify by attempting CREATE_VECTOR_INDEX on the schema — if the
    extension isn't loaded the call would raise "function not defined".
    """
    with _make_db(tmp_path) as db:
        db.initialise_schema()
        # If VECTOR extension loaded correctly, this won't raise.
        # The index may already exist (initialise_schema creates it), so
        # we catch "already exists" which also proves the function is defined.
        try:
            db._run(
                "CALL CREATE_VECTOR_INDEX('Algorithm', 'test_vec_idx', 'description_embedding')"
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "already exist" in msg or "already been created" in msg:
                pass  # Function exists — extension is loaded.
            else:
                raise
