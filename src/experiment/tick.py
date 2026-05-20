"""Heartbeat tick: the main loop the cron entry calls every 5 minutes.

Why this exists
---------------
This is the experiment scaffold's only piece of imperative logic. Every
state transition, every spawn decision, every retry, every block goes
through here. Pure, time-injected, spawner-injected so the whole loop
is unit-testable against canned observations.

Boundaries:
- All wallclock comes from the caller (`now` parameter). Tests pin time.
- All process IO is behind `SpawnFn` (default = shell_spawn). Tests inject
  a stub that returns canned SpawnResults.
- All filesystem reads (state.yaml, budget.yaml, sentinels, logs) are
  read at the start of the tick; the tick is otherwise pure.
- Returns a `TickReport` summarising what changed. Caller (the shell
  entry) decides whether to log it or print it.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.experiment import observation
from src.experiment.budget import BudgetConfig
from src.experiment.spawn import DEFAULT_SPAWN, SpawnFn, task_workdir
from src.experiment.state import (
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


@dataclass(frozen=True)
class TickReport:
    """Summary of what one tick did. Consumed by experiment-status.sh
    and the Mayor dashboard."""

    now: float
    paused: bool = False
    stopped: bool = False
    transitions: list[str] = field(default_factory=list)
    spawned: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _read_result(task: Task, exp_dir: Path) -> dict[str, object] | None:
    """A running worker is done when its result.json appears. Returns
    parsed JSON or None if no result yet."""
    result_path = task_workdir(exp_dir, task) / "result.json"
    if not result_path.exists():
        return None
    try:
        return json.loads(result_path.read_text())
    except json.JSONDecodeError:
        return None


def _acceptance_passed(result: dict[str, object]) -> bool:
    """Worker writes `{"acceptance": "pass" | "fail", ...}` in result.json.
    Anything that isn't an explicit pass counts as fail."""
    return result.get("acceptance") == "pass"


def _running_count(tasks: list[Task]) -> int:
    return sum(1 for t in tasks if t.state == TaskState.RUNNING)


def tick(
    *,
    state_path: Path,
    budget_path: Path,
    exp_dir: Path,
    now: float | None = None,
    spawn_fn: SpawnFn = DEFAULT_SPAWN,
) -> TickReport:
    """Run one heartbeat cycle.

    1. Load state + budget.
    2. Honour STOP / PAUSE sentinels.
    3. For each running task: check completion, check stall.
    4. For each stalled task: retry or block.
    5. For each blocked task: surface it (triage is a separate task,
       enqueued externally — the tick doesn't auto-create triage entries
       to keep the loop deterministic).
    6. For each queued task whose deps are met: spawn under parallel cap.
    7. Save state. Return the report.
    """
    if now is None:
        now = time.time()
    cfg = BudgetConfig.load(budget_path)
    tasks = load(state_path)
    stopped = observation.stop_requested(exp_dir)
    paused = observation.pause_requested(exp_dir)
    report = TickReport(now=now, paused=paused, stopped=stopped)

    # Snapshot ids of tasks already queued at tick entry. A task that
    # transitions INTO queued during this tick (stall -> retry) must
    # wait at least one heartbeat before spawning — a natural cooldown
    # that stops a broken task from chewing through its attempts in
    # one tick.
    initially_queued = {t.id for t in tasks if t.state == TaskState.QUEUED}

    # ---- 1. Running tasks: completion + stall checks --------------------
    for t in tasks:
        if t.state != TaskState.RUNNING:
            continue
        result = _read_result(t, exp_dir)
        if result is not None:
            if _acceptance_passed(result):
                mark_done(t)
                report.transitions.append(f"{t.id}: running -> done")
                report.completed.append(t.id)
            else:
                # Acceptance failed: treat as a stall so retry/block logic
                # applies (worker may have written a bad result; retry
                # gives it a second chance, then triage).
                mark_stalled(t)
                t.blocked_reason = "acceptance-failed"
                report.transitions.append(f"{t.id}: running -> stalled (acceptance)")
            continue
        log_path = task_workdir(exp_dir, t) / "log"
        if observation.is_stalled(
            pid=t.pid,
            log_path=log_path,
            now=now,
            stall_window_s=cfg.worker_stall_minutes * 60,
            started_at=t.started_at,
        ):
            mark_stalled(t)
            report.transitions.append(f"{t.id}: running -> stalled")

    # ---- 2. Stalled tasks: retry or block ------------------------------
    for t in tasks:
        if t.state != TaskState.STALLED:
            continue
        prev = t.state
        retry_or_block(t)
        report.transitions.append(f"{t.id}: {prev.value} -> {t.state.value}")
        if t.state == TaskState.BLOCKED:
            report.blocked.append(t.id)

    # ---- 3. STOP sentinel: SIGTERM any survivors, mark stopped ---------
    if stopped:
        # The pre-push gate to running workers would be hard kills; doing
        # that lives in scripts/experiment-tick.sh (POSIX kill) rather
        # than here so the Python tick is portable. We just refuse new
        # spawns; the shell entry handles signalling.
        save(tasks, state_path)
        return report

    # ---- 4. New spawns: under PAUSE we skip this section ---------------
    if paused:
        save(tasks, state_path)
        return report

    capacity = cfg.max_parallel_workers - _running_count(tasks)
    for t in tasks:
        if capacity <= 0:
            break
        if t.state != TaskState.QUEUED:
            continue
        if t.id not in initially_queued:
            # Just transitioned into queued this tick (stall -> retry).
            # Skip; will be eligible next tick.
            continue
        if not deps_met(t, tasks):
            continue
        try:
            sr = spawn_fn(task=t, kind="worker", exp_dir=exp_dir, now=now)
        except Exception as exc:  # pragma: no cover - rare path
            report.errors.append(f"spawn failed for {t.id}: {exc}")
            mark_failed(t, f"spawn-error: {exc}")
            continue
        mark_running(t, pid=sr.pid, started_at=sr.started_at)
        report.transitions.append(f"{t.id}: queued -> running (pid={sr.pid})")
        report.spawned.append(t.id)
        capacity -= 1

    save(tasks, state_path)
    return report


def filter_active(tasks: list[Task]) -> list[Task]:
    """Tasks that still need attention (everything not terminal).
    Convenience for the Mayor dashboard."""
    return [t for t in tasks if not is_terminal(t)]
