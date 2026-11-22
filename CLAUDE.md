# CLAUDE.md

## What this repo is

An agentic RAG system that applies Kimball dimensional modelling to a property graph. Data source: OpenML (18M+ ML experiment runs). Agent: Claude via OAuth with three tools: Cypher graph query, semantic search, aggregate measures. Stack: LadybugDB (embedded graph + native HNSW vector index), sentence-transformers (local embeddings), Streamlit (UI). The graph store and embeddings are local; inference goes through the Claude Code session, with no API billing. Single store: ChromaDB removed in pass 15.

Full architecture: `docs/architecture.md`. Full spec: `docs/spec.md`.

## Stack

- **Python 3.11+** (3.14 confirmed on this machine)
- **LadybugDB 0.16.0+**: embedded property graph, Cypher interface, native HNSW vector index via VECTOR extension. Single store for graph + semantic, with no ChromaDB dependency.
- **sentence-transformers**: `BAAI/bge-small-en-v1.5` (384-dim, ~130MB), local CPU inference, no network at query time
- **openml**: Python client for OpenML API (read-only, no auth required)
- **claude-agent-sdk**: Claude via the Claude Agent SDK (claude-agent-sdk on PyPI, v0.1.81+). Auth is handled automatically via the Claude Code OAuth session: no env vars, no API key, costs charged to Max quota.
- **Streamlit**: local demo UI only

## Secrets and auth

