# Stage card 01-fu2-cypher-binding: FU2 segfault root-cause + parameterised Cypher binding

## Metadata

- **Authored:** 2026-08-13
- **Orchestrator:** Claude Fable 5 <claude-fable-5@local>
- **Worker:** Claude Opus 5 <claude-opus-5@local>
- **Verifier:** GPT-5.6 Sol <gpt-5-6-sol@local>
- **Worker effort:** low
- **Verifier effort:** medium
- **Verifier panel:** false
- **Pairing rationale:** Hypothesis-driven debugging of an intermittent rc=139
  segfault wants a frontier Claude tier; the binding refactor and its tests
  are then cross-family verified by a Codex tier that re-runs the suite
  outside the worker sandbox.

## Re-brief 2026-08-14: this is a confirmation round, not a rebuild

Round 1 ran on 2026-08-13 and every deliverable is already in the tree. It
reported `partial` for one reason only: acceptance criterion 1 was failing on
`tests/unit/test_config.py::test_claude_model_default`, an unrelated
pre-existing model-string drift that the worker correctly declined to fix
inside this stage's commit. That drift was fixed separately in `56291cc`, and
the suite is now 533 passed / 0 failed.

The stage was nonetheless hand-marked `completed` in `state.yaml` on a
`partial` envelope and a verifier that left no artefact, so this work has
never actually been verified. That is what this round is for.

What already landed, and must not be redone:

- Deliverables 1-3 predate the card, in ancestor commit `319c7ba` (2026-06-07):
  `queries.py` uses `$description` bind params, `openml_fetch_async.py` builds
  no Cypher, and `tests/unit/test_queries_binding.py` has 16 tests covering
  non-ASCII, quotes, backslashes, control chars, ~3KB unicode and two
  injection payloads.
- Deliverables 4-5 landed in round 1 as `b642006`: the checkpointed backfill
  script and `runs/build-log/pass-30-fu2-note.md`.

Your job is to confirm all five against the criteria below and report, not to
rewrite working code. If every criterion holds, emit `pass` — the criteria are
now genuinely satisfiable, which they were not in round 1. Only make changes
if a criterion is actually unmet, and say which.

## Objective

Close the FU2 ingest segfault issue properly: attempt a local reproduction of
the rc=139 crash, and regardless of repro outcome, replace the
string-interpolated Cypher description `SET` with parameterised binding, add
tests for the dangerous string classes, and make the backfill script safely
resumable. If the segfault will not reproduce, report the evidence gathered
honestly — do not fake a root cause.

Repro suspects, in priority order: long non-ASCII Cypher string literals in
the description `SET`; Python 3.14 / httpx concurrency; LadybugDB WAL phantom
from a non-clean shutdown.

## Inputs (read these in your own context)

- runs/build-log/pass-26-recovery-note.md (mitigation history)
- src/ingestion/openml_fetch_async.py
- src/graph/queries.py
- scripts/backfill-openml-descriptions.sh
- docs/REVIEW-BACKLOG.md (the "Cypher guard hardening" pass overlaps this work)

Do not read anything else unless you need to; keep your context lean.

## Deliverables

1. src/ingestion/openml_fetch_async.py — description `SET` uses parameterised
   binding, no string interpolation of description content into Cypher.
2. src/graph/queries.py — same.
3. tests/unit/ — new unit tests covering non-ASCII and injection-like
   description strings through the binding path.
4. scripts/backfill-openml-descriptions.sh — rewritten: 50-entity batches,
   explicit commits, staged recovery points (safe to re-run after a crash).
5. runs/build-log/pass-30-fu2-note.md — root-cause note, or best-evidence
   note if the crash did not reproduce, citing any repro logs.

## Constraints

- Work on `dev`. **Never touch `publish` (public orphan mirror). Never push.**
- No full ingest over the network; small local batches only for repro
  attempts.
- Do not regress existing behaviour: descriptions written via the new binding
  must be byte-identical to the old path for plain-ASCII input.

## Acceptance criteria

The verifier will check each of these. Failure of any one is a failure of the stage.

1. `python3 -m pytest tests/unit/ -q` green, including the new binding tests
   (533 unit tests, 0 fail — confirmed green on 2026-08-14).
2. No f-string/format/percent interpolation of description content into
   Cypher remains in the two named source files.
3. New tests genuinely exercise non-ASCII and injection-like strings.
4. Backfill script batches at 50 with explicit commits and staged recovery.
5. pass-30 note exists and its claims are supported by the diff and any logs
   it cites.

## Contract test

- **Test file:** None
- **Assertions digest:** None

## Out of scope

- The grounding-score work (judge context, prompt tightening) — separate card.
- Full ingest re-runs or recall measurement.
- Anything on `publish`; any push.

## Budget

- **Worker wall-clock:** 90 minutes
- **Verifier wall-clock:** 30 minutes

## Verifier handoff

Worker reports: repro outcome (reproduced with root cause / not reproduced
with evidence), files changed, new test names, and the pass-30 note path.

## Family-specific notes

None
