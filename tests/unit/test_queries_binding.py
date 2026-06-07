"""
Tests for the parameterised write fragments in src/graph/queries.py.

These cover the binding that replaced the pass-26 string-interpolated SET
(the suspected ingest-segfault path). Every payload is stored as a bound
$parameter, so the assertions prove two things at once:

1. Round-trip fidelity — long non-ASCII / quote / backslash / control text
   comes back byte-for-byte, with no Cypher-escaping corruption.
2. Injection safety — a description crafted to look like trailing Cypher does
   not mutate any other property; it is stored verbatim as data.

Uses pytest's tmp_path fixture for an isolated DB per test — never touches
data/ladybug_db.
"""

from __future__ import annotations

import pytest

from src.config import Config
from src.graph.db import GraphDB
from src.graph.queries import SET_ALGORITHM_DESCRIPTION, SET_DATASET_DESCRIPTION


def _make_db(tmp_path) -> GraphDB:
    cfg = Config(ladybug_db_path=tmp_path / "ladybug_test")
    return GraphDB(cfg)


def _init_for_writes(db: GraphDB) -> None:
    """Initialise the schema, then drop every vector index.

    LadybugDB rejects SET on a column that backs a vector index (the same
    constraint the backfill works around by dropping the index as its first
    step). The description write fragments NULL description_embedding, so the
    index must be gone before they run.
    """
    db.initialise_schema()
    for row in db._run("CALL SHOW_INDEXES() RETURN table_name, index_name"):
        try:
            db._run(
                f"CALL DROP_VECTOR_INDEX('{row['table_name']}', '{row['index_name']}')"
            )
        except Exception:
            pass


def _seed_algorithm(db: GraphDB, flow_id: int = 7, name: str = "SeedAlgo") -> None:
    db.execute_write(
        "CREATE (a:Algorithm {flow_id: $flow_id, name: $name, family: 'tree', "
        "description: 'seed', description_embedding: null})",
        {"flow_id": flow_id, "name": name},
    )


def _seed_dataset(db: GraphDB, dataset_id: int = 11, name: str = "SeedData") -> None:
    db.execute_write(
        "CREATE (d:Dataset {dataset_id: $dataset_id, name: $name, "
        "description: 'seed', description_embedding: null})",
        {"dataset_id": dataset_id, "name": name},
    )


# Payloads that the old string-interpolated SET had to escape by hand. Each must
# round-trip exactly through parameterised binding.
_PAYLOADS = {
    "non_ascii": "café résumé naïve 数据集 算法 🚀📊 — μοντέλο",
    "quotes": "it's a \"quoted\" value with 'single' and \"double\" marks",
    "backslashes": r"path C:\temp\x and a regex \d+\s*\\ trailing",
    "control_chars": "line1\nline2\ttab\r carriage and  doublespace",
    "long_unicode": "λ-emble " * 400,  # ~3.2 KB of repeated non-ASCII
    "cypher_lookalike": "'} ) SET a.name = 'PWNED' // trailing comment",
    "injection_delete": "x'}) DETACH DELETE a //",
}


@pytest.mark.parametrize("label,payload", list(_PAYLOADS.items()))
def test_algorithm_description_round_trips(tmp_path, label, payload):
    """Parameterised SET stores the description verbatim and mutates nothing else."""
    with _make_db(tmp_path) as db:
        _init_for_writes(db)
        _seed_algorithm(db, flow_id=7, name="SeedAlgo")

        db.execute_write(
            SET_ALGORITHM_DESCRIPTION, {"flow_id": 7, "description": payload}
        )

        rows = db.execute(
            "MATCH (a:Algorithm {flow_id: 7}) "
            "RETURN a.description AS description, a.name AS name"
        )
        assert len(rows) == 1, f"{label}: node should still exist as a single row"
        assert rows[0]["description"] == payload, f"{label}: description not byte-exact"
        # Injection safety: the name property is untouched regardless of payload.
        assert rows[0]["name"] == "SeedAlgo", f"{label}: payload mutated another property"


@pytest.mark.parametrize("label,payload", list(_PAYLOADS.items()))
def test_dataset_description_round_trips(tmp_path, label, payload):
    """Same guarantees for the Dataset write fragment."""
    with _make_db(tmp_path) as db:
        _init_for_writes(db)
        _seed_dataset(db, dataset_id=11, name="SeedData")

        db.execute_write(
            SET_DATASET_DESCRIPTION, {"dataset_id": 11, "description": payload}
        )

        rows = db.execute(
            "MATCH (d:Dataset {dataset_id: 11}) "
            "RETURN d.description AS description, d.name AS name"
        )
        assert len(rows) == 1, f"{label}: node should still exist"
        assert rows[0]["description"] == payload, f"{label}: description not byte-exact"
        assert rows[0]["name"] == "SeedData", f"{label}: payload mutated another property"


def test_injection_payload_does_not_delete_node(tmp_path):
    """A DETACH-DELETE-shaped description must leave the node (and its peers) intact."""
    with _make_db(tmp_path) as db:
        _init_for_writes(db)
        _seed_algorithm(db, flow_id=1, name="KeepMe")
        _seed_algorithm(db, flow_id=2, name="AlsoKeep")

        db.execute_write(
            SET_ALGORITHM_DESCRIPTION,
            {"flow_id": 1, "description": "'}) DETACH DELETE a //"},
        )

        assert db.node_count("Algorithm") == 2, "injection-shaped payload deleted a node"


def test_description_embedding_nulled_for_reembed(tmp_path):
    """The write fragment must NULL description_embedding so build-index re-embeds it."""
    with _make_db(tmp_path) as db:
        _init_for_writes(db)
        # Seed with a non-null embedding so the NULL-ing is observable.
        emb = "[" + ",".join("0.1" for _ in range(384)) + "]"
        db.execute_write(
            f"CREATE (a:Algorithm {{flow_id: 9, name: 'Embedded', family: 'tree', "
            f"description: 'seed', description_embedding: {emb}}})"
        )

        db.execute_write(
            SET_ALGORITHM_DESCRIPTION, {"flow_id": 9, "description": "fresh text"}
        )

        # The re-embed pass finds work via `description_embedding IS NULL`.
        # (LadybugDB materialises the nulled FLOAT[384] as NaN in pandas, but
        # the Cypher IS NULL predicate is the contract build-index relies on.)
        nulled = db.execute(
            "MATCH (a:Algorithm {flow_id: 9}) "
            "WHERE a.description_embedding IS NULL "
            "RETURN a.flow_id AS flow_id"
        )
        assert len(nulled) == 1, "embedding should read as NULL so build-index re-embeds it"
