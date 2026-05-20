#!/usr/bin/env bash
# Cross-family verifier launcher. Called by src/experiment/tick.py (via
# SpawnFn) as a detached background process to independently verify a
# task brief after the worker has finished.
#
# Reads $workdir/brief.md, parses the optional YAML frontmatter for a
# `model:` key, and dispatches to the correct LLM CLI:
#   gpt-5.5 → codex exec --model gpt-5.5  (default — cross-family inverse)
#   sonnet  → claude -p --model sonnet --output-format text
#
# The verifier is instructed to read $workdir/diff.patch and $workdir/log,
# independently verify each acceptance criterion, and write $workdir/result.json.
# It must not echo the worker's claim — it must reach its own verdict.
#
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
  echo "usage: experiment-spawn-verifier.sh <task_id> <workdir>" >&2
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
  echo "experiment-spawn-verifier: ERROR — brief.md not found at $BRIEF" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# Parse frontmatter model (default: gpt-5.5 for verifier — cross-family inverse)
# ---------------------------------------------------------------------------
DEFAULT_MODEL="gpt-5.5"

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
## Verifier instruction

You are an independent verifier. Do NOT simply echo the worker's conclusions.

1. Read the worker's diff at:     $WORKDIR/diff.patch
2. Read the worker's log at:      $WORKDIR/log
3. For each acceptance criterion listed in the brief above, independently
   verify it by running the relevant tests, inspecting outputs, or reasoning
   from the diff. Do not assume the worker is correct.

When you have completed your independent verification, write a JSON file at:

  $RESULT

The file must contain exactly:

  {\"acceptance\": \"pass\" | \"fail\", \"evidence\": \"<one-paragraph summary>\"}

Set acceptance to \"pass\" only if you have independently confirmed that every
acceptance criterion is met. If the diff.patch or log are missing, treat that
as a verification failure and set acceptance to \"fail\".
Task ID: $TASK_ID
"

PROMPT="${BRIEF_BODY}${RESULT_INSTRUCTION}"

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
echo "experiment-spawn-verifier: task=$TASK_ID model=$MODEL"

case "$MODEL" in
  sonnet)
    claude -p --model sonnet --output-format text "$PROMPT"
    ;;
  gpt-5.5)
    codex exec --model gpt-5.5 "$PROMPT"
    ;;
  *)
    echo "experiment-spawn-verifier: ERROR — unknown model '$MODEL' (supported: sonnet, gpt-5.5)" >&2
    exit 1
    ;;
esac

MODEL_EXIT=$?

# ---------------------------------------------------------------------------
# Gate: confirm result.json was written
# ---------------------------------------------------------------------------
if [ $MODEL_EXIT -ne 0 ]; then
  echo "experiment-spawn-verifier: model invocation exited $MODEL_EXIT" >&2
  exit $MODEL_EXIT
fi

if [ ! -f "$RESULT" ]; then
  echo "experiment-spawn-verifier: ERROR — model exited 0 but $RESULT was not created" >&2
  exit 1
fi

echo "experiment-spawn-verifier: done — result.json written"
