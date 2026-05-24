#!/usr/bin/env bash
set -u

cd "$(dirname "$0")/.."

failures=()

add_failure() {
  failures+=("$1")
}

if ! command -v python3 >/dev/null 2>&1; then
  add_failure "python3 not found on PATH"
else
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    add_failure "Python 3.11+ required"
  fi
fi

if ! python3 -c 'import claude_agent_sdk' >/dev/null 2>&1; then
  add_failure "claude-agent-sdk is not importable"
fi

if ! python3 -c '
from src.config import get_config
from src.graph.db import GraphDB
with GraphDB(get_config()) as db:
    db.execute("RETURN 1 AS ok")
' >/dev/null 2>&1; then
  add_failure "LadybugDB/VECTOR extension check failed"
fi

if ! command -v claude >/dev/null 2>&1; then
  add_failure "claude CLI not found (OAuth session check unavailable)"
elif ! claude --version >/dev/null 2>&1; then
  add_failure "claude CLI check failed (OAuth session may be unavailable)"
fi

# Create data/ if missing — a fresh clone won't have it, and there's no
# reason to fail the preflight on something this trivial.
mkdir -p data 2>/dev/null || true
if [[ ! -d data ]]; then
  add_failure "data/ directory could not be created (parent not writable?)"
elif [[ ! -w data ]]; then
  add_failure "data/ exists but is not writable"
fi

if [[ ${#failures[@]} -eq 0 ]]; then
  echo "OK"
  exit 0
fi

echo "Preflight failed:"
i=1
for failure in "${failures[@]}"; do
  echo "$i. $failure"
  i=$((i + 1))
done
exit 1
