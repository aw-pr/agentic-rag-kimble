# Experiment scaffold — operator manual

The runbook for actually using the long-running experiment scaffold. The
*plan* lives in `docs/EXPERIMENT-PLAN.md`; this file is the steady-state
how-to.

## TL;DR

```bash
# One-time on this machine:
./scripts/install-experiment-cron.sh    # adds */5 * * * * tick to your crontab

# Open the Mayor (read-only dashboard + control shell):
./scripts/experiment-mayor.sh           # tmux session, detach/reattach as usual

# Pause spawning (e.g. before looking at something):
#   in the Mayor's control shell:  pause   /  resume

# Hard stop (kills running workers next tick):
#   in the Mayor's control shell:  stop

# Walk-away stop (no more tick at all):
./scripts/uninstall-experiment-cron.sh
```

## What is actually running

The scaffold is **not** one process. It is three layers, glued by files
under `runs/experiment/`:

1. **Cron** fires `scripts/experiment-tick.sh` every five minutes. No LLM cost.
2. **The tick** (Python, `src.experiment.tick`) reads state, decides who runs, spawns workers in the background via `scripts/experiment-spawn-{worker,verifier}.sh`, then exits.
3. **The Mayor** (`scripts/experiment-mayor.sh`) is a tmux session you keep open. The dashboard reads state every 2 s; the control shell writes sentinel files when you tell it to.

If you close the Mayor, the cron keeps ticking. If you remove the cron, the Mayor keeps showing the last state on disk. Each layer is independent.

## The files that matter

| Path | What |
|---|---|
| `runs/experiment/state.yaml` | The queue. Every task as a YAML entry, current state. Source of truth. |
| `runs/experiment/budget.yaml` | Pacing knobs (parallel cap, stall window, daily spawn threshold). Edits take effect on next tick. |
| `runs/experiment/budget.yaml.example` | Committed default; copy to `budget.yaml` to override. |
| `runs/experiment/events.log` | One-line tick markers + per-transition log. Append-only. |
| `runs/experiment/tasks/<id>/` | Per-task workdir: `brief.md` (input), `log` (worker/verifier output), `result.worker.json` (worker verdict, preserved on worker-pass), `result.json` (final verifier verdict on `done` tasks), `diff.patch` (proposed changes). |
| `runs/experiment/PAUSE` | Sentinel: tick stops spawning; in-flight workers finish. |
| `runs/experiment/STOP` | Sentinel: tick stops spawning *and* SIGTERMs running workers. |
| `runs/experiment/cron.log` | stdout/stderr of the cron-invoked tick. Useful when the heartbeat itself is misbehaving. |

## States, and what each one means

| State | Meaning | Next states |
|---|---|---|
| `queued` | Eligible; waiting for capacity or for deps to finish. | `running` |
| `running` | Worker spawned. pid + start time recorded. | `verifying`, `failed`, `stalled` |
| `verifying` | Worker passed acceptance; cross-family verifier spawned to independently confirm. Worker's result preserved at `result.worker.json`; verifier writes `result.json`. | `done`, `queued` (verifier rejected, retry budget left), `blocked` (verifier rejected twice), `stalled` |
| `stalled` | The pid is gone or the log has not been touched within the stall window. Applies to both worker and verifier rounds. | `queued` (retry budget left) or `blocked` |
| `blocked` | Out of retries, or verifier rejected twice (`blocked_reason: verifier-rejected-twice`). Needs triage. | `queued` (re-spec) or `failed` (abort), set by an explicit triage task. |
| `done` | Verifier independently returned PASS. Terminal. | — |
| `failed` | Triage aborted, or hard error in tick (e.g. verifier-spawn-error). Terminal. | — |

A task that stalls and retries within a single tick **does not respawn that
tick** — natural one-heartbeat cooldown. This prevents a chronically broken
task from chewing through its attempts in seconds.

## Daily flow

1. Author a task brief in `runs/experiment/tasks/<id>/brief.md`. Frontmatter optional (`model: sonnet` or `model: gpt-5.5` to override the default).
2. Append a task entry to `runs/experiment/state.yaml` (see section C of the plan for the schema). State `queued`. `worker_pref` defaults to `sonnet`; `verifier_pref` to `gpt-5.5` (the cross-family inverse).
3. Within five minutes the tick spawns the worker. The Mayor shows it move to `running`.
4. When the worker exits with `acceptance: pass`, the tick preserves the worker verdict as `result.worker.json` and spawns the cross-family verifier; the Mayor shows the task move to `verifying`. If the worker reports `acceptance: fail`, the task goes to `stalled` and retry/block logic applies.
5. When the verifier writes its own `result.json` with `acceptance: pass`, the next tick promotes the task to `done`. A verifier rejection sends the task back to `queued` (attempts++) or to `blocked` with `blocked_reason: verifier-rejected-twice`.
6. The tick commits the worker's `diff.patch` on `done`, with per-agent author attribution and a `Verified-By: <verifier model>` trailer. *(Commit wiring is not yet implemented — see the pass-29 build-log for the open follow-up.)*

## Control commands (in the Mayor's bottom pane)

