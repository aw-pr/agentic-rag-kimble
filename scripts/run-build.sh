#!/usr/bin/env bash
# Build orchestration for agentic-rag-kimble.
#
# Implements:
#   - Sequential phases with inter-phase review agents
#   - Parallel execution for phases with no shared state
#   - Bounded self-improvement eval loop (max 3 iterations)
#
# Usage:
#   ./scripts/run-build.sh                    # full build from current state
#   ./scripts/run-build.sh --from 05          # resume from phase 05
#   ./scripts/run-build.sh --phase 06         # run one phase only
#   ./scripts/run-build.sh --eval-loop-only   # run improvement loop only

set -euo pipefail
cd "$(dirname "$0")/.."

PROMPTS="agent-prompts"
FROM_PHASE=""
SINGLE_PHASE=""
EVAL_LOOP_ONLY=0
EVAL_TARGET=0.6
EVAL_MAX_ITERATIONS=3

# Model assignments — Sonnet for implementation, Opus for judgment
MODEL_IMPL="claude-sonnet-4-6"      # phases 04-10: code generation, structured tasks
MODEL_JUDGE="claude-opus-4-7"       # review agents + tune agent: diagnosis, tradeoff reasoning

for arg in "$@"; do
  case $arg in
    --from) FROM_PHASE="${2:-}" ;;
    --phase) SINGLE_PHASE="${2:-}" ;;
    --eval-loop-only) EVAL_LOOP_ONLY=1 ;;
  esac
done

# ── Helpers ──────────────────────────────────────────────────────────────────

log() { echo "[build] $*"; }

phase_done() {
  local phase="$1"
  git log --oneline | grep -q "^[a-f0-9]* feat.*phase-${phase}\|agent-0${phase}" 2>/dev/null \
    || git log --oneline | grep -q "agent-${phase}" 2>/dev/null
}

build_context() {
  # Assembles: ontology + optional review + task prompt → stdout
  local prompt="$1"
  local review_file="${2:-}"
  {
    echo "## Domain ontology (canonical definitions — use these everywhere)"
    echo ""
    cat "docs/ontology.md"
    echo ""
    echo "---"
    echo ""
    if [[ -f "$review_file" ]]; then
      echo "## Review findings from previous phase"
      echo ""
      cat "$review_file"
      echo ""
      echo "---"
      echo ""
    fi
    cat "$prompt"
  }
}

run_phase() {
  local n="$1"
  local label="${2:-phase $n}"
  local prompt_file="${PROMPTS}/agent-$(printf '%02d' "$n")-*.md"

  local prompt
  prompt=$(ls $prompt_file 2>/dev/null | head -1)
  if [[ -z "$prompt" ]]; then
    log "ERROR: no prompt file found matching $prompt_file"
    exit 1
  fi

  log "=== Phase $n: $label ==="

  local review_file="${PROMPTS}/review-$(printf '%02d' $((n-1))).md"
  local combined
  combined=$(mktemp)
  build_context "$prompt" "$review_file" > "$combined"
  claude --model "$MODEL_IMPL" -p "$(cat "$combined")"
  rm "$combined"
}

run_review() {
  local completed_phase="$1"
  local next_phase="$2"
  local review_file="${PROMPTS}/review-$(printf '%02d' "$completed_phase").md"

  log "--- Review: phase $completed_phase → preparing phase $next_phase ---"

  local review_prompt
  review_prompt=$(cat "${PROMPTS}/review-template.md")
  review_prompt="${review_prompt//__PHASE__/$completed_phase}"
  review_prompt="${review_prompt//__NEXT__/$next_phase}"

  claude --model "$MODEL_JUDGE" -p "$review_prompt" > "$review_file"
  log "  Review saved to $review_file"
}

