#!/usr/bin/env bash
# Curated publish replay — advance a publish branch with the source branch's new
# commits, minus the private-tier paths that must never reach the public mirror.
#
# CANONICAL SOURCE: mcp-hub/scripts/publish-sync.sh. Copy it into an adopting
# repo as scripts/publish-sync.sh (or run it from here with --repo <path>);
# nothing in it is repo-specific — everything comes from `git config`.
#
# Why this exists: a file can be tracked on `dev` (so it follows worktrees and
# clones) OR published by fast-forward, not both — a fast-forward makes the
# publish tree identical to dev's. This script replays the commits instead of
# fast-forwarding them, dropping the private paths from each one, then moves the
# publish ref forward by fast-forward. See docs/PUBLISH-SYNC.md.
#
# Config (git config, local, never committed):
#   publishguard.privatefile   (repeatable) path dropped from every replayed
#                              commit. Default: HANDOFF.md
#   publishguard.publishbranch (repeatable) protected branch; the FIRST value is
#                              the default target. Default: publish
#
# DRY RUN BY DEFAULT. Nothing is written without --apply.
#
# Usage:
#   scripts/publish-sync.sh [--apply] [--source dev] [--target publish]
#                           [--scratch publish-sync] [--repo <dir>]
#                           [--on-conflict abort|skip|theirs]
#
# --on-conflict picks the curation policy when a replayed commit does not apply:
#   abort  (default) stop and change nothing; resolve by hand.
#   skip   drop the conflicting commit and carry on. Right for stale churn the
#          publish line never received — typically a change and its later revert,
#          which cancel out. The tree comparison at the end is what proves
#          nothing of substance was lost.
#   theirs take the SOURCE branch's version of every conflicted file. Right when
#          the source is the sole authority for public content.
#
# Exit codes: 0 ok / nothing to do, 1 refused or failed, 2 bad usage.
set -euo pipefail

apply=0
source_branch="dev"
target_branch=""
scratch_branch="publish-sync"
repo_dir=""
resolve="abort"

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) apply=1 ;;
    --source) shift; source_branch="${1:-}" ;;
    --target) shift; target_branch="${1:-}" ;;
    --scratch) shift; scratch_branch="${1:-}" ;;
    --repo) shift; repo_dir="${1:-}" ;;
    --on-conflict)
      shift; resolve="${1:-}"
      case "$resolve" in
        abort|skip|theirs) : ;;
        *) echo "publish-sync: --on-conflict accepts abort|skip|theirs" >&2; exit 2 ;;
      esac
      ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "publish-sync: unknown arg '$1'" >&2; exit 2 ;;
  esac
  shift
done

[ -n "$repo_dir" ] && cd "$repo_dir"
git rev-parse --show-toplevel >/dev/null 2>&1 \
  || { echo "publish-sync: not a git repo" >&2; exit 1; }
cd "$(git rev-parse --show-toplevel)"

if [ -z "$target_branch" ]; then
  target_branch="$(git config --get-all publishguard.publishbranch 2>/dev/null | head -n 1 || true)"
  [ -n "$target_branch" ] || target_branch="publish"
fi

private_paths="$(git config --get-all publishguard.privatefile 2>/dev/null || true)"
[ -n "$private_paths" ] || private_paths="HANDOFF.md"

for b in "$source_branch" "$target_branch"; do
  git rev-parse --verify --quiet "refs/heads/$b" >/dev/null \
    || { echo "publish-sync: branch '$b' does not exist" >&2; exit 1; }
done

