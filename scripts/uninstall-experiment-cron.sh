#!/usr/bin/env bash
# uninstall-experiment-cron.sh — surgical remover for the experiment-tick cron entry.
#
# Removes exactly the two lines written by install-experiment-cron.sh:
#   <SENTINEL_COMMENT>
#   */5 * * * * … experiment-tick.sh …
#
# Any other crontab entries are left untouched.  Never calls crontab -r.
#
# Sentinel: # agentic-rag-kimble experiment tick — managed by install-experiment-cron.sh
#
# To reinstall: ./scripts/install-experiment-cron.sh
set -euo pipefail

SENTINEL="# agentic-rag-kimble experiment tick — managed by install-experiment-cron.sh"

# Read existing crontab; treat "no crontab" as empty.
existing_crontab="$(crontab -l 2>/dev/null || true)"

if ! echo "$existing_crontab" | grep -qF "$SENTINEL"; then
  echo "uninstall-experiment-cron: not installed — nothing to remove."
  exit 0
fi

# Filter out the sentinel line and the immediately-following line.
# awk: when the sentinel is seen, skip it and set a flag to skip the next line too.
new_crontab="$(echo "$existing_crontab" | awk -v sentinel="$SENTINEL" '
  $0 == sentinel { skip_next=1; next }
  skip_next       { skip_next=0; next }
                  { print }
')"

echo "$new_crontab" | crontab -

echo "uninstall-experiment-cron: removed experiment tick from crontab."
echo "  The tick will no longer fire."
echo "  experiment-mayor.sh will continue to display the last-known experiment state"
echo "  until it is restarted or the state files are cleared."
echo "  To reinstall: ./scripts/install-experiment-cron.sh"
