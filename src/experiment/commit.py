"""Per-task git commit logic for the experiment scaffold.

Why this exists
---------------
When a task transitions verifying → done, the tick needs to commit
whatever the worker and verifier agents wrote to disk. This module owns
that logic: it builds the commit message (subject from brief.md H1, body
with a Kimball-style trailer block including Verified-By), maps worker/
verifier model names to canonical git author identities, and shells out
to git via an injectable `run` callable so the whole thing is testable
without a real repo.

Parallel-write caveat
---------------------
`git add -A` is used to stage everything. This is correct when tasks write
to disjoint paths (the parallel cap is the soft brake for this). If two
tasks are RUNNING or VERIFYING simultaneously and their workers modify the
same file, the commit for the second one will include the first one's
unstaged changes — corrupting provenance. Keep max_parallel_workers=1 until
per-task worktrees or per-task add-by-path are implemented.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from src.experiment.state import Task

# ---------------------------------------------------------------------------
# Model → canonical git author identity
# Extend this table whenever a new worker/verifier family is introduced.
# The canonical list lives in ~/.claude/rules/mcp-hub-dev-rules.md; the
# entries below must match that table. gpt-5.5 is a forward-compatible
# extension of "Codex GPT-5 <codex-gpt-5@local>" — bump the canonical
# table when this stops being the dev rule.
# ---------------------------------------------------------------------------
_MODEL_AUTHORS: dict[str, str] = {
    "sonnet": "Claude Sonnet 4.6 <claude-sonnet-4-6@local>",
    "gpt-5.5": "Codex GPT-5.5 <codex-gpt-5-5@local>",
}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommitResult:
    """What a commit attempt returns to the tick."""

    ok: bool
    sha: str | None
    error: str | None  # populated when ok=False, contains stderr tail


class CommitFn(Protocol):
    """Injectable signature for the commit operation. Real impl calls git;
    tests pass a stub that returns a canned CommitResult."""

    def __call__(self, *, task: Task, repo_root: Path, exp_dir: Path) -> CommitResult: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STDERR_TAIL = 1000  # characters kept from stderr on failure


def _extract_subject(brief_path: Path, task_id: str) -> str:
    """Return the first # H1 line from brief.md, stripped of the leading
    hash and whitespace. Falls back to a generic message if no H1 found."""
    if brief_path.exists():
        for line in brief_path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                h1_text = stripped[2:].strip()
                if h1_text:
                    return f"experiment({task_id}): {h1_text}"
    return f"experiment({task_id}): task complete"


def _resolve_author(model_key: str, label: str) -> str | None:
    """Look up a model key in _MODEL_AUTHORS. Returns None if not found,
    with `label` used in the caller's error message."""
    return _MODEL_AUTHORS.get(model_key)


def _build_message(task: Task, subject: str, verifier_author: str) -> str:
    """Compose the full commit message: subject, blank line, body, blank
    line, trailer block."""
    body = "Done; verifier confirmed PASS."
    trailers = "\n".join([
        f"Task-Id: {task.id}",
        f"Phase: {task.phase}",
        f"Tier: {task.tier}",
        f"Worker: {task.worker_pref}",
        f"Verifier: {task.verifier_pref}",
        f"Co-Authored-By: {verifier_author}",
        f"Verified-By: {verifier_author}",
    ])
    return f"{subject}\n\n{body}\n\n{trailers}"


# ---------------------------------------------------------------------------
# Production implementation
# ---------------------------------------------------------------------------

RunCallable = Callable[..., subprocess.CompletedProcess[str]]


def shell_commit(
    *,
    task: Task,
    repo_root: Path,
    exp_dir: Path,
    run: RunCallable = subprocess.run,
) -> CommitResult:
    """Stage everything and commit with full cross-family attribution.

    Steps:
      1. Resolve worker + verifier author strings; fail immediately if
         either model key is unknown — don't fall back silently.
      2. Build the commit message from brief.md H1.
      3. `git add -A` to stage all changes.
      4. `git commit --author=<worker> -m <msg>` with verifier in trailers.
      5. `git rev-parse HEAD` to capture the resulting sha.

    The `run` parameter is injectable for tests; defaults to subprocess.run.
    """
    # ---- 1. Resolve identities -------------------------------------------
    worker_author = _resolve_author(task.worker_pref, "worker")
    if worker_author is None:
        return CommitResult(
            ok=False,
            sha=None,
            error=(
                f"unknown worker model '{task.worker_pref}'"
                " — add it to _MODEL_AUTHORS in commit.py"
            ),
        )

    verifier_author = _resolve_author(task.verifier_pref, "verifier")
    if verifier_author is None:
        return CommitResult(
            ok=False,
            sha=None,
            error=(
                f"unknown verifier model '{task.verifier_pref}'"
                " — add it to _MODEL_AUTHORS in commit.py"
            ),
        )

    # ---- 2. Build message ------------------------------------------------
    brief_path = exp_dir / "tasks" / task.id / "brief.md"
    subject = _extract_subject(brief_path, task.id)
    message = _build_message(task, subject, verifier_author)

    # ---- 3. git add -A ---------------------------------------------------
    add_result = run(
        ["git", "add", "-A"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if add_result.returncode != 0:
        stderr_tail = (add_result.stderr or "")[-_STDERR_TAIL:]
        return CommitResult(
            ok=False,
            sha=None,
            error=f"git add -A failed (rc={add_result.returncode}): {stderr_tail}",
        )

    # ---- 4. git commit ---------------------------------------------------
    commit_result = run(
        ["git", "commit", f"--author={worker_author}", "-m", message],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        stderr_tail = (commit_result.stderr or "")[-_STDERR_TAIL:]
        return CommitResult(
            ok=False,
            sha=None,
            error=f"git commit failed (rc={commit_result.returncode}): {stderr_tail}",
        )

    # ---- 5. Capture sha --------------------------------------------------
    rev_result = run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    sha = rev_result.stdout.strip() if rev_result.returncode == 0 else None
    return CommitResult(ok=True, sha=sha, error=None)


DEFAULT_COMMIT: CommitFn = shell_commit


__all__ = [
    "CommitFn",
    "CommitResult",
    "DEFAULT_COMMIT",
    "shell_commit",
]