# Is $1 inside the private set (exact path or a directory prefix of it)?
is_private() {
  _f="$1"
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    case "$_f" in
      "$p"|"$p"/*) return 0 ;;
    esac
  done <<EOF
$private_paths
EOF
  return 1
}

# Drop the private paths from index AND worktree, so the next replay starts from
# a consistent state and the branch we return to can be checked out cleanly.
strip_private() {
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    git rm -r -f -q --ignore-unmatch -- "$p" >/dev/null 2>&1 || true
  done <<EOF
$private_paths
EOF
}

# Merge commits are not replayed: a merge carries no changes of its own that a
# linear public line needs, and the tree comparison at the end proves nothing
# was lost. If that check reports a difference, resolve it by hand.
commits="$(git rev-list --reverse --no-merges "$target_branch..$source_branch" || true)"

echo "publish-sync: repo    $(pwd)"
echo "publish-sync: source  $source_branch ($(git rev-parse --short "$source_branch"))"
echo "publish-sync: target  $target_branch ($(git rev-parse --short "$target_branch"))"
echo "publish-sync: private $(printf '%s' "$private_paths" | tr '\n' ' ')"

if [ -z "$commits" ]; then
  echo "publish-sync: nothing to replay — $target_branch is already up to date."
  exit 0
fi

# Plan: classify each commit before touching anything.
plan_replay=0
plan_skip=0
echo "publish-sync: plan"
while IFS= read -r c; do
  [ -z "$c" ] && continue
  public_change=0
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    is_private "$f" || { public_change=1; break; }
  done <<EOF
$(git diff-tree --no-commit-id --name-only -r "$c")
EOF
  if [ "$public_change" -eq 1 ]; then
    echo "  replay  $(git log -1 --format='%h %s' "$c")"
    plan_replay=$((plan_replay + 1))
  else
    echo "  skip    $(git log -1 --format='%h %s' "$c")  (private-only)"
    plan_skip=$((plan_skip + 1))
  fi
done <<EOF
$commits
EOF
echo "publish-sync: $plan_replay to replay, $plan_skip private-only to drop"

if [ "$apply" -eq 0 ]; then
  echo "publish-sync: DRY RUN — nothing written. Re-run with --apply to act."
  exit 0
fi

# --- from here on we write ---------------------------------------------------

[ -z "$(git status --porcelain)" ] \
  || { echo "publish-sync: working tree is dirty — commit or stash first." >&2; exit 1; }

if git rev-parse --verify --quiet "refs/heads/$scratch_branch" >/dev/null; then
  echo "publish-sync: scratch branch '$scratch_branch' already exists." >&2
  echo "  A previous run left it behind, or the name is in use. Inspect it, then:" >&2
  echo "    git branch -D $scratch_branch" >&2
  exit 1
fi

start_branch="$(git symbolic-ref --quiet --short HEAD || true)"
[ -n "$start_branch" ] \
  || { echo "publish-sync: detached HEAD — switch to a branch first." >&2; exit 1; }
target_before="$(git rev-parse "$target_branch")"

# Any exit before `finished=1` unwinds everything: the sequencer, the worktree,
# the branch we were on, and the scratch branch. The target ref is only ever
# moved on the success path, so an abort leaves it exactly where it was.
finished=0
on_exit() {
  [ "$finished" -eq 1 ] && return 0
  echo "publish-sync: aborting, restoring $start_branch" >&2
  git cherry-pick --abort >/dev/null 2>&1 || true
  git cherry-pick --quit >/dev/null 2>&1 || true
  git reset --hard --quiet >/dev/null 2>&1 || true
  git switch --quiet --force "$start_branch" >/dev/null 2>&1 || true
  git branch -D "$scratch_branch" >/dev/null 2>&1 || true
  echo "publish-sync: $target_branch left at $(git rev-parse --short "$target_branch")" >&2
}
trap 'on_exit' EXIT

git switch --quiet -c "$scratch_branch" "$target_branch"

replayed=0
skipped=0
while IFS= read -r c; do
  [ -z "$c" ] && continue

  public_change=0
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    is_private "$f" || { public_change=1; break; }
  done <<EOF
$(git diff-tree --no-commit-id --name-only -r "$c")
EOF
  if [ "$public_change" -eq 0 ]; then
    echo "  skip    $(git log -1 --format='%h %s' "$c")  (private-only)"
    skipped=$((skipped + 1))
    continue
  fi

  if ! git cherry-pick -n "$c" >/dev/null 2>&1; then
    # A private path the target tree does not carry conflicts as modify/delete.
    # Strip it, then insist nothing else is still unmerged.
    strip_private
    if [ -n "$(git diff --name-only --diff-filter=U)" ]; then
      case "$resolve" in
        abort)
          echo "publish-sync: conflict replaying $(git log -1 --format='%h %s' "$c")" >&2
          git diff --name-only --diff-filter=U | sed 's/^/    /' >&2
          echo "  Re-run with --on-conflict skip or --on-conflict theirs, or resolve by hand." >&2
          exit 1
          ;;
        skip)
          echo "  conflict $(git log -1 --format='%h %s' "$c")  (dropped: $(git diff --name-only --diff-filter=U | tr '\n' ' '))"
          git reset --hard --quiet HEAD
          git cherry-pick --quit >/dev/null 2>&1 || true
          skipped=$((skipped + 1))
          continue
          ;;
        theirs)
          while IFS= read -r u; do
            [ -z "$u" ] && continue
            if git cat-file -e "$c:$u" 2>/dev/null; then
              git checkout "$c" -- "$u"
            else
              git rm -f -q --ignore-unmatch -- "$u"
            fi
            echo "  resolve $u  (took $source_branch's version)"
          done <<EOF
$(git diff --name-only --diff-filter=U)
EOF
          ;;
      esac
    fi
  fi
  strip_private
  git cherry-pick --quit >/dev/null 2>&1 || true

  if git diff --cached --quiet HEAD; then
    echo "  empty   $(git log -1 --format='%h %s' "$c")  (nothing left after strip)"
    skipped=$((skipped + 1))
    continue
  fi

  # -C preserves the original author, date and message; only the committer moves.
  git commit --quiet -C "$c"
  echo "  replay  $(git log -1 --format='%h %s' HEAD)"
  replayed=$((replayed + 1))
done <<EOF
$commits
EOF

git switch --quiet "$start_branch"

# Move the publish ref by fast-forward only. Never commit on it directly — the
# protected-branch guard blocks that, by design.
git merge-base --is-ancestor "$target_branch" "$scratch_branch" \
  || { echo "publish-sync: $scratch_branch is not a descendant of $target_branch" >&2; exit 1; }
git branch -f "$target_branch" "$scratch_branch"
git branch -D "$scratch_branch" >/dev/null
finished=1

echo "publish-sync: $target_branch $(git rev-parse --short "$target_before") -> $(git rev-parse --short "$target_branch")  ($replayed replayed, $skipped dropped)"

# Verification: the public-path trees must now match, and no private path may
# survive on the target.
excludes=""
while IFS= read -r p; do
  [ -z "$p" ] && continue
  excludes="$excludes :(exclude)$p"
done <<EOF
$private_paths
EOF

# shellcheck disable=SC2086
drift="$(git diff --name-only "$target_branch" "$source_branch" -- . $excludes)"
if [ -n "$drift" ]; then
  echo "publish-sync: WARNING — public-path trees differ from $source_branch:" >&2
  printf '%s\n' "$drift" | sed 's/^/    /' >&2
else
  echo "publish-sync: verified — public-path trees identical to $source_branch."
fi

leak=0
while IFS= read -r p; do
  [ -z "$p" ] && continue
  if [ -n "$(git ls-tree -r --name-only "$target_branch" -- "$p")" ]; then
    echo "publish-sync: WARNING — private path '$p' is present on $target_branch." >&2
    leak=1
  fi
done <<EOF
$private_paths
EOF
[ "$leak" -eq 0 ] && echo "publish-sync: verified — no private path on $target_branch."

echo "publish-sync: done. Nothing was pushed; publish with your repo's publish path."
