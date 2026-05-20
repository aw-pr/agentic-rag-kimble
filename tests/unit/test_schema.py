"""Unit tests for src/graph/schema.py — no live DB required."""


from src.graph.schema import NODE_TABLES, REL_TABLES, VECTOR_INDEXES

# ── NODE_TABLES ────────────────────────────────────────────────────────────

def test_node_tables_contains_required_tables():
    required = {"Run", "Algorithm", "Dataset", "Task", "Date"}
    assert required <= NODE_TABLES.keys(), (
        f"Missing tables: {required - NODE_TABLES.keys()}"
    )


def test_run_node_has_required_columns():
    cols = {name for name, _ in NODE_TABLES["Run"]}
    assert "run_id" in cols
    assert "accuracy" in cols
    assert "setup_id" in cols


def test_algorithm_node_has_required_columns():
    cols = {name for name, _ in NODE_TABLES["Algorithm"]}
    assert "flow_id" in cols
    assert "name" in cols
    assert "display_name" in cols
    assert "family" in cols
    assert "paradigm" in cols
    assert "is_ensemble" in cols
    assert "training_cost_class" in cols
    assert "description" in cols


def test_dataset_node_has_required_columns():
    cols = {name for name, _ in NODE_TABLES["Dataset"]}
    assert "dataset_id" in cols
    assert "n_rows" in cols
    assert "n_features" in cols
    assert "n_classes" in cols
    assert "size_bucket" in cols
    assert "dim_bucket" in cols
    assert "imbalance_bucket" in cols


def test_task_node_has_required_columns():
    cols = {name for name, _ in NODE_TABLES["Task"]}
    assert "task_id" in cols
    assert "task_type" in cols
    assert "description" in cols


def test_node_tables_only_use_allowed_kuzu_types():
    # FLOAT[384] is the array type used for embedding columns.
    import re
    allowed_scalars = {"INT64", "DOUBLE", "STRING", "BOOLEAN"}
    float_array_re = re.compile(r"^FLOAT\[\d+\]$")
    for table, cols in NODE_TABLES.items():
        for col_name, col_type in cols:
            assert col_type in allowed_scalars or float_array_re.match(col_type), (
                f"{table}.{col_name} uses unexpected type '{col_type}'"
            )


# ── REL_TABLES ─────────────────────────────────────────────────────────────

def test_node_tables_date_has_required_columns():
    cols = {name for name, _ in NODE_TABLES["Date"]}
    assert "date_id" in cols
    assert "date" in cols
    assert "year" in cols
    assert "quarter" in cols
    assert "month" in cols
    assert "month_name" in cols
    assert "day_of_week" in cols
    assert "is_weekend" in cols


def test_node_tables_date_primary_key_is_int64():
    # First column must be date_id INT64
    first_col_name, first_col_type = NODE_TABLES["Date"][0]
    assert first_col_name == "date_id"
    assert first_col_type == "INT64"


def test_rel_tables_contains_required_relationships():
    rel_names = {r[0] for r in REL_TABLES}
    required = {"USED_ALGORITHM", "ON_DATASET", "FOR_TASK", "PART_OF_TASK", "RUN_ON_DATE"}
    assert required <= rel_names, (
        f"Missing relationships: {required - rel_names}"
    )


def test_rel_tables_used_algorithm_direction():
    rel = {r[0]: (r[1], r[2]) for r in REL_TABLES}
    assert rel["USED_ALGORITHM"] == ("Run", "Algorithm")


def test_rel_tables_on_dataset_direction():
    rel = {r[0]: (r[1], r[2]) for r in REL_TABLES}
    assert rel["ON_DATASET"] == ("Run", "Dataset")


def test_rel_tables_for_task_direction():
    rel = {r[0]: (r[1], r[2]) for r in REL_TABLES}
    assert rel["FOR_TASK"] == ("Run", "Task")


def test_rel_tables_part_of_task_direction():
    rel = {r[0]: (r[1], r[2]) for r in REL_TABLES}
    assert rel["PART_OF_TASK"] == ("Dataset", "Task")


def test_rel_tables_run_on_date_direction():
    rel = {r[0]: (r[1], r[2]) for r in REL_TABLES}
    assert rel["RUN_ON_DATE"] == ("Run", "Date")


def test_rel_tables_each_entry_has_three_elements():
    for entry in REL_TABLES:
        assert len(entry) == 3, f"Malformed REL_TABLES entry: {entry}"


# ── VECTOR_INDEXES ─────────────────────────────────────────────────────────

def test_vector_indexes_have_correct_dimension():
    """all-MiniLM-L6-v2 produces 384-dimensional embeddings."""
    for table, idx_name, col, dim in VECTOR_INDEXES:
        assert dim == 384, (
            f"Expected dim=384 for {table}.{col}, got {dim}"
        )


def test_vector_indexes_cover_expected_tables():
    # Vector indexes are on the _embedding columns (FLOAT[384]), not description (STRING).
    indexed = {(t, c) for t, _, c, _ in VECTOR_INDEXES}
    assert ("Algorithm", "description_embedding") in indexed
    assert ("Dataset", "description_embedding") in indexed
    assert ("Task", "description_embedding") in indexed


def test_vector_indexes_columns_exist_in_node_tables():
    for table, _idx_name, col, _ in VECTOR_INDEXES:
        assert table in NODE_TABLES, f"Vector index references unknown table '{table}'"
        col_names = {c for c, _ in NODE_TABLES[table]}
        assert col in col_names, (
            f"Vector index column '{col}' not found in {table} node table"
        )
