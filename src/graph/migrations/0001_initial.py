"""
Migration 0001 — initial Kimball schema.

Captures the schema that existed before the migration runner was introduced:
all NODE_TABLES, REL_TABLES, and VECTOR_INDEXES declared in
``src.graph.schema``. Idempotent at the statement level — each DDL uses
``IF NOT EXISTS`` or checks ``SHOW_INDEXES()`` first — so this migration is
safe to re-run on a database that was bootstrapped under the pre-migrator
code path.
"""

from __future__ import annotations

from src.graph.schema import (
    NODE_TABLES,
    REL_TABLES,
    VECTOR_INDEXES,
    _PRIMARY_KEYS,
    _col_defs,
)


def up(conn) -> None:
    for table, cols in NODE_TABLES.items():
        pk = _PRIMARY_KEYS[table]
        conn.execute(
            f"CREATE NODE TABLE IF NOT EXISTS {table} ({_col_defs(cols, pk)})"
        )

    for rel_name, from_table, to_table in REL_TABLES:
        conn.execute(
            f"CREATE REL TABLE IF NOT EXISTS {rel_name} "
            f"(FROM {from_table} TO {to_table})"
        )

    existing_idx_res = conn.execute(
        "CALL SHOW_INDEXES() RETURN table_name, index_name"
    )
    existing_df = existing_idx_res.get_as_df()
    existing_indexes: set[tuple[str, str]] = set()
    if not existing_df.empty:
        for _, row in existing_df.iterrows():
            existing_indexes.add((row["table_name"], row["index_name"]))

    for table, idx_name, col, _dim in VECTOR_INDEXES:
        if (table, idx_name) in existing_indexes:
            continue
        conn.execute(
            f"CALL CREATE_VECTOR_INDEX('{table}', '{idx_name}', '{col}')"
        )