run_parallel() {
  local phase_a="$1"
  local phase_b="$2"
  local label_a="${3:-phase $phase_a}"
  local label_b="${4:-phase $phase_b}"

  log "=== Parallel: $label_a || $label_b ==="
  log "  WARNING: parallel agents must write to non-overlapping files."
  log "  Both agents will stage but NOT commit. A merge commit follows."

  local prompt_a prompt_b
  prompt_a=$(ls "${PROMPTS}/agent-$(printf '%02d' "$phase_a")-*.md" 2>/dev/null | head -1)
  prompt_b=$(ls "${PROMPTS}/agent-$(printf '%02d' "$phase_b")-*.md" 2>/dev/null | head -1)

  # Run in background, capture PIDs
  local combined_a combined_b
  combined_a=$(mktemp)
  combined_b=$(mktemp)
  build_context "$prompt_a" > "$combined_a"
  build_context "$prompt_b" > "$combined_b"
  printf '\nIMPORTANT: stage your changes with git add but do NOT commit. The orchestrator will commit.' >> "$combined_a"
  printf '\nIMPORTANT: stage your changes with git add but do NOT commit. The orchestrator will commit.' >> "$combined_b"

  claude --model "$MODEL_IMPL" -p "$(cat "$combined_a")" &
  PID_A=$!
  claude --model "$MODEL_IMPL" -p "$(cat "$combined_b")" &
  PID_B=$!
  rm "$combined_a" "$combined_b"

  # Wait for both
  wait $PID_A && log "  Phase $phase_a complete" || { log "  Phase $phase_a FAILED"; exit 1; }
  wait $PID_B && log "  Phase $phase_b complete" || { log "  Phase $phase_b FAILED"; exit 1; }

  # Merge commit
  git commit -m "feat: phases $phase_a and $phase_b (parallel execution)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>" || log "  Nothing to commit from parallel phases."
}

run_eval_loop() {
  log "=== Self-improvement eval loop (max $EVAL_MAX_ITERATIONS iterations) ==="

  for i in $(seq 1 $EVAL_MAX_ITERATIONS); do
    log "  Eval iteration $i / $EVAL_MAX_ITERATIONS"

    # Run offline retrieval eval
    python3 -m src.eval.metrics --output-json > runs/eval-loop-iter-${i}.json 2>/dev/null \
      || { log "  Eval metrics failed — check that ingestion and tools are working"; exit 1; }

    local score
    score=$(python3 -c "
import json, sys
with open('runs/eval-loop-iter-${i}.json') as f:
    d = json.load(f)
print(d.get('recall_at_10', 0))
" 2>/dev/null || echo "0")

    log "  recall@10 = $score  (target: $EVAL_TARGET)"

    if python3 -c "exit(0 if float('$score') >= $EVAL_TARGET else 1)" 2>/dev/null; then
      log "  Target achieved at iteration $i. Stopping."
      break
    fi

    if [[ $i -lt $EVAL_MAX_ITERATIONS ]]; then
      log "  Below target — running tuning agent..."
      local tune_combined
      tune_combined=$(mktemp)
      {
        echo "## Domain ontology"
        echo ""
        cat "docs/ontology.md"
        echo ""
        echo "---"
        echo ""
        sed "s/__ITER__/$i/g; s/__SCORE__/$score/g" "${PROMPTS}/tune-template.md"
      } > "$tune_combined"
      claude --model "$MODEL_JUDGE" -p "$(cat "$tune_combined")"
      rm "$tune_combined"
    else
      log "  Max iterations reached. Final recall@10: $score"
    fi
  done

  log "Eval loop complete. Results in runs/eval-loop-iter-*.json"
}

# ── Main build sequence ───────────────────────────────────────────────────────

if [[ $EVAL_LOOP_ONLY -eq 1 ]]; then
  run_eval_loop
  exit 0
fi

if [[ -n "$SINGLE_PHASE" ]]; then
  run_phase "$SINGLE_PHASE"
  exit 0
fi

# Phases 01-03 assumed complete (agent prompts already exist and were run)
# Start from phase 04 unless --from overrides

PHASES_DONE=3
[[ -n "$FROM_PHASE" ]] && PHASES_DONE=$((FROM_PHASE - 1))

# Phase 04: Eval harness (Karpathy move — build signal early)
if [[ $PHASES_DONE -lt 4 ]]; then
  run_phase 4 "eval harness (early)"
  run_review 4 5
fi

# Phase 05 + 06: Semantic layer and tool stubs (parallel — non-overlapping files)
if [[ $PHASES_DONE -lt 5 ]]; then
  run_parallel 5 6 "semantic layer" "tool implementations"
  run_review 6 7
fi

# Phase 07: Orchestrator (needs tools + semantic layer)
if [[ $PHASES_DONE -lt 7 ]]; then
  run_phase 7 "Claude orchestrator + auth"
  run_review 7 8
fi

# Self-improvement loop
if [[ $PHASES_DONE -lt 8 ]]; then
  run_eval_loop
fi

# Phase 08 + 09: UI and full eval integration (parallel — completely separate files)
if [[ $PHASES_DONE -lt 8 ]]; then
  run_parallel 8 9 "Streamlit UI" "eval integration"
  run_review 9 10
fi

# Phase 10: README (last — reflects the real built system)
if [[ $PHASES_DONE -lt 10 ]]; then
  run_phase 10 "public README"
fi

log ""
log "=== Build complete ==="
log "Run: streamlit run src/ui/app.py"
log "Run: python3 -m pytest tests/ -q"
