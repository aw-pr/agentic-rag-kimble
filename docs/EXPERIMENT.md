# EXPERIMENT — hypothesis spec for agentic-rag-kimble

Date drafted: 2026-05-20.
Authority: this document, not the plan. `docs/EXPERIMENT-PLAN.md`
captures *how* the experiment is run; this document captures *what is
being tested and what would falsify it*. If the plan and the spec
disagree on hypothesis, metric, or falsification, the spec wins —
amend it explicitly rather than drift.

## 1. Hypothesis

**Dimensional modelling of the corpus (Kimball facts/dimensions
realised as a property graph with conformed snowflake outriggers and
vector-indexed semantic dimensions) materially improves retrieval
recall over a flat-RAG baseline that treats each entity as an
independent embedded chunk, on questions that require relationship
traversal or grouped aggregation.**

Two corollaries the experiment also pins:

- **C1.** The improvement is not an artefact of the OpenML corpus.
  It holds (with reduced effect size, possibly) on a second corpus
  (arXiv ML metadata) built under the same dimensional discipline.
- **C2.** The improvement is not purely from semantic search quality.
  Ablating the `aggregate_measures` and `graph_query` tools while
  retaining `semantic_search` degrades recall measurably.

Out of scope for this experiment:

- Response quality / hallucination rate. The judge harness exists
  (`src/eval/judge.py`) and runs as a secondary signal, but judge
  grounding is reported, not bet on. The hypothesis is about
  *retrieval*, not *generation*.
- Latency, cost, or operability. The dimensional path is more
  complex to build than flat-RAG. If the recall lift is small, the
  complexity is not justified. That trade-off is what the
  falsification criterion encodes.

## 2. Primary outcome metric

`recall@10` on the held-out fixture set, computed per fixture and
aggregated by arithmetic mean across fixtures.

For each fixture (a natural-language query plus a set of expected
entity IDs):

```
recall@10(fixture) = |{expected ∩ top10_retrieved}| / |expected|
```

Aggregate:

```
recall@10(system, holdout) = mean_over_fixtures(recall@10(fixture))
```

Two systems are compared on the same held-out set:

- **System D (dimensional)** — the production agent with all three
  tools (`graph_query`, `semantic_search`, `aggregate_measures`)
  against the Kimball-modelled LadybugDB store.
- **System F (flat-RAG)** — a baseline implemented in Phase 4. Each
  graph entity (Run, Algorithm, Dataset, Task) is serialised to a
  single text chunk, embedded with the same BGE-small model, indexed
  in a single vector store. Retrieval is `semantic_search` only,
  scaled to top-k=50 to give the baseline a generous budget; the
  top-10 cut for `recall@10` is taken from the model's reranked /
  selected subset.

Both systems retrieve from the **same underlying data** (same OpenML
snapshot, same descriptions). The difference is structure, not
content.

## 3. Statistical method

Bootstrap confidence interval, BCa (bias-corrected and accelerated),
10 000 resamples, α = 0.05 — so a two-sided 95% CI.

- **Resampling unit:** the fixture. Resample the held-out fixture set
  with replacement, recompute mean recall@10 per resample, derive the
  BCa CI from the resample distribution.
- **Paired comparison:** System D and System F are evaluated on the
  *same* fixtures, so the difference statistic
  `Δ = recall@10(D) - recall@10(F)` is paired per fixture. The CI is
  computed on the paired Δ distribution, not on two independent means.
- **Reference implementation:** scipy `stats.bootstrap` with
  `method="BCa"`, `n_resamples=10000`, `confidence_level=0.95`. The
  Phase 3 task builds the harness and pins a regression test that
  asserts agreement with scipy to 3 decimal places on a small fixture.
- **No multiple-comparison correction.** Primary outcome is a single
  comparison (D vs F on held-out). Corollary tests in §6 are reported
  with their own CIs and explicitly framed as secondary; no family-
  wise α adjustment is claimed.

## 4. Falsification criterion

The hypothesis is falsified if **any** of the following are true on
the held-out set:

1. **Effect too small.** The point estimate `Δ = recall@10(D) -
   recall@10(F)` is less than `Δ_min = 0.05` (5 percentage points).
2. **CI overlaps zero.** The 95% BCa CI on Δ includes 0.
3. **Effect not robust across query types.** The fixture set
   stratifies into three query classes (graph-traversal, semantic-
   only, aggregation). If on any one class the per-class Δ point
   estimate is *negative* (System D worse than System F), the
   hypothesis as stated above is falsified — even if the aggregate is
   positive. A negative class would mean dimensional structure is
   actively *harming* one query type, which the hypothesis does not
   accommodate.

`Δ_min = 0.05` is the pinned threshold for §4.1. It is the smallest
improvement that justifies the structural complexity of dimensional
modelling for a portfolio / certification demonstration of this kind.
A smaller lift is theoretically interesting but does not support
"materially improves" in any practical sense.

These criteria are bound to the experiment as currently scoped (one
corpus, one held-out partition). Negative results on a different
corpus do not retroactively falsify the hypothesis here; they update
the generalisation claim in C1.

## 5. Secondary outcomes (reported, not bet on)

- **`recall@5` on held-out** — same methodology, narrower retrieval
  window. Reports whether the lift narrows or grows under tighter
  budgets.
- **Per-tool-class recall@10 on held-out**, three buckets:
  - graph: questions that require relationship traversal
  - semantic: questions that depend on vague-name lookup
  - aggregate: questions that require grouped measures
