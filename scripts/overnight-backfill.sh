#!/usr/bin/env bash
# overnight-backfill.sh — sequential runner for the 4 backfills.
#
# Order (least risky first):
#   1. backfill-dimension-attributes.sh   (schema + SET, local only)
#   2. backfill-algorithm-families.sh     (new tables + embeddings, local only)
#   3. backfill-date-dimension.sh         (network: OpenML list_runs)
#   4. backfill-openml-descriptions.sh    (network + segfault-prone; runs LAST)
#
# Each step logs to runs/backfill-<DATE>/<N>-<step>.log
# Stops on first non-zero exit.

set -euo pipefail
cd "$(dirname "$0")/.."

LOG_DIR="runs/backfill-2026-05-14"
mkdir -p "$LOG_DIR"

run_step() {
    local n="$1" script="$2"
    local log="$LOG_DIR/${n}-$(basename "$script" .sh).log"
    echo ""
    echo "=== [$n] $(date '+%F %T') START $script ==="
    echo "    log: $log"
    if bash "$script" >"$log" 2>&1; then
        echo "=== [$n] $(date '+%F %T') DONE $script ==="
    else
        local rc=$?
        echo "=== [$n] $(date '+%F %T') FAILED rc=$rc — see $log ==="
        echo "Stopping. Remaining steps were NOT run."
        exit $rc
    fi
}

echo "=== Overnight backfill started $(date '+%F %T') ==="

run_step 1 scripts/backfill-dimension-attributes.sh
run_step 2 scripts/backfill-algorithm-families.sh
run_step 3 scripts/backfill-date-dimension.sh
run_step 4 scripts/backfill-openml-descriptions.sh

echo ""
echo "=== Overnight backfill finished $(date '+%F %T') ==="
