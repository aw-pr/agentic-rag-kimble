#!/usr/bin/env bash
# Heartbeat entry point. Called by cron every 5 minutes (set up via
# scripts/install-experiment-cron.sh).
#
# Responsibilities at the shell level (kept out of Python so the loop
# stays pure):
#   1. cd into the repo so all paths are absolute and stable.
#   2. Append a one-line tick marker to the events log.
#   3. Invoke the Python tick (src.experiment.tick) which does the
#      actual state-machine work.
#   4. On STOP sentinel: SIGTERM any worker pids it reports as running,
#      so a "stop" from the Mayor is immediate, not next-tick.
#
# Designed to be idempotent and crash-safe: a tick that dies leaves
# state on disk consistent with what it observed, and the next tick
# picks up from there.
set -euo pipefail
cd "$(dirname "$0")/.."

EXP_DIR="runs/experiment"
STATE="$EXP_DIR/state.yaml"
BUDGET="$EXP_DIR/budget.yaml"
EVENTS="$EXP_DIR/events.log"
mkdir -p "$EXP_DIR"

# Cold-start safety: missing files are normal on first run.
[ -f "$STATE" ] || : > "$STATE"
[ -f "$BUDGET" ] || cp "$EXP_DIR/budget.yaml.example" "$BUDGET" 2>/dev/null || true

ts="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
echo "$ts tick" >> "$EVENTS"

# Run the Python tick. JSON-formatted summary on stdout for the dashboard.
python3 -m src.experiment.tick_cli \
  --state "$STATE" \
  --budget "$BUDGET" \
  --exp-dir "$EXP_DIR" \
  --events "$EVENTS"