- **Judge grounding** (from `src/eval/judge.py`, post-FU1 rubric):
  per-fixture grounding score 1–4 with the post-FU1 strict-grounding
  prompt. Pre-FU1 was 2.40; pass-28 lifted to 3.00; the 3.5+ target
  is still open. Reported as context; no falsification claim.
- **Tool-call count per query** — average tool invocations per
  fixture. Reported for cost framing. Higher is not necessarily worse.

## 6. Corollary tests

### C1 — generalisation gesture (Phase 6)

Repeat the primary comparison on the arXiv ML metadata corpus, with a
Kimball schema built per the §J.3 sketch: `Paper` (fact-ish),
`Author`, `Venue`, `Date`, `Topic/Category` (dim). Same retrieval
methodology, same bootstrap CI. C1 is supported if Δ on arXiv is
positive with CI excluding 0. Effect size is allowed to be smaller
than on OpenML — arXiv is a softer test because the questions are
less naturally aggregation-shaped.

Honest caveat for the write-up: arXiv is adjacent to OpenML in
domain (both ML literature / metadata). The C1 result is a *gesture*
toward generalisation, not a domain-wide claim.

### C2 — tool ablation (Phase 5)

Run System D in three ablated configurations on held-out:
- D−agg: `aggregate_measures` disabled
- D−graph: `graph_query` disabled
- D−sem: `semantic_search` disabled

For each, compute recall@10 and the CI vs D. C2 is supported if
disabling EITHER `graph_query` OR `aggregate_measures` causes a
measurable drop (Δ negative, CI excluding 0). If disabling either
tool has no effect, then either the dimensional advantage is
entirely from semantic quality (not structure) or the fixture mix
does not actually exercise those tools.

## 7. Fixture set and held-out split

- Current fixture set: 24 (pass 28). Expanded in Phase 2 to a target
  of 60–100 fixtures across the three query classes (graph, semantic,
  aggregate) in roughly equal thirds.
- Held-out split: 30% of fixtures, stratified by query class, randomly
  selected with a fixed seed committed alongside the split. The held-
  out partition lives under `tests/fixtures/holdout/` and is
  gitignored from any retrieval path (the agent must not be able to
  discover it during dev). Only the Phase 7 runner reads it.
- Dev split: 70% of fixtures, used for prompt-engineering, schema
  iteration, and tool-tuning during Phases 2–5.
- No fixture leakage: the leak guard (`scripts/check-secrets.sh` will
  be extended in Phase 2) scans commits for held-out fixture IDs
  appearing in non-Phase-7 paths.

## 8. Pre-registered analyses

To avoid garden-of-forking-paths drift, the following analyses are
declared *before* Phase 4 (baseline) runs:

- Primary: paired BCa bootstrap on Δ = recall@10(D) − recall@10(F)
  on held-out.
- Per-class breakdown of the same statistic across the three query
  buckets.
- C1 on arXiv: same methodology.
- C2 ablations: per-ablation paired Δ.
- Secondary: judge-grounding mean ± SD on held-out, both systems.

Any analysis added post-hoc is reported in Phase 8 explicitly as
*exploratory*, with no CI claim attached.

## 9. What this experiment does NOT prove

- That dimensional modelling beats flat-RAG in production on a
  *different* domain or corpus shape (e-commerce, log analytics,
  legal documents, etc.). Out of scope.
- That dimensional modelling is *cost-justified* for any particular
  use case. The recall lift is the technical claim; the cost-benefit
  is a business decision the data does not settle.
- That the specific tool design (three tools, schemas as in
  `src/retrieval/`) is optimal. The hypothesis is about structure
  versus no-structure, not about this specific structure being best.
- That the LLM is reasoning well over the retrieved context. That is
  the judge's territory, and judge grounding is secondary.

## 10. Provenance and reproducibility

- All commits land on `pass-29` with per-task attribution
  (`Author: <worker model>`, `Co-Authored-By: <verifier>`,
  `Verified-By: <verifier>`). The cross-family verifier is Codex
  GPT-5.5 for Claude-Sonnet workers and vice versa, per §D of the
  plan.
- Phase boundaries are annotated tags
  (`experiment/phase-N-complete`). Anyone reading `git log` can
  reproduce the experiment's chronology without reading this spec.
- Fixture splits use a committed seed; bootstrap resamples use a
  committed seed. Reproducible from the commit hash that contains
  the final `runs/eval/` outputs.
- The baseline (System F) is implemented twice — once by Sonnet and
  once independently by Codex GPT-5.5 — and the two must agree
  numerically to 3 decimal places on a small reference fixture
  before Phase 4 is allowed to claim its result. Implementation
  disagreement is itself a finding.

## 11. Open decisions still to pin (before Phase 4 runs)

- **Δ_min sensitivity.** §4.1 sets Δ_min = 0.05. Should this be
  smaller (0.02 — any non-trivial lift counts) or larger (0.10 —
  insists on a clear win)? Current 0.05 is the proposal; revisit
  after seeing pilot numbers on the dev split if needed, but lock
  before held-out runs.
- **Embedding parity.** System F uses BGE-small to match System D.
  Should F also be tested with a stronger embedding (BGE-large or
  text-embedding-3-small) to control for "is it the structure or the
  vectors"? If yes, F-strong becomes a third arm in §2. Recommend
  yes; pin in Phase 4 brief.
- **Reranker.** No reranker is currently planned for either system.
  Adding one to either would conflate effects. Confirmed: no
  reranker in either arm of the primary comparison.
