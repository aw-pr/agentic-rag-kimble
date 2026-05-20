#!/usr/bin/env bash
# run-eval.sh — run the offline retrieval eval harness and write JSON to runs/
#
# Usage:
#   ./scripts/run-eval.sh              # standard run, prints human-readable output
#   ./scripts/run-eval.sh --json       # also prints raw JSON to stdout
#   ./scripts/run-eval.sh --k 5        # use recall@5 cutoff
#
# LLM judge:
#   WITH_JUDGE=1 ./scripts/run-eval.sh   # enable live judge scoring (uses Max quota)
#
#   The judge is NOT invoked by default (no network required).
#   When WITH_JUDGE=1, the first 5 fixtures are scored via Claude Agent SDK
#   (same OAuth auth path as the orchestrator). Each fixture triggers one agent
#   invocation + one judge call, so budget ~10 SDK calls per run.
#
# Output file: runs/eval-<timestamp>.json

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

EXTRA_ARGS=()
JSON_FLAG=false

for arg in "$@"; do
    case "$arg" in
        --json) JSON_FLAG=true ;;
        *) EXTRA_ARGS+=("$arg") ;;
    esac
done

# Inject --with-judge when WITH_JUDGE=1 is set in the environment
WITH_JUDGE_FLAG=""
if [ "${WITH_JUDGE:-0}" = "1" ]; then
    WITH_JUDGE_FLAG="--with-judge"
    echo "WITH_JUDGE=1 detected — LLM judge will score the first 5 fixtures (uses Max quota)"
fi

echo "=== agentic-rag-kimble retrieval eval ==="
echo "Working directory: $REPO_ROOT"
echo ""

CMD=(python3 -m src.eval.metrics)
if $JSON_FLAG; then
    CMD+=(--output-json)
fi
if [ -n "$WITH_JUDGE_FLAG" ]; then
    CMD+=("$WITH_JUDGE_FLAG")
fi
CMD+=("${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}")

"${CMD[@]}"

echo ""
echo "=== eval complete ==="
echo "Latest file:"
ls -1t "$REPO_ROOT/runs"/eval-*.json 2>/dev/null | head -1 || echo "(no eval JSON found)"
