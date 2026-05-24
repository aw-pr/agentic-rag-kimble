"""Tests for src/experiment/spawn.py — planned_command model-arg wiring.

Why these tests exist
---------------------
state.yaml worker_pref / verifier_pref must flow into the shell command so
the tick-authoritative model wins over brief frontmatter. These tests lock in
the argument ordering so a refactor can't silently drop the model arg.
"""
from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from src.experiment.state import Task, TaskState
from src.experiment.spawn import planned_command


@pytest.fixture()
def base_task() -> Task:
    return Task(
        id="smoke-01",
        phase=0,
        tier="tier-1",
        worker_pref="sonnet",
        verifier_pref="gpt-5.5",
        state=TaskState.QUEUED,
    )


@pytest.fixture()
def exp_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs" / "experiment"


def test_planned_command_worker_includes_worker_pref(
    base_task: Task, exp_dir: Path
) -> None:
    cmd_str = planned_command(task=base_task, kind="worker", exp_dir=exp_dir)
    args = shlex.split(cmd_str)
    # Expected: [script, task_id, workdir, model]
    assert args[-1] == base_task.worker_pref, (
        f"last arg should be worker_pref={base_task.worker_pref!r}, got {args[-1]!r}"
    )
    assert args[1] == base_task.id


def test_planned_command_verifier_includes_verifier_pref(
    base_task: Task, exp_dir: Path
) -> None:
    cmd_str = planned_command(task=base_task, kind="verifier", exp_dir=exp_dir)
    args = shlex.split(cmd_str)
    assert args[-1] == base_task.verifier_pref, (
        f"last arg should be verifier_pref={base_task.verifier_pref!r}, got {args[-1]!r}"
    )
    assert args[1] == base_task.id


def test_planned_command_worker_and_verifier_differ_for_cross_family_task(
    base_task: Task, exp_dir: Path
) -> None:
    """Cross-family discipline: the two model args must differ."""
    worker_cmd = shlex.split(planned_command(task=base_task, kind="worker", exp_dir=exp_dir))
    verifier_cmd = shlex.split(planned_command(task=base_task, kind="verifier", exp_dir=exp_dir))
    assert worker_cmd[-1] != verifier_cmd[-1], (
        "worker and verifier should use different models for cross-family validation"
    )


def test_planned_command_four_args(base_task: Task, exp_dir: Path) -> None:
    """Command must be exactly: script task_id workdir model."""
    for kind in ("worker", "verifier"):
        args = shlex.split(planned_command(task=base_task, kind=kind, exp_dir=exp_dir))
        assert len(args) == 4, f"expected 4 args for kind={kind!r}, got {len(args)}: {args}"
