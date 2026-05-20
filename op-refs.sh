#!/usr/bin/env bash
# Single source of truth for 1Password references in this repo.
# Committed. These are pointers, not secrets. Do not inline op:// strings elsewhere.
#
# Usage:
#   source ./op-refs.sh
#   exec op-fetch CLAUDE_CODE_OAUTH_TOKEN="$OP_REF_CLAUDE_CODE_OAUTH_TOKEN" -- "$@"

export OP_REF_CLAUDE_CODE_OAUTH_TOKEN="op://dev-stuff/Claude oauth token/credential"
