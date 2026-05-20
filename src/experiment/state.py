"""Task state model and pure transition functions for the experiment scaffold.

Why this exists
---------------
The heartbeat tick (`scripts/experiment-tick.sh`) is dumb shell. All the
judgement about *what state a task should move to next* lives in this
module, behind a pure interface that takes a task plus a system
observation (which pids are alive, which log files have been touched)
and returns the next task. Keeping the transitions pure makes them
testable without spawning subprocesses.

States mirror docs/EXPERIMENT-PLAN.md section C exactly; the tests in
tests/unit/experiment/test_state.py codify each transition in that
table.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    STALLED = "stalled"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"


TERMINAL_STATES: frozenset[TaskState] = frozenset({TaskState.DONE, TaskState.FAILED})


@dataclass
class Task:
    """One row of the queue. Fields match the YAML schema in section C of
    the plan; optional runtime fields are populated as the task moves."""

    id: str
    phase: int
    tier: str
    worker_pref: str
    verifier_pref: str
    depends_on: list[str] = field(default_factory=list)
    state: TaskState = TaskState.QUEUED
    attempts: int = 0
    max_attempts: int = 2
    expected_subagents: int = 1
    brief_path: str | None = None
    acceptance: list[str] = field(default_factory=list)
    # Runtime fields, set by the tick when the task starts running.
    pid: int | None = None
    started_at: float | None = None
    last_heartbeat: float | None = None
    result_path: str | None = None
    blocked_reason: str | None = None


def load(path: Path) -> list[Task]:
    """Read the queue from a YAML file. Returns [] if the file is empty
    or missing."""
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or []
    return [_task_from_dict(t) for t in raw]


def save(tasks: list[Task], path: Path) -> None:
    """Write the queue to a YAML file. State enum is serialised as its
    string value so the YAML stays human-readable."""
    data = [_task_to_dict(t) for t in tasks]
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _task_from_dict(d: dict[str, Any]) -> Task:
    kwargs = dict(d)
    if "state" in kwargs:
        kwargs["state"] = TaskState(kwargs["state"])
    return Task(**kwargs)


def _task_to_dict(t: Task) -> dict[str, Any]:
    d = asdict(t)
    d["state"] = t.state.value
    return d


def deps_met(task: Task, tasks: list[Task]) -> bool:
    """A task is eligible to run when every task it depends on is `done`."""
    by_id = {t.id: t for t in tasks}
    return all(
        d in by_id and by_id[d].state == TaskState.DONE for d in task.depends_on
    )


def mark_stalled(task: Task) -> Task:
    """A running task whose pid died or whose log mtime exceeds the stall
    window. Caller (the tick) decides which signal triggered this."""
    if task.state != TaskState.RUNNING:
        raise ValueError(f"task {task.id} not running (state={task.state.value})")
    task.state = TaskState.STALLED
    return task


def retry_or_block(task: Task) -> Task:
    """A stalled task: increment attempts; retry if budget remains,
    otherwise block for triage."""
    if task.state != TaskState.STALLED:
        raise ValueError(f"task {task.id} not stalled (state={task.state.value})")
    task.attempts += 1
    if task.attempts < task.max_attempts:
        task.state = TaskState.QUEUED
        task.pid = None
        task.started_at = None
        task.last_heartbeat = None
    else:
        task.state = TaskState.BLOCKED
        task.blocked_reason = task.blocked_reason or "exhausted-retries"
    return task


def mark_running(task: Task, pid: int, started_at: float) -> Task:
    """Tick has spawned this task. Record the pid and start time so the
    next tick can detect stalls."""
    if task.state != TaskState.QUEUED:
        raise ValueError(f"task {task.id} not queued (state={task.state.value})")
    task.state = TaskState.RUNNING
    task.pid = pid
    task.started_at = started_at
    task.last_heartbeat = started_at
    return task


def mark_done(task: Task) -> Task:
    """Worker wrote result.json and acceptance criteria passed and
    verifier returned PASS. Terminal."""
    if task.state not in {TaskState.RUNNING, TaskState.STALLED}:
        raise ValueError(f"task {task.id} cannot complete from {task.state.value}")
    task.state = TaskState.DONE
    return task


def mark_failed(task: Task, reason: str) -> Task:
    """Triage decided abort, or hard error in the tick. Terminal."""
    task.state = TaskState.FAILED
    task.blocked_reason = reason
    return task


def is_terminal(task: Task) -> bool:
    """Done or failed tasks are not touched again."""
    return task.state in TERMINAL_STATES
