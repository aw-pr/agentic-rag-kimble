"""Tests for src/experiment/tick.py.

Why this exists
---------------
The tick is the only piece of imperative logic in the scaffold. Each
test here pins one behaviour from the plan's section C transition
table plus the spawn / pause / stop branches in section F. The tick
is fully time-injected and spawner-injected, so every test runs in
milliseconds with no real subprocesses.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.experiment.spawn import SpawnResult
from src.experiment.state import Task, TaskState, load, save
from src.experiment.tick import tick


def _budget_yaml(tmp_path: Path, **overrides: object) -> Path:
    """Write a minimal budget.yaml. Caller can override any field."""
    defaults: dict[str, object] = dict(
        max_parallel_workers=2,
        worker_stall_minutes=10,
        per_task_subagent_cap=3,
        daily_spawn_threshold=50,
        phase_overspend_multiplier=2.0,
        tick_interval_minutes=5,
    )
    defaults.update(overrides)
    path = tmp_path / "budget.yaml"
    path.write_text(
        "\n".join(f"{k}: {v}" for k, v in defaults.items()) + "\n"
    )
    return path


def _exp_dir(tmp_path: Path) -> Path:
    """Make `runs/experiment` layout under tmp_path so the tick has a
    place to look for sentinels and per-task workdirs."""
    # Mirror the layout: <repo_root>/runs/experiment, with repo_root
    # being a grandparent of exp_dir so spawn.task_workdir can compute
    # the right script path (it climbs two parents).
    exp = tmp_path / "runs" / "experiment"
    (exp / "tasks").mkdir(parents=True)
    return exp


def _task(**kw: object) -> Task:
    defaults: dict[str, object] = dict(
        id="t1",
        phase=0,
        tier="T2",
        worker_pref="sonnet",
        verifier_pref="gpt-5.5",
    )
    defaults.update(kw)
    return Task(**defaults)


def _stub_spawn(
    *, pid: int = 1234, now: float = 100.0
):
    """Return a SpawnFn that records every call and yields a canned
    SpawnResult. Lets tests assert exactly which tasks spawned."""
    calls: list[tuple[str, str]] = []

    def fn(*, task, kind, exp_dir, now: float) -> SpawnResult:
        calls.append((task.id, kind))
        log_path = exp_dir / "tasks" / task.id / "log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch()
        return SpawnResult(pid=pid, log_path=log_path, started_at=now)

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


# ---- spawn decision --------------------------------------------------------


def test_queued_with_no_deps_gets_spawned(tmp_path: Path) -> None:
    """The simplest happy path: one queued task, no deps, capacity free.
    Result: tick spawns it and the task is now `running` with the
    recorded pid."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    save([_task(state=TaskState.QUEUED)], state)
    budget = _budget_yaml(tmp_path)
    spawn = _stub_spawn(pid=42, now=100.0)
    report = tick(
        state_path=state,
        budget_path=budget,
        exp_dir=exp,
        now=100.0,
        spawn_fn=spawn,
    )
    [t] = load(state)
    assert t.state == TaskState.RUNNING
    assert t.pid == 42
    assert t.started_at == 100.0
    assert spawn.calls == [("t1", "worker")]
    assert report.spawned == ["t1"]


def test_queued_with_unmet_deps_is_skipped(tmp_path: Path) -> None:
    """A consumer task whose dep is still queued must not run early.
    This is the dependency-ordering guarantee."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    dep = _task(id="dep", state=TaskState.QUEUED)
    consumer = _task(id="c", depends_on=["dep"], state=TaskState.QUEUED)
    save([dep, consumer], state)
    spawn = _stub_spawn()
    tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path, max_parallel_workers=1),
        exp_dir=exp,
        now=100.0,
        spawn_fn=spawn,
    )
    # Only the dep spawned; consumer waits.
    assert spawn.calls == [("dep", "worker")]
    [dep_after, consumer_after] = load(state)
    assert dep_after.state == TaskState.RUNNING
    assert consumer_after.state == TaskState.QUEUED


def test_parallel_cap_limits_spawns_per_tick(tmp_path: Path) -> None:
    """With max_parallel_workers=2 and three queued tasks, exactly two
    spawn this tick. The third waits for capacity."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    tasks = [_task(id=f"t{i}", state=TaskState.QUEUED) for i in range(3)]
    save(tasks, state)
    spawn = _stub_spawn()
    tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path, max_parallel_workers=2),
        exp_dir=exp,
        now=100.0,
        spawn_fn=spawn,
    )
    assert len(spawn.calls) == 2
    after = load(state)
    states = [t.state for t in after]
    assert states.count(TaskState.RUNNING) == 2
    assert states.count(TaskState.QUEUED) == 1


