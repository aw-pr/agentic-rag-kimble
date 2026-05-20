#!/usr/bin/env bash
# Auth is now handled by the Claude Agent SDK via the Claude Code CLI session.
# The old op-fetch CLAUDE_CODE_OAUTH_TOKEN injection is no longer needed.
#
# This script is retained as a thin alias for muscle-memory compatibility.
#
# Usage:
#   ./run-secure-query.sh --query "Your question here"
#   ./run-secure-query.sh --interactive
set -euo pipefail

cd "$(dirname "$0")"

exec python3 -m src.agent.orchestrator "$@"
