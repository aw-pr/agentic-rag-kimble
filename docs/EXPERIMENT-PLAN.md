# Experimental-rigour scaffold — plan

Status: **draft, not yet executed**. Branch: `pass-29` off `dev`. Do not start
execution until the open decisions in section J are made.

## A. Why a scaffold at all

A Claude Code session is not persistent: it has a finite context window and
dies when closed. The work-to-deliver (flat-RAG baseline, tool ablation,
fixture expansion, bootstrap CIs, domain gesture, write-up) is at least one
to two weeks of bounded tasks. Running it inside one session would burn
context on coordination and break on the first stall.

The scaffold moves coordination *out of Claude entirely*: a tickless,
LLM-free state machine driven by `cron` decides which task runs next and
respawns stalled work. LLMs only get invoked when there is a discrete,
bounded brief to execute or verify. Orchestration cost approaches zero
between tasks.

## B. Architecture

```
                    cron (every 5 min)
                          │
                          ▼
              scripts/experiment-tick.sh           (pure bash/python, no LLM)
                          │
            ┌─────────────┼───────────────┐
            ▼             ▼               ▼
       Queue state   Stall detection  Spawn decision
   runs/experiment/  (pid alive +     (deps met +
   state.yaml        log mtime <12m)   parallel slot free)
                          │
       ┌──────────────────┼──────────────────────┐
       ▼                  ▼                      ▼
   Worker            Verifier               Triage
   T2/T3             Cross-family           T1 Opus
   Sonnet 4.6  OR    GPT-5.5 via            (this session
   GPT-5.5 via       Codex CLI              when invoked
   Codex CLI         (different family      headless)
                     from worker)
       │                  │                      │
       └──────────────────┴──────────────────────┘
                          │
                          ▼
                  Per-task workdir
              runs/experiment/tasks/<id>/
              ├── brief.md
              ├── log               (heartbeat = mtime)
              ├── result.json       (acceptance criteria, evidence)
              └── diff.patch        (proposed changes)
                          │
                          ▼
                  Tick reads result.json
                  Stages diff, commits granularly,
                  enqueues dependent tasks.
```

**Why this shape:**

- Cron + bash gives the autonomy you asked for without inventing a daemon.
- The tick script is dumb. All judgement lives in the LLM tasks. The
  boundary is clean and testable.
- Worker and verifier *must* be different model families (Anthropic vs
  OpenAI). Same-family verification has known same-error bias. This is the
  single most defensible piece of independent verification cheap enough to
  do everywhere.
- Triage stays Opus (this session, headless `claude -p`) because triage is
  rare and benefits from the strongest reasoning.

## C. State machine

Five states. Transitions are explicit so the log is auditable:

| State | Entry condition | Exit |
|---|---|---|
| `queued` | task created or `stalled` retry | tick sees deps met + slot free -> `running` |
| `running` | tick spawned the worker (pid recorded, start time logged) | result.json written -> `done` or `failed`; or stall detected -> `stalled` |
| `stalled` | pid dead OR log mtime > 12 min | attempts < max -> `queued`; else -> `blocked` |
| `blocked` | retry budget exhausted, or verifier rejected twice | triage task enqueued; on triage decision -> `queued` (re-spec) or `failed` (abort) |
| `done` | result.json passes acceptance criteria AND verifier returned PASS | commit + enqueue dependents |
| `failed` | triage decided abort, or hard error in tick script | logged; phase marked broken; surfaces in heartbeat report |

Queue format (one task):

```yaml
- id: phase-4-baseline-chunker
  phase: 4
  tier: T3
  worker_pref: sonnet            # claude code subagent
  verifier_pref: gpt-5.5          # codex exec
  depends_on: [phase-1-spec, phase-2-fixtures]
  state: queued
  attempts: 0
  max_attempts: 2
  brief_path: runs/experiment/tasks/phase-4-baseline-chunker/brief.md
  acceptance:
    - "src/baseline/chunker.py exists and exports chunk_entity(node)->list[Chunk]"
    - "pytest tests/unit/baseline/test_chunker.py -q is green"
    - "ruff + mypy clean per smoke-test"
```

## D. Tier routing for this experiment

Per the agent-orchestrator skill rubric, mapped to your models:

