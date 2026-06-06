# Schema migrations

## What this is and why

The graph schema in `src/graph/schema.py` used to be created via `CREATE ... IF NOT EXISTS` plus a destructive `reset_schema()` nuke-and-recreate path. That worked for a single-developer greenfield project but gave us no version metadata, no ordered upgrade story, and no way to tell whether a long-lived `data/ladybug_db` store (the single LadybugDB file, renamed from the Kùzu-era `kuzu_db` in pass-30) was up-to-date with the code. The migration runner in `src/graph/migrations/` fixes that with the smallest possible footprint: an integer version stored inside the database, a folder of numbered Python modules, and an idempotent runner that applies any migration whose number is newer than the recorded version.

## How to write a new migration

- Pick the next number and create `src/graph/migrations/NNNN_short_name.py` (four-digit zero-padded number, lower-snake-case name).
- Export a single function `def up(conn): ...`. The argument is a live LadybugDB `Connection`; use `conn.execute(...)` directly — do not go through `GraphDB.execute`, which blocks write keywords.
- Keep each migration self-contained: do not import other migrations, and prefer raw DDL over reaching back into `NODE_TABLES`/`REL_TABLES` so the migration history stays readable even if the constants drift.
- Add a one-paragraph docstring explaining *why* the migration exists (the schema change is visible in the diff; the motivation is not).
- If the migration touches existing data, remember that LadybugDB is a single-writer store — there is no transactional rollback. Test on a throwaway copy of the database first.

## How the migrator runs

`run_migrations(conn)` (called automatically from `create_schema`, and therefore from `GraphDB.initialise_schema`) ensures a `SchemaVersion` node table exists, reads its single row to find the highest applied migration number, then applies every migration whose number is strictly greater, in ascending order. Each successful `up()` is followed by an upsert of the version row to that migration's number. Calling the runner against an up-to-date database is a no-op. Migrations within a single call run sequentially against the same connection; a failure leaves the version row at the last successfully-applied number, so a retry resumes from the right place.

## LadybugDB-specific gotchas

- **VECTOR extension is per-process.** The runner does not call `LOAD EXTENSION VECTOR`; `GraphDB.connect()` does that once before the runner is invoked. If you write a migration that creates a vector index, you can assume the extension is loaded — but never assume the migration will be re-run in a fresh process where you might have loaded extensions yourself.
- **`SET` against indexed columns is rejected.** The runner upserts the version row by `DELETE`-then-`CREATE`. If your migration needs to update an indexed value, drop the index first or rewrite the row.
- **Single-writer.** Do not run two migration processes against the same database directory in parallel — LadybugDB's file lock will hold, but the second process will simply fail. The orchestrator and ingestion paths assume serial schema work.
- **`NULL` arrives as `NaN`.** When the version row is brand new, `_current_version` defensively coerces the value; copy that pattern if you read scalar columns in a future migration.

## Worked example

Suppose we want to add a `Hyperparameter` dimension hanging off `Run`:

```python
# src/graph/migrations/0002_hyperparameter_dimension.py
"""
Adds a Hyperparameter dimension and the HAS_HYPERPARAMETER relationship
from Run, so we can ask 'which hyperparameter settings dominate the top
10% of runs on dataset X'.
"""

def up(conn):
    conn.execute(
        "CREATE NODE TABLE IF NOT EXISTS Hyperparameter ("
        "  hp_id INT64 PRIMARY KEY,"
        "  name STRING,"
        "  value STRING,"
        "  description STRING,"
        "  description_embedding FLOAT[384]"
        ")"
    )
    conn.execute(
        "CREATE REL TABLE IF NOT EXISTS HAS_HYPERPARAMETER "
        "(FROM Run TO Hyperparameter)"
    )
    conn.execute(
        "CALL CREATE_VECTOR_INDEX('Hyperparameter', "
        "'description_embedding_vec_idx', 'description_embedding')"
    )
```

The next process to call `GraphDB.initialise_schema()` will pick this up automatically; an existing database will jump from version 1 to version 2, and a fresh database will go from 0 to 2 in one pass.

## Deferred design questions

- No `down(conn)` callback yet. Rollback in a single-writer embedded store is a file-copy operation, not a reverse-DDL one; if multi-writer support ever lands this is the first thing to revisit.
- No checksum on migration contents. The runner trusts the filesystem ordering and the integer in the filename. If we ever need to detect a migration that was edited after being applied, add a SHA256 column to `SchemaVersion` and compare on each run.
