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
from src.experiment.commit import DEFAULT_COMMIT, CommitFn
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
    mark_verifier_failed,
    mark_verifying,
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
    committed: list[str] = field(default_factory=list)
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
    # Verifying tasks are also holding a subprocess slot; count them against cap.
    return sum(1 for t in tasks if t.state in {TaskState.RUNNING, TaskState.VERIFYING})


def tick(
    *,
    state_path: Path,
    budget_path: Path,
    exp_dir: Path,
    now: float | None = None,
    spawn_fn: SpawnFn = DEFAULT_SPAWN,
    commit_fn: CommitFn | None = None,
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
    if commit_fn is None:
        commit_fn = DEFAULT_COMMIT
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
    # Tasks already in verifying at entry — stall detection is gated on
    # this so a verifier spawned this tick doesn't immediately stall
    # (its pid is real but its log may not be flushed yet).
    initially_verifying = {t.id for t in tasks if t.state == TaskState.VERIFYING}

    # ---- 1. Running tasks: completion + stall checks --------------------
    for t in tasks:
        if t.state != TaskState.RUNNING:
            continue
        result = _read_result(t, exp_dir)
        if result is not None:
            if _acceptance_passed(result):
                # Worker passed; preserve its result before verifier overwrites
                # the same path. Rename to result.worker.json so the verifier
                # writes result.json cleanly and the tick reads that for the
                # verifier verdict.
                workdir = task_workdir(exp_dir, t)
                worker_result_path = workdir / "result.json"
                worker_result_path.rename(workdir / "result.worker.json")
                try:
                    sr = spawn_fn(task=t, kind="verifier", exp_dir=exp_dir, now=now)
                except Exception as exc:  # pragma: no cover - rare path
                    report.errors.append(f"verifier spawn failed for {t.id}: {exc}")
                    mark_failed(t, f"verifier-spawn-error: {exc}")
                    continue
                mark_verifying(t, pid=sr.pid, started_at=sr.started_at)
                report.transitions.append(f"{t.id}: running -> verifying (pid={sr.pid})")
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

    # ---- 1b. Verifying tasks: verifier completion + stall checks --------
    for t in tasks:
        if t.state != TaskState.VERIFYING:
            continue
        result = _read_result(t, exp_dir)
        if result is not None:
            if _acceptance_passed(result):
                mark_done(t)
                report.transitions.append(f"{t.id}: verifying -> done")
                report.completed.append(t.id)
                # Commit everything the worker + verifier wrote to disk.
                # repo_root is two levels above exp_dir (runs/experiment → repo root).
                repo_root = exp_dir.parent.parent
                cr = commit_fn(task=t, repo_root=repo_root, exp_dir=exp_dir)
                if cr.ok:
                    report.committed.append(cr.sha or "unknown-sha")
                else:
                    mark_failed(t, f"commit-failed: {cr.error}")
                    report.completed.remove(t.id)
                    report.errors.append(f"commit failed for {t.id}: {cr.error}")
            else:
                # Verifier rejected: archive this attempt's artefacts so a
                # fresh worker round starts with a clean workdir, stash the
                # verifier's evidence so the next worker round can address
                # it (pass-29 bug Y), then retry-or-block.
                workdir = task_workdir(exp_dir, t)
                attempt_n = t.attempts + 1  # mark_verifier_failed increments after
                verifier_result = workdir / "result.json"
                worker_result = workdir / "result.worker.json"
                if verifier_result.exists():
                    verifier_result.rename(
                        workdir / f"result.verifier.attempt-{attempt_n}.json"
                    )
                if worker_result.exists():
                    worker_result.rename(
                        workdir / f"result.worker.attempt-{attempt_n}.json"
                    )
                evidence_raw = result.get("evidence", "")
                evidence = str(evidence_raw).strip() if evidence_raw else ""
                if evidence:
                    t.last_verifier_evidence = evidence
                    feedback_path = workdir / "feedback.md"
                    feedback_path.write_text(
                        "## Previous attempt was rejected by the cross-family"
                        " verifier\n\n"
                        f"Attempt {attempt_n} was rejected with this evidence:\n\n"
                        f"> {evidence}\n\n"
                        "Address each point in your new attempt. Do not repeat"
                        " the previous approach unchanged. Treat this as the"
                        " primary signal for what to fix.\n"
                    )
                mark_verifier_failed(t)
                if t.state == TaskState.QUEUED:
                    report.transitions.append(
                        f"{t.id}: verifying -> queued"
                        f" (verifier rejected, retry {t.attempts}/{t.max_attempts})"
                    )
                else:
                    report.transitions.append(
                        f"{t.id}: verifying -> blocked (verifier-rejected-twice)"
                    )
                    report.blocked.append(t.id)
            continue
        if t.id not in initially_verifying:
            # Just spawned this tick — skip stall check; give it at least one
            # heartbeat to write its first log line.
            continue
        log_path = task_workdir(exp_dir, t) / "log"
        if observation.is_stalled(
            pid=t.pid,
            log_path=log_path,
            now=now,
            stall_window_s=cfg.worker_stall_minutes * 60,
            started_at=t.started_at,
        ):
            # Stalled verifier: treat like a worker stall so retry_or_block
            # handles the attempt budget consistently.
            mark_stalled(t)
            report.transitions.append(f"{t.id}: verifying -> stalled")

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
