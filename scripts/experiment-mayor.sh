#!/usr/bin/env bash
# Mayor session launcher.
#
# Why this exists
# ---------------
# Creates (or reattaches to) the `mayor` tmux session: the always-on
# operator view for the experiment scaffold. Two panes:
#   - Top (~70 % height): scripts/experiment-dashboard.py  (live, read-only)
#   - Bottom (~30 % height): scripts/experiment-control.sh (interactive REPL)
#
# If the session already exists (e.g. you detached with prefix-d), this
# script just attaches — it never destroys a running session.
#
# Usage:
#   ./scripts/experiment-mayor.sh          # create or reattach
#   tmux detach                            # (prefix-d) detach without killing
set -euo pipefail

SESSION="mayor"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DASHBOARD="$REPO_ROOT/scripts/experiment-dashboard.py"
CONTROL="$REPO_ROOT/scripts/experiment-control.sh"

# Resolve python3 path at launch time so the subcommand runs in the same env.
PYTHON3="$(command -v python3)"

# ---------------------------------------------------------------------------
# If session already exists, just attach.
# ---------------------------------------------------------------------------
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "mayor session already running — attaching."
  exec tmux attach-session -t "$SESSION"
fi

# ---------------------------------------------------------------------------
# Create new session.
# The first window starts detached so we can set it up before attaching.
# ---------------------------------------------------------------------------
tmux new-session -d -s "$SESSION" -x 220 -y 50

# Top pane: live dashboard (~70 % of height)
tmux send-keys -t "$SESSION:0.0" \
  "cd '$REPO_ROOT' && $PYTHON3 '$DASHBOARD'" Enter

# Split horizontally (30 % for the bottom pane) and run the control shell.
tmux split-window -v -p 30 -t "$SESSION:0"
tmux send-keys -t "$SESSION:0.1" \
  "cd '$REPO_ROOT' && bash '$CONTROL'" Enter

# Put focus in the control pane so the operator can type immediately.
tmux select-pane -t "$SESSION:0.1"

# Attach.
exec tmux attach-session -t "$SESSION"
