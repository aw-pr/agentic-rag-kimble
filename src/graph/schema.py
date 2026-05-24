"""
LadybugDB schema definitions for the agentic-rag-kimble graph.

NODE_TABLES  : table_name → [(col_name, ladybug_type)]
REL_TABLES   : [(rel_name, from_table, to_table)]
VECTOR_INDEXES: [(table_name, column_name, dimension)]

The primary-key column is always listed first and must be INT64.
Embedding dimension for all-MiniLM-L6-v2 is 384.

AlgorithmFamily (Stage 3) — outrigger / snowflaked sub-dimension.
STRING primary keys are not supported by LadybugDB (INT64 required). A
deterministic hash of the family slug is used as family_id instead. The slug
is stored in family_name for human readability and FK joins.
"""

from __future__ import annotations

NODE_TABLES: dict[str, list[tuple[str, str]]] = {
    "Run": [
        ("run_id", "INT64"),
        ("accuracy", "DOUBLE"),
        ("f1", "DOUBLE"),
        ("auc", "DOUBLE"),
        ("runtime_sec", "DOUBLE"),
        ("memory_mb", "DOUBLE"),
        ("setup_id", "INT64"),
    ],
    "Date": [
        ("date_id", "INT64"),          # YYYYMMDD primary key
        ("date", "STRING"),            # ISO "YYYY-MM-DD"
        ("year", "INT64"),
        ("quarter", "STRING"),         # e.g. "2024-Q1"
        ("month", "INT64"),
        ("month_name", "STRING"),      # e.g. "March"
        ("day_of_week", "STRING"),     # e.g. "Monday"
        ("is_weekend", "BOOLEAN"),
    ],
    "Algorithm": [
        ("flow_id", "INT64"),
        ("name", "STRING"),
        ("display_name", "STRING"),
        ("family", "STRING"),
        ("paradigm", "STRING"),
        ("is_ensemble", "BOOLEAN"),
        ("training_cost_class", "STRING"),
        ("description", "STRING"),
        ("description_embedding", "FLOAT[384]"),
    ],
    "Dataset": [
        ("dataset_id", "INT64"),
        ("name", "STRING"),
        ("n_rows", "INT64"),
        ("n_features", "INT64"),
        ("n_classes", "INT64"),
        ("imbalance_ratio", "DOUBLE"),
        ("size_bucket", "STRING"),
        ("dim_bucket", "STRING"),
        ("imbalance_bucket", "STRING"),
        ("domain_tags", "STRING"),
        ("description", "STRING"),
        ("description_embedding", "FLOAT[384]"),
    ],
    "Task": [
        ("task_id", "INT64"),
        ("task_type", "STRING"),
        ("target_feature", "STRING"),
        ("evaluation_measure", "STRING"),
        ("description", "STRING"),
        ("description_embedding", "FLOAT[384]"),
    ],
    "AlgorithmFamily": [
        # family_id: deterministic INT64 hash of the family slug (STRING PKs are
        # not supported by LadybugDB — INT64 is required for all primary keys).
        ("family_id", "INT64"),
        ("family_name", "STRING"),          # slug, e.g. "tree_ensemble"
        ("display_name", "STRING"),          # human label, e.g. "Tree ensembles"
        ("paradigm", "STRING"),
        ("interpretability", "STRING"),      # one of: high, medium, low
        ("typical_use_case", "STRING"),
        ("description", "STRING"),
        ("description_embedding", "FLOAT[384]"),
    ],
}

REL_TABLES: list[tuple[str, str, str]] = [
    ("USED_ALGORITHM", "Run", "Algorithm"),
    ("ON_DATASET", "Run", "Dataset"),
    ("FOR_TASK", "Run", "Task"),
    ("PART_OF_TASK", "Dataset", "Task"),
    ("RUN_ON_DATE", "Run", "Date"),
    ("BELONGS_TO_FAMILY", "Algorithm", "AlgorithmFamily"),
]

# (table_name, index_name, column_name, embedding_dimension)
# Column must be FLOAT[384] — see NODE_TABLES above (description_embedding).
# Pass-26 history: the Algorithm index name went through two recovery renames
# after WAL phantoms from forcibly-killed backfills. Current canonical name is
# 'description_embedding_vec_idx_Algo_v2'. Future fresh-DB rebuilds will use
# this name; older DBs may still reference earlier names if they exist.
VECTOR_INDEXES: list[tuple[str, str, str, int]] = [
    ("Algorithm", "description_embedding_vec_idx_Algo_v2", "description_embedding", 384),
    ("Dataset", "description_embedding_vec_idx", "description_embedding", 384),
    ("Task", "description_embedding_vec_idx", "description_embedding", 384),
    ("AlgorithmFamily", "description_embedding_vec_idx", "description_embedding", 384),
]

# Primary-key column per node table (first column by convention above)
_PRIMARY_KEYS: dict[str, str] = {
    "Run": "run_id",
    "Algorithm": "flow_id",
    "Dataset": "dataset_id",
    "Task": "task_id",
    "Date": "date_id",
    "AlgorithmFamily": "family_id",
}


def _col_defs(cols: list[tuple[str, str]], pk: str) -> str:
    parts = []
    for name, ktype in cols:
        constraint = " PRIMARY KEY" if name == pk else ""
        parts.append(f"{name} {ktype}{constraint}")
    return ", ".join(parts)


def create_schema(conn) -> None:
    """Create all node tables, relationship tables, and vector indexes in *conn*.

    Thin backward-compatibility wrapper. The actual DDL now lives in
    ``src.graph.migrations`` and is driven by an ordered migration runner
    with version tracking inside the database itself. See
    ``docs/MIGRATIONS.md`` for the recipe used by new migrations.

    Behaviour is unchanged for callers: this remains idempotent and
    creates the full Kimball schema on a fresh database.
    """
    # Imported here to avoid a circular import: the initial migration
    # reads NODE_TABLES / REL_TABLES / VECTOR_INDEXES from this module.
    from src.graph.migrations import run_migrations

    run_migrations(conn)


def drop_schema(conn) -> None:
    """Drop vector indexes, relationship tables, then node tables (order matters)."""
    # Vector indexes must be dropped before their node tables can be dropped.
    existing_idx_res = conn.execute("CALL SHOW_INDEXES() RETURN table_name, index_name")
    existing_df = existing_idx_res.get_as_df()
    if not existing_df.empty:
        for _, row in existing_df.iterrows():
            try:
                conn.execute(
                    f"CALL DROP_VECTOR_INDEX('{row['table_name']}', '{row['index_name']}')"
                )
            except RuntimeError:
                pass  # Already gone

    for rel_name, _, _ in reversed(REL_TABLES):
        conn.execute(f"DROP TABLE IF EXISTS {rel_name}")
    for table in NODE_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    # The migration runner's version-tracking table is part of the schema
    # too; drop it so a subsequent create_schema() replays migration 0001.
    conn.execute("DROP TABLE IF EXISTS SchemaVersion")