| Command | What it does |
|---|---|
| `pause` | Touch `runs/experiment/PAUSE`. New spawns blocked; running workers continue. |
| `resume` | Remove `PAUSE`. Spawning resumes next tick. |
| `stop` | Touch `runs/experiment/STOP`. Tick refuses new spawns *and* the shell entry SIGTERMs any pids it reports as running. |
| `show <task>` | Print the task's row from state.yaml. |
| `brief <task>` | Open `tasks/<task>/brief.md` in $PAGER. |
| `tail <task>` | `tail -f` the worker log until Ctrl-C. |
| `requeue <task>` | Force a `blocked`/`failed` task back to `queued`, attempts reset. Use only after you've fixed the cause. |
| `verify <task>` | Enqueue an out-of-band verifier task that depends on the named one. |
| `ack-overspend <phase>` | Release the soft brake when a phase tripped the wall-clock guard and you've decided to let it continue. |
| `help` | List commands. |
| `quit` | Leave the control shell. The tmux session stays up. |

## How to stop it — pick the right severity

| Need | Use |
|---|---|
| Look at something safely | `pause` |
| Halt now, kill in-flight workers | `stop` |
| Walk away, no more ticks until I re-install | `./scripts/uninstall-experiment-cron.sh` |
| Crontab is wedged, can't reach the Mayor | `crontab -e`, delete the experiment lines (never `crontab -r`) |

## Budget guards

Three soft brakes / visibility lines, all configurable in `budget.yaml`:

| Guard | Trips when | Action |
|---|---|---|
| `per_task_subagent_cap` | Worker spawned > N nested subagents | Tick SIGTERMs it, marks `blocked` with reason `runaway-subagents`. Triage decides. |
| `phase_overspend_multiplier` | A phase's LLM-active time > N × expected | Mayor warning + heartbeat pauses spawning for that phase until you `ack-overspend <phase>`. |
| `daily_spawn_threshold` | Rolling 24h spawn count > N | Mayor warning. No automatic action. Visibility only. |

None of these cap money — the auth routes are subscription (Claude Max +
ChatGPT), so the cost knob is quota throttling, not billing. The guards
exist to catch *task-level* runaways, not budget compliance.

## Triage protocol

When a task lands in `blocked`:

1. The Mayor shows it in the blocked count. `show <task>` and `brief <task>` to read the context.
2. Decide one of three things:
   - **Re-spec** — the brief was wrong. Edit `brief.md`, then `requeue <task>` to send it back to queued. Attempts reset.
   - **Abort** — the task isn't recoverable. Manually edit state.yaml to set state to `failed` with a `blocked_reason` line.
   - **Escalate** — a higher-tier reasoning task should look at it. Enqueue a triage task (`tier: T1`, `worker_pref: opus`) that depends on the blocked one and outputs a decision.
3. Triage decisions land in `runs/experiment/triage/<timestamp>.md` for the dashboard's "last triage decision" panel.

## Recovery from a crash

- If the tick script itself crashes mid-run, state.yaml is saved at the end of every successful step; partial-state corruption is unlikely. Worst case: a task is marked `running` but no pid is alive. Next tick detects it as stalled and recovers via the normal retry path.
- If `state.yaml` is irrecoverable, restore from git. The repository commit history captures every state change because the tick commits on each task completion.

## Health checks

```bash
# Has the cron actually fired recently?
tail -3 runs/experiment/cron.log

# What did the tick last do?
tail -10 runs/experiment/events.log

# What's the queue look like?
python3 -c "from pathlib import Path; from src.experiment.state import load; print('\n'.join(f'{t.state.value:>8}  {t.id}' for t in load(Path('runs/experiment/state.yaml'))))"

# Is the Mayor running?
tmux has-session -t mayor 2>/dev/null && echo "mayor is up" || echo "mayor not running"
```

## What the scaffold deliberately does NOT do

- It does **not** auto-create triage tasks. The tick marks `blocked` and stops there. Triage is an explicit, operator-or-Opus decision.
- It does **not** push to any remote. Commits land on `pass-29` only.
- It does **not** modify `main` or `dev`.
- It does **not** require any API key. Auth is subscription-only on both Claude and Codex.
- It does **not** persist across machine reboots automatically. Cron does (it lives in your user crontab). The Mayor doesn't (tmux doesn't survive reboot); re-launch with `./scripts/experiment-mayor.sh`.

## Relationship to the rest of the repo

- The leak guard (`scripts/check-secrets.sh`) and publish guard (`scripts/git-hooks/pre-commit`) run on every commit the tick makes, exactly like every commit you make by hand. A worker that tries to introduce a leaky pattern is blocked the same way you would be.
- The smoke-test (`scripts/smoke-test.sh`) is the binding gate for all scaffold code. Phase 0 tests live under `tests/unit/experiment/` and join the existing 420 unit tests.
- The experiment is part of, not separate from, the project repo. There is no separate fork or sandbox. Each task commits into `pass-29` with author attribution naming the model that did the work.

## When you're done with the experiment

After Phase 9 (verdict) and Phase 10 (learnings) commit:

```bash
./scripts/uninstall-experiment-cron.sh    # stop the heartbeat
tmux kill-session -t mayor                # close the dashboard
```

The `runs/experiment/` directory stays as the audit trail. Build-log
entries under `runs/build-log/pass-*.md` are the human-readable record
of what each phase did and what was learned.
