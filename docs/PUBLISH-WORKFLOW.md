# Publish workflow (private work → public mirror)

A repo-agnostic runbook for keeping messy private development separate from a
clean public mirror, with a fail-closed gate so you can't publish by accident.
Copy this pattern to any repo you intend to open-source.

Placeholders used below: `PRIV` = your private remote (default `origin`),
`PUB` = your public remote, `PUB_MATCH` = a substring of the public remote URL
(e.g. `myorg/myrepo`), `PUBLISH_BRANCH` = the local line that becomes public
(this repo uses `publish`).

**History mode for this repo:** `preserve` (since 2026-06-09). The public line
keeps atomic, per-agent-authored commits, with no per-batch squash. The two earlier
`release:` squashes were rebuilt into their underlying atomic commits and
force-pushed once (incident-grade, recorded below). The mode is stored in
`git config publishguard.historymode`.

> **Topology note:** this repo's `publish` line is an **orphan** (no shared
> ancestry with `dev`; see the seed in step 3). So publishing is not a plain
> `git merge --ff-only dev`; it is an atomic **cherry-pick** of `dev`'s new
> commits onto `publish` with private paths stripped (see "Day-to-day"). Never
> squash them into one commit.

## Model

- **One private working repo.** Full, messy history is fine: it never goes
  public.
- **One public mirror**, seeded from a single sanitised **orphan** commit
  (no shared ancestry with the messy history → nothing leaks through).
- **One permanent publish line** (`PUBLISH_BRANCH`) that fast-forwards onto the
  public default branch (`main`). It is append-only and always publish-clean.
- **Atomic publishing.** Bring `dev`'s new public-relevant commits onto
  `PUBLISH_BRANCH` individually (cherry-pick), private paths stripped, never
  collapsed into one squash. Private-only commits are dropped; messy/pre-guard
  history never reaches the public line (the orphan seed is the firewall).

Do **not** add a second permanent "integration" branch unless you actually
have collaborators, PRs, or CI that need one. The gap between your local
`PUBLISH_BRANCH` HEAD and `PUB/main` already *is* your staging buffer.

## The one hard invariant

Whatever commit `PUB/main` points at is **immutable**. Rewrite/squash freely
*above* it (commits not yet published); never *at or below* it. Rewriting a
published commit forces a history-rewriting push to the public remote, the
exact hazard the orphan-squash exists to avoid. Treat that as an incident, not
routine.

## One-time setup for a NEW repo

1. **Back up** the full private history: `git bundle create ~/<repo>-history.bundle --all`.
2. **Sanitise the working tree** (parameterise machine paths, move secrets to
   gitignored `*.local`, add `.env.example`, scrub org/repo names; see
   `docs/SECURITY.md`).
3. **Orphan squash** to one clean commit:
   ```bash
   git checkout --orphan PUBLISH_BRANCH
   git add -A && git commit -m "<repo>: initial public release"
   ```
4. **Fresh PUBLIC remote** (create empty, private first), push only this branch:
   ```bash
   git remote add PUB <public-url>
   git push PUB PUBLISH_BRANCH:main
   ```
5. **Rebuild the private line** from the clean base so it ff's to public, and
   point the private remote at it:
   ```bash
   git branch -m <old-dev> <old-dev>-archive-$(date +%Y%m%d)
   git push PRIV <old-dev>-archive-$(date +%Y%m%d)     # keep messy history private
   git push -fu PRIV PUBLISH_BRANCH
   ```
6. **Configure the gate** (local `git config`, never committed):
   ```bash
   git config publishguard.publicmatch   'PUB_MATCH'
   git config publishguard.publicremote  'PUB'
   git config publishguard.publishbranch 'PUBLISH_BRANCH'
   git config publishguard.privateremote 'PRIV'      # optional, default origin
   git config publishguard.sentinel      'PUBLISH_GUARD_OK'   # optional default
   ```
7. **Arm the guards**: `bash scripts/install-guards.sh` (idempotent: installs
   `pre-commit`/`pre-push`, seeds `.publish-guard.local`, reconciles the
   `git publish` alias from the config above).
8. **Restore real personal patterns** in `.publish-guard.local` (install-guards
   seeds it from the placeholder example; it is toothless until you put your
   real machine/org strings back). Verify: a planted personal string in a
   staged file is blocked by `pre-commit`.
9. Flip the public repo to public when satisfied.

## Day-to-day

Work on `dev` with atomic, per-agent-authored commits. Private artefacts
(`HANDOFF.md`, `RUNBOOK.md`, `runs/`, `archive/`, and the Autometta runtime under
`state/`) stay tracked on `dev`, so they follow clones and worktrees; the publish
step strips them, and the `pre-push` private-file scan refuses any that slip
through.

Because `publish` is an orphan line, publish by cherry-picking `dev`'s new public
commits onto it (no squash). `scripts/publish-sync.sh` does this: it is a dry run
by default, `--apply` acts, and it strips every path listed in
`publishguard.privatefile` (one entry each: `HANDOFF.md`, `RUNBOOK.md`, `runs`,
`archive`, `state`, `.publish-guard-strict.local.example`). Each replayed commit
carries a `Publish-Sync-Source: <dev sha>` trailer, and the newest one on
`publish` is the watermark the next run starts from. Commits published before
2026-11-15 carry no trailer, so read the dry run's `base` line: if it is missing
and the plan starts at the root commit, the watermark is absent and the plan is
wrong for this mirror; do not `--apply` it.

The same replay by hand, from the last published `dev` commit:

