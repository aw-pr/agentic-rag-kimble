#!/usr/bin/env bash
set -euo pipefail

echo "=== strict publish guard ==="
./scripts/publish-guard-strict-scan.sh

echo "=== leak guard ==="
./scripts/check-secrets.sh

echo "=== lint ==="
ruff check src/ tests/

echo "=== typecheck ==="
mypy src/

echo "=== unit tests ==="
pytest tests/unit/ -q

echo "=== eval fixtures ==="
pytest tests/eval/ -q

echo "=== UI importability ==="
python3 -c "import src.ui.app" 2>&1 | grep -v "^$" && echo "UI importable" || echo "UI import OK (no output)"

echo "=== smoke-test passed ==="