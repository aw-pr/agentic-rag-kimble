#!/usr/bin/env bash
# Idempotent installer for the publish guards. Re-runnable; only fills gaps.
# Arms .git/hooks/* from every hook present in scripts/git-hooks/ (the
# canonical set is defined by mcp-hub/templates/git-hooks/, not repeated
# here — glob so a new hook, e.g. commit-msg, is picked up without editing
# this script) and seeds a gitignored .publish-guard.local from the
# committed .example.
#
# Canonical copy: mcp-hub/templates/git-hooks/install-guards.sh, distributed
# by scripts/sync-guard-hooks.sh into each participating repo's scripts/.
# Do not hand-edit the per-repo copy — it drifts; edit the canonical copy.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
hooks_src="scripts/git-hooks"
hooks_dst="$(git rev-parse --git-path hooks)"
mkdir -p "$hooks_dst"

for hook_path in "$hooks_src"/*; do
  hook="$(basename "$hook_path")"
  case "$hook" in *.md|*.bak) continue ;; esac
  if [ -f "$hooks_dst/$hook" ] && ! cmp -s "$hooks_src/$hook" "$hooks_dst/$hook"; then
    echo "install-guards: existing $hook differs — backing up to $hook.bak"
    cp "$hooks_dst/$hook" "$hooks_dst/$hook.bak"
  fi
  install -m 0755 "$hooks_src/$hook" "$hooks_dst/$hook"
  echo "install-guards: armed $hook"
done

if [ ! -f .publish-guard.local ]; then
  if [ -f .publish-guard.local.example ]; then
    cp .publish-guard.local.example .publish-guard.local
    echo "install-guards: seeded .publish-guard.local — edit it with your real patterns"
  else
    echo "install-guards: WARN no .publish-guard.local.example to seed from" >&2
  fi
else
  echo "install-guards: .publish-guard.local already present — left untouched"
fi

# Publish gate config (git config, local, not committed). Idempotent: only
# fills gaps. sentinel has a stable default; publicmatch/publicremote are
# repo-specific and must be set once (we never guess org/repo names).
git config --get publishguard.sentinel >/dev/null 2>&1 \
  || git config publishguard.sentinel PUBLISH_GUARD_OK

# History mode: `preserve` (default — filter-repo seed, --no-ff merges,
# per-commit cleanliness required) or `squash` (orphan-squash seed, --squash
# merges). The family default flipped to preserve on 2026-06-09 so a repo
# adopting the guards publishes atomic public history with no per-repo config;
# templates/git-hooks/pre-push documents the same default. Do not reintroduce
# squash here — the older per-repo installers still carrying it are the drift
# this canonical copy exists to end.
git config --get publishguard.historymode >/dev/null 2>&1 \
  || git config publishguard.historymode preserve

pub_match="$(git config --get publishguard.publicmatch || true)"
pub_remote="$(git config --get publishguard.publicremote || true)"
priv_remote="$(git config --default origin --get publishguard.privateremote)"
# The local branch that is published. Deliberately never inferred from HEAD:
# an alias guessed from whatever was checked out at arm time publishes the
# wrong line.
pub_branch="$(git config --get publishguard.publishbranch || true)"
# Same defaults as pre-push: PR boundary, PR source = the publish branch.
boundary="$(git config --default pr --get publishguard.boundary)"
pr_source="$(git config --get publishguard.prsource || true)"
[ -z "$pr_source" ] && pr_source="$pub_branch"

# set_alias <name> <wanted> [<value an earlier install wrote>...]
# Writes when unset or stale; keeps anything else as hand-written, with a warning.
set_alias() {
  local name="$1" want="$2" have stale
  shift 2
  have="$(git config --get "alias.$name" || true)"
  if [ -z "$have" ]; then
    git config "alias.$name" "$want"
    echo "install-guards: set 'git $name' alias"
    return
  fi
  if [ "$have" = "$want" ]; then
    echo "install-guards: 'git $name' alias already current — left untouched"
    return
  fi
  for stale in "$@"; do
    if [ "$have" = "$stale" ]; then
      git config "alias.$name" "$want"
      echo "install-guards: replaced the 'git $name' alias an earlier install wrote"
      return
    fi
  done
  echo "install-guards: WARN 'git $name' alias is hand-written — left as it is." >&2
  echo "  install-guards would have written: $want" >&2
  echo "  to take that value: git config --unset alias.$name && bash scripts/install-guards.sh" >&2
}

if [ -z "$pub_match" ] || [ -z "$pub_remote" ]; then
  echo "install-guards: publish gate INERT — set the public remote once:"
  echo "  git config publishguard.publicmatch  'YOUR_ORG/YOUR_REPO'"
  echo "  git config publishguard.publicremote 'YOUR_PUBLIC_REMOTE_NAME'"
  echo "  git config publishguard.publishbranch 'publish'   # local line that ff's to main"
  echo "  git config publishguard.privateremote 'origin'    # optional, default origin"
elif [ -z "$pub_branch" ]; then
  echo "install-guards: no publish aliases written — publishguard.publishbranch is unset."
  echo "  Set the local line that is published, then re-run this installer:"
  echo "  git config publishguard.publishbranch 'publish'"
else
  sentinel="$(git config --get publishguard.sentinel)"
  pr_message="Next: gh pr create --base main --head ${pr_source}   then read the diff on the forge and merge it there."
  pr_flow="!git push ${priv_remote} ${pub_branch} && git push ${pub_remote} ${pub_branch}:${pr_source} && echo '${pr_message}'"
  direct_flow="!git push ${priv_remote} ${pub_branch} && ${sentinel}=1 git push ${pub_remote} ${pub_branch}:main"

  set_alias publish-pr "$pr_flow"
  if [ "$boundary" = "direct" ]; then
    set_alias publish "$direct_flow" "$pr_flow"
    echo "install-guards: boundary = direct — 'git publish' fast-forwards ${pub_remote} main; 'git publish-pr' opens the PR flow"
  else
    set_alias publish "$pr_flow" "$direct_flow"
    echo "install-guards: boundary = ${boundary} — the PR flow pushes ${pub_branch} to ${pub_remote} ${pr_source}; public main advances only by a merge on the forge"
  fi
fi

history_mode="$(git config --get publishguard.historymode)"
echo "install-guards: history mode = ${history_mode} (flip with: git config publishguard.historymode <squash|preserve>)"

echo "install-guards: done."