```bash
git switch -c publish-sync publish
for c in $(git rev-list --reverse --no-merges <last-published-dev-commit>..dev); do
  git cherry-pick -n "$c" || true
  git rm -r -f -q --ignore-unmatch runs archive state RUNBOOK.md HANDOFF.md \
    .publish-guard-strict.local.example
  git diff --cached --quiet && continue        # drop private-only commits
  git commit -C "$c" --trailer "Publish-Sync-Source: $c"   # keep message + author
done
git diff --stat publish-sync <dev-tip>         # sanity: public-path trees match
git switch dev && git branch -f publish publish-sync && git branch -D publish-sync
git push PRIV publish                           # private backup first
git push PUB publish                            # PR source, private-file scanned
gh pr create --base main --head publish         # read the diff, then merge on the forge
```

Public `main` advances only by merging that PR on the forge. After the merge,
fast-forward the local `publish` to `PUB/main` if the forge added a merge
commit. Each cherry-pick onto an orphan base can conflict on files the seed
curated differently; resolve toward `dev`'s version (`git checkout <c> -- <file>`),
since `dev` is the source of truth for public content.

`git publish` (alias, set by install-guards) =
`git push PRIV PUBLISH_BRANCH && PUBLISH_GUARD_OK=1 git push PUB PUBLISH_BRANCH:main`.
It backs up to the private remote first, then pushes public `main`. Under the
default `publishguard.boundary=pr` that second push is refused (see below), so
the alias only completes a publish in a repo that has opted into `direct`.

## The gate (why it can't be bypassed by accident)

`pre-push` fails closed on the public remote (matched by
`publishguard.publicmatch`):

- non-default branch to public → rejected, except the configured PR source
  (`publishguard.prsource`, default `publish`), which is allowed
  private-file-scanned so the publish→main PR can exist;
- default branch to public → rejected **unless** the `PUBLISH_GUARD_OK=1`
  sentinel is set, which only `git publish` does, and even then rejected while
  `publishguard.boundary` is `pr` (the default): the public default branch
  advances by a reviewed merge on the forge, not by a push. There is no
  environment-variable override; opt out per repo with
  `git config publishguard.boundary direct`;
- non-fast-forward to the public default branch → rejected, sentinel or not.

So a hand-typed `git push PUB PUBLISH_BRANCH:main` is blocked and told to push
the PR source and open a PR instead. Deliberate
one-off override: `git push --no-verify`. Org/repo names live only in local
`git config`: never in the committed tree, so this file stays publishable.

Why fail-closed, not a warning: publishing is effectively irreversible (objects
stay fetchable by SHA, content gets cached/indexed). A guardrail for an
irreversible outward action must stop it and point at the right command, not
narrate the mistake as it completes.

## History mode: squash vs preserve

Two ways to keep the public mirror clean, picked once per repo and recorded at
the top of this file.

**`squash` (default)**: orphan-squash seed; `git merge --squash wip/x` for
every merge so the publish line is one-commit-per-topic. Right when the
private history carries secrets/absolute paths/personal positioning copy that's
painful to scrub commit-by-commit, or when the pivots aren't part of the story.

**`preserve`**: sanitise the *full* history with `git filter-repo` (paths,
secrets, personal positioning copy scrubbed across every commit), then push as the seed.
Ongoing merges use `git merge --no-ff wip/x` so per-agent authors, atomic
commits, and pivots survive on the public mirror. Right when the repo was
built with commit discipline (atomic commits, per-agent `--author=`) and the
pivots *are* the story (portfolio value).

**Preserve-readiness test** (before switching): every commit must pass the
pre-commit guards, not just the tip. Walk the history:

```bash
git rev-list publish | while read sha; do
  git checkout "$sha" -- . && bash scripts/check-secrets.sh || { echo "FAIL at $sha"; break; }
done
```

If anything trips, either `git filter-repo` until clean, or stay on `squash`.

**Switching modes mid-life** is incident-grade for `squash → preserve` (you
have to rebuild the squashed public commits into their atomic originals and
force-push, which the gate blocks by default). `preserve → squash` is just "from
now on, collapse": no rewrite needed.

> **Done here (2026-06-09).** The two `release:` squashes (`e7e40d4`, `1452b9f`)
> were rebuilt into the 10 atomic `dev` commits they bundled (cherry-picked onto
> `519eb7b`, private paths stripped) and force-pushed to `PUB/main`. The final
> tree was verified byte-identical to the pre-rewrite public tip, so only commit
> granularity changed. Backup bundle: `~/repos/agentic-rag-kimble-backup-20260609.bundle`;
> the rebuilt line is also on `PRIV` as `publish-atomic`. From here, follow the
> atomic cherry-pick flow in "Day-to-day"; do not squash again.

### Squashing rules (both modes)

- Squash *unpublished* commits at will: topic-branch `--squash` merge (best:
  the publish line stays append-only, every push is a clean ff, no force
  anywhere), or `git rebase -i <PUB/main commit>` for a quick local tidy
  (then the private push needs `--force-with-lease`).
- Never squash/rebase commits already on `PUB/main`. The pre-push hook now
  blocks non-fast-forward pushes to public `main`/`master` even with the
  sentinel set; override is `--no-verify`, treat that use as an incident.

## Applying to other repos

The mechanism is shipped, not hand-rolled: `scripts/git-hooks/{pre-commit,
pre-push}` + `scripts/install-guards.sh` + `.publish-guard.local.example` are
generic and config-driven. To adopt in another repo, copy those four files,
run the one-time setup above, and set the five `publishguard.*` config keys.
Nothing in the committed tree is repo-specific.

Notes when porting:
- If a repo's default public branch is `master`, the `pre-push` hook already
  allows `main` or `master`; the alias pushes `PUBLISH_BRANCH:main`; adjust
  the alias target if you want `master`.
- A repo that already has `.env`/secrets wrappers (e.g. an `op-refs` +
  `.env.example` setup) only needs the publish-gate config + guards armed; the
  credential side is already conformant.