# ---- pause / stop ----------------------------------------------------------


def test_pause_sentinel_blocks_new_spawns(tmp_path: Path) -> None:
    """PAUSE halts new spawns; in-flight workers finish on their own.
    The tick must respect this on the very tick the sentinel appears."""
    exp = _exp_dir(tmp_path)
    (exp / "PAUSE").write_text("")
    state = tmp_path / "state.yaml"
    save([_task(state=TaskState.QUEUED)], state)
    spawn = _stub_spawn()
    report = tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path),
        exp_dir=exp,
        now=100.0,
        spawn_fn=spawn,
    )
    assert report.paused
    assert spawn.calls == []
    [t] = load(state)
    assert t.state == TaskState.QUEUED


def test_stop_sentinel_blocks_new_spawns(tmp_path: Path) -> None:
    """STOP halts new spawns immediately. Killing live workers is the
    shell entry's job (POSIX kill); the Python tick just refuses to
    start anything else."""
    exp = _exp_dir(tmp_path)
    (exp / "STOP").write_text("")
    state = tmp_path / "state.yaml"
    save([_task(state=TaskState.QUEUED)], state)
    spawn = _stub_spawn()
    report = tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path),
        exp_dir=exp,
        now=100.0,
        spawn_fn=spawn,
    )
    assert report.stopped
    assert spawn.calls == []


# ---- completion ------------------------------------------------------------


def test_running_task_with_passing_result_spawns_verifier(tmp_path: Path) -> None:
    """Worker writes result.json with acceptance=pass; tick renames it to
    result.worker.json and spawns the verifier. Task enters `verifying`,
    NOT `done` yet — verifier confirmation is required."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    t = _task(
        state=TaskState.RUNNING,
        pid=99,
        started_at=10.0,
        last_heartbeat=10.0,
    )
    save([t], state)
    workdir = exp / "tasks" / "t1"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "log").write_text("done")
    (workdir / "result.json").write_text(
        json.dumps({"acceptance": "pass", "evidence": "..."})
    )
    spawn = _stub_spawn(pid=200, now=100.0)
    report = tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path),
        exp_dir=exp,
        now=100.0,
        spawn_fn=spawn,
    )
    [t_after] = load(state)
    assert t_after.state == TaskState.VERIFYING
    assert t_after.pid == 200
    assert spawn.calls == [("t1", "verifier")]
    # Worker result preserved; verifier result slot is clear.
    assert (workdir / "result.worker.json").exists()
    assert not (workdir / "result.json").exists()
    assert report.completed == []


def _noop_commit_fn(*, task, repo_root, exp_dir):
    """Commit stub that always succeeds; used by tests that don't test
    commit behaviour and just need the verifying -> done transition to
    complete cleanly without a real git repo."""
    from src.experiment.commit import CommitResult
    return CommitResult(ok=True, sha="noop", error=None)


def test_verifying_task_with_passing_verifier_marks_done(tmp_path: Path) -> None:
    """Verifier writes result.json with acceptance=pass; tick promotes to done."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    t = _task(
        state=TaskState.VERIFYING,
        pid=200,
        started_at=50.0,
        last_heartbeat=50.0,
    )
    save([t], state)
    workdir = exp / "tasks" / "t1"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "log").write_text("verifier done")
    (workdir / "result.json").write_text(
        json.dumps({"acceptance": "pass", "evidence": "independently verified"})
    )
    spawn = _stub_spawn()
    report = tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path),
        exp_dir=exp,
        now=100.0,
        spawn_fn=spawn,
        commit_fn=_noop_commit_fn,
    )
    [t_after] = load(state)
    assert t_after.state == TaskState.DONE
    assert report.completed == ["t1"]
    assert spawn.calls == []


