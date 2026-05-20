"""Tests for src/experiment/state.py.

Why this exists
---------------
The heartbeat tick relies on these transitions being correct, and the
plan's section C table is the source of truth. Each test here pins one
row of that table or one IO invariant, so a change that breaks the
state machine breaks a test with a docstring naming the broken row.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.experiment.state import (
    TERMINAL_STATES,
    Task,
    TaskState,
    deps_met,
    is_terminal,
    load,
    mark_done,
    mark_failed,
    mark_running,
    mark_stalled,
    retry_or_block,
    save,
)


def _task(**kw) -> Task:
    """Build a Task with the required positional fields defaulted so
    each test names only what it cares about."""
    defaults: dict[str, object] = dict(
        id="t1",
        phase=0,
        tier="T2",
        worker_pref="sonnet",
        verifier_pref="gpt-5.5",
    )
    defaults.update(kw)
    return Task(**defaults)


# ---- IO round-trip ----------------------------------------------------------


def test_save_then_load_preserves_task(tmp_path: Path) -> None:
    """A queue written and re-read is structurally identical, including
    the TaskState enum value."""
    path = tmp_path / "state.yaml"
    save([_task(state=TaskState.RUNNING, pid=123, attempts=1)], path)
    [reloaded] = load(path)
    assert reloaded.state == TaskState.RUNNING
    assert reloaded.pid == 123
    assert reloaded.attempts == 1


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    """A missing state.yaml is a normal cold-start condition, not an error."""
    assert load(tmp_path / "absent.yaml") == []


def test_load_empty_file_returns_empty(tmp_path: Path) -> None:
    """An empty (touched but unwritten) state.yaml is also a normal
    initialisation state."""
    path = tmp_path / "state.yaml"
    path.write_text("")
    assert load(path) == []


# ---- deps_met --------------------------------------------------------------


def test_deps_met_when_no_dependencies() -> None:
    """A task with no dependencies is always eligible."""
    assert deps_met(_task(depends_on=[]), [])


def test_deps_met_when_all_done() -> None:
    """All declared dependencies are in TaskState.DONE."""
    dep = _task(id="dep", state=TaskState.DONE)
    consumer = _task(id="c", depends_on=["dep"])
    assert deps_met(consumer, [dep, consumer])


def test_deps_not_met_when_a_dep_still_running() -> None:
    """If any dep is not yet `done`, the consumer is not eligible."""
    dep = _task(id="dep", state=TaskState.RUNNING)
    consumer = _task(id="c", depends_on=["dep"])
    assert not deps_met(consumer, [dep, consumer])


def test_deps_not_met_when_a_dep_id_is_unknown() -> None:
    """A missing dep id is treated as not-met. A typo in a brief should
    not silently let the consumer run."""
    consumer = _task(id="c", depends_on=["does-not-exist"])
    assert not deps_met(consumer, [consumer])


# ---- mark_stalled ----------------------------------------------------------


def test_mark_stalled_from_running() -> None:
    """Plan section C: running -> stalled when pid dead or log mtime old."""
    t = mark_stalled(_task(state=TaskState.RUNNING, pid=42))
    assert t.state == TaskState.STALLED


def test_mark_stalled_rejects_non_running() -> None:
    """Refusing this transition from `queued` or `done` is how we
    catch a tick logic bug early."""
    with pytest.raises(ValueError):
        mark_stalled(_task(state=TaskState.QUEUED))


# ---- retry_or_block --------------------------------------------------------


def test_stalled_retries_when_under_budget() -> None:
    """Plan section C: stalled -> queued when attempts < max_attempts.
    Runtime fields are cleared so the next spawn starts clean."""
    t = retry_or_block(
        _task(
            state=TaskState.STALLED,
            attempts=0,
            max_attempts=2,
            pid=99,
            started_at=1.0,
            last_heartbeat=1.5,
        )
    )
    assert t.state == TaskState.QUEUED
    assert t.attempts == 1
    assert t.pid is None
    assert t.started_at is None
    assert t.last_heartbeat is None


def test_stalled_blocks_when_retry_budget_exhausted() -> None:
    """Plan section C: stalled -> blocked when attempts == max_attempts
    after this increment. `blocked_reason` is populated for triage."""
    t = retry_or_block(_task(state=TaskState.STALLED, attempts=1, max_attempts=2))
    assert t.state == TaskState.BLOCKED
    assert t.blocked_reason == "exhausted-retries"


def test_stalled_block_preserves_existing_reason() -> None:
    """If the worker set a more specific blocked_reason before stalling
    (e.g. runaway-subagents), we keep it rather than overwrite."""
    t = retry_or_block(
        _task(
            state=TaskState.STALLED,
            attempts=1,
            max_attempts=2,
            blocked_reason="runaway-subagents",
        )
    )
    assert t.blocked_reason == "runaway-subagents"


# ---- mark_running ----------------------------------------------------------


def test_mark_running_records_pid_and_times() -> None:
    """The tick records the pid and start time so the next tick can
    detect stalls."""
    t = mark_running(_task(state=TaskState.QUEUED), pid=2024, started_at=100.0)
    assert t.state == TaskState.RUNNING
    assert t.pid == 2024
    assert t.started_at == 100.0
    assert t.last_heartbeat == 100.0


def test_mark_running_rejects_non_queued() -> None:
    """Running a task that's already running or terminal is a logic bug."""
    with pytest.raises(ValueError):
        mark_running(_task(state=TaskState.DONE), pid=1, started_at=0.0)


# ---- mark_done / mark_failed -----------------------------------------------


def test_mark_done_from_running() -> None:
    """Worker wrote result.json and verifier returned PASS."""
    assert mark_done(_task(state=TaskState.RUNNING)).state == TaskState.DONE


def test_mark_done_from_stalled_allowed() -> None:
    """A worker that returned just before the stall window closed is
    still a valid completion."""
    assert mark_done(_task(state=TaskState.STALLED)).state == TaskState.DONE


def test_mark_done_rejects_terminal() -> None:
    """Cannot re-complete a done task."""
    with pytest.raises(ValueError):
        mark_done(_task(state=TaskState.DONE))


def test_mark_failed_records_reason() -> None:
    """Triage's abort decision must always carry a reason."""
    t = mark_failed(_task(state=TaskState.BLOCKED), "triage-aborted")
    assert t.state == TaskState.FAILED
    assert t.blocked_reason == "triage-aborted"


# ---- terminal set ----------------------------------------------------------


def test_terminal_set_matches_plan() -> None:
    """Plan section C names DONE and FAILED as terminal. If this set
    changes, every consumer that filters by it (tick, dashboard, status
    reader) must change with it."""
    assert TERMINAL_STATES == frozenset({TaskState.DONE, TaskState.FAILED})


def test_is_terminal_for_each_state() -> None:
    """One row per TaskState, so adding a new state can never silently
    skip the terminal check."""
    expected = {
        TaskState.QUEUED: False,
        TaskState.RUNNING: False,
        TaskState.STALLED: False,
        TaskState.BLOCKED: False,
        TaskState.DONE: True,
        TaskState.FAILED: True,
    }
    for state, expect in expected.items():
        assert is_terminal(_task(state=state)) is expect, state
