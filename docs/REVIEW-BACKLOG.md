# Code review backlog — 2026-07-20

A code-integrity and understandability review of the repo. Findings are grouped
by priority and framed as bounded passes, each ending with a green suite. Run
when tokens allow. Line references were accurate at review time; re-confirm
before editing.

**Status at 2026-11-15:** 1.1 is resolved (the config test asserts
`claude-sonnet-5`, `pytest-mock` is in `requirements-dev.txt`, and the unit
suite is 533 passed). Item 1 of section 4 is resolved for the public docs: the
README badge and body both say 533. Everything else below is still open.

## Suggested order (highest value first)

1. **Red-to-green pass** — restore the test gate's meaning. Small.
2. **Guard hardening pass** — make the read-only Cypher claim actually true.
3. **Tool-schema unification** — restore the agent's full tool capability.
4. **AlgorithmFamily search + fixture refresh + un-skip E2E** — phantom → feature.
5. **Dead-code and drift sweep.**
6. **Async loader ordering fix** — before any re-ingest.
7. *(Stretch)* UI decomposition; parameterised loader writes.

---

## 1. Integrity — fix first

### 1.1 Test suite is red on `dev`; dev env not reproducible
- `tests/unit/test_config.py:48` asserts `claude-sonnet-4-6`; commit `7547c64`
  bumped the default to `claude-sonnet-5` without updating the test.
- `requirements-dev.txt` is missing `pytest-mock` — on a fresh venv, 27 tests
  error with `fixture 'mocker' not found`. The green gate has been passing on
  ambient environment state. With the dep installed: 532 passed, 1 failed.
- Also reconcile the three contradictory test-count claims (see 4.1).

### 1.2 Read-only Cypher guarantee is bypassable
Single guard is the keyword regex at `src/graph/db.py:24-27`
(`CREATE|MERGE|DELETE|SET|DROP|REMOVE`). Gaps:
- `COPY` not blocked — `COPY (MATCH ...) TO '/path'` writes arbitrary files;
  `COPY Table FROM '...'` bulk-writes. `execute_write`'s own docstring names COPY.
- `CALL` procedures not blocked, and `\b` defeats the ones that matter: `_` is a
  word char, so `\bDROP\b` does not match `CALL DROP_VECTOR_INDEX(...)` — a
  procedure `vector_store.py:135` uses to mutate.
- `ATTACH`, `INSTALL`, `LOAD`, `ALTER`, `IMPORT/EXPORT DATABASE` uncovered.
- `aggregate_measures.filter_cypher` is raw agent Cypher interpolated verbatim
  (`aggregate_tool.py:102-103`) while the module docstring claims injection is
  prevented — that claim is false.

**Fix:** flip denylist → allowlist. Accept only queries whose first keyword is
`MATCH`/`OPTIONAL`/`RETURN`/`WITH`/`UNWIND`; reject `CALL`/`COPY` outright.
Validate `filter_cypher` too. Add bypass-vector tests (`COPY`,
`CALL DROP_VECTOR_INDEX`, underscore evasion, `REMOVE`) — none exist today.

### 1.3 Async loader can silently lose edges
`loader_async.py`: a flow/task ID is reserved into the dedup set (lines 232-233)
*before* its node CREATE is enqueued (line 292), with multiple `await`s between.
A concurrent coroutine sees the ID present, skips node creation, and enqueues a
`USED_ALGORITHM` edge that can drain through the single writer queue before the
node exists; the MATCH finds nothing and the writer only warns (lines 148-152).
Related:
- Failed node writes are never retried within a run (reservation not rolled back,
  unlike the dataset-failure path at 194-196).
- Edges on already-existing nodes are never repaired (family-edge creation only
  runs in the new-node branch: `loader.py:272`, `loader_async.py:297-308`).

**Fix:** enqueue node CREATE before releasing the reservation, or track in-flight
nodes and defer dependent edges. Do before any re-ingest.

