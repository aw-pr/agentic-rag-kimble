"""
Schema-migration runner for the LadybugDB graph.

Design (deliberately minimal):

* Version tracking lives in the graph itself, in a single-row node table
  called ``SchemaVersion``. The row's ``applied`` column holds the highest
  migration number that has been applied to this database.
* Migrations are Python modules in this package named ``NNNN_short_name.py``
  and exposing a module-level ``up(conn)`` function. The runner discovers
  them via filesystem scan, sorts by the leading integer, and applies any
  whose number is strictly greater than ``SchemaVersion.applied``.
* There are no down-migrations. LadybugDB is a single-writer embedded
  store; rollback would mean restoring a file copy of the database
  directory rather than running reverse DDL, so a ``down(conn)`` callback
  would buy us nothing. If multi-writer support ever lands this becomes
  worth revisiting.
* The runner is idempotent. Calling ``run_migrations(conn)`` against a
  fully-migrated database is a no-op (one tiny version-row read).

LadybugDB-specific notes:

* The VECTOR extension must be loaded once per process — that is the
  caller's job (``GraphDB.connect`` already does it before any migration
  runs).
* ``SET`` against an indexed column is rejected by LadybugDB. The version
  row is therefore upserted with a DELETE-then-CREATE pair rather than a
  ``MATCH ... SET``.
* Single-writer: callers must not run two migration processes in parallel
  against the same database directory. There is no file-level lock here;
  LadybugDB's own file lock is the backstop.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Callable, Protocol

_MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.py$")


class _MigrationModule(Protocol):
    def up(self, conn: object) -> None: ...  # pragma: no cover


# ── Version table ──────────────────────────────────────────────────────────

def _ensure_version_table(conn) -> None:
    """Create the single-row SchemaVersion node table if absent.

    The row's primary key is hard-coded to 1; there is only ever one
    version row per database.
    """
    conn.execute(
        "CREATE NODE TABLE IF NOT EXISTS SchemaVersion ("
        "version_id INT64 PRIMARY KEY, applied INT64)"
    )


def _current_version(conn) -> int:
    """Return the highest applied migration number, or 0 if none applied."""
    _ensure_version_table(conn)
    res = conn.execute(
        "MATCH (v:SchemaVersion {version_id: 1}) RETURN v.applied AS applied"
    )
    df = res.get_as_df()
    if df.empty:
        return 0
    val = df.iloc[0]["applied"]
    # Pandas can hand us a NaN for a NULL column; treat as 0.
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _record_version(conn, number: int) -> None:
    """Upsert the SchemaVersion row to ``number``.

    LadybugDB rejects ``SET`` against indexed columns (and the primary key
    counts), so we DELETE-then-CREATE the single row rather than mutate it
    in place.
    """
    conn.execute("MATCH (v:SchemaVersion {version_id: 1}) DELETE v")
    conn.execute(
        f"CREATE (:SchemaVersion {{version_id: 1, applied: {int(number)}}})"
    )


# ── Discovery ──────────────────────────────────────────────────────────────

def _discover_migrations(
    directory: Path | None = None,
) -> list[tuple[int, str, Callable[[object], None]]]:
    """Return migrations sorted by number as ``(number, name, up_fn)`` tuples."""
    pkg_dir = directory or Path(__file__).parent
    found: list[tuple[int, str, Callable[[object], None]]] = []
    for entry in sorted(pkg_dir.iterdir()):
        if not entry.is_file():
            continue
        match = _MIGRATION_FILENAME_RE.match(entry.name)
        if not match:
            continue
        number = int(match.group(1))
        name = match.group(2)
        module = importlib.import_module(f"{__name__}.{entry.stem}")
        up_fn = getattr(module, "up", None)
        if up_fn is None:
            raise RuntimeError(
                f"Migration {entry.name} is missing an up(conn) function"
            )
        found.append((number, name, up_fn))
    # Sort by number; reject duplicates which would be ambiguous.
    found.sort(key=lambda t: t[0])
    seen: set[int] = set()
    for number, name, _ in found:
        if number in seen:
            raise RuntimeError(
                f"Duplicate migration number {number:04d} (second name: {name})"
            )
        seen.add(number)
    return found


# ── Public entry point ─────────────────────────────────────────────────────

def run_migrations(conn, *, directory: Path | None = None) -> list[int]:
    """Apply every migration whose number > current SchemaVersion.applied.

    Returns the list of migration numbers that were applied in this call
    (empty if the database was already up-to-date).
    """
    _ensure_version_table(conn)
    current = _current_version(conn)
    applied: list[int] = []
    for number, _name, up_fn in _discover_migrations(directory):
        if number <= current:
            continue
        up_fn(conn)
        _record_version(conn, number)
        applied.append(number)
    return applied
