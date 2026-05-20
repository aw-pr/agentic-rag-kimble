#!/usr/bin/env bash
# Interactive control REPL for the experiment scaffold.
#
# Why this exists
# ---------------
# Sits in the bottom pane of the `mayor` tmux session (launched by
# scripts/experiment-mayor.sh). Lets the operator pause, resume, stop,
# inspect, and fix tasks without touching state.yaml by hand.
#
# All state-mutating commands (requeue, verify) use src.experiment.state
# via a small Python helper so the YAML serialisation stays consistent.
# File-touch commands (pause/resume/stop) are pure bash — no Python needed.
#
# Usage: run interactively. Type `help` to list commands.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXP_DIR="$REPO_ROOT/runs/experiment"
STATE="$EXP_DIR/state.yaml"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ensure_exp_dir() {
  mkdir -p "$EXP_DIR"
}

_require_task_id() {
  local tid="$1"
  if [ -z "$tid" ]; then
    echo "error: task-id required" >&2
    return 1
  fi
}

_python() {
  # Run a Python snippet with the repo on sys.path.
  python3 -c "import sys; sys.path.insert(0, '$REPO_ROOT'); $*"
}

# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

cmd_pause() {
  _ensure_exp_dir
  touch "$EXP_DIR/PAUSE"
  echo "paused: PAUSE sentinel written — tick will stop spawning new workers."
}

cmd_resume() {
  _ensure_exp_dir
  rm -f "$EXP_DIR/PAUSE"
  echo "resumed: PAUSE sentinel removed."
}

cmd_stop() {
  _ensure_exp_dir
  touch "$EXP_DIR/STOP"
  echo "stopped: STOP sentinel written — tick will SIGTERM running workers."
}

cmd_show() {
  local tid="$1"
  _require_task_id "$tid"
  _python "
from pathlib import Path
from src.experiment.state import load
import yaml, dataclasses
tasks = load(Path('$STATE'))
match = [t for t in tasks if t.id == '$tid']
if not match:
    print('task not found: $tid')
else:
    import yaml, dataclasses
    d = dataclasses.asdict(match[0])
    d['state'] = match[0].state.value
    print(yaml.dump(d, sort_keys=False).rstrip())
"
}

cmd_brief() {
  local tid="$1"
  _require_task_id "$tid"
  local brief="$EXP_DIR/tasks/$tid/brief.md"
  if [ ! -f "$brief" ]; then
    echo "no brief found at $brief" >&2
    return 1
  fi
  "${PAGER:-less}" "$brief"
}

cmd_tail() {
  local tid="$1"
  _require_task_id "$tid"
  local log="$EXP_DIR/tasks/$tid/log"
  if [ ! -f "$log" ]; then
    echo "no log found at $log" >&2
    return 1
  fi
  echo "tailing $log  (Ctrl-C to stop)"
  tail -f "$log"
}

cmd_requeue() {
  local tid="$1"
  _require_task_id "$tid"
  _python "
from pathlib import Path
from src.experiment.state import load, save, TaskState
p = Path('$STATE')
tasks = load(p)
match = [t for t in tasks if t.id == '$tid']
if not match:
    print('task not found: $tid')
else:
    t = match[0]
    t.state = TaskState.QUEUED
    t.attempts = 0
    t.pid = None
    t.started_at = None
    t.last_heartbeat = None
    t.blocked_reason = None
    save(tasks, p)
    print('requeued: $tid  (state=queued, attempts=0)')
"
}

cmd_ack_overspend() {
  local phase="$1"
  if [ -z "$phase" ]; then
    echo "error: phase number required (e.g. ack-overspend 4)" >&2
    return 1
  fi
  local ack_dir="$EXP_DIR/ack"
  mkdir -p "$ack_dir"
  touch "$ack_dir/phase-${phase}"
  echo "ack-overspend: wrote $ack_dir/phase-${phase} — soft brake released for phase $phase."
}

cmd_verify() {
  local tid="$1"
  _require_task_id "$tid"
  _python "
from pathlib import Path
from src.experiment.state import load, save, Task, TaskState
import time
p = Path('$STATE')
tasks = load(p)
match = [t for t in tasks if t.id == '$tid']
if not match:
    print('task not found: $tid')
else:
    src = match[0]
    new_id = '${tid}-oob-verify-' + str(int(time.time()))[-6:]
    verifier = Task(
        id=new_id,
        phase=src.phase,
        tier='T1',
        worker_pref=src.verifier_pref,
        verifier_pref=src.worker_pref,
        depends_on=['$tid'],
        state=TaskState.QUEUED,
        attempts=0,
        max_attempts=1,
        brief_path=src.brief_path,
        acceptance=src.acceptance,
    )
    tasks.append(verifier)
    save(tasks, p)
    print(f'verify: enqueued out-of-band verifier task {new_id!r}')
"
}

cmd_help() {
  cat <<'HELP'
Mayor control shell — commands
  pause                     touch PAUSE sentinel; tick stops new spawns
  resume                    remove PAUSE sentinel
  stop                      touch STOP sentinel; tick SIGTERMs running workers
  show <task-id>            print full state row from state.yaml
  brief <task-id>           open task brief.md in $PAGER (default: less)
  tail <task-id>            tail -f the task's log file (Ctrl-C to stop)
  requeue <task-id>         reset task to queued, attempts=0
  ack-overspend <phase>     release soft brake for the named phase
  verify <task-id>          enqueue an out-of-band verifier for the task
  help                      show this message
  quit                      exit the control shell (tmux session stays up)
HELP
}

# ---------------------------------------------------------------------------
# REPL loop
# ---------------------------------------------------------------------------

echo "Mayor control shell. Type 'help' for commands."
while IFS= read -r -p "> " line 2>/dev/null || IFS= read -r line; do
  # Trim leading/trailing whitespace
  line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [ -z "$line" ] && continue

  # Split into command and first argument
  cmd="${line%% *}"
  rest="${line#* }"
  # If no space, rest equals cmd; clear it.
  [ "$rest" = "$cmd" ] && rest=""
  arg1="${rest%% *}"

  case "$cmd" in
    pause)           cmd_pause ;;
    resume)          cmd_resume ;;
    stop)            cmd_stop ;;
    show)            cmd_show "$arg1" ;;
    brief)           cmd_brief "$arg1" ;;
    tail)            cmd_tail "$arg1" ;;
    requeue)         cmd_requeue "$arg1" ;;
    ack-overspend)   cmd_ack_overspend "$arg1" ;;
    verify)          cmd_verify "$arg1" ;;
    help|"?")        cmd_help ;;
    quit|exit|q)     echo "Goodbye."; exit 0 ;;
    *)
      echo "unknown command: $cmd  (type 'help' for list)" ;;
  esac
done
