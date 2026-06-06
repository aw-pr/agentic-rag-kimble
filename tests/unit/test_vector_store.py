"""
Unit tests for src/retrieval/vector_store.py (LadybugDB-backed).

All GraphDB calls are mocked — no on-disk DB required. Covers:
  - index_entities: SETs embedding column and creates the vector index
  - search: returns results with "name" and "score" keys, sorted desc by score
  - collection_count: returns count of non-null embeddings
  - empty-list and empty-collection edge cases
"""

from __future__ import annotations

import numpy as np

from src.config import Config

# ── Embedder tests (unchanged from ChromaDB era) ──────────────────────────────


def test_embed_returns_correct_dimension(mocker):
    """embed_one should return a plain list of 384 floats."""
    mock_model = mocker.MagicMock()
    mock_model.encode.return_value = np.zeros((1, 384), dtype=np.float32)
    mocker.patch("src.retrieval.embedder.SentenceTransformer", return_value=mock_model)

    from src.retrieval.embedder import Embedder

    embedder = Embedder(Config())
    result = embedder.embed_one("test query")

    assert isinstance(result, list), "embed_one must return a plain list"
    assert len(result) == 384, f"Expected 384 dims, got {len(result)}"
    assert all(isinstance(v, float) for v in result), "All values must be floats"


def test_embed_batch_returns_list_of_lists(mocker):
    """embed(texts) must return a list of float lists, not numpy arrays."""
    mock_model = mocker.MagicMock()
    mock_model.encode.return_value = np.ones((3, 384), dtype=np.float32)
    mocker.patch("src.retrieval.embedder.SentenceTransformer", return_value=mock_model)

    from src.retrieval.embedder import Embedder

    embedder = Embedder(Config())
    result = embedder.embed(["a", "b", "c"])

    assert isinstance(result, list)
    assert len(result) == 3
    assert all(isinstance(row, list) for row in result)
    assert all(len(row) == 384 for row in result)


def test_embedder_dimension_property():
    from src.retrieval.embedder import Embedder

    embedder = Embedder(Config())
    assert embedder.dimension == 384


# ── VectorStore helpers ────────────────────────────────────────────────────────


def _make_store(mocker, ladybug_path: str = "/tmp/test_ladybug"):
    """
    Build a VectorStore with fully mocked embedder and GraphDB.

    Returns (store, mock_db, mock_embedder).
    The mock_db has _run and execute_write patched for inspection.
    """
    import pathlib

    from src.retrieval.embedder import Embedder
    from src.retrieval.vector_store import VectorStore

    cfg = Config()
    cfg.ladybug_db_path = pathlib.Path(ladybug_path)

    mock_embedder = mocker.MagicMock(spec=Embedder)
    mock_embedder.embed.return_value = [[0.1] * 384]
    mock_embedder.embed_one.return_value = [0.1] * 384

    # GraphDB is imported lazily inside connect() — patch at the graph.db module level.
    mock_db = mocker.MagicMock()
    mocker.patch("src.graph.db.GraphDB", return_value=mock_db)

    store = VectorStore(cfg, mock_embedder)
    # Bypass connect() — inject mock directly so no real DB is opened.
    store._db = mock_db

    return store, mock_db, mock_embedder


# ── COLLECTION_NAMES backward-compat ──────────────────────────────────────────


def test_collection_names_cover_all_entity_types():
    from src.retrieval.vector_store import COLLECTION_NAMES

    assert set(COLLECTION_NAMES.keys()) == {"Algorithm", "Dataset", "Task"}


def test_collection_names_are_fixed():
    """Collection names must not change — pass 06+ may grep them."""
    from src.retrieval.vector_store import COLLECTION_NAMES

    assert COLLECTION_NAMES["Algorithm"] == "algo_descriptions"
    assert COLLECTION_NAMES["Dataset"] == "dataset_descriptions"
    assert COLLECTION_NAMES["Task"] == "task_descriptions"


# ── index_entities ────────────────────────────────────────────────────────────


def test_index_entities_calls_execute_write_per_entity(mocker):
    """index_entities should call execute_write once per entity with a description."""
    store, mock_db, mock_embedder = _make_store(mocker)

    mock_embedder.embed_one.return_value = [0.1] * 384
    mock_db._run.return_value = []  # CREATE_VECTOR_INDEX returns nothing

    entities = [
        {"flow_id": 1, "name": "RandomForest", "description": "A tree ensemble"},
        {"flow_id": 2, "name": "SVM", "description": "Support vector machine"},
        {"flow_id": 3, "name": "KNN", "description": "K-nearest neighbours"},
    ]

    store.index_entities("Algorithm", entities)

    assert mock_db.execute_write.call_count == 3, (
        f"Expected 3 execute_write calls, got {mock_db.execute_write.call_count}"
    )
    # Each call should include the flow_id and SET
    first_call_args = mock_db.execute_write.call_args_list[0][0][0]
    assert "flow_id: 1" in first_call_args
    assert "SET n.description_embedding" in first_call_args