| Tier | Model | When used in this experiment |
|---|---|---|
| T0 orchestrator | Opus 4.7 (this main session) | Phase 1 spec, Phase 7 held-out run, Phase 9 verdict, Phase 10 synthesis, all triage |
| T1 hard reasoning (subagent) | Opus 4.7 via `claude -p` headless | Triage decisions, second-domain schema design, write-up critique |
| T1 cross-family verifier | **GPT-5.5 via Codex CLI** | Verifying any T1 output (experimental design, schema design, write-up) |
| T2 specified implementation | Sonnet 4.6 via Claude Code subagent | Baseline retrieval, ablation parametrisation, harness extensions |
| T3 mechanical | Sonnet 4.6 (low thinking) or **GPT-5.5 via Codex CLI** | Chunkers, fixture batches, doc rewrites |
| T4 retrieval | Explore agent (Haiku) | Codebase surveys before any T1/T2 brief |

**Cross-family verification rule:** every task with a `worker_pref` of
`sonnet` gets a `verifier_pref` of `gpt-5.5`, and vice versa. The
orchestrator picks the worker family to balance load; the verifier is
always the other. Phase outputs you will read (EXPERIMENT.md, results
tables) get *both* a Codex critique and an Opus sanity-check before
commit.

## E. Phases

Numbered. Each phase ends with a tagged commit
(`experiment/phase-N-complete`) and a build-log entry.

### Phase 0 — Bootstrap the scaffold (manual session)

This is the only phase done by us in one session, because the scaffold
has to exist before it can run anything. About 4-6 commits.

Artefacts:
- `scripts/experiment-tick.sh` (heartbeat + state machine, bash)
- `scripts/experiment-spawn-worker.sh` (wraps `claude -p` and `codex exec`)
- `scripts/experiment-spawn-verifier.sh` (always cross-family)
- `scripts/experiment-status.sh` (read-only dashboard)
- `scripts/install-experiment-cron.sh` (cron entry installer)
- `scripts/uninstall-experiment-cron.sh` (surgical: removes only the
  experiment-tick line, leaves other crontab entries intact)
- `runs/experiment/` layout
- `docs/EXPERIMENT-SCAFFOLD.md` (operator manual: how to read state, how
  to abort, how to resume)
- `tests/unit/experiment/` — state machine, stall detection, retry policy
- Smoke-test gate extension: scaffold tests join the existing gate

### Phase 1 — Experimental specification (T0/T1)

Pin the hypothesis before any code runs. Output: `docs/EXPERIMENT.md`
with hypothesis, primary metric, secondary metrics, falsification
criterion, statistical method (bootstrap CIs, 10k resamples, BCa
interval, alpha=0.05), domain caveats. Drafted by Opus in main session;
Codex GPT-5.5 critiques the experimental design (different family
review).

Falsification example to be pinned: *"If flat-RAG recall@10 on the
held-out set is within Δ=0.05 of dimensional recall@10 with overlapping
95% CIs, the hypothesis fails."* The exact Δ is in the open decisions.

### Phase 2 — Fixture expansion + held-out split (T2/T3 + verify)

Expand 20 -> 100 fixtures. Eight worker batches of 10 fixtures each,
parallel, disjoint output files. Held-out 20 sealed into
`tests/eval/fixtures/holdout/` and gitignored from any retrieval path.
Each batch verified by a Codex run that re-checks every expected entity
exists in the live graph and is solvable by at least one tool.

### Phase 3 — Statistical harness (T2 + verify)

Add bootstrap CIs to `src/eval/metrics.py`. n_resamples=10000, BCa
interval. Per-tool breakdown also gets CIs. Tests for the bootstrap
against scipy reference. Codex independently re-implements the
bootstrap in a separate file and confirms numbers agree to 3 decimal
places.

### Phase 4 — Flat-RAG baseline (T2/T3 + verify) — *the big one*

Implement naive RAG over the same OpenML corpus:

- 4a — chunker (one chunk per entity, controls for chunking strategy). T3, Codex.
- 4b — flat vector store (BGE-small, same embedding model, controls for embedder). T2, Sonnet.
- 4c — baseline orchestrator: same Claude Agent SDK loop, single tool `flat_search(query, k)`. T2, Sonnet.
- 4d — baseline eval runner; produces the same JSON output shape as the dimensional run. T3, Codex.
- 4e — verifier: Codex independently re-runs 4c against fixtures and confirms numbers match Sonnet's report to 3 decimal places.

Output: a single table in EXPERIMENT.md, dimensional vs flat across
recall@5/10, judge dimensions, latency, agent token cost.

### Phase 5 — Tool ablation matrix (T2 + verify)

Parametrise the orchestrator with `--tools` allowlist. Run seven cells:
{graph}, {semantic}, {aggregate}, {graph+semantic}, {graph+aggregate},
{semantic+aggregate}, {all_three}, plus the flat baseline from Phase 4
as control. 80 dev fixtures per cell. Outputs a matrix in
EXPERIMENT.md. Sonnet builds; Codex spot-checks two cells end-to-end.

