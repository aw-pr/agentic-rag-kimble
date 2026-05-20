#!/usr/bin/env python3
"""Operator monitoring dashboard for the experiment scaffold.

Why this exists
---------------
Provides a live read-only view of the experiment queue, current task,
recent git history, last triage decision, budget guards, and event tail.
Designed for the top pane of the `mayor` tmux session (launched via
scripts/experiment-mayor.sh). Refreshes every 2 seconds.

Pure read: never writes to disk, never modifies state.

Usage:
  python3 scripts/experiment-dashboard.py          # live loop
  python3 scripts/experiment-dashboard.py --once   # one render, then exit
  python3 scripts/experiment-dashboard.py --help   # show this help
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# Repo root — scripts/ is one level below root.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
EXP_DIR = REPO_ROOT / "runs" / "experiment"
STATE_YAML = EXP_DIR / "state.yaml"
EVENTS_LOG = EXP_DIR / "events.log"
BUDGET_YAML = EXP_DIR / "budget.yaml"
BUDGET_EXAMPLE = EXP_DIR / "budget.yaml.example"
TRIAGE_DIR = EXP_DIR / "triage"

# Ensure src package is importable when run as a script.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.experiment.budget import BudgetConfig  # noqa: E402
from src.experiment.state import Task, TaskState, load  # noqa: E402

# ---------------------------------------------------------------------------
# State colours
# ---------------------------------------------------------------------------
STATE_COLOUR: dict[TaskState, str] = {
    TaskState.QUEUED: "cyan",
    TaskState.RUNNING: "green",
    TaskState.STALLED: "yellow",
    TaskState.BLOCKED: "red",
    TaskState.DONE: "bright_black",
    TaskState.FAILED: "red",
}

STATE_ICON: dict[TaskState, str] = {
    TaskState.QUEUED: "",
    TaskState.RUNNING: "[green]●[/]",
    TaskState.STALLED: "[yellow]~[/]",
    TaskState.BLOCKED: "[red]![/]",
    TaskState.DONE: "",
    TaskState.FAILED: "[red]✗[/]",
}

REFRESH_SECONDS = 2


# ---------------------------------------------------------------------------
# Data-reading helpers — all return safe defaults on missing files.
# ---------------------------------------------------------------------------


def _load_tasks() -> list[Task]:
    try:
        return load(STATE_YAML)
    except Exception:
        return []


def _budget_config() -> BudgetConfig:
    for path in (BUDGET_YAML, BUDGET_EXAMPLE):
        try:
            return BudgetConfig.load(path)
        except Exception:
            pass
    return BudgetConfig()


def _tail_file(path: Path, n: int) -> list[str]:
    """Return the last n non-empty lines of a text file; [] if missing."""
    if not path.exists():
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()
        return [ln for ln in lines[-n:] if ln]
    except Exception:
        return []


def _git_recent(n: int = 5) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "log", f"-{n}", "--pretty=format:%h %s"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip().splitlines()
    except Exception:
        pass
    return []


def _last_triage() -> list[str]:
    """Read the first 6 lines of the most recent triage .md file."""
    if not TRIAGE_DIR.exists():
        return []
    try:
        mds = sorted(TRIAGE_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not mds:
            return []
        lines = mds[0].read_text(errors="replace").splitlines()
        return [ln for ln in lines[:6] if ln]
    except Exception:
        return []


def _count_24h_spawns() -> int:
    """Count spawn lines in events.log from the last 24 hours."""
    lines = _tail_file(EVENTS_LOG, 10000)
    now = time.time()
    cutoff = now - 86400
    count = 0
    for line in lines:
        if "spawn" not in line.lower() and "running" not in line.lower():
            continue
        # Lines look like: 2026-05-18T14:27:14Z phase-X -> running
        parts = line.strip().split()
        if not parts:
            continue
        try:
            import datetime

            ts_str = parts[0].rstrip("Z").replace("T", " ")
            ts = datetime.datetime.fromisoformat(ts_str).replace(
                tzinfo=datetime.timezone.utc
            )
            if ts.timestamp() >= cutoff:
                count += 1
        except Exception:
            pass
    return count


def _running_task_last_log(task: Task) -> str:
    """Tail one line from the running task's log file."""
    if task.brief_path is None:
        return ""
    # brief_path is runs/experiment/tasks/<id>/brief.md
    task_dir = Path(task.brief_path).parent
    log_path = task_dir / "log"
    lines = _tail_file(log_path, 5)
    return lines[-1] if lines else ""


