## Updated as of pass 21 (2026-05-11)

The spec below reflects the original design intent (pass 09). The following requirements have been superseded or updated by later passes:

| Section | Original spec | Current state |
|---|---|---|
| F1 — Data ingestion | `loader.py` writes to Kùzu graph; <30 min on laptop | `loader_async.py` (pass 13); ~90 min for full 500-dataset corpus |
| F2 — Vector indexes | `all-MiniLM-L6-v2` on Algorithm/Dataset/Task descriptions | `BAAI/bge-small-en-v1.5` (pass 19); stored in LadybugDB HNSW index |
| F3 — `semantic_search` | Searches ChromaDB collection | Searches LadybugDB native HNSW index (pass 15); ChromaDB removed |
| F4 — Orchestrator | Raw `anthropic` SDK, op-fetch auth | `claude-agent-sdk` 0.1.81, automatic OAuth via Claude Code session (pass 17) |
| F5 — Eval judge | `judge_score` null in all runs; scorer callable standalone | Judge live via SDK (`claude-haiku-4-5`), sampled 5 fixtures per run (pass 20) |
| Graph engine | Kùzu 0.11.3 | LadybugDB 0.16+ (pass 11) |
| `chroma_db_path` in Config | Present | Removed in pass 21; single-store LadybugDB |

---

# Project Specification: agentic-rag-kimble

## Goal

Build and ship a locally-runnable agentic RAG system using a Kimball-structured property graph over OpenML experimental data. The system must be demonstrable, publicly linkable, and referenceable on a CV as a production GenAI artefact.

## Non-goals

- No cloud infrastructure (no AWS, no Azure, no GCP, no managed vector DBs)
- No paid APIs (Claude via OAuth uses existing Max subscription)
- No GPU requirement (CPU-viable stack throughout)
- No authentication/login for v1

---

## Functional requirements

### F1 — Data ingestion

- Pull OpenML runs, datasets, tasks, and algorithms via the `openml` Python client
- Scope: top 500 datasets by number of runs, classification tasks only, runs with at least one accuracy measure
- Transform to Kimball dimensional schema before loading
- Load into Kùzu embedded property graph
- Idempotent: re-running ingestion updates existing nodes, does not duplicate
- Estimated volume: ~100k–500k runs for the scoped dataset; must complete on a laptop in <30 mins

### F2 — Graph schema

Nodes and required properties:

| Node label | Required properties | Optional |
|---|---|---|
| `Run` | `run_id`, `accuracy`, `setup_id` | `f1`, `auc`, `runtime_sec`, `memory_mb` |
| `Algorithm` | `flow_id`, `name`, `family`, `description` | `hyperparameter_schema` |
| `Dataset` | `dataset_id`, `name`, `n_rows`, `n_features`, `n_classes` | `imbalance_ratio`, `domain_tags` |
| `Task` | `task_id`, `task_type`, `target_feature`, `evaluation_measure` | |

Edges:

| From | Relationship | To |
|---|---|---|
| `Run` | `USED_ALGORITHM` | `Algorithm` |
| `Run` | `ON_DATASET` | `Dataset` |
| `Run` | `FOR_TASK` | `Task` |
| `Dataset` | `PART_OF_TASK` | `Task` |

Vector indexes:
- `Algorithm.description` — sentence-transformers `all-MiniLM-L6-v2`
- `Dataset.description` — same model
- `Task.description` — same model (synthesised from task metadata if no natural description)

### F3 — Agent tools

Three tools exposed to the Claude orchestrator:

**`graph_query(cypher, explain)`**
- Executes read-only Cypher against the Kùzu graph
- Returns list of dicts (column names from RETURN clause)
- Max 200 rows returned; agent must handle pagination if needed
- Rejects any Cypher containing CREATE, MERGE, DELETE, SET

**`semantic_search(query, entity_type, top_k=10)`**
- Embeds query with sentence-transformers (local)
- Searches ChromaDB collection for `entity_type`
- Returns top_k entities with their properties and similarity score
- `entity_type` must be one of: `"Algorithm"`, `"Dataset"`, `"Task"`

**`aggregate_measures(group_by, measure, filter_cypher="")`**
- Executes aggregation via Kùzu (Cypher built internally, not from agent)
- Returns `{group_value: {mean, median, p75, count}}` dict
- `group_by`: one of `"algorithm.family"`, `"algorithm.name"`, `"dataset.n_rows_bucket"`, `"dataset.n_features_bucket"`, `"task.task_type"`
- `measure`: one of `"accuracy"`, `"f1"`, `"auc"`, `"runtime_sec"`
- Optional `filter_cypher`: a WHERE clause fragment (validated against allowlist before execution)

### F4 — Orchestrator

- Claude model: `claude-sonnet-4-6` via OAuth (Max subscription, no API cost)
- Auth: same 1Password + op-fetch pattern as research-sweeper
- Max 5 tool calls per query (prevent runaway loops)
- Response must include inline citations in format `[Run #{id}]` or `[Algorithm: {name}]`
- System prompt instructs grounding in retrieved evidence, not prior knowledge