### 1.4 Write path interpolates strings; safe parameterised path unused
Both loaders build CREATEs escaping only `\` and `'` (`loader_async.py:70-71`),
with free-form OpenML descriptions flowing through. `queries.py:110-127` already
defines `$description` bind-param queries documented as the injection-safe path,
used by neither loader. Latent: `_cypher_value` renders `float('inf')` as invalid
Cypher (`loader.py:68`; NaN filtered, inf not).

### 1.5 Two divergent copies of every tool schema; agent sees the stale one
Live path = `@tool` decorators in `orchestrator.py:81-140`. The `*_tool_schema()`
functions in `src/retrieval/` are referenced only by tests, and have drifted:
- `aggregate_measures` `@tool` description advertises 4 group-by axes; the impl
  supports 14 (all `date.*`, `algorithm.paradigm`, etc.). **The model cannot
  discover most of the tool's capability** — a direct cap on answer quality.
- `@tool` copies drop enum constraints and `top_k` bounds.

**Fix:** one source of truth feeding both `@tool` registration and tests. Likely
the single highest-leverage quality fix in the repo.

---

## 2. The phantom feature

`AlgorithmFamily` semantic search is documented in `architecture.md:111`,
`spec.md:69`, `README.md:17` and the UI (`app.py:690`), but `semantic_tool.py:12`
rejects anything outside `{Algorithm, Dataset, Task}`. `vector_store.py` supports
it underneath — the embeddings exist with no retrieval path. Either wire it
through (add to frozenset + tool enum + prompt) or correct the docs. Wiring it is
the better story.

---

## 3. Understandability & structure

- **`src/experiment/` is a foreign subsystem** — a cron-driven orchestration
  harness, architecturally disjoint from the RAG runtime (no imports either way):
  ~8 src modules, 8 scripts, 3 docs, 6 test files. No front-door doc mentions it.
  Marked "executed (pass-29)", dormant. Move to `archive/` or its own repo, or add
  a one-paragraph pointer in `architecture.md`.
- **~640 lines of dead sync ingestion.** `loader.py` and the fetch functions in
  `openml_fetch.py` are unreachable except as home of the `FetchedDataset`/
  `FetchedRun` dataclasses. The sync/async pair also populate different Run fields
  (sync never sets `runtime_sec`). Delete sync path; extract dataclasses to
  `models.py`.
- **`app.py` (1105 lines) triplicates business rules.** Bucket thresholds
  (`app.py:449-476`) duplicate `transform.py:62-88`; citation regex (`app.py:45`)
  duplicates `orchestrator.py:144`; a third copy of the graph shape; 23 inline
  `db.execute` calls bypass the near-empty `queries.py`. Extract shared constants
  and a data-access layer.
- **Small confusions:** `prompts.py:33` says "maximum of 5 tool calls" while config
  enforces 15; `orchestrator.py:204-210` logs tool *input* under key
  `result_preview`; `vector_store.py:37` claims index names are entity-specific
  while three entities share one name; `db.close()` only nulls references (and
  `loader_async` depends on that); the migration framework is a full discovery
  engine for one migration (so `test_migrations.py` writes real files into the
  live package to test it).

---

## 4. Doc drift (matters — repo is publish-bound)

1. Three contradictory test counts: README badge 517, README body 533,
   RUNBOOK 420.
2. `run-secure-query.sh` deleted in pass-29 but still the advertised entry point
   in `app.py:11` and `RUNBOOK.md:26` — following the runbook fails.
3. RUNBOOK says lead model is Opus; config says `claude-sonnet-5`.
4. Leftover ChromaDB references in `scripts/semantic.sh:2` and the
   `test_end_to_end.py` docstring.
5. E2E eval tests are hard-skipped — `skipif(condition=True)` at
   `test_end_to_end.py:24,61` — so the `RUN_E2E_EVAL` mechanism the docstring
   advertises is dead.
6. Eval fixtures hardcode pass-16 corpus counts and a pass-24 story (0.90 recall,
   two failures) while README reports pass-28 (0.95, one failure).
7. Minor: `.mcp.json` untracked and unignored — pick one (benign; tracking is fine).
