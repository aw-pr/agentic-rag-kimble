#!/usr/bin/env bash
# Curated publish replay: advance a publish branch with the source branch's new
# commits, minus the private-tier paths that must never reach the public mirror.
#
# CANONICAL SOURCE: mcp-hub/scripts/publish-sync.sh. Copy it into an adopting
# repo as scripts/publish-sync.sh (or run it from here with --repo <path>);
# nothing in it is repo-specific, everything comes from `git config`.
#
# Why this exists: a file can be tracked on `dev` (so it follows worktrees and
# clones) OR published by fast-forward, not both: a fast-forward makes the
# publish tree identical to dev's. This script replays the commits instead of
# fast-forwarding them, dropping the private paths from each one, then moves the
# publish ref forward by fast-forward. See docs/PUBLISH-SYNC.md in mcp-hub.
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
#                           [--base <rev>] [--scratch publish-sync]
#                           [--repo <dir>] [--on-conflict abort|skip|theirs]
#
# The replay range is <base>..<source>, and the base has to be one the script
# can prove. In order of precedence:
#   --base <rev>  named by you; must be an ancestor of the source.
#   watermark     the last Publish-Sync-Source trailer value of the newest
#                 target commit that carries one; must name a commit that is
#                 an ancestor of the source.
#   the target    only when no target commit carries the trailer AND the
#                 target is an ancestor of the source (a linear publish line).
# Anything else is refused with exit 1 before anything is written, in a dry run
# too: an orphan or hand-diverged target with no watermark, a watermark that is
# not a commit here or not an ancestor of the source, a --base that is not an
# ancestor of the source. The refusal prints `suggested --base <sha>`, the
# newest source commit whose tree, private paths aside, equals the target's,
# when there is one. Check it, then pass it as --base.
#
# --on-conflict picks the curation policy when a replayed commit does not apply:
#   abort  (default) stop and change nothing; resolve by hand.
#   skip   drop the conflicting commit and carry on. Right for stale churn the
#          publish line never received, typically a change and its later revert,
#          which cancel out. The tree comparison at the end is what proves
#          nothing of substance was lost.
#   theirs take the SOURCE branch's version of every conflicted file. Right when
#          the source is the sole authority for public content. It settles
#          conflicts inside a proven range; it does not stand in for --base.
#
# Exit codes: 0 ok / nothing to do, 1 refused, failed, or the final check found
# drift or a private path on the target (the ref has moved; inspect it), 2 bad
# usage.
set -euo pipefail

apply=0
source_branch="dev"
target_branch=""
base_arg=""
scratch_branch="publish-sync"
repo_dir=""
resolve="abort"

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) apply=1 ;;
    --source) shift; source_branch="${1:-}" ;;
    --target) shift; target_branch="${1:-}" ;;
    --base)
      case "${2:-}" in
        ""|-*) echo "publish-sync: --base needs a commit: --base <rev>" >&2; exit 2 ;;
      esac
      shift; base_arg="$1"
      ;;
    --scratch) shift; scratch_branch="${1:-}" ;;
    --repo) shift; repo_dir="${1:-}" ;;
    --on-conflict)
      shift; resolve="${1:-}"
      case "$resolve" in
        abort|skip|theirs) : ;;
        *) echo "publish-sync: --on-conflict accepts abort|skip|theirs" >&2; exit 2 ;;
      esac
      ;;
    -h|--help) awk 'NR > 1 { if (/^#/) print; else exit }' "$0"; exit 0 ;;
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

# One spelling per entry. A directory is named without its trailing slash, and a
# glob is refused: `git rm` would expand it while the classifier below cannot,
# so the plan and the apply would disagree about the same commit.
while IFS= read -r p; do
  case "$p" in
    *[\*\?\[]*)
      echo "publish-sync: publishguard.privatefile '$p' is a glob; name the file or directory." >&2
      exit 2 ;;
  esac
done <<EOF
$private_paths
EOF
private_paths="$(while IFS= read -r p; do
  [ -n "$p" ] && printf '%s\n' "${p%/}"
done <<EOF
$private_paths
EOF
)"

# The private set as arguments: plain paths for a history scan, and the
# pathspec that selects everything else for a tree comparison.
private_args=()
public_spec=(.)
while IFS= read -r p; do
  [ -z "$p" ] && continue
  private_args+=("$p")
  public_spec+=(":(exclude)$p")
done <<EOF
$private_paths
EOF

for b in "$source_branch" "$target_branch"; do
  git rev-parse --verify --quiet "refs/heads/$b" >/dev/null \
    || { echo "publish-sync: branch '$b' does not exist" >&2; exit 1; }