### F5 — Evaluation harness

Offline retrieval eval:
- 20 hand-crafted query→expected_entity pairs (created during spec pass)
- Measures recall@5 and recall@10 per tool type
- Run with `python -m pytest tests/eval/`

Online LLM judge:
- Scores each response on grounding (1-5), reasoning (1-5), completeness (1-5)
- Uses `claude-haiku-4-5-20251001` (cheap)
- Appended to `runs/eval.json`
- CLI: `python -m src.eval.judge --query "..." --response "..."`

### F6 — Streamlit UI

- Single-page local app (`streamlit run src/ui/app.py`)
- Four panels: query input, tool trace, answer with citations, eval score
- No login, no persistence between sessions
- Must render on 1440px screen without horizontal scroll

---

## Non-functional requirements

| Requirement | Target |
|---|---|
| Ingestion time (full scope) | < 30 min on M-series MacBook |
| Query latency (end-to-end) | < 15 sec for typical query |
| Graph DB size on disk | < 2 GB for scoped dataset |
| Vector index size | < 500 MB |
| Python version | 3.11+ (3.14 confirmed available) |
| No network calls at query time | Except Claude OAuth token refresh |
| Test coverage (src/ excl. ui/) | > 60% line coverage |

---

## Build phases and agent passes

| Pass | Agent file | Deliverable | Depends on |
|---|---|---|---|
| 01 | `agent-01-scaffold.md` | Repo scaffold, requirements.txt, CLAUDE.md, .gitignore | — |
| 02 | `agent-02-schema.md` | Kùzu graph schema, migration script, schema tests | 01 |
| 03 | `agent-03-ingestion.md` | OpenML fetch + transform + load pipeline | 02 |
| 04 | `agent-04-semantic.md` | ChromaDB setup, embedding pipeline, semantic index | 02 |
| 05 | `agent-05-tools.md` | Three agent tools, tool tests, Cypher allowlist | 03 + 04 |
| 06 | `agent-06-orchestrator.md` | Claude orchestrator, system prompt, OAuth auth | 05 |
| 07 | `agent-07-eval.md` | Eval harness, 20 query pairs, judge integration | 06 |
| 08 | `agent-08-ui.md` | Streamlit UI, tool trace panel, citation rendering | 06 |
| 09 | `agent-09-readme.md` | Public README, Mermaid diagram, demo screenshot | 07 + 08 |

---

## File layout (target)

```
agentic-rag-kimble/
├── src/
│   ├── ingestion/
│   │   ├── openml_fetch.py     # OpenML API client wrapper
│   │   ├── transform.py        # OpenML → Kimball schema mapping
│   │   └── loader.py           # Kùzu graph writer
│   ├── graph/
│   │   ├── schema.py           # Node/edge definitions, migration
│   │   └── queries.py          # Reusable Cypher fragments
│   ├── retrieval/
│   │   ├── graph_tool.py       # graph_query tool implementation
│   │   ├── semantic_tool.py    # semantic_search tool implementation
│   │   └── aggregate_tool.py   # aggregate_measures tool implementation
│   ├── agent/
│   │   ├── orchestrator.py     # Claude SDK client, tool loop
│   │   ├── tools.py            # Tool registry (binds retrieval → Claude tool schema)
│   │   └── prompts.py          # System prompt, few-shot examples
│   ├── eval/
│   │   ├── judge.py            # LLM-as-judge scorer
│   │   ├── metrics.py          # Retrieval recall computation
│   │   └── fixtures.py         # 20 hand-crafted query→entity pairs
│   └── ui/
│       └── app.py              # Streamlit app
├── tests/
│   ├── unit/                   # Fast, no network/DB
│   └── eval/                   # Eval harness tests
├── data/
│   ├── kuzu_db/                # gitignored, created at runtime
│   └── chroma_db/              # gitignored, created at runtime
├── docs/
│   ├── architecture.md         # This repo's design doc
│   └── spec.md                 # This file
├── archive/agent-prompts/      # Original Claude Code agent prompts (build history)
├── runs/build-log/             # Per-pass retrospectives (provenance trail)
├── scripts/
│   ├── ingest.sh               # Runs full ingestion pipeline
│   └── smoke-test.sh           # typecheck + test + quick ingest dry-run
├── op-refs.sh                  # 1Password refs (committed, refs not secrets)
├── run-secure-query.sh         # Injects Claude OAuth token via op-fetch
├── requirements.txt
├── requirements-dev.txt
├── pyproject.toml
├── CLAUDE.md
├── .gitignore
└── README.md
```

---

## Definition of done

The project is shippable when:

1. `./scripts/ingest.sh --dry-run` completes without error
2. `./scripts/ingest.sh` loads at least 10k runs into the graph
3. A query asking for algorithm recommendations on a tabular classification task returns a grounded, cited answer
4. `python -m pytest tests/` passes with >60% coverage
5. `streamlit run src/ui/app.py` opens a working UI in the browser
6. `README.md` contains a Mermaid architecture diagram and example query output
7. The repo is publishable on a public GitHub mirror (sanitised, no
   personal/recruiter-positioning copy)