def test_verifying_task_with_failing_verifier_requeues_worker(tmp_path: Path) -> None:
    """Verifier rejects; attempts < max so worker goes back to queued."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    t = _task(
        state=TaskState.VERIFYING,
        pid=200,
        started_at=50.0,
        attempts=0,
        max_attempts=2,
    )
    save([t], state)
    workdir = exp / "tasks" / "t1"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "result.json").write_text(
        json.dumps({"acceptance": "fail", "evidence": "criterion B not met"})
    )
    tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path),
        exp_dir=exp,
        now=100.0,
        spawn_fn=_stub_spawn(),
    )
    [t_after] = load(state)
    assert t_after.state == TaskState.QUEUED
    assert t_after.attempts == 1


def test_verifying_task_with_failing_verifier_writes_feedback_for_retry(
    tmp_path: Path,
) -> None:
    """Pass-29 bug Y: verifier evidence must reach the next worker round.
    On rejection, the tick archives the verifier's result.json, stashes the
    evidence on the task, and writes feedback.md for the worker wrapper to
    prepend."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    t = _task(
        state=TaskState.VERIFYING,
        pid=200,
        started_at=50.0,
        attempts=0,
        max_attempts=2,
    )
    save([t], state)
    workdir = exp / "tasks" / "t1"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "result.json").write_text(
        json.dumps(
            {
                "acceptance": "fail",
                "evidence": "hardcoded constants instead of calling scipy",
            }
        )
    )
    # Simulate the worker's preserved result from the previous round
    # (the verifier round renamed it; tick should archive both).
    (workdir / "result.worker.json").write_text(
        json.dumps({"acceptance": "pass", "evidence": "tests pass"})
    )
    tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path),
        exp_dir=exp,
        now=100.0,
        spawn_fn=_stub_spawn(),
    )
    [t_after] = load(state)
    assert t_after.state == TaskState.QUEUED
    assert t_after.attempts == 1
    assert t_after.last_verifier_evidence == (
        "hardcoded constants instead of calling scipy"
    )
    # Workdir now clean of result.json + result.worker.json, both archived,
    # and feedback.md written for the next worker round.
    assert not (workdir / "result.json").exists()
    assert not (workdir / "result.worker.json").exists()
    assert (workdir / "result.verifier.attempt-1.json").exists()
    assert (workdir / "result.worker.attempt-1.json").exists()
    feedback = (workdir / "feedback.md").read_text()
    assert "hardcoded constants instead of calling scipy" in feedback
    assert "Previous attempt was rejected" in feedback


def test_verifying_task_failing_without_evidence_skips_feedback(tmp_path: Path) -> None:
    """If the verifier returns fail without an evidence string, no
    feedback.md is written (nothing useful to surface) but the workdir
    archive still happens so the next worker round starts clean."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    t = _task(
        state=TaskState.VERIFYING,
        pid=200,
        started_at=50.0,
        attempts=0,
        max_attempts=2,
    )
    save([t], state)
    workdir = exp / "tasks" / "t1"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "result.json").write_text(json.dumps({"acceptance": "fail"}))
    tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path),
        exp_dir=exp,
        now=100.0,
        spawn_fn=_stub_spawn(),
    )
    [t_after] = load(state)
    assert t_after.state == TaskState.QUEUED
    assert t_after.last_verifier_evidence is None
    assert not (workdir / "feedback.md").exists()
    assert (workdir / "result.verifier.attempt-1.json").exists()


def test_verifying_task_with_failing_verifier_blocks_when_exhausted(tmp_path: Path) -> None:
    """Second verifier rejection -> blocked with blocked_reason=verifier-rejected-twice."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    t = _task(
        state=TaskState.VERIFYING,
        pid=200,
        started_at=50.0,
        attempts=1,
        max_attempts=2,
    )
    save([t], state)
    workdir = exp / "tasks" / "t1"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "result.json").write_text(
        json.dumps({"acceptance": "fail", "evidence": "still failing"})
    )
    report = tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path),
        exp_dir=exp,
        now=100.0,
        spawn_fn=_stub_spawn(),
    )
    [t_after] = load(state)
    assert t_after.state == TaskState.BLOCKED
    assert t_after.blocked_reason == "verifier-rejected-twice"
    assert report.blocked == ["t1"]