def _heartbeat_age(task: Task) -> Optional[str]:
    """Human-readable age of last heartbeat from log mtime."""
    if task.brief_path is None:
        return None
    task_dir = Path(task.brief_path).parent
    log_path = task_dir / "log"
    if not log_path.exists():
        return None
    age = time.time() - log_path.stat().st_mtime
    if age < 60:
        return f"{int(age)}s ago"
    return f"{int(age // 60)}m{int(age % 60)}s ago"


# ---------------------------------------------------------------------------
# Panel renderers
# ---------------------------------------------------------------------------


def render_queue_panel(tasks: list[Task]) -> Panel:
    counts: Counter[TaskState] = Counter(t.state for t in tasks)
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="left")
    table.add_column(justify="right")
    table.add_column(justify="left")

    for state in TaskState:
        n = counts.get(state, 0)
        colour = STATE_COLOUR[state]
        icon = STATE_ICON[state]
        label = Text(state.value, style=colour)
        count_text = Text(str(n), style="bold " + colour)
        table.add_row(label, count_text, Text.from_markup(icon))

    return Panel(table, title="[bold]Queue[/]", border_style="blue", box=box.ROUNDED)


def render_current_task_panel(tasks: list[Task]) -> Panel:
    running = [t for t in tasks if t.state == TaskState.RUNNING]
    if not running:
        content = Text("no task running", style="bright_black")
        return Panel(content, title="[bold]Current task[/]", border_style="green", box=box.ROUNDED)

    task = running[0]  # show the first running task
    table = Table.grid(padding=(0, 1))
    table.add_column(style="bright_black", justify="right")
    table.add_column()

    table.add_row("id:", Text(task.id, style="bold green"))
    table.add_row("worker:", Text(task.worker_pref, style="cyan"))

    if task.started_at is not None:
        import datetime

        start_dt = datetime.datetime.fromtimestamp(task.started_at)
        elapsed = time.time() - task.started_at
        mins, secs = divmod(int(elapsed), 60)
        table.add_row(
            "started:",
            Text(f"{start_dt.strftime('%H:%M')}  ({mins}m{secs:02d}s ago)", style="white"),
        )

    hb_age = _heartbeat_age(task)
    if hb_age:
        ok = "[green]✓[/]" if "s ago" in hb_age and int(hb_age.split("s")[0]) < 60 else ""
        table.add_row("heartbeat:", Text.from_markup(f"{hb_age} {ok}"))

    last_log = _running_task_last_log(task)
    if last_log:
        # Truncate to fit panel width
        if len(last_log) > 55:
            last_log = last_log[:52] + "..."
        table.add_row("last log:", Text(last_log, style="italic"))

    if len(running) > 1:
        table.add_row("also:", Text(f"+{len(running) - 1} more running", style="bright_black"))

    return Panel(table, title="[bold]Current task[/]", border_style="green", box=box.ROUNDED)


def render_commits_panel() -> Panel:
    commits = _git_recent(5)
    if not commits:
        body = Text("no git history", style="bright_black")
    else:
        body = Text("\n".join(commits), style="white")
    return Panel(body, title="[bold]Recent commits[/]", border_style="blue", box=box.ROUNDED)


def render_triage_panel() -> Panel:
    lines = _last_triage()
    if not lines:
        body = Text("none", style="bright_black")
    else:
        body = Text("\n".join(lines), style="white")
    return Panel(
        body, title="[bold]Last triage decision[/]", border_style="yellow", box=box.ROUNDED
    )


