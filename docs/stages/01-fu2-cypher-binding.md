# Stage card 01-fu2-cypher-binding: FU2 segfault root-cause + parameterised Cypher binding

## Metadata

- **Authored:** 2026-08-13
- **Orchestrator:** Claude Fable 5 <claude-fable-5@local>
- **Worker:** Claude Opus 5 <claude-opus-5@local>
- **Verifier:** Codex GPT-5.6 Terra <codex-gpt-5-6-terra@local>
- **Verifier panel:** false
- **Pairing rationale:** Hypothesis-driven debugging of an intermittent rc=139
  segfault wants a frontier Claude tier; the binding refactor and its tests are
  then cross-family verified by a Codex tier that re-runs the suite outside the
  worker sandbox.

## Context (read first)

- `runs/build-log/pass-26-recovery-note.md` — the mitigation history.
- `docs/REVIEW-BACKLOG.md` — "Cypher guard hardening" pass overlaps this work.
- Trunk is `dev`. **Never touch `publish` (public orphan mirror) or push.**

## Objective

Close the FU2 ingest segfault issue properly:

1. **Attempt local reproduction** of the rc=139 segfault: small batches,
   verbose logging. Suspects, in order: long non-ASCII Cypher string literals
   in the description `SET`; Python 3.14 / httpx concurrency; LadybugDB WAL
   phantom from a non-clean shutdown. If it will not reproduce, say so with
   the evidence gathered — do not fake a root cause.
2. **Parameterise the binding** regardless of repro outcome: replace the
   string-interpolated description `SET` with parameterised binding in
   `src/ingestion/openml_fetch_async.py` and `src/graph/queries.py`. Add unit
   tests covering non-ASCII and injection-like description strings.
3. **Rewrite the backfill** — `scripts/backfill-openml-descriptions.sh`:
   50-entity batches, explicit commits, staged recovery points.
4. Write a short root-cause (or best-evidence) note at
   `runs/build-log/pass-30-fu2-note.md`.

## Out of scope

- The grounding-score work (judge context, prompt tightening) — separate card.
- Re-running full ingest against OpenML over the network.
- Anything on the `publish` branch; any push.

## Acceptance command

`python3 -m pytest tests/unit/ -q` — all tests green including the new
binding tests (baseline 420 unit tests, 0 fail).

## Verifier brief

Re-run the acceptance command outside the worker sandbox. Confirm the `SET`
interpolation is gone from both named files (grep for f-string/`.format`/`%`
interpolation into Cypher around description writes), that the new tests
genuinely exercise non-ASCII + injection-like strings, and that the backfill
script batches at 50 with explicit commits. Read the pass-30 note and check
its claims are supported by the diff and any repro logs it cites.
