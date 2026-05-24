"""Tests for src/experiment/commit.py.

Why this exists
---------------
commit.py is the only new piece of state-mutating logic in pass-29.
These tests verify the subject-extraction heuristic, the model-author
mapping, the exact git commands issued (in order), the message trailer
content, and the error paths — all without touching a real git repo.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.experiment.commit import (
    _MODEL_AUTHORS,
    _extract_subject,
    shell_commit,
)
from src.experiment.state import Task, TaskState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(**kw: Any) -> Task:
    defaults: dict[str, Any] = dict(
        id="t1",
        phase=0,
        tier="T2",
        worker_pref="sonnet",
        verifier_pref="gpt-5.5",
        state=TaskState.DONE,
    )
    defaults.update(kw)
    return Task(**defaults)


def _fake_run(responses: list[tuple[int, str, str]]):
    """Return a callable that replays (returncode, stdout, stderr) tuples.
    Captures every call so tests can inspect the exact commands issued."""
    calls: list[list[str]] = []
    responses_iter = iter(responses)

    def run(cmd: list[str], *, cwd: Path, capture_output: bool, text: bool) -> Any:
        calls.append(list(cmd))
        rc, stdout, stderr = next(responses_iter)
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


# ---------------------------------------------------------------------------
# Subject extraction
# ---------------------------------------------------------------------------


def test_extract_subject_from_h1(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text("# My Great Task\n\nSome body text.\n")
    result = _extract_subject(brief, "t99")
    assert result == "experiment(t99): My Great Task"


def test_extract_subject_ignores_h2(tmp_path: Path) -> None:
    """H2 lines are not a subject; fallback should fire."""
    brief = tmp_path / "brief.md"
    brief.write_text("## Not an H1\n\nBody.\n")
    result = _extract_subject(brief, "t99")
    assert result == "experiment(t99): task complete"


def test_extract_subject_fallback_no_h1(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text("No heading here.\n")
    result = _extract_subject(brief, "t42")
    assert result == "experiment(t42): task complete"


def test_extract_subject_fallback_missing_file(tmp_path: Path) -> None:
    result = _extract_subject(tmp_path / "nonexistent.md", "t7")
    assert result == "experiment(t7): task complete"


# ---------------------------------------------------------------------------
# Author identity mapping
# ---------------------------------------------------------------------------


def test_known_model_sonnet_maps_correctly() -> None:
    assert _MODEL_AUTHORS["sonnet"] == "Claude Sonnet 4.6 <claude-sonnet-4-6@local>"


def test_known_model_gpt55_maps_correctly() -> None:
    assert _MODEL_AUTHORS["gpt-5.5"] == "Codex GPT-5.5 <codex-gpt-5-5@local>"


def test_unknown_worker_model_returns_error(tmp_path: Path) -> None:
    task = _task(worker_pref="mystery-model")
    run = _fake_run([])  # should never be called
    result = shell_commit(task=task, repo_root=tmp_path, exp_dir=tmp_path, run=run)
    assert result.ok is False
    assert result.sha is None
    assert "unknown worker model" in (result.error or "")
    assert run.calls == []  # no git commands issued


def test_unknown_verifier_model_returns_error(tmp_path: Path) -> None:
    task = _task(verifier_pref="mystery-verifier")
    run = _fake_run([])
    result = shell_commit(task=task, repo_root=tmp_path, exp_dir=tmp_path, run=run)
    assert result.ok is False
    assert "unknown verifier model" in (result.error or "")
    assert run.calls == []


# ---------------------------------------------------------------------------
# Successful commit path
# ---------------------------------------------------------------------------


def test_successful_commit_issues_correct_commands_in_order(tmp_path: Path) -> None:
    """Verify the exact git commands in the right order and the sha capture."""
    fake_sha = "abc1234def5678"
    task = _task()
    exp_dir = tmp_path / "runs" / "experiment"
    workdir = exp_dir / "tasks" / task.id
    workdir.mkdir(parents=True)
    (workdir / "brief.md").write_text("# Implement semantic layer\n\nDetails.\n")

    run = _fake_run([
        (0, "", ""),            # git add -A
        (0, "", ""),            # git commit
        (0, fake_sha + "\n", ""),  # git rev-parse HEAD
    ])

    result = shell_commit(task=task, repo_root=tmp_path, exp_dir=exp_dir, run=run)

    assert result.ok is True
    assert result.sha == fake_sha
    assert result.error is None

    # Exact command order
    assert run.calls[0] == ["git", "add", "-A"]
    assert run.calls[1][0:3] == ["git", "commit", f"--author={_MODEL_AUTHORS['sonnet']}"]
    assert run.calls[2] == ["git", "rev-parse", "HEAD"]


def test_commit_message_contains_all_required_trailers(tmp_path: Path) -> None:
    task = _task()
    exp_dir = tmp_path / "runs" / "experiment"
    (exp_dir / "tasks" / task.id).mkdir(parents=True)

    captured_messages: list[str] = []

    def capturing_run(cmd: list[str], *, cwd: Path, capture_output: bool, text: bool):
        if cmd[:2] == ["git", "commit"]:
            # -m <msg> is the last two elements
            msg_idx = cmd.index("-m")
            captured_messages.append(cmd[msg_idx + 1])
        return SimpleNamespace(returncode=0, stdout="deadbeef\n", stderr="")

    shell_commit(task=task, repo_root=tmp_path, exp_dir=exp_dir, run=capturing_run)

    assert captured_messages, "git commit was not called"
    msg = captured_messages[0]
    assert "Task-Id: t1" in msg
    assert "Phase: 0" in msg
    assert "Tier: T2" in msg
    assert "Worker: sonnet" in msg
    assert "Verifier: gpt-5.5" in msg
    verifier_author = _MODEL_AUTHORS["gpt-5.5"]
    assert f"Co-Authored-By: {verifier_author}" in msg
    assert f"Verified-By: {verifier_author}" in msg
    assert "Done; verifier confirmed PASS." in msg


def test_commit_subject_comes_from_brief_h1(tmp_path: Path) -> None:
    task = _task()
    exp_dir = tmp_path / "runs" / "experiment"
    workdir = exp_dir / "tasks" / task.id
    workdir.mkdir(parents=True)
    (workdir / "brief.md").write_text("# Add AlgorithmFamily snowflake\n")

    captured: list[str] = []

    def capturing_run(cmd: list[str], *, cwd: Path, capture_output: bool, text: bool):
        if cmd[:2] == ["git", "commit"]:
            captured.append(cmd[cmd.index("-m") + 1])
        return SimpleNamespace(returncode=0, stdout="cafe1234\n", stderr="")

    shell_commit(task=task, repo_root=tmp_path, exp_dir=exp_dir, run=capturing_run)
    assert captured
    assert captured[0].startswith("experiment(t1): Add AlgorithmFamily snowflake")


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_git_add_failure_returns_error(tmp_path: Path) -> None:
    task = _task()
    exp_dir = tmp_path / "runs" / "experiment"
    (exp_dir / "tasks" / task.id).mkdir(parents=True)

    long_stderr = "x" * 2000  # longer than the 1000-char tail
    run = _fake_run([
        (1, "", long_stderr),   # git add -A fails
    ])

    result = shell_commit(task=task, repo_root=tmp_path, exp_dir=exp_dir, run=run)
    assert result.ok is False
    assert result.sha is None
    assert "git add -A failed" in (result.error or "")
    # Tail capped at 1000 chars
    assert len(result.error or "") < 1200
    # git commit should not have been attempted
    assert len(run.calls) == 1


def test_git_commit_failure_returns_error(tmp_path: Path) -> None:
    task = _task()
    exp_dir = tmp_path / "runs" / "experiment"
    (exp_dir / "tasks" / task.id).mkdir(parents=True)

    long_stderr = "e" * 2000
    run = _fake_run([
        (0, "", ""),            # git add -A succeeds
        (1, "", long_stderr),   # git commit fails
    ])

    result = shell_commit(task=task, repo_root=tmp_path, exp_dir=exp_dir, run=run)
    assert result.ok is False
    assert result.sha is None
    assert "git commit failed" in (result.error or "")
    assert len(result.error or "") < 1200


def test_git_commit_failure_does_not_call_rev_parse(tmp_path: Path) -> None:
    task = _task()
    exp_dir = tmp_path / "runs" / "experiment"
    (exp_dir / "tasks" / task.id).mkdir(parents=True)

    run = _fake_run([
        (0, "", ""),  # git add -A
        (1, "", "some error"),  # git commit
    ])

    shell_commit(task=task, repo_root=tmp_path, exp_dir=exp_dir, run=run)
    assert len(run.calls) == 2  # add + commit; no rev-parse