- Claude auth route: Claude Agent SDK OAuth (Max subscription quota, no API billing). Auth is automatic via the Claude Code CLI session: no env vars, no API key, no op-fetch required.
- No `.env` files. No hardcoded keys anywhere. The previous `run-secure-query.sh` thin alias and `op-refs.sh` 1Password pointer file were removed in pass-29 as vestigial; the orchestrator is invoked directly via `python -m src.agent.orchestrator`.
- **Five hooks** under `scripts/git-hooks/`, armed by `scripts/install-guards.sh`. `pre-commit` enforces, in order:
  1. The protected-branch guard: refuses a direct commit on `publish` (or any branch named in `releasetrain.protectedbranch` / `publishguard.publishbranch`); an in-progress cherry-pick is exempt.
  2. The strict scanner: `scripts/publish-guard-strict-scan.sh`, scans the entire working tree (tracked + untracked, gitignore-aware) against the strict pattern list (`.publish-guard-strict.local`). No path exemptions. Block on hit.
  3. The publish guard: never-commit files (`.env`, `*.local`, etc.), symlinks with absolute targets, and a path-aware personal-pattern scan reading the gitignored `.publish-guard.local`. Exempts `runs/`, `archive/`, `HANDOFF.md`, `RUNBOOK.md`, `CLEANUP-PROMPT.md` (private-tier, stripped from the public mirror), `LICENSE` (carries the author's name by design) and the two `*.example` pattern files.
  4. The leak guard: `scripts/check-secrets.sh`, chained at the end (auth/op-fetch leak patterns).
- `pre-merge-commit` applies the protected-branch guard to merge commits, which skip `pre-commit`.
- `pre-push` runs the strict scanner on every push and fails closed on the public remote: only `publish` may go there, as the PR source, and a push to public `main` is refused (`publishguard.boundary` defaults to `pr`), so public `main` advances only by merging that PR on the forge.
- `reference-transaction` refuses any move of a protected branch (the union of `releasetrain.protectedbranch` and `publishguard.publishbranch`, defaulting to `publish`) to a commit that no other branch and no remote already holds, so cherry-pick, revert, rebase, `am` and `--no-verify` commits cannot bypass `pre-commit`.
- `commit-msg` appends a vendor `Co-Authored-By` trailer to agent-authored commits (cosmetic, not a guard).
- `scripts/check-secrets.sh` and `scripts/publish-guard-strict-scan.sh` both run as steps of `scripts/smoke-test.sh`, so leak/strict protection is enforced even without the git hooks (which are not cloned).

### Forbidden phrases (block-on-sight)

Never write personal positioning copy anywhere in this repo: comments, docstrings, prompts, prose, anywhere. This applies to Claude and Codex equally.

The authoritative list is in `.publish-guard-strict.local.example` (tracked in the private working repo; kept off the public mirror). It covers categories like personal-status taxonomy, application-process language, and rate-card terms. The strict scanner blocks commits and pushes if any pattern appears in a publishable path. If you need to refer to the *category* in prose, say "personal positioning copy" or "personal-pattern scan".

Private-tier paths (`runs/`, `archive/`, `HANDOFF.md`, `RUNBOOK.md`) are exempt because they never reach the public mirror: the publish replay strips them from every commit.
- **Publish workflow** is live: see `docs/PUBLISH-WORKFLOW.md`. The public mirror is a separate remote named only in local `publishguard.*` git config (no org or repo name is written into the tracked tree). `publish` is an orphan line advanced by atomic cherry-pick of `dev`'s public commits; public `main` moves only by merging a reviewed PR from `publish` on the forge.

## Kimball schema

```
[AlgorithmFamily]
        │ BELONGS_TO_FAMILY (snowflake outrigger)
        │
[Algorithm]──USED_ALGORITHM──[Run (fact)]──ON_DATASET──[Dataset]
                                   │   │
                              FOR_TASK   RUN_ON_DATE
                                   │             │
                               [Task]         [Date]
```

Run = fact node (measures: accuracy, f1, auc, runtime_sec)
Algorithm/Dataset/Task/Date = dimension nodes (semantic embeddings on `.description` for the first three; Date keyed YYYYMMDD INT64)
AlgorithmFamily = snowflaked outrigger sub-dimension off Algorithm (9 families, own description embedding + HNSW index). Added pass 28; see `runs/build-log/pass-28-snowflake-and-green-gate.md`.

## Agent tools

Three tools only, registered via `@tool` decorator in the Claude Agent SDK. Do not add tools without updating `docs/spec.md`:

- `graph_query(cypher, explain)`: read-only Cypher against LadybugDB, rejects writes
- `semantic_search(query, entity_type, top_k)`: LadybugDB native HNSW vector index + BAAI/bge-small-en-v1.5
- `aggregate_measures(group_by, measure, filter_cypher)`: LadybugDB aggregation query

## Agent passes

Build history (passes 01-21) staged in `archive/agent-prompts/` and `runs/build-log/` (private working repo only; not on the public mirror). Originals kept as a provenance trail; not live instructions.

| Pass | File | Builds |
|---|---|---|
| 01 | agent-01-scaffold.md | Repo scaffold, requirements, .gitignore |
| 02 | agent-02-schema.md | Kùzu schema + migration |
| 03 | agent-03-ingestion.md | OpenML fetch + transform + load |
| 04 | agent-04-eval-harness.md | Eval harness + LLM judge (built early as the signal) |
| 05 | agent-05-semantic.md | ChromaDB + embedding pipeline |
| 06 | agent-06-tools.md | Three agent tools + tests |
| 07 | agent-07-orchestrator.md | Claude orchestrator + auth |
| 08 | agent-08-ui.md | Streamlit UI |
| 09 | agent-09-eval-integration.md | Eval suite integrated with the live agent |
| 10 | agent-10-readme.md | Public README + Mermaid diagram |

Check `git log --oneline` to see which passes have completed before resuming.

## Key rules

- `data/ladybug_db` (the LadybugDB store file; renamed from the Kùzu-era `kuzu_db` in pass-30) is gitignored (large, recreatable)
- No `op://` references anywhere in the tree (the old `op-refs.sh` was removed in pass-29)
- Cypher write operations (CREATE/MERGE/DELETE/SET) must be blocked in `graph_tool.py`
- All agent tool functions must have unit tests in `tests/unit/`
- `runs/` directory (eval outputs) is tracked