done

explicit_base=""
if [ -n "$base_arg" ]; then
  explicit_base="$(git rev-parse --verify --quiet "$base_arg^{commit}")" \
    || { echo "publish-sync: --base '$base_arg' does not name a commit in this repository" >&2; exit 2; }
fi

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

# Does $1 change anything outside the private set? --root diffs a root commit
# against the empty tree; without it a root commit lists no paths at all. An
# empty commit stays private-only, since it has nothing to publish. Paths come
# NUL-delimited so a space or a non-ASCII name is compared verbatim.
commit_is_private_only() {
  while IFS= read -r -d '' f; do
    [ -z "$f" ] && continue
    is_private "$f" || return 1
  done < <(git diff-tree --root --no-commit-id --name-only -r -z "$1")
  return 0
}

# Where the last replay stopped. A replayed commit is a new SHA, so once the
# target has diverged, target..source would name every commit since the fork
# for ever, and re-applying an old change over a newer one conflicts. Each
# replayed commit therefore carries one Publish-Sync-Source trailer naming the
# source commit it came from. The watermark is the LAST value on the newest
# target commit that carries the key: a commit written before the replay
# stripped copied trailers can hold an older value ahead of its own.
# One line per target commit, "<sha> TAB <trailer> TAB <trailer>...", read to
# the end so a large history cannot SIGPIPE git under pipefail.
wm_line="$(git log --no-show-signature "$target_branch" \
    --format='%H%x09%(trailers:key=Publish-Sync-Source,unfold,separator=%x09)' \
  | awk -F '\t' '!found && $2 != "" {
      found = 1; v = $NF; sub(/^[^:]*:[ \t]*/, "", v); split(v, word, /[ \t]+/)
      print $1 " " word[1]
    }')"
wm_carrier="${wm_line%% *}"
watermark="${wm_line#* }"
wm_base=""
[ -z "$wm_carrier" ] || wm_base="$(git rev-parse --verify --quiet "$watermark^{commit}" || true)"

# The base must be provable, because <base>..<source> is replayed as it stands
# and everything in it becomes public history. A base that is not an ancestor
# of the source proves nothing: the range then reaches back to the fork, or to
# the root when the lines share no history, and republishes commits the target
# already has in another form or was built to leave out. So an unprovable base
# is a refusal, never a fallback to the target. Without a watermark the target
# is its own proof only when it is an ancestor of the source.
base=""
base_from=""
refusal=""
refusal_detail=""
if [ -n "$explicit_base" ]; then
  if git merge-base --is-ancestor "$explicit_base" "$source_branch"; then
    base="$explicit_base"
    base_from="--base"
    if [ -n "$wm_base" ] && [ "$wm_base" != "$explicit_base" ] \
       && git merge-base --is-ancestor "$wm_base" "$source_branch"; then
      base_from="--base, in place of the watermark $(git rev-parse --short "$wm_base")"
    fi
  else
    refusal="--base $(git rev-parse --short "$explicit_base") is not an ancestor of $source_branch"
    refusal_detail="The replay is what $source_branch gained after the base, so only one of $source_branch's own commits can be it."
  fi
elif [ -n "$wm_carrier" ]; then
  wm_named="Publish-Sync-Source: $(printf '%.40s' "$watermark") (on $(git rev-parse --short "$wm_carrier"))"
  if [ -z "$watermark" ]; then
    refusal="the watermark on $target_branch has no value"
    refusal_detail="$(git rev-parse --short "$wm_carrier") is the newest $target_branch commit with a Publish-Sync-Source trailer, and its last one is empty."
  elif [ -z "$wm_base" ]; then
    refusal="the watermark on $target_branch is not a commit in this repository"
    refusal_detail="$wm_named was recorded in another clone, or $source_branch was rewritten and the commit is gone."
  elif ! git merge-base --is-ancestor "$wm_base" "$source_branch"; then
    refusal="the watermark on $target_branch is not an ancestor of $source_branch"
    refusal_detail="$wm_named names a commit $source_branch no longer contains, so $source_branch was rewritten since the last replay."
  else
    base="$wm_base"
    base_from="watermark from the last replay"
  fi
elif git merge-base --is-ancestor "$target_branch" "$source_branch"; then
  base="$target_branch"
else
  refusal="$target_branch carries no Publish-Sync-Source watermark and is not an ancestor of $source_branch"
  refusal_detail="On an orphan or hand-diverged line, $target_branch..$source_branch is every $source_branch commit since the root or the fork, published or not."
fi

