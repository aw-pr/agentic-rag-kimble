#!/usr/bin/env bash
# install-experiment-cron.sh — idempotent installer for the experiment-tick cron entry.
#
# Appends exactly two lines to the user's crontab:
#   <SENTINEL_COMMENT>
#   */5 * * * * /bin/bash -lc 'cd <repo_root> && ./scripts/experiment-tick.sh >> runs/experiment/cron.log 2>&1'
#
# The sentinel comment is the unique marker used by both this script and
# uninstall-experiment-cron.sh to detect and surgically remove the entry.
#
# Sentinel: # agentic-rag-kimble experiment tick — managed by install-experiment-cron.sh
#
# To remove: ./scripts/uninstall-experiment-cron.sh
set -euo pipefail

SENTINEL="# agentic-rag-kimble experiment tick — managed by install-experiment-cron.sh"

repo_root="$(git rev-parse --show-toplevel)"
cron_line="*/5 * * * * /bin/bash -lc 'cd ${repo_root} && ./scripts/experiment-tick.sh >> runs/experiment/cron.log 2>&1'"

# Read existing crontab; treat "no crontab" as empty.
existing_crontab="$(crontab -l 2>/dev/null || true)"

if echo "$existing_crontab" | grep -qF "$SENTINEL"; then
  echo "install-experiment-cron: already installed — experiment tick is active (every 5 minutes)."
  exit 0
fi

# Append the sentinel comment and the cron line.
new_crontab="${existing_crontab}
${SENTINEL}
${cron_line}
"

# Strip any leading blank lines that arise when existing_crontab was empty.
new_crontab="$(echo "$new_crontab" | sed '/./,$!d')"

echo "$new_crontab" | crontab -

echo "install-experiment-cron: installed."
echo "  Cron entry:"
echo "    ${SENTINEL}"
echo "    ${cron_line}"
echo ""
echo "  This fires every 5 minutes on this machine."
echo "  View the current experiment state with: ./scripts/experiment-mayor.sh"
echo "  Logs: ${repo_root}/runs/experiment/cron.log"
echo "  To remove: ./scripts/uninstall-experiment-cron.sh"
