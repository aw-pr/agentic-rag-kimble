#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

DRY_RUN=0
RESET=0
for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=1 ;;
    --reset)   RESET=1 ;;
  esac
done

if [[ $RESET -eq 1 ]]; then
  echo "Resetting schema..."
  python3 -c "
from src.config import get_config
from src.graph.db import GraphDB
with GraphDB(get_config()) as db:
    db.reset_schema()
    print('Schema reset.')
"
fi

if [[ $DRY_RUN -eq 1 ]]; then
  echo "Dry run: initialising schema only..."
  python3 -c "
from src.config import get_config
from src.graph.db import GraphDB
with GraphDB(get_config()) as db:
    db.initialise_schema()
    for table in ['Run', 'Algorithm', 'Dataset', 'Task']:
        print(f'  {table}: {db.node_count(table)} nodes')
print('Dry run complete.')
"
  exit 0
fi

echo "Running full ingestion pipeline..."
python3 -m src.ingestion.loader