# The newest source commit whose tree, private paths aside, equals the
# target's: the point the target is provably level with, so a replay from it
# adds exactly what the source gained since. Read-only. One diff-tree walks the
# source's history, comparing each commit with the target as though the target
# were its parent; a commit with no ':' diff line under its header matches. A
# scan that fails suggests nothing.
suggest_base() {
  _level="$(git rev-parse "$target_branch^{commit}")"
  _match="$(git rev-list "$source_branch" | sed "s/\$/ $_level/" \
    | git diff-tree --stdin --always -r -- "${public_spec[@]}" \
    | awk '
        /^:/ { differs = 1; next }
        { if (candidate != "" && !differs && match_sha == "") match_sha = candidate
          candidate = $1; differs = 0 }
        END { if (candidate != "" && !differs && match_sha == "") match_sha = candidate
              if (match_sha != "") print match_sha }')" || _match=""
  printf '%s' "$_match"
}

# Exit 1 with the reason, and with the base to pass when one can be proved.
# Runs before the plan, so the dry run refuses exactly as --apply does.
refuse() {
  echo "publish-sync: refused: $refusal." >&2
  echo "  $refusal_detail" >&2
  echo "  Nothing was planned or written. Name the base with --base <rev>." >&2
  _suggested="$(suggest_base)"
  if [ -n "$_suggested" ]; then
    echo "publish-sync: suggested --base $_suggested  ($(git log -1 --no-show-signature --date=short --format='%s, %ad' "$_suggested"))" >&2
    echo "  Its tree, private paths aside, is identical to $target_branch's, so a replay from it adds" >&2
    echo "  only what $source_branch gained since. Check that is where $target_branch stands, then re-run with it." >&2
  else
    echo "publish-sync: no $source_branch commit has $target_branch's tree once the private paths are set aside, so no base is suggested." >&2
    echo "  Pass the $source_branch commit $target_branch was last level with as --base <rev>; the tree" >&2
    echo "  comparison at the end of the run reports whatever still differs." >&2
  fi
  exit 1
}

echo "publish-sync: repo    $(pwd)"
echo "publish-sync: source  $source_branch ($(git rev-parse --short "$source_branch"))"
echo "publish-sync: target  $target_branch ($(git rev-parse --short "$target_branch"))"
private_line="publish-sync: private $(printf '%s' "$private_paths" | tr '\n' ' ')"
if [ -n "$refusal" ]; then
  echo "$private_line"
  refuse
fi
[ -z "$base_from" ] || echo "publish-sync: base    $(git rev-parse --short "$base") ($base_from)"
echo "$private_line"

# Merge commits are not replayed: a merge carries no changes of its own that a
# linear public line needs, and the tree comparison at the end proves nothing
# was lost. If that check reports a difference, resolve it by hand.
commits="$(git rev-list --reverse --no-merges "$base..$source_branch" || true)"

# Private paths already tracked on $1, one per line. A leak that predates this
# run is still a leak: the pre-push guard will refuse the next publish, so say
# so now rather than after a replay, or never when there is nothing to replay.
leaked_paths() {
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    [ -n "$(git ls-tree -r --name-only "$1" -- "$p")" ] && echo "$p"
  done <<EOF
$private_paths
EOF
  return 0
}

leak="$(leaked_paths "$target_branch")"
if [ -n "$leak" ]; then
  printf '%s\n' "$leak" | sed "s/^/publish-sync: WARNING: private path '/; s/\$/' is already present on $target_branch./" >&2
fi

if [ -z "$commits" ]; then
  echo "publish-sync: nothing to replay, $target_branch is already up to date."
  [ -z "$leak" ] || exit 1
  exit 0
fi

# Plan: classify each commit once, before touching anything. The apply loop
# reads these verdicts rather than classifying again.
plan=""
plan_replay=0
plan_skip=0
echo "publish-sync: plan"
while IFS= read -r c; do
  [ -z "$c" ] && continue
  if commit_is_private_only "$c"; then
    echo "  skip    $(git log -1 --format='%h %s' "$c")  (private-only)"
    plan="${plan}skip $c
"
    plan_skip=$((plan_skip + 1))
  else
    echo "  replay  $(git log -1 --format='%h %s' "$c")"
    plan="${plan}replay $c
"
    plan_replay=$((plan_replay + 1))
  fi
done <<EOF
$commits
EOF
echo "publish-sync: $plan_replay to replay, $plan_skip private-only to drop"

