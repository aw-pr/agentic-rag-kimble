# Publish workflow (private work → public mirror)

A repo-agnostic runbook for keeping messy private development separate from a
clean public mirror, with a fail-closed gate so you can't publish by accident.
Copy this pattern to any repo you intend to open-source.

Placeholders used below: `PRIV` = your private remote (default `origin`),
`PUB` = your public remote, `PUB_MATCH` = a substring of the public remote URL
(e.g. `myorg/myrepo`), `PUBLISH_BRANCH` = the local line that becomes public
(this repo uses `publish`).

**History mode for this repo:** `squash` (default). Each adopting repo declares
its mode here at adoption — flip to `preserve` only after the readiness test
below. The mode is stored in `git config publishguard.historymode`.

## Model

- **One private working repo.** Full, messy history is fine — it never goes
  public.
- **One public mirror**, seeded from a single sanitised **orphan** commit
  (no shared ancestry with the messy history → nothing leaks through).
- **One permanent publish line** (`PUBLISH_BRANCH`) that fast-forwards onto the
  public default branch (`main`). It is append-only and always publish-clean.
- **Ephemeral topic branches** for everything else. Squash-merge them into
  `PUBLISH_BRANCH` when ready. Messy commits never reach the public line.

Do **not** add a second permanent "integration" branch unless you actually
have collaborators, PRs, or CI that need one. The gap between your local
`PUBLISH_BRANCH` HEAD and `PUB/main` already *is* your staging buffer.

## The one hard invariant

Whatever commit `PUB/main` points at is **immutable**. Rewrite/squash freely
*above* it (commits not yet published); never *at or below* it. Rewriting a
published commit forces a history-rewriting push to the public remote — the
exact hazard the orphan-squash exists to avoid. Treat that as an incident, not
routine.

## One-time setup for a NEW repo

1. **Back up** the full private history: `git bundle create ~/<repo>-history.bundle --all`.
2. **Sanitise the working tree** (parameterise machine paths, move secrets to
   gitignored `*.local`, add `.env.example`, scrub org/repo names — see
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
   seeds it from the placeholder example — it is toothless until you put your
   real machine/org strings back). Verify: a planted personal string in a
   staged file is blocked by `pre-commit`.
9. Flip the public repo to public when satisfied.

## Day-to-day

```bash
git switch -c wip/<thing>        # messy commits, freely
# …work…
git switch PUBLISH_BRANCH
git merge --squash wip/<thing>
git commit -m "One clean message"
git publish                      # PRIV PUBLISH_BRANCH, then ff PUB/main
git branch -D wip/<thing>
```

`git publish` (alias, set by install-guards) =
`git push PRIV PUBLISH_BRANCH && PUBLISH_GUARD_OK=1 git push PUB PUBLISH_BRANCH:main`.
It backs up to the private remote first, then publishes.

## The gate (why it can't be bypassed by accident)

`pre-push` fails closed on the public remote (matched by
`publishguard.publicmatch`):

- non-default branch to public → rejected;
- default branch to public → rejected **unless** the `PUBLISH_GUARD_OK=1`
  sentinel is set, which only `git publish` does.

So a hand-typed `git push PUB PUBLISH_BRANCH:main` is blocked and told to use
`git publish` (which guarantees the private backup happened first). Deliberate
one-off override: `git push --no-verify`. Org/repo names live only in local
`git config` — never in the committed tree, so this file stays publishable.

Why fail-closed, not a warning: publishing is effectively irreversible (objects
stay fetchable by SHA, content gets cached/indexed). A guardrail for an
irreversible outward action must stop it and point at the right command, not
narrate the mistake as it completes.

## History mode: squash vs preserve

Two ways to keep the public mirror clean, picked once per repo and recorded at
the top of this file.

**`squash` (default)** — orphan-squash seed; `git merge --squash wip/x` for
every merge so the publish line is one-commit-per-topic. Right when the
private history carries secrets/absolute paths/personal positioning copy that's
painful to scrub commit-by-commit, or when the pivots aren't part of the story.

**`preserve`** — sanitise the *full* history with `git filter-repo` (paths,
secrets, personal positioning copy scrubbed across every commit), then push as the seed.
Ongoing merges use `git merge --no-ff wip/x` so per-agent authors, atomic
commits, and pivots survive on the public mirror. Right when the repo was
built with commit discipline (atomic commits, per-agent `--author=`) and the
pivots *are* the story (portfolio value).

**Preserve-readiness test** — before switching: every commit must pass the
pre-commit guards, not just the tip. Walk the history:

```bash
git rev-list publish | while read sha; do
  git checkout "$sha" -- . && bash scripts/check-secrets.sh || { echo "FAIL at $sha"; break; }
done
```

If anything trips, either `git filter-repo` until clean, or stay on `squash`.

**Switching modes mid-life** is incident-grade for `squash → preserve` (you
have to rebuild the public seed from a sanitised history and force-push, which
the gate blocks by default). `preserve → squash` is just "from now on, merge
with `--squash`" — no rewrite needed.

### Squashing rules (both modes)

- Squash *unpublished* commits at will — topic-branch `--squash` merge (best:
  the publish line stays append-only, every push is a clean ff, no force
  anywhere), or `git rebase -i <PUB/main commit>` for a quick local tidy
  (then the private push needs `--force-with-lease`).
- Never squash/rebase commits already on `PUB/main`. The pre-push hook now
  blocks non-fast-forward pushes to public `main`/`master` even with the
  sentinel set — override is `--no-verify`, treat that use as an incident.

## Applying to other repos

The mechanism is shipped, not hand-rolled: `scripts/git-hooks/{pre-commit,
pre-push}` + `scripts/install-guards.sh` + `.publish-guard.local.example` are
generic and config-driven. To adopt in another repo, copy those four files,
run the one-time setup above, and set the five `publishguard.*` config keys.
Nothing in the committed tree is repo-specific.

Notes when porting:
- If a repo's default public branch is `master`, the `pre-push` hook already
  allows `main` or `master`; the alias pushes `PUBLISH_BRANCH:main` — adjust
  the alias target if you want `master`.
- A repo that already has `.env`/secrets wrappers (e.g. an `op-refs` +
  `.env.example` setup) only needs the publish-gate config + guards armed; the
  credential side is already conformant.
