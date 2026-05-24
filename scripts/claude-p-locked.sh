#!/usr/bin/env bash
# Serialise `claude -p` invocations on this machine.
#
# The Claude Code CLI keeps a single OAuth session under ~/.claude/. When
# two `claude -p` processes run concurrently they race on the shared token
# (observed in pass-29: one invocation 401s with "Not logged in" while
# another is mid-call). The experiment scaffold spawns workers and
# verifiers in parallel, which is exactly the unsupported case.
#
# This wrapper holds a machine-wide lock for the duration of one
# `claude -p` call, then releases. Callers see normal claude -p behaviour
# but lose parallelism for as long as the lock is contended.
#
# Lock primitive: mkdir on a sentinel directory. mkdir is atomic on every
# POSIX filesystem we care about and ships on macOS without Homebrew
# (flock does not). Stale lock detection runs at MAX_LOCK_AGE_SEC.
#
# Override knobs (env vars):
#   CLAUDE_P_LOCK_DIR       (default /tmp/claude-p.lock.d)
#   CLAUDE_P_LOCK_MAX_AGE   stale-lock break threshold, seconds (default 1800)
#   CLAUDE_P_LOCK_MAX_WAIT  give-up threshold, seconds (default 3600)
#   CLAUDE_P_LOCK_POLL      poll interval, seconds (default 2)
set -euo pipefail

LOCK_DIR="${CLAUDE_P_LOCK_DIR:-/tmp/claude-p.lock.d}"
MAX_AGE="${CLAUDE_P_LOCK_MAX_AGE:-1800}"
MAX_WAIT="${CLAUDE_P_LOCK_MAX_WAIT:-3600}"
POLL="${CLAUDE_P_LOCK_POLL:-2}"

# Portable mtime read: BSD stat (macOS) uses -f %m, GNU stat uses -c %Y.
_dir_mtime() {
  stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0
}

_acquire() {
  local waited=0
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    if [ -d "$LOCK_DIR" ]; then
      local age=$(( $(date +%s) - $(_dir_mtime "$LOCK_DIR") ))
      if [ "$age" -gt "$MAX_AGE" ]; then
        echo "claude-p-locked: breaking stale lock (age ${age}s > ${MAX_AGE}s)" >&2
        rmdir "$LOCK_DIR" 2>/dev/null || true
        continue
      fi
    fi
    sleep "$POLL"
    waited=$(( waited + POLL ))
    if [ "$waited" -ge "$MAX_WAIT" ]; then
      echo "claude-p-locked: gave up waiting for lock after ${waited}s" >&2
      return 1
    fi
  done
  # Stash holder pid for debugging; mkdir is the lock, this is just info.
  echo "$$" > "$LOCK_DIR/holder.pid" 2>/dev/null || true
}

_release() {
  rm -f "$LOCK_DIR/holder.pid" 2>/dev/null || true
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

if ! _acquire; then
  exit 1
fi
trap _release EXIT INT TERM

claude -p "$@"