# Replaying rewrites every SHA, which turns a target that was an ancestor of
# the source into a diverged line for good. That price is only worth paying
# when something actually has to be stripped. When the target is still an
# ancestor, it carries no leak, and no commit the ref would cross touches a
# private path, the ref simply fast-forwards and the linear model is kept.
# The scan covers target..source, not the replay range: the ref crosses every
# commit in it, wherever the base sits. --full-history keeps a merged side
# branch that added and removed a private path in view, and the paths are
# taken literally in one call. A scan that fails means replay.
mode="replay"
if git merge-base --is-ancestor "$target_branch" "$source_branch" \
   && [ -z "$leak" ] \
   && private_touch="$(git --literal-pathspecs rev-list --full-history -n 1 "$target_branch..$source_branch" -- "${private_args[@]}")" \
   && [ -z "$private_touch" ]; then
  mode="fast-forward"
fi
echo "publish-sync: mode $mode"

if [ "$apply" -eq 0 ]; then
  echo "publish-sync: DRY RUN, nothing written. Re-run with --apply to act."
  exit 0
fi

# --- from here on we write ---------------------------------------------------

# Untracked files are not a reason to refuse: nothing here touches them. They
# are listed so a surprising one is not overlooked.
[ -z "$(git status --porcelain --untracked-files=no)" ] \
  || { echo "publish-sync: working tree has uncommitted changes: commit or stash first." >&2; exit 1; }
untracked="$(git ls-files --others --exclude-standard)"
[ -z "$untracked" ] || echo "publish-sync: untracked files left alone: $(printf '%s' "$untracked" | tr '\n' ' ')"

if git rev-parse --verify --quiet "refs/heads/$scratch_branch" >/dev/null; then
  echo "publish-sync: scratch branch '$scratch_branch' already exists." >&2
  echo "  A previous run left it behind, or the name is in use. Inspect it, then:" >&2
  echo "    git branch -D $scratch_branch" >&2
  exit 1
fi

start_branch="$(git symbolic-ref --quiet --short HEAD || true)"
[ -n "$start_branch" ] \
  || { echo "publish-sync: detached HEAD: switch to a branch first." >&2; exit 1; }

# `git branch -f` refuses a branch that any worktree has checked out, and that
# refusal would otherwise arrive only after every commit had been replayed.
[ "$start_branch" != "$target_branch" ] \
  || { echo "publish-sync: $target_branch is checked out here; run from $source_branch or another branch." >&2; exit 1; }
if git worktree list --porcelain | grep -qx "branch refs/heads/$target_branch"; then
  echo "publish-sync: $target_branch is checked out in a worktree; the ref cannot be moved from here:" >&2
  git worktree list | grep "\[$target_branch\]" | sed 's/^/    /' >&2
  exit 1
fi
target_before="$(git rev-parse "$target_branch")"

