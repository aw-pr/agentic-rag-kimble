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
if [ $# -lt 2 ] || [ $# -gt 3 ]; then
  echo "usage: experiment-spawn-verifier.sh <task_id> <workdir> [model]" >&2
  exit 1
fi

TASK_ID="$1"
WORKDIR="$2"
CLI_MODEL="${3:-}"
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

# $3 (CLI_MODEL) from state.yaml is authoritative; frontmatter and default are fallbacks.
if [ -n "$CLI_MODEL" ]; then
  MODEL="$CLI_MODEL"
  MODEL_SOURCE="cli"
else
  MODEL_RAW="$(parse_model)"
  if [ -n "$MODEL_RAW" ]; then
    MODEL="$MODEL_RAW"
    MODEL_SOURCE="frontmatter"
  else
    MODEL="$DEFAULT_MODEL"
    MODEL_SOURCE="default"
  fi
fi

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------
BRIEF_BODY="$(awk '
  /^---[[:space:]]*$/ && NR == 1 { in_fm=1; next }
  in_fm && /^---[[:space:]]*$/ { in_fm=0; next }
  !in_fm { print }
' "$BRIEF")"

# Verifier instruction varies by model:
#
# - sonnet path: respond with ONLY a JSON object. The wrapper captures
#   the JSON envelope from `claude -p --output-format json`, extracts
#   `.result`, and writes $RESULT itself. This bypasses the Write tool,
#   which sonnet-as-verifier was unreliable at invoking (pass-29 bug X).
#
# - gpt-5.5 path: codex exec runs the verifier with workspace-write,
#   so it writes $RESULT directly. The file-write instruction stays.
VERIFIER_PREAMBLE="
---
## Verifier instruction

You are an independent verifier. Do NOT simply echo the worker's conclusions.

1. Read the worker's diff at:     $WORKDIR/diff.patch
2. Read the worker's log at:      $WORKDIR/log
3. For each acceptance criterion listed in the brief above, independently
   verify it by running the relevant tests, inspecting outputs, or reasoning
   from the diff. Do not assume the worker is correct.

Set acceptance to \"pass\" only if you have independently confirmed that every
acceptance criterion is met. If the diff.patch or log are missing, treat that
as a verification failure and set acceptance to \"fail\".
Task ID: $TASK_ID
"

VERIFIER_FILE_TAIL="
When you have completed your independent verification, write a JSON file at:

  $RESULT

The file must contain exactly:

  {\"acceptance\": \"pass\" | \"fail\", \"evidence\": \"<one-paragraph summary>\"}
"

VERIFIER_INLINE_TAIL="
## Output format — read carefully

Your response is parsed by a wrapper script that searches the response
for a single JSON object and writes it to disk. Anything else in your
response is discarded. If no JSON object is found, the task fails.

Respond with a JSON object of exactly this shape:

  {\"acceptance\": \"pass\", \"evidence\": \"<one-paragraph summary of what you checked and what you found>\"}

or:

  {\"acceptance\": \"fail\", \"evidence\": \"<one-paragraph summary of why this fails verification>\"}

Examples of VALID responses (any of these would be accepted):

  {\"acceptance\":\"pass\",\"evidence\":\"Both files exist with expected content; criteria met.\"}

  Here is my verdict:
  {\"acceptance\":\"pass\",\"evidence\":\"Both files exist with expected content.\"}

Examples of INVALID responses (these would FAIL the task):

  Both files exist with the correct content. Acceptance criteria are met.
  ^ no JSON object — task will fail

  acceptance: pass
  evidence: Both files exist.
  ^ YAML, not JSON — task will fail

Do not call any Write tool. The JSON object IS your verdict.
"

case "$MODEL" in
  sonnet)  PROMPT="${BRIEF_BODY}${VERIFIER_PREAMBLE}${VERIFIER_INLINE_TAIL}" ;;
  *)       PROMPT="${BRIEF_BODY}${VERIFIER_PREAMBLE}${VERIFIER_FILE_TAIL}" ;;
esac

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
echo "experiment-spawn-verifier: task=$TASK_ID model=$MODEL (source=$MODEL_SOURCE)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_EXIT=0
case "$MODEL" in
  sonnet)
    # claude-p-locked.sh serialises parallel claude -p invocations on
    # this machine to avoid the OAuth token race (pass-29 bug Z).
    # --output-format json wraps the model's text response in an envelope;
    # we extract `.result` (the model's response, instructed to be a bare
    # JSON object) and persist it to $RESULT ourselves — sidesteps bug X
    # (sonnet-as-verifier doesn't reliably call the Write tool).
    SONNET_RAW="$(mktemp)"
    if ! "$SCRIPT_DIR/claude-p-locked.sh" --model sonnet --output-format json "$PROMPT" >"$SONNET_RAW"; then
      MODEL_EXIT=$?
      cat "$SONNET_RAW" >&2 || true
      rm -f "$SONNET_RAW"
      echo "experiment-spawn-verifier: claude -p exited $MODEL_EXIT" >&2
      exit "$MODEL_EXIT"
    fi
    # Envelope shape: {"type":"result", "result":"<text>", ...}. The
    # `.result` field is the model's textual answer — we asked for a
    # bare JSON object; extract and validate. If the model wrapped it in
    # markdown fences anyway, strip them.
    if ! command -v jq >/dev/null 2>&1; then
      echo "experiment-spawn-verifier: ERROR — jq required to parse sonnet output envelope" >&2
      rm -f "$SONNET_RAW"
      exit 1
    fi
    RESULT_TEXT="$(jq -r '.result // empty' "$SONNET_RAW")"
    rm -f "$SONNET_RAW"
    if [ -z "$RESULT_TEXT" ]; then
      echo "experiment-spawn-verifier: ERROR — empty .result from claude envelope" >&2
      exit 1
    fi
    # Strip optional ```json … ``` fences and any leading/trailing prose.
    CLEAN="$(printf '%s' "$RESULT_TEXT" | sed -E 's/^[[:space:]]*```(json)?[[:space:]]*//; s/```[[:space:]]*$//')"
    if printf '%s' "$CLEAN" | jq -e 'has("acceptance")' >/dev/null 2>&1; then
      printf '%s' "$CLEAN" > "$RESULT"
    else
      # Last resort: extract a {...} object containing "acceptance".
      EXTRACTED="$(printf '%s' "$CLEAN" | python3 -c '
import json, re, sys
text = sys.stdin.read()
m = re.search(r"\{[^{}]*\"acceptance\"[^{}]*\}", text, re.DOTALL)
if not m:
    sys.exit(1)
obj = json.loads(m.group(0))
print(json.dumps(obj))
' 2>/dev/null)" || true
      if [ -n "$EXTRACTED" ]; then
        printf '%s' "$EXTRACTED" > "$RESULT"
      else
        echo "experiment-spawn-verifier: ERROR — sonnet response not parseable as {acceptance,evidence}" >&2
        echo "Raw response was:" >&2
        printf '%s\n' "$RESULT_TEXT" >&2
        exit 1
      fi
    fi
    ;;
  gpt-5.5)
    # --sandbox workspace-write: codex defaults to read-only, which blocks
    # the verifier from writing $RESULT (the one thing this script needs).
    # workspace-write scopes writes to the current working directory only.
    codex exec --model gpt-5.5 --sandbox workspace-write "$PROMPT" || MODEL_EXIT=$?
    ;;
  *)
    echo "experiment-spawn-verifier: ERROR — unknown model '$MODEL' (supported: sonnet, gpt-5.5)" >&2
    exit 1
    ;;
esac

# ---------------------------------------------------------------------------
# Gate: confirm result.json was written
# ---------------------------------------------------------------------------
if [ "$MODEL_EXIT" -ne 0 ]; then
  echo "experiment-spawn-verifier: model invocation exited $MODEL_EXIT" >&2
  exit "$MODEL_EXIT"
fi

if [ ! -f "$RESULT" ]; then
  echo "experiment-spawn-verifier: ERROR — model exited 0 but $RESULT was not created" >&2
  exit 1
fi

echo "experiment-spawn-verifier: done — result.json written"