def test_index_entities_creates_vector_index(mocker):
    """index_entities should call CREATE_VECTOR_INDEX after embedding."""
    store, mock_db, _ = _make_store(mocker)
    mock_db._run.return_value = []

    entities = [{"flow_id": 1, "description": "A tree ensemble"}]
    store.index_entities("Algorithm", entities)

    # _run is called once for CREATE_VECTOR_INDEX
    _run_calls = [str(c) for c in mock_db._run.call_args_list]
    assert any("CREATE_VECTOR_INDEX" in c for c in _run_calls), (
        "Expected CREATE_VECTOR_INDEX call in _run"
    )


def test_index_entities_empty_list_is_noop(mocker):
    """index_entities with an empty list must not call execute_write."""
    store, mock_db, _ = _make_store(mocker)
    store.index_entities("Algorithm", [])
    mock_db.execute_write.assert_not_called()


def test_index_entities_skips_empty_descriptions(mocker):
    """Entities with empty or missing description should be skipped."""
    store, mock_db, _ = _make_store(mocker)
    mock_db._run.return_value = []

    entities = [
        {"flow_id": 1, "description": ""},       # empty — skip
        {"flow_id": 2},                            # missing — skip
        {"flow_id": 3, "description": "Valid"},   # should embed
    ]
    store.index_entities("Algorithm", entities)

    # Only one entity has a valid description.
    assert mock_db.execute_write.call_count == 1


def test_index_entities_swallows_already_exists_error(mocker):
    """CREATE_VECTOR_INDEX 'already exists' errors should be silently ignored."""
    store, mock_db, _ = _make_store(mocker)
    mock_db._run.side_effect = Exception("Index already exists for table Algorithm")

    # Should not raise.
    entities = [{"flow_id": 1, "description": "A tree ensemble"}]
    store.index_entities("Algorithm", entities)


# ── search ────────────────────────────────────────────────────────────────────


def test_search_returns_name_and_score(mocker):
    """search() results must contain 'name' and 'score' keys; score in [0, 1]."""
    store, mock_db, _ = _make_store(mocker)

    # collection_count check
    mock_db._run.side_effect = [
        [{"cnt": 5}],  # collection_count
        [  # QUERY_VECTOR_INDEX result
            {"pk": 1, "entity_name": "RandomForest", "distance": 0.25},
        ],
    ]

    results = store.search("Algorithm", "ensemble method", top_k=5)

    assert len(results) == 1
    assert results[0]["name"] == "RandomForest"
    assert "score" in results[0]
    score = results[0]["score"]
    assert 0.0 <= score <= 1.0, f"Score out of range: {score}"
    assert abs(score - 0.75) < 1e-6


def test_search_results_sorted_descending_by_score(mocker):
    """search() must return results sorted highest score first."""
    store, mock_db, _ = _make_store(mocker)

    mock_db._run.side_effect = [
        [{"cnt": 10}],
        [
            {"pk": 1, "entity_name": "A", "distance": 0.5},
            {"pk": 2, "entity_name": "B", "distance": 0.1},
            {"pk": 3, "entity_name": "C", "distance": 0.8},
        ],
    ]

    results = store.search("Algorithm", "query", top_k=3)
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True), "Results must be sorted desc by score"


def test_search_empty_collection_returns_empty_list(mocker):
    """search() on an empty collection must return [] without calling QUERY_VECTOR_INDEX."""
    store, mock_db, _ = _make_store(mocker)
    mock_db._run.return_value = [{"cnt": 0}]

    results = store.search("Algorithm", "anything", top_k=5)

    assert results == []


def test_search_handles_nan_name(mocker):
    """search() must handle LadybugDB returning float('nan') for NULL name columns."""

    store, mock_db, _ = _make_store(mocker)

    mock_db._run.side_effect = [
        [{"cnt": 1}],
        [{"pk": 1, "entity_name": float("nan"), "distance": 0.3}],
    ]

    results = store.search("Algorithm", "query", top_k=1)
    assert results[0]["name"] == ""


# ── collection_count ──────────────────────────────────────────────────────────


def test_collection_count_returns_correct_value(mocker):
    """collection_count() returns count from LadybugDB."""
    store, mock_db, _ = _make_store(mocker)
    mock_db._run.return_value = [{"cnt": 42}]

    assert store.collection_count("Algorithm") == 42


def test_collection_count_handles_nan_result(mocker):
    """collection_count() must handle LadybugDB returning float('nan') for count."""
    store, mock_db, _ = _make_store(mocker)
    mock_db._run.return_value = [{"cnt": float("nan")}]

    assert store.collection_count("Algorithm") == 0


def test_collection_count_empty_rows(mocker):
    """collection_count() returns 0 if _run returns empty list."""
    store, mock_db, _ = _make_store(mocker)
    mock_db._run.return_value = []

    assert store.collection_count("Algorithm") == 0


# ── Semantic wrapper tests ─────────────────────────────────────────────────────


def test_semantic_search_returns_list(mocker):
    """semantic_search() must return a list (can be empty if store is empty)."""
    mock_store = mocker.MagicMock()
    mock_store.search.return_value = [
        {"name": "RandomForest", "score": 0.9, "flow_id": 1}
    ]
    mocker.patch(
        "src.retrieval.semantic._get_store",
        return_value=mock_store,
    )

    from src.retrieval.semantic import semantic_search

    results = semantic_search("tree ensemble", "Algorithm", top_k=5, config=Config())
    assert isinstance(results, list)
    assert results[0]["name"] == "RandomForest"
    assert "score" in results[0]
