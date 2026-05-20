#!/usr/bin/env bash
# Idempotent installer for the publish guards. Re-runnable; only fills gaps.
# Arms .git/hooks/{pre-commit,pre-push} from scripts/git-hooks/ and seeds a
# gitignored .publish-guard.local from the committed .example.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
hooks_src="scripts/git-hooks"
hooks_dst="$(git rev-parse --git-path hooks)"
mkdir -p "$hooks_dst"

for hook in pre-commit pre-push; do
  if [ -f "$hooks_dst/$hook" ] && ! cmp -s "$hooks_src/$hook" "$hooks_dst/$hook"; then
    echo "install-guards: existing $hook differs — backing up to $hook.bak"
    cp "$hooks_dst/$hook" "$hooks_dst/$hook.bak"
  fi
  install -m 0755 "$hooks_src/$hook" "$hooks_dst/$hook"
  echo "install-guards: armed $hook"
done

if [ ! -f .publish-guard.local ]; then
  cp .publish-guard.local.example .publish-guard.local
  echo "install-guards: seeded .publish-guard.local — edit it with your real patterns"
else
  echo "install-guards: .publish-guard.local already present — left untouched"
fi

# Publish gate config (git config, local, not committed). Idempotent: only
# fills gaps. sentinel has a stable default; publicmatch/publicremote are
# repo-specific and must be set once (we never guess org/repo names).
git config --get publishguard.sentinel >/dev/null 2>&1 \
  || git config publishguard.sentinel PUBLISH_GUARD_OK

pub_match="$(git config --get publishguard.publicmatch || true)"
pub_remote="$(git config --get publishguard.publicremote || true)"
priv_remote="$(git config --default origin --get publishguard.privateremote)"
# The local branch that fast-forwards onto the public default branch.
# Deterministic + portable across machines (don't infer from HEAD).
pub_branch="$(git config --get publishguard.publishbranch || true)"
[ -z "$pub_branch" ] && pub_branch="$(git symbolic-ref --short HEAD 2>/dev/null || echo publish)"

if [ -z "$pub_match" ] || [ -z "$pub_remote" ]; then
  echo "install-guards: publish gate INERT — set the public remote once:"
  echo "  git config publishguard.publicmatch  'YOUR_ORG/YOUR_REPO'"
  echo "  git config publishguard.publicremote 'YOUR_PUBLIC_REMOTE_NAME'"
  echo "  git config publishguard.publishbranch 'publish'   # local line that ff's to main"
  echo "  git config publishguard.privateremote 'origin'    # optional, default origin"
else
  sentinel="$(git config --get publishguard.sentinel)"
  want_alias="!git push ${priv_remote} ${pub_branch} && ${sentinel}=1 git push ${pub_remote} ${pub_branch}:main"
  if [ "$(git config --get alias.publish || true)" != "$want_alias" ]; then
    git config alias.publish "$want_alias"
    echo "install-guards: set 'git publish' alias (${priv_remote} ${pub_branch} → ${pub_remote} main)"
  else
    echo "install-guards: 'git publish' alias already current — left untouched"
  fi
fi

echo "install-guards: done."
