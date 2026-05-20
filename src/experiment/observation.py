"""System observation primitives for the tick.

Why this exists
---------------
The tick has to look at the real filesystem (pid alive? log file recently
touched? STOP/PAUSE sentinel present?) and turn those observations into
state transitions. Keeping the observation layer thin and pure-Python
means the tick itself doesn't shell out — every call here is one syscall
or one stat — and unit tests can pass canned observations rather than
mocking subprocesses.
"""
from __future__ import annotations

import os
from pathlib import Path


def is_pid_alive(pid: int) -> bool:
    """True iff `pid` is a running process this user can signal.

    Uses `os.kill(pid, 0)` which delivers no signal but raises if the
    process is gone or owned by another user. `signal 0` is the canonical
    cross-Unix liveness probe.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user. Treat as alive: we
        # can't manage it, but it isn't a stall.
        return True
    return True


def log_mtime_age(log_path: Path, now: float) -> float | None:
    """Seconds since the log file was last modified, or None if missing.

    A worker writes incremental progress to its log; the file's mtime is
    the cheapest heartbeat signal available. Missing file means the worker
    hasn't written anything yet — caller decides what that means relative
    to `started_at`.
    """
    if not log_path.exists():
        return None
    return now - log_path.stat().st_mtime


def is_stalled(
    *,
    pid: int | None,
    log_path: Path | None,
    now: float,
    stall_window_s: float,
    started_at: float | None,
) -> bool:
    """A running task is stalled if its pid is gone, or its log hasn't
    been touched within the stall window since the task started.

    `started_at` matters for the "no log file yet" case: a brand-new
    spawn shouldn't be flagged stalled in the first few seconds before
    it has had a chance to write.
    """
    if pid is None:
        return True
    if not is_pid_alive(pid):
        return True
    if log_path is None:
        return False
    age = log_mtime_age(log_path, now)
    if age is None:
        # No log yet. Stall only if we're well past the window since spawn.
        if started_at is None:
            return False
        return (now - started_at) > stall_window_s
    return age > stall_window_s


def sentinel_path(exp_dir: Path, name: str) -> Path:
    """Path of a control sentinel (`STOP`, `PAUSE`) inside the experiment
    workdir. Centralised so the tick and the Mayor agree on names."""
    return exp_dir / name


def stop_requested(exp_dir: Path) -> bool:
    """`runs/experiment/STOP` halts new spawns and signals running
    workers to terminate. Set by the Mayor's `stop` command."""
    return sentinel_path(exp_dir, "STOP").exists()


def pause_requested(exp_dir: Path) -> bool:
    """`runs/experiment/PAUSE` halts new spawns; running workers finish.
    Set by the Mayor's `pause` command."""
    return sentinel_path(exp_dir, "PAUSE").exists()