### Phase 6 — Domain generalisation gesture (T1 schema + T2 build + verify)

One additional corpus. Build a minimal Kimball model on it (Opus
designs schema, Codex critiques for Kimball orthodoxy), ingest a small
slice, run ~20 cross-domain fixtures. Explicitly framed as a gesture,
not a comprehensive cross-domain study, and the write-up will say so.
Corpus choice is in the open decisions.

### Phase 7 — Held-out test run (T0 only, no delegation)

Lock everything. Run the 20 held-out fixtures once against dimensional,
flat, and the winning ablation cell. Report with CIs. No tweaking after
seeing this result. Opus runs it in main session; output committed
verbatim.

### Phase 8 — Write-up (T1 draft + cross-family critique + T3 polish)

Update `EXPERIMENT.md` with final results. Refresh README to match the
experimental framing (architectural narrative, not recruiter
positioning). Build-log entries per phase. Codex critiques the draft.
T3 does the mechanical doc rewrites.

### Phase 9 — Verdict (T0 only)

Compare results to the Phase-1 pre-registered falsification criterion.
PASS / PARTIAL / FAIL. Documented in EXPERIMENT.md and a build-log
entry. Honest, with the limitations section as long as the results
section.

### Phase 10 — Learnings review

Runs only after Phase 9 commits, so the verdict is settled before
lessons are derived (no halo-effect bias on the lessons). Tier: T0
Opus synthesises; Codex GPT-5.5 critiques the draft; Sonnet does any
mechanical skill-file edits you approve.

Six categories, each with a concrete artefact:

| # | Category | Looking for | Output artefact |
|---|---|---|---|
| 1 | Existing-skill updates | What did `agent-orchestrator` over/underpower? Did cross-family verification reveal patterns worth codifying? Did `auth-route-security` need anything new from running Codex + Claude side-by-side? Did `simplify` apply to any scaffold code? | Diff against `~/.claude/skills/<name>/SKILL.md` for review |
| 2 | New skills needed | `experiment-scaffold` (abstracted heartbeat + queue), `cross-family-verification` (worker/verifier pairing), `phase-commit-curation` (branch-per-phase + tagged trailers). Decided on evidence, not speculation. | New skill scaffold under `~/.claude/skills/<name>/` |
| 3 | Auth / automation rules ("autumn rules" — assumed auth/auto, flag if mis-read) | Any new credential surface from `codex exec` + `claude -p` running unattended. Hook entries in settings.json for the new commands. Pre-tool guard for the leak-check on scaffold writes. | Diffs to `settings.json` and `auth-route-security` skill if patterns generalised |
| 4 | Git curation | What worked / didn't in the per-task commit model. Trailer convention adoption rate. Branch hygiene at phase boundaries. Whether the leak guard caught anything live. Tag strategy review. | `docs/GIT-CURATION.md` lessons; possible new skill `phase-commit-curation` |
| 5 | Scaffolding meta-process | Tick interval (was 5 min right?), parallel cap (was 3 right?), triage rate (too eager / too late?), Codex vs Claude reliability deltas, watchdog tuning, real verifier-disagreement causes. | `runs/build-log/pass-N-scaffold-postmortem.md` |
| 6 | Experiment outcomes | Did the hypothesis answer hold up under the falsification criterion? Which Phase-1 calls were premature in hindsight? Was the second-domain gesture informative? iTone material? Project memory worth writing for future work? | Project memory files in `~/.claude/projects/.../memory/`, build-log entry, iTone-post outline |

## F. Heartbeat and stall handling

- Cron tick: every 5 min. (5 min is short enough that a stall is detected
  within one watchdog window; long enough that cron load is negligible.)
- Worker stall window: 12 min (gives margin over the Claude Code subagent
  10-min watchdog noted in the project memory).
- Max attempts per task: 2. Third failure triggers triage.
- Max parallel workers: 3 (avoid thrashing the Claude Max quota or the
  Codex rate limit).
- Triage queue is single-threaded (one decision at a time).
- Hard abort: a sentinel file `runs/experiment/STOP` halts new spawns
  immediately; running workers finish their current task.

### Budget guards (warnings, not silent kills)

Subscription routes aren't per-token billed, so a hard token cap would
be theatre. These guards exist to catch a misbehaving *task* before it
wedges the quota or the experiment.

1. **Per-task subagent cap.** Each task declares an `expected_subagents`
   count in its queue entry (default 1). If a worker spawns more than 3
   nested subagents, tick SIGTERMs it and marks the task `blocked` with
   reason `runaway-subagents`. Triage decides.