def render_budget_panel(tasks: list[Task], cfg: BudgetConfig) -> Panel:
    spawns_24h = _count_24h_spawns()
    spawn_ok = spawns_24h <= cfg.daily_spawn_threshold
    spawn_icon = "[green]✓[/]" if spawn_ok else "[yellow]burning fast[/]"

    # Phase LLM-active: we don't have a phase manifest yet in Phase 0, so
    # surface a placeholder unless we find phase-level data in the task list.
    running_phases = sorted({t.phase for t in tasks if t.state == TaskState.RUNNING})
    if running_phases:
        phase_str = f"phase-{running_phases[0]} LLM-active N/A / expected N/A"
    else:
        phase_str = "phase-N N/A (no manifest)"

    # Runaway subagent count: tasks blocked with runaway reason
    runaway = sum(
        1
        for t in tasks
        if t.state == TaskState.BLOCKED and (t.blocked_reason or "").startswith("runaway")
    )
    runaway_icon = "[green]✓[/]" if runaway == 0 else "[red]![/]"

    table = Table.grid(padding=(0, 2))
    table.add_column(min_width=40)
    table.add_column()

    table.add_row(
        Text(phase_str, style="bright_black"),
        Text.from_markup(""),
    )
    table.add_row(
        Text(
            f"24h spawns: {spawns_24h} / threshold {cfg.daily_spawn_threshold}",
            style="white" if spawn_ok else "yellow",
        ),
        Text.from_markup(spawn_icon),
    )
    table.add_row(
        Text(f"runaway subagents this run: {runaway}", style="white" if runaway == 0 else "red"),
        Text.from_markup(runaway_icon),
    )

    pause_active = (EXP_DIR / "PAUSE").exists()
    stop_active = (EXP_DIR / "STOP").exists()
    if stop_active:
        table.add_row(Text("STOP sentinel active", style="bold red"), Text.from_markup("[red]■[/]"))
    elif pause_active:
        table.add_row(
            Text("PAUSE sentinel active", style="bold yellow"),
            Text.from_markup("[yellow]⏸[/]"),
        )

    return Panel(table, title="[bold]Budget[/]", border_style="magenta", box=box.ROUNDED)


def render_events_panel() -> Panel:
    lines = _tail_file(EVENTS_LOG, 20)
    if not lines:
        body = Text("no events yet", style="bright_black")
    else:
        body = Text("\n".join(lines), style="white")
    rel = EVENTS_LOG.relative_to(REPO_ROOT)
    return Panel(
        body,
        title=f"[bold]Event tail[/] [bright_black](last 20 lines of {rel})[/]",
        border_style="bright_black",
        box=box.ROUNDED,
    )


# ---------------------------------------------------------------------------
# Main layout builder
# ---------------------------------------------------------------------------


def build_layout() -> tuple[Panel, ...]:
    """Return all panel objects that make up one dashboard frame."""
    tasks = _load_tasks()
    cfg = _budget_config()

    queue_panel = render_queue_panel(tasks)
    current_panel = render_current_task_panel(tasks)
    commits_panel = render_commits_panel()
    triage_panel = render_triage_panel()
    budget_panel = render_budget_panel(tasks, cfg)
    events_panel = render_events_panel()

    return queue_panel, current_panel, commits_panel, triage_panel, budget_panel, events_panel


def render_dashboard() -> Layout:
    """Compose a single-frame Layout from all panels."""
    queue_p, current_p, commits_p, triage_p, budget_p, events_p = build_layout()

    layout = Layout()

    layout.split_column(
        Layout(name="header", size=1),
        Layout(name="top_row", size=10),
        Layout(name="mid_row", size=10),
        Layout(name="budget_row", size=7),
        Layout(name="events_row"),
    )

    layout["header"].update(
        Text(
            "MAYOR — experiment-rigour scaffold",
            style="bold white on blue",
            justify="center",
        )
    )

    layout["top_row"].split_row(
        Layout(queue_p, name="queue"),
        Layout(current_p, name="current"),
    )

    layout["mid_row"].split_row(
        Layout(commits_p, name="commits"),
        Layout(triage_p, name="triage"),
    )

    layout["budget_row"].update(budget_p)
    layout["events_row"].update(events_p)

    return layout


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live operator dashboard for the experiment scaffold.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Render one frame and exit (useful for CI / smoke testing).",
    )
    args = parser.parse_args()

    console = Console()

    # Also exit without looping when stdout is not a tty (e.g. pipe / test).
    if args.once or not sys.stdout.isatty():
        console.print(render_dashboard())
        return

    with Live(render_dashboard(), console=console, refresh_per_second=1, screen=True) as live:
        while True:
            time.sleep(REFRESH_SECONDS)
            live.update(render_dashboard())


if __name__ == "__main__":
    main()