def test_verifying_task_stall_retries_via_stall_path(tmp_path: Path) -> None:
    """Verifier pid dies without writing result.json: stall detected,
    retry_or_block gives it another chance (same as worker stall)."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    workdir = exp / "tasks" / "t1"
    workdir.mkdir(parents=True, exist_ok=True)
    log = workdir / "log"
    log.write_text("old")
    import os
    os.utime(log, (0.0, 0.0))
    t = _task(
        state=TaskState.VERIFYING,
        pid=1_000_000,  # dead
        started_at=0.0,
        last_heartbeat=0.0,
        attempts=0,
        max_attempts=2,
    )
    save([t], state)
    tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path, worker_stall_minutes=1),
        exp_dir=exp,
        now=10_000.0,
        spawn_fn=_stub_spawn(),
    )
    [t_after] = load(state)
    # Stalled then retried in same tick -> QUEUED with attempts incremented.
    assert t_after.state == TaskState.QUEUED
    assert t_after.attempts == 1


def test_running_task_with_failing_result_goes_to_stalled(tmp_path: Path) -> None:
    """Acceptance fail is not terminal; tick marks stalled so retry/block
    logic gives the worker a second chance, then triage."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    t = _task(state=TaskState.RUNNING, pid=99, started_at=10.0)
    save([t], state)
    workdir = exp / "tasks" / "t1"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "result.json").write_text(
        json.dumps({"acceptance": "fail", "reason": "spec mismatch"})
    )
    tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path),
        exp_dir=exp,
        now=100.0,
        spawn_fn=_stub_spawn(),
    )
    [t_after] = load(state)
    # Stalled now; the same-tick retry_or_block pass moves it to QUEUED
    # (attempts < max). So end state is `queued` with attempts=1.
    assert t_after.state == TaskState.QUEUED
    assert t_after.attempts == 1
    assert t_after.blocked_reason == "acceptance-failed"


# ---- stall + retry + block ------------------------------------------------


def test_running_with_stale_log_stalls_then_retries(tmp_path: Path) -> None:
    """Live pid but log untouched longer than the stall window: the
    tick stalls the task and (since attempts < max) re-queues it."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    workdir = exp / "tasks" / "t1"
    workdir.mkdir(parents=True, exist_ok=True)
    log = workdir / "log"
    log.write_text("written long ago")
    import os
    os.utime(log, (0.0, 0.0))  # very old mtime
    t = _task(
        state=TaskState.RUNNING,
        pid=1_000_000,  # not actually alive, but pid check makes us stall anyway
        started_at=0.0,
        last_heartbeat=0.0,
        attempts=0,
        max_attempts=2,
    )
    save([t], state)
    tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path, worker_stall_minutes=1),
        exp_dir=exp,
        now=10_000.0,
        spawn_fn=_stub_spawn(),
    )
    [t_after] = load(state)
    assert t_after.state == TaskState.QUEUED
    assert t_after.attempts == 1


def test_stalled_task_exhausting_retries_blocks(tmp_path: Path) -> None:
    """Second stall puts the task in `blocked`; triage handles it from
    there. The tick does not auto-create the triage entry — that's a
    separate, externally-enqueued task per the plan."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    t = _task(
        state=TaskState.RUNNING,
        pid=1_000_000,
        started_at=0.0,
        last_heartbeat=0.0,
        attempts=1,
        max_attempts=2,
    )
    save([t], state)
    report = tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path, worker_stall_minutes=1),
        exp_dir=exp,
        now=10_000.0,
        spawn_fn=_stub_spawn(),
    )
    [t_after] = load(state)
    assert t_after.state == TaskState.BLOCKED
    assert report.blocked == ["t1"]


# ---- cooldown: stall+retry doesn't respawn same tick -----------------------