2. **Per-phase wall-clock guard.** Each phase manifest declares
   `expected_llm_hours`. When the cumulative LLM-active time for a
   running phase passes 2× that figure, the Mayor flashes a yellow
   warning and the heartbeat *pauses spawning for that phase only*
   until you acknowledge with `ack-overspend <phase>` in the Mayor
   shell. Soft brake.
3. **Daily spawn observation.** The tick maintains a rolling 24h
   counter of worker+verifier spawns. Above the threshold (default 50,
   set in `runs/experiment/budget.yaml`) the Mayor surfaces "burning
   fast" with the rate. No automatic action; pure visibility.

Defaults live in `runs/experiment/budget.yaml`. Editing the file takes
effect on the next tick (no restart). Override per task in the queue
entry.

## G. Git policy — granular traceability

- Every `done`+verified task = one commit, conventional commits:
  - `experiment(scaffold): …`
  - `experiment(phase-2): fixture batch 03 of 08 — semantic queries`
  - `test(experiment): bootstrap CI reference parity with scipy`
- Trailers on every commit:
  - `Co-Authored-By: <worker model> <noreply@…>`
  - `Verified-By: <verifier model>`
- Branch strategy: feature branch per phase off `pass-29`. Phase-complete
  merges into `pass-29` via fast-forward only. `dev` and `main` untouched
  until you say so. (Branch strategy is in the open decisions — single
  branch is the alternative.)
- Tags: `experiment/phase-N-complete` after verifier passes and tests are
  green.
- The leak guard already in `pre-commit` runs on every scaffold commit too.

## H. Test policy — complete, defendable, readable

- Every new module has a paired test file with a "Why this exists" header.
- Every test has a docstring stating *what it proves*, not just *what it
  does*.
- Property-based tests (Hypothesis library) for the bootstrap CI and the
  state machine — small additional dependency, high ROI for "defendable".
- Golden fixtures live next to the test that uses them, with an inline
  comment explaining selection.
- New `docs/TESTING.md` codifies the policy so future contributors can
  defend it.
- Coverage gate: the existing pytest suite already runs in smoke-test; the
  new experiment tests join it. Mypy and ruff clean on all new code (the
  pragmatic baseline from pass 28).
- Phase 0 includes tests for the scaffold itself (state machine
  transitions, stall detection mocked, retry counter, parallel-spawn
  isolation).

## I. Cost and risk

- Token spend: dominated by the LLM judge runs (Max quota) and Codex
  verifier runs (your ChatGPT subscription). No API billing on either
  route — both are subscription routes. The auth-route-security skill's
  discipline already enforces this in the repo.
- Anthropic rate limits: 3-parallel cap stays well within Max quota for
  non-bursty work.
- Wall-clock estimate: 5-10 calendar days of unattended ticking, plus 1-2
  sessions of operator attention at phase boundaries.
- Biggest single risk: the baseline implementation (Phase 4) being subtly
  biased to favour the dimensional system. Mitigation: Codex independently
  re-implements the baseline runner in a separate file and asserts
  numerical parity with the Sonnet version. If they disagree, that itself
  is a Phase-4 finding.
- Second risk: fixture leakage between dev and held-out. Mitigation:
  held-out fixtures are in a `holdout/` directory that is gitignored from
  any retrieval path and only readable by the Phase-7 runner.

## J. Decisions (settled)

1. **Primary outcome metric:** `recall@10` on the held-out set. Bootstrap
   95% CI. Judge grounding is reported as secondary, not the hypothesis
   bet.
2. **Branch strategy:** all commits direct on `pass-29`. Phase boundaries
   marked by annotated tags (`experiment/phase-N-complete`) rather than
   branch merges. The chronological log is the trail.
3. **Second corpus for Phase 6:** arXiv ML metadata. Schema candidates:
   `Paper` (fact-ish), `Author`, `Venue`, `Date`, `Topic/Category` (dim).
   Honest caveat in the write-up that arXiv is adjacent to OpenML in
   domain, so the generalisation claim is modest.
4. **Codex model pinned:** `gpt-5.5`. Fall-back path: if a task spawn
   fails because the model isn't routable through your Codex install,
   the spawner exits non-zero, the tick marks the task `stalled`, and
   triage surfaces it to you rather than silently downgrading.
5. **"Autumn rules" = hooks.** Phase 10 category 3 produces concrete
   `settings.json` hook entries derived from what actually happened
   during the experiment (e.g. a `PreToolUse` hook for `Bash` that
   blocks `git commit` if smoke-test is dirty; a `Stop` hook that prints
   the experiment status). Existing `update-config` skill is the
   delivery mechanism.

## K. Monitoring and control — "the Mayor"

