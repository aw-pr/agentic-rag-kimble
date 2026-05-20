#!/usr/bin/env bash
# Cross-family worker launcher. Called by src/experiment/tick.py (via
# SpawnFn) as a detached background process to execute a task brief.
#
# Reads $workdir/brief.md, parses the optional YAML frontmatter for a
# `model:` key, and dispatches to the correct LLM CLI:
#   sonnet  → claude -p --model sonnet --output-format text
#   gpt-5.5 → codex exec --model gpt-5.5
#
# The model is instructed to write $workdir/result.json on completion.
# stdout/stderr stream to the inherited file descriptor (tick redirects
# those to $workdir/log before spawning — do not redirect here).
#
# Auth invariants: never set ANTHROPIC_API_KEY or OPENAI_API_KEY.
# Claude uses the Claude Code OAuth session; Codex uses ~/.codex/auth.json.
set -euo pipefail

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
if [ $# -ne 2 ]; then
  echo "usage: experiment-spawn-worker.sh <task_id> <workdir>" >&2
  exit 1
fi

TASK_ID="$1"
WORKDIR="$2"
BRIEF="$WORKDIR/brief.md"
RESULT="$WORKDIR/result.json"

# ---------------------------------------------------------------------------
# Validate inputs
# ---------------------------------------------------------------------------
if [ ! -f "$BRIEF" ]; then
  echo "experiment-spawn-worker: ERROR — brief.md not found at $BRIEF" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# Parse frontmatter model (default: sonnet for worker)
# ---------------------------------------------------------------------------
DEFAULT_MODEL="sonnet"

parse_model() {
  # Extract `model:` value from YAML frontmatter (between opening --- and
  # closing --- lines).  Returns empty string if no frontmatter or no key.
  awk '
    /^---[[:space:]]*$/ && NR == 1 { in_fm=1; next }
    in_fm && /^---[[:space:]]*$/ { in_fm=0; next }
    in_fm && /^model:[[:space:]]*/ {
      sub(/^model:[[:space:]]*/, "")
      gsub(/[[:space:]]*$/, "")
      print
    }
  ' "$BRIEF"
}

MODEL_RAW="$(parse_model)"
MODEL="${MODEL_RAW:-$DEFAULT_MODEL}"

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------
BRIEF_BODY="$(awk '
  /^---[[:space:]]*$/ && NR == 1 { in_fm=1; next }
  in_fm && /^---[[:space:]]*$/ { in_fm=0; next }
  !in_fm { print }
' "$BRIEF")"

RESULT_INSTRUCTION="
---
## Worker instruction

When you have completed all work described above, write a JSON file at:

  $RESULT

The file must contain exactly:

  {\"acceptance\": \"pass\" | \"fail\", \"evidence\": \"<one-paragraph summary>\"}

Set acceptance to \"pass\" only if every acceptance criterion in the brief is fully met.
Do not write the file until you have verified each criterion.
Task ID: $TASK_ID
"

PROMPT="${BRIEF_BODY}${RESULT_INSTRUCTION}"

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
echo "experiment-spawn-worker: task=$TASK_ID model=$MODEL"

case "$MODEL" in
  sonnet)
    claude -p --model sonnet --output-format text "$PROMPT"
    ;;
  gpt-5.5)
    codex exec --model gpt-5.5 "$PROMPT"
    ;;
  *)
    echo "experiment-spawn-worker: ERROR — unknown model '$MODEL' (supported: sonnet, gpt-5.5)" >&2
    exit 1
    ;;
esac

MODEL_EXIT=$?

# ---------------------------------------------------------------------------
# Gate: confirm result.json was written
# ---------------------------------------------------------------------------
if [ $MODEL_EXIT -ne 0 ]; then
  echo "experiment-spawn-worker: model invocation exited $MODEL_EXIT" >&2
  exit $MODEL_EXIT
fi

if [ ! -f "$RESULT" ]; then
  echo "experiment-spawn-worker: ERROR — model exited 0 but $RESULT was not created" >&2
  exit 1
fi

echo "experiment-spawn-worker: done — result.json written"
