"""Tests for src/experiment/observation.py.

Why this exists
---------------
The tick decides whether a worker is alive by calling these. If they
lie, the heartbeat hallucinates stalls or misses real ones. Each test
pins one observation contract with no LLM and no shell.
"""
from __future__ import annotations

import os
from pathlib import Path

from src.experiment.observation import (
    is_pid_alive,
    is_stalled,
    log_mtime_age,
    pause_requested,
    sentinel_path,
    stop_requested,
)

# ---- is_pid_alive ----------------------------------------------------------


def test_current_pid_is_alive() -> None:
    """The test process itself is the easiest live pid to check."""
    assert is_pid_alive(os.getpid())


def test_non_positive_pid_is_dead() -> None:
    """0 and negative pids aren't valid; treating them as alive would
    let the tick wedge forever waiting on a non-existent worker."""
    assert not is_pid_alive(0)
    assert not is_pid_alive(-1)


def test_almost_certainly_unused_pid_is_dead() -> None:
    """Pid 1 million is well outside the macOS/Linux default pid_max;
    we use it as a stand-in for 'definitely not a running process'."""
    assert not is_pid_alive(1_000_000)


# ---- log_mtime_age ---------------------------------------------------------


def test_missing_log_returns_none(tmp_path: Path) -> None:
    """No log file yet is a distinct signal from 'log very old'.
    Caller (is_stalled) must handle them differently."""
    assert log_mtime_age(tmp_path / "absent", now=10_000.0) is None


def test_log_age_is_now_minus_mtime(tmp_path: Path) -> None:
    """Age is straightforward subtraction; pinning it stops bugs where
    we accidentally compare to ctime or atime."""
    p = tmp_path / "log"
    p.write_text("touch")
    os.utime(p, (1_000.0, 1_000.0))
    assert log_mtime_age(p, now=1_005.0) == 5.0


# ---- is_stalled ------------------------------------------------------------


def test_stalled_when_no_pid() -> None:
    """A running task with no pid recorded is corrupted state, treat as
    stalled so the retry/block logic recovers it."""
    assert is_stalled(
        pid=None,
        log_path=None,
        now=0.0,
        stall_window_s=60.0,
        started_at=None,
    )


def test_stalled_when_pid_dead() -> None:
    """A pid that isn't running anymore means the worker crashed
    silently. Stall window doesn't matter."""
    assert is_stalled(
        pid=1_000_000,
        log_path=None,
        now=0.0,
        stall_window_s=60.0,
        started_at=0.0,
    )


def test_not_stalled_when_pid_alive_and_no_log_yet_within_window(
    tmp_path: Path,
) -> None:
    """Fresh spawn that hasn't written its log yet. As long as we're
    inside the stall window from started_at, it's still warming up."""
    assert not is_stalled(
        pid=os.getpid(),
        log_path=tmp_path / "absent",
        now=10.0,
        stall_window_s=60.0,
        started_at=0.0,
    )


def test_stalled_when_pid_alive_but_no_log_after_window(tmp_path: Path) -> None:
    """Past the window without a single log byte = worker is wedged
    even though the process is technically alive."""
    assert is_stalled(
        pid=os.getpid(),
        log_path=tmp_path / "absent",
        now=120.0,
        stall_window_s=60.0,
        started_at=0.0,
    )


def test_stalled_when_log_mtime_older_than_window(tmp_path: Path) -> None:
    """Live pid, log present but stale. This is the modal stall case."""
    log = tmp_path / "log"
    log.write_text("started")
    os.utime(log, (1_000.0, 1_000.0))
    assert is_stalled(
        pid=os.getpid(),
        log_path=log,
        now=1_100.0,
        stall_window_s=60.0,
        started_at=900.0,
    )


def test_not_stalled_when_log_fresh(tmp_path: Path) -> None:
    """Log written within the window — worker is making progress."""
    log = tmp_path / "log"
    log.write_text("progress")
    os.utime(log, (1_000.0, 1_000.0))
    assert not is_stalled(
        pid=os.getpid(),
        log_path=log,
        now=1_010.0,
        stall_window_s=60.0,
        started_at=900.0,
    )


# ---- sentinels -------------------------------------------------------------


def test_sentinel_path_uses_experiment_dir(tmp_path: Path) -> None:
    """STOP and PAUSE live at the top of the experiment workdir so the
    Mayor can write them with one path and the tick reads them from one
    path. If this layout changes, the Mayor control commands break."""
    assert sentinel_path(tmp_path, "STOP") == tmp_path / "STOP"
    assert sentinel_path(tmp_path, "PAUSE") == tmp_path / "PAUSE"


def test_stop_and_pause_detection(tmp_path: Path) -> None:
    """Each sentinel is detected independently; a STOP without a PAUSE
    (and vice versa) is the expected steady state."""
    assert not stop_requested(tmp_path)
    assert not pause_requested(tmp_path)
    (tmp_path / "STOP").write_text("")
    assert stop_requested(tmp_path)
    assert not pause_requested(tmp_path)
    (tmp_path / "PAUSE").write_text("")
    assert pause_requested(tmp_path)