You wanted something always-on you can glance at, that lets you ask
questions and stop things. Built as a tmux session running a Python
`rich`-based live dashboard plus an interactive control shell. No new
dependencies beyond `rich`, which is already a transitive dep via
Streamlit.

### Layout

```
┌─────────────────────────────────────────────────────────────────┐
│ MAYOR — experiment-rigour scaffold                              │
├──────────────────────┬──────────────────────────────────────────┤
│ Queue                │ Current task                             │
│ queued      8        │ id: phase-4-baseline-store               │
│ running     2  ●●    │ worker: sonnet (subagent 04bd...)        │
│ stalled     0        │ started: 14:23  (4m12s ago)              │
│ blocked     1  !     │ heartbeat: 12s ago  ✓                    │
│ done       17        │ last log: "embeddings built, indexing…"  │
│ failed      0        │                                          │
├──────────────────────┼──────────────────────────────────────────┤
│ Recent commits (5)   │ Last triage decision                     │
│ a1b2c3 experiment(…) │ task: phase-2-fixture-batch-06           │
│ d4e5f6 test(experi…) │ verdict: re-spec (acceptance ambiguous)  │
│ …                    │ when: 13:58                              │
├──────────────────────┴──────────────────────────────────────────┤
│ Budget                                                          │
│ phase-4 LLM-active 3h12m / expected 6h     ✓                    │
│ 24h spawns: 23 / threshold 50              ✓                    │
│ runaway subagents this run: 0              ✓                    │
├─────────────────────────────────────────────────────────────────┤
│ Event tail (last 20 lines of runs/experiment/events.log)        │
│ 14:27:14  phase-4-baseline-store -> running                     │
│ 14:26:55  phase-3-bootstrap-ci   -> done (verified by gpt-5.5)  │
│ …                                                               │
├─────────────────────────────────────────────────────────────────┤
│ > _                                                             │
│   pause | resume | stop | show <task> | brief <task>             │
│   verify <task> | requeue <task> | tail <task> | quit            │
└─────────────────────────────────────────────────────────────────┘
```

### Pieces

- `scripts/experiment-mayor.sh` — starts (or attaches to) a tmux session
  named `mayor`. Two panes: dashboard (top) and control shell (bottom).
  Detach with the usual tmux prefix; reattach with the same script.
- `scripts/experiment-dashboard.py` — pure read-only. Refreshes every
  2 s. Reads `runs/experiment/state.yaml`, `events.log`, `git log -5`,
  the per-task `log` files. Does nothing to the running system.
- `scripts/experiment-control.sh` — interactive REPL in the lower
  pane. Commands:
  - `pause` — writes `runs/experiment/PAUSE`. Tick stops spawning new
    workers; in-flight workers finish their task.
  - `resume` — removes `PAUSE`.
  - `stop` — writes `runs/experiment/STOP`. Tick stops spawning *and*
    sends SIGTERM to running workers. The hard abort from section F.
  - `show <task>` — full state row from state.yaml.
  - `brief <task>` — opens the task brief in `$PAGER`.
  - `tail <task>` — tails the worker log.
  - `verify <task>` — queues an out-of-band verifier re-run.
  - `requeue <task>` — forces a `blocked`/`failed` task back to
    `queued` with attempts reset (after you've fixed the cause).
  - `quit` — leaves the control shell; tmux session stays up.

### "Ask questions"

The control shell answers state questions (`show`, `brief`, `tail`,
`why-blocked <task>`). For *experimental* questions ("does the
bootstrap CI agree with scipy?", "show me the baseline numbers so
far") the same shell exposes `query <natural language>` which spawns
a one-off Opus headless session against the experiment workdir,
read-only. Bounded cost, no parallel cap impact.

### Failure mode

If the tmux session dies, nothing breaks: the cron tick is the source
of truth and keeps running. Re-launch with `scripts/experiment-mayor.sh`
and you're back. The mayor is observability, not the control plane.

### Why this shape

You asked for something visible from any terminal that lets you stop
things and ask questions. tmux gives detach/reattach across terminals
without reinventing session management. `rich` gives a readable live
dashboard without inventing a TUI. The sentinel-file pattern (`STOP`,
`PAUSE`) is how the heartbeat already learns to halt, so the control
plane has zero new state. Everything is one Python file plus two
bash scripts.

## L. Ready to start

Decisions locked, the Mayor specified. The plan is now committable. On
your "go" I:

1. Commit this plan to `pass-29` (one commit).
2. Begin Phase 0 in this session: build the scaffold + the Mayor,
   commit granularly, leave you with a working tmux dashboard before
   the autonomous loop kicks off.
