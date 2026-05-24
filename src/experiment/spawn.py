"""Spawn dispatch interface for workers and verifiers.

Why this exists
---------------
The tick needs to start a worker (or verifier) and record its pid + log
path in state. The *how* of starting one depends on which family the
task is routed to (Claude Code subagent via `claude -p`, Codex via
`codex exec`) and lives in shell scripts under `scripts/`. The *what* —
the contract between the tick and a spawn — lives here in pure Python.
Tests pass an in-memory `SpawnFn` rather than really starting processes.
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.experiment.state import Task


@dataclass(frozen=True)
class SpawnResult:
    """What a spawn returns to the tick so state can be updated."""

    pid: int
    log_path: Path
    started_at: float


class SpawnFn(Protocol):
    """Function signature for any spawner. Real impl uses subprocess;
    tests pass a stub that returns a canned SpawnResult."""

    def __call__(
        self, *, task: Task, kind: str, exp_dir: Path, now: float
    ) -> SpawnResult: ...


def task_workdir(exp_dir: Path, task: Task) -> Path:
    """Per-task working directory under runs/experiment/tasks/<id>/.

    All artefacts (brief.md, log, result.json, diff.patch) for one task
    live here. Centralised so spawners, tick, and Mayor agree on paths.
    """
    return exp_dir / "tasks" / task.id


def shell_spawn(
    *, task: Task, kind: str, exp_dir: Path, now: float
) -> SpawnResult:
    """Production spawner: invokes the appropriate shell wrapper as a
    detached background process and returns its pid.

    `kind` is `worker` or `verifier`. The shell scripts live at
    `scripts/experiment-spawn-{kind}.sh` and own the per-family routing
    (Sonnet vs Codex GPT-5.5). All the tick cares about is the pid and
    where logs land.
    """
    if kind not in {"worker", "verifier"}:
        raise ValueError(f"unknown spawn kind: {kind!r}")
    workdir = task_workdir(exp_dir, task)
    workdir.mkdir(parents=True, exist_ok=True)
    log_path = workdir / "log"
    log_path.touch(exist_ok=True)

    repo_root = exp_dir.parent.parent  # runs/experiment -> repo root
    script = repo_root / "scripts" / f"experiment-spawn-{kind}.sh"
    if not script.exists():
        raise FileNotFoundError(f"spawner script missing: {script}")

    # state.yaml worker_pref / verifier_pref are authoritative; pass them
    # as $3 so the shell script never falls back to the brief frontmatter.
    model = task.worker_pref if kind == "worker" else task.verifier_pref
    cmd = [str(script), task.id, str(workdir), model]
    # Detach so the tick returns immediately. The worker writes to its
    # own log; the tick polls log mtime + pid liveness next cycle.
    with log_path.open("ab") as logf:
        proc = subprocess.Popen(
            cmd,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return SpawnResult(pid=proc.pid, log_path=log_path, started_at=now)


def planned_command(*, task: Task, kind: str, exp_dir: Path) -> str:
    """Human-readable preview of what would be run. Used by the Mayor's
    `show <task>` command and by dry-run mode."""
    workdir = task_workdir(exp_dir, task)
    repo_root = exp_dir.parent.parent
    script = repo_root / "scripts" / f"experiment-spawn-{kind}.sh"
    model = task.worker_pref if kind == "worker" else task.verifier_pref
    return shlex.join([str(script), task.id, str(workdir), model])


# Default spawner the tick uses unless a test injects something else.
DEFAULT_SPAWN: SpawnFn = shell_spawn


__all__ = [
    "DEFAULT_SPAWN",
    "SpawnFn",
    "SpawnResult",
    "planned_command",
    "shell_spawn",
    "task_workdir",
]
