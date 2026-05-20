# CLAUDE.md

## What this repo is

An agentic RAG system that applies Kimball dimensional modelling to a property graph. Data source: OpenML (18M+ ML experiment runs). Agent: Claude via OAuth with three tools — Cypher graph query, semantic search, aggregate measures. Stack: LadybugDB (embedded graph + native HNSW vector index), sentence-transformers (local embeddings), Streamlit (UI). Fully local, no cloud services. Single store — ChromaDB removed in pass 15.

Full architecture: `docs/architecture.md`. Full spec: `docs/spec.md`.

## Stack

- **Python 3.11+** (3.14 confirmed on this machine)
- **LadybugDB 0.16.1+** — embedded property graph, Cypher interface, native HNSW vector index via VECTOR extension. Single store for graph + semantic — no ChromaDB dependency.
- **sentence-transformers** — `BAAI/bge-small-en-v1.5` (384-dim, ~130MB), local CPU inference, no network at query time
- **openml** — Python client for OpenML API (read-only, no auth required)
- **claude-agent-sdk** — Claude via the Claude Agent SDK (claude-agent-sdk on PyPI, v0.1.81+). Auth is handled automatically via the Claude Code OAuth session — no env vars, no API key, costs charged to Max quota.
- **Streamlit** — local demo UI only

## Secrets and auth

- Claude auth route: Claude Agent SDK OAuth (Max subscription quota, no API billing). Auth is automatic via the Claude Code CLI session — no env vars, no API key, no op-fetch required.
- `run-secure-query.sh` — vestigial thin alias; now just calls `python3 -m src.agent.orchestrator "$@"`. The old op-fetch injection is no longer used.
- `op-refs.sh` — retained for historical reference; no load-bearing role in current auth path.
- No `.env` files. No hardcoded keys anywhere.
- **Two guards, one hook**: `scripts/git-hooks/pre-commit` (armed by `scripts/install-guards.sh`) enforces, in order:
  1. The publish guard — never-commit files (`.env`, `*.local`, etc.) plus a personal/CV pattern scan reading the gitignored `.publish-guard.local`.
  2. The leak guard — `scripts/check-secrets.sh`, chained at the end (auth/op-fetch leak patterns).
- `scripts/check-secrets.sh` also runs as the first step of `scripts/smoke-test.sh`, so the leak side is enforced even without the git hook (which is not cloned).
- **Publish workflow** is set up but inert: see `docs/PUBLISH-WORKFLOW.md`. Five `publishguard.*` git-config keys are set (intended public org configured locally; no org or repo name is written into the tracked tree, per the skill's discipline); no public remote is added yet. Publishing only happens via `git publish` (which routes through the fail-closed `pre-push`).

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

- `graph_query(cypher, explain)` — read-only Cypher against LadybugDB, rejects writes
- `semantic_search(query, entity_type, top_k)` — LadybugDB native HNSW vector index + BAAI/bge-small-en-v1.5
- `aggregate_measures(group_by, measure, filter_cypher)` — LadybugDB aggregation query

## Agent passes

Build history (passes 01-21) staged in `archive/agent-prompts/` and `runs/build-log/`. Originals kept as a provenance trail; not live instructions.

| Pass | File | Builds |
|---|---|---|
| 01 | agent-01-scaffold.md | Repo scaffold, requirements, .gitignore |
| 02 | agent-02-schema.md | Kùzu schema + migration |
| 03 | agent-03-ingestion.md | OpenML fetch + transform + load |
| 04 | agent-04-semantic.md | ChromaDB + embedding pipeline |
| 05 | agent-05-tools.md | Three agent tools + tests |
| 06 | agent-06-orchestrator.md | Claude orchestrator + auth |
| 07 | agent-07-eval.md | Eval harness + LLM judge |
| 08 | agent-08-ui.md | Streamlit UI |
| 09 | agent-09-readme.md | Public README + Mermaid diagram |

Check `git log --oneline` to see which passes have completed before resuming.

## Key rules

- `data/kuzu_db/` is gitignored (large, recreatable)
- `op://` references only in `op-refs.sh`
- Cypher write operations (CREATE/MERGE/DELETE/SET) must be blocked in `graph_tool.py`
- All agent tool functions must have unit tests in `tests/unit/`
- `runs/` directory (eval outputs) is tracked