def test_stalled_task_requeued_does_not_respawn_same_tick(tmp_path: Path) -> None:
    """When a running task stalls and is retried within one tick, the
    spawn loop must skip it that tick. Cooldown of at least one
    heartbeat. Otherwise a chronically broken task burns through its
    attempt budget in seconds rather than minutes."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    t = _task(
        state=TaskState.RUNNING,
        pid=1_000_000,  # dead
        started_at=0.0,
        attempts=0,
        max_attempts=2,
    )
    save([t], state)
    spawn = _stub_spawn()
    tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path, worker_stall_minutes=1),
        exp_dir=exp,
        now=10_000.0,
        spawn_fn=spawn,
    )
    [t_after] = load(state)
    # End-of-tick state is QUEUED (just retried), and spawn was NOT called.
    assert t_after.state == TaskState.QUEUED
    assert t_after.attempts == 1
    assert spawn.calls == [], "must not respawn in the same tick that retried"


# ---- spawn error -----------------------------------------------------------


def test_spawn_exception_marks_task_failed(tmp_path: Path) -> None:
    """If the spawner itself raises (e.g. missing shell script), the
    task is marked `failed` with the error in its blocked_reason and
    the tick keeps going for the rest of the queue."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    save([_task(state=TaskState.QUEUED)], state)

    def bad_spawn(*, task, kind, exp_dir, now):
        raise RuntimeError("script missing")

    report = tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path),
        exp_dir=exp,
        now=100.0,
        spawn_fn=bad_spawn,
    )
    [t_after] = load(state)
    assert t_after.state == TaskState.FAILED
    assert "script missing" in (t_after.blocked_reason or "")
    assert report.errors and "t1" in report.errors[0]


# ---- commit_fn integration -------------------------------------------------


def _stub_commit_fn(*, ok: bool = True, sha: str = "deadbeef"):
    """Return a CommitFn stub that records calls and yields a canned result."""
    from src.experiment.commit import CommitResult

    calls: list[object] = []

    def fn(*, task, repo_root, exp_dir):
        calls.append(task)
        if ok:
            return CommitResult(ok=True, sha=sha, error=None)
        return CommitResult(ok=False, sha=None, error="git commit failed (rc=1): fatal: no changes")

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


def test_commit_fn_called_on_verifying_to_done(tmp_path: Path) -> None:
    """When a verifying task's verifier writes acceptance=pass, the commit_fn
    must be called exactly once with that task, the task ends DONE, and the
    sha appears in report.committed."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    t = _task(
        state=TaskState.VERIFYING,
        pid=200,
        started_at=50.0,
        last_heartbeat=50.0,
    )
    save([t], state)
    workdir = exp / "tasks" / "t1"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "result.json").write_text(
        json.dumps({"acceptance": "pass", "evidence": "confirmed"})
    )

    commit = _stub_commit_fn(ok=True, sha="abc123")
    report = tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path),
        exp_dir=exp,
        now=100.0,
        spawn_fn=_stub_spawn(),
        commit_fn=commit,
    )
    [t_after] = load(state)
    assert t_after.state == TaskState.DONE
    assert report.completed == ["t1"]
    assert report.committed == ["abc123"]
    assert report.errors == []
    assert len(commit.calls) == 1
    assert commit.calls[0].id == "t1"  # type: ignore[union-attr]


def test_commit_fn_failure_marks_task_failed(tmp_path: Path) -> None:
    """When commit_fn returns ok=False, the task should end FAILED (not DONE),
    report.completed must NOT contain the task, and report.errors must mention
    the commit failure."""
    exp = _exp_dir(tmp_path)
    state = tmp_path / "state.yaml"
    t = _task(
        state=TaskState.VERIFYING,
        pid=200,
        started_at=50.0,
        last_heartbeat=50.0,
    )
    save([t], state)
    workdir = exp / "tasks" / "t1"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "result.json").write_text(
        json.dumps({"acceptance": "pass", "evidence": "confirmed"})
    )

    commit = _stub_commit_fn(ok=False)
    report = tick(
        state_path=state,
        budget_path=_budget_yaml(tmp_path),
        exp_dir=exp,
        now=100.0,
        spawn_fn=_stub_spawn(),
        commit_fn=commit,
    )
    [t_after] = load(state)
    assert t_after.state == TaskState.FAILED
    assert "t1" not in report.completed
    assert any("commit" in e for e in report.errors)