# Any exit before `finished=1` unwinds everything: the sequencer, the worktree,
# the branch we were on, and the scratch branch. The target ref is only ever
# moved on the success path, so an abort leaves it exactly where it was.
finished=0
on_exit() {
  rm -f "${pick_err:-}" "${msg_file:-}"
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
pick_err="$(mktemp)"
msg_file="$(mktemp)"

replayed=0
skipped=0
if [ "$mode" = "fast-forward" ]; then
  git branch -f "$target_branch" "$source_branch"
  finished=1
  replayed=$plan_replay
else

git switch --quiet -c "$scratch_branch" "$target_branch"

while IFS=' ' read -r verdict c; do
  [ -z "$c" ] && continue
  if [ "$verdict" = "skip" ]; then
    skipped=$((skipped + 1))
    continue
  fi

  if ! git cherry-pick -n "$c" </dev/null >/dev/null 2>"$pick_err"; then
    # A failure that left nothing unmerged was not a conflict at all (an
    # untracked file in the way, a bad object, a held index lock) and must not
    # pass as an empty commit.
    if [ -z "$(git diff --name-only --diff-filter=U)" ]; then
      echo "publish-sync: cherry-pick of $(git log -1 --format='%h %s' "$c") failed:" >&2
      sed 's/^/    /' "$pick_err" >&2
      exit 1
    fi
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

  # Author, author date and message are the source commit's; only the committer
  # moves. The message loses any Publish-Sync-Source line it already carries
  # (copied back from the target, or pasted) and gains the one trailer that
  # records where this commit came from, so the next run reads a single value.
  # The attribution hook is skipped so it cannot rewrite the message, and stdin
  # is closed so a hook that reads it cannot eat the rest of the commit list.
  git log -1 --no-show-signature --format=%B "$c" \
    | awk 'tolower($0) !~ /^publish-sync-source[ \t]*:/' > "$msg_file"
  GIT_AUTHOR_NAME="$(git log -1 --no-show-signature --format=%an "$c")" \
  GIT_AUTHOR_EMAIL="$(git log -1 --no-show-signature --format=%ae "$c")" \
  GIT_AUTHOR_DATE="@$(git log -1 --no-show-signature --date=raw --format=%ad "$c")" \
  SKIP_AGENT_TRAILER=1 \
    git commit --quiet -F "$msg_file" --trailer "Publish-Sync-Source: $c" </dev/null
  recorded="$(git log -1 --no-show-signature --format='%(trailers:key=Publish-Sync-Source,valueonly)' HEAD | sed '/^$/d')"
  if [ "$recorded" != "$c" ]; then
    echo "publish-sync: the replay of $(git log -1 --format='%h %s' "$c") does not carry exactly one Publish-Sync-Source trailer naming it." >&2
    echo "  The next run could not resume from it. Check the trailer.* git config and any commit-msg hook." >&2
    exit 1
  fi
  echo "  replay  $(git log -1 --format='%h %s' HEAD)"
  replayed=$((replayed + 1))
done <<EOF
$plan
EOF

git switch --quiet "$start_branch"

# Move the publish ref by fast-forward only. Never commit on it directly; the
# protected-branch guard blocks that, by design.
git merge-base --is-ancestor "$target_branch" "$scratch_branch" \
  || { echo "publish-sync: $scratch_branch is not a descendant of $target_branch" >&2; exit 1; }
git branch -f "$target_branch" "$scratch_branch"
git branch -D "$scratch_branch" >/dev/null
finished=1

fi
if [ "$mode" = "fast-forward" ]; then
  echo "publish-sync: $target_branch $(git rev-parse --short "$target_before") -> $(git rev-parse --short "$target_branch")  ($replayed fast-forwarded)"
else
  echo "publish-sync: $target_branch $(git rev-parse --short "$target_before") -> $(git rev-parse --short "$target_branch")  ($replayed replayed, $skipped dropped)"
fi

# Verification: the public-path trees must now match, and no private path may
# survive on the target. Either failure exits 1. The ref stays where it moved
# so the operator can inspect it; nothing has been pushed.
excludes=()
while IFS= read -r p; do
  [ -z "$p" ] && continue
  excludes+=(":(exclude)$p")
done <<EOF
$private_paths
EOF

drift="$(git -c core.quotePath=false diff --name-only "$target_branch" "$source_branch" -- . "${excludes[@]}")"
if [ -n "$drift" ]; then
  echo "publish-sync: WARNING: public-path trees differ from $source_branch:" >&2
  printf '%s\n' "$drift" | sed 's/^/    /' >&2
else
  echo "publish-sync: verified: public-path trees identical to $source_branch."
fi

leak="$(leaked_paths "$target_branch")"
if [ -n "$leak" ]; then
  printf '%s\n' "$leak" | sed "s/^/publish-sync: WARNING: private path '/; s/\$/' is present on $target_branch./" >&2
else
  echo "publish-sync: verified: no private path on $target_branch."
fi

# Every commit the target gained in this run, whatever the mode, must lack the
# private paths in its tree: a path added and later removed would otherwise
# reach public history with a clean tip. Commits the target already had are
# not judged again. A scan that cannot run counts as a leak.
history_leak=""
gained=""
if [ "${#private_args[@]}" -gt 0 ] && ! gained="$(git rev-list "$target_before..$target_branch")"; then
  history_leak="(cannot list $target_before..$target_branch)"
fi
while IFS= read -r c; do
  [ -z "$c" ] && continue
  if ! found="$(git -c core.quotePath=false --literal-pathspecs ls-tree -r --name-only "$c" -- "${private_args[@]}")"; then
    history_leak="(cannot read $c)"
    break
  fi
  if [ -n "$found" ]; then
    history_leak="$(git log -1 --format='%h %s' "$c"): $(printf '%s' "$found" | tr '\n' ' ')"
    break
  fi
done <<EOF
$gained
EOF
if [ -n "$history_leak" ]; then
  echo "publish-sync: WARNING: a commit $target_branch gained in this run carries a private path: $history_leak" >&2
else
  echo "publish-sync: verified: no private path in any commit $target_branch gained."
fi

if [ -n "$drift" ] || [ -n "$leak" ] || [ -n "$history_leak" ]; then
  echo "publish-sync: verification failed; $target_branch has moved but must not be published as is." >&2
  exit 1
fi
echo "publish-sync: done. Nothing was pushed; publish with your repo's publish path."
