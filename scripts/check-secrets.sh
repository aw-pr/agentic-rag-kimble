#!/usr/bin/env bash
# Fail-closed leak guard. Blocks the repo from building leaky auth/secret
# patterns that would need cleanup before publishing.
#
# Auth route for this repo: Claude Agent SDK OAuth via the Claude Code CLI
# session. No API key, no op-fetch at runtime. No op:// references should
# appear anywhere in the tree (the previous op-refs.sh pointer file was
# removed in pass-29 as vestigial).
#
# Run standalone, from smoke-test.sh, or as a pre-commit hook
# (scripts/install-guards.sh installs it).
set -euo pipefail
cd "$(dirname "$0")/.."

fail=0
flag() { echo "LEAK GUARD: $1" >&2; fail=1; }

# 1. Raw op:// strings must not appear anywhere in the tree (docs/examples
#    excluded). The op-refs.sh pointer file was removed in pass-29.
if git grep -nE 'op://[^"'"'"'[:space:]]+' -- ':!scripts/check-secrets.sh' ':!*.md' ':!*.example' >/dev/null 2>&1; then
  flag "op:// reference found — auth is now OAuth-via-session, no op-fetch pointers needed"
  git grep -nE 'op://[^"'"'"'[:space:]]+' -- ':!scripts/check-secrets.sh' ':!*.md' ':!*.example' >&2 || true
fi

# 2. Superseded auth patterns that over-hydrate or are broken.
if git grep -nE 'op run --env-file|run-secure-with-op' -- ':!*.md' ':!scripts/check-secrets.sh' >/dev/null 2>&1; then
  flag "legacy op run --env-file / run-secure-with-op — use op-fetch NAME=ref -- cmd"
  git grep -nE 'op run --env-file|run-secure-with-op' -- ':!*.md' ':!scripts/check-secrets.sh' >&2 || true
fi

# 3. Hardcoded credential shapes (value assigned, not just the var name).
if git grep -nIE 'sk-ant-[A-Za-z0-9]{8}|(ANTHROPIC|OPENAI)_API_KEY[[:space:]]*=[[:space:]]*["'"'"'][A-Za-z0-9]|BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY' -- ':!*.md' ':!scripts/check-secrets.sh' >/dev/null 2>&1; then
  flag "hardcoded key/credential shape — never commit secret values"
  git grep -nIE 'sk-ant-[A-Za-z0-9]{8}|(ANTHROPIC|OPENAI)_API_KEY[[:space:]]*=[[:space:]]*["'"'"'][A-Za-z0-9]|BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY' -- ':!*.md' ':!scripts/check-secrets.sh' >&2 || true
fi

# 4. Tracked .env files (op:// or real values both leak / over-hydrate).
if git ls-files | grep -E '(^|/)\.env([.][^/]*)?$' >/dev/null 2>&1; then
  flag ".env file is tracked — use .env.local (gitignored) for secrets"
  git ls-files | grep -E '(^|/)\.env([.][^/]*)?$' >&2 || true
fi

if [ "$fail" -ne 0 ]; then
  echo "LEAK GUARD: FAILED — see lines above. Fix before committing/publishing." >&2
  exit 1
fi
echo "leak guard: clean (no leaky auth/secret patterns)"
