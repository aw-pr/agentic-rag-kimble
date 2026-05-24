## Updated as of pass 29 (2026-05-21)

Last reviewed: 2026-05-21 (pass-29)

The spec below reflects the original design intent (pass 09). The following requirements have been superseded by later passes:

| Section | Original spec | Current state |
|---|---|---|
| F1 - Data ingestion | `loader.py` writes to Kùzu graph; <30 min on laptop | async ingestion path (`openml_fetch_async` + `loader_async`); ~90 min for full 500-dataset corpus |
| F2 - Vector indexes | `all-MiniLM-L6-v2` on Algorithm/Dataset/Task descriptions | `BAAI/bge-small-en-v1.5`; stored in LadybugDB native HNSW index |
| F3 - `semantic_search` | Searches ChromaDB collection | Searches LadybugDB native HNSW index; ChromaDB removed |
| F4 - Orchestrator | Raw `anthropic` SDK, op-fetch auth | `claude-agent-sdk`, automatic OAuth via Claude Code session |
| Graph engine | Kùzu 0.11.3 | LadybugDB 0.16+ |
| Kimball shape | Star with Evaluator dim | Snowflaked model with `AlgorithmFamily` outrigger plus `Date` dimension |

---

# Project Specification: agentic-rag-kimble

## Goal

Build and ship a locally-runnable agentic RAG system using a Kimball-structured property graph over OpenML experimental data.

## Non-goals

- No cloud infrastructure
- No API-key billing path for the core agent route
- No GPU requirement
- No auth/login product surface for v1

---

## Functional requirements

### F1 - Data ingestion

- Pull OpenML runs, datasets, tasks, and algorithms via the `openml` Python client
- Scope: top 500 datasets by run count, classification-oriented workload
- Transform to Kimball schema before loading
- Load into LadybugDB embedded graph
- Idempotent: re-running ingestion updates existing nodes, does not duplicate
- Full corpus runtime is expected to be about 90 minutes on local hardware

### F2 - Graph schema

Nodes and required properties:

| Node label | Required properties | Optional |
|---|---|---|
| `Run` | `run_id`, `accuracy`, `setup_id` | `f1`, `auc`, `runtime_sec`, `memory_mb` |
| `Algorithm` | `flow_id`, `name`, `description` | `hyperparameter_schema` |
| `AlgorithmFamily` | `family_id`, `name`, `description` | |
| `Dataset` | `dataset_id`, `name`, `n_rows`, `n_features`, `n_classes` | `imbalance_ratio`, `domain_tags` |
| `Task` | `task_id`, `task_type`, `target_feature`, `evaluation_measure` | |
| `Date` | `date_key` (`YYYYMMDD`) | `year`, `month`, `day` |

Edges:

| From | Relationship | To |
|---|---|---|
| `Run` | `USED_ALGORITHM` | `Algorithm` |
| `Algorithm` | `BELONGS_TO_FAMILY` | `AlgorithmFamily` |
| `Run` | `ON_DATASET` | `Dataset` |
| `Run` | `FOR_TASK` | `Task` |
| `Run` | `RUN_ON_DATE` | `Date` |

Vector indexes:
- `Algorithm.description`
- `AlgorithmFamily.description`
- `Dataset.description`
- `Task.description`

Embedding model: sentence-transformers `BAAI/bge-small-en-v1.5`.

### F3 - Agent tools

Three tools exposed to the Claude orchestrator:

**`graph_query(cypher, explain)`**
- Executes read-only Cypher against LadybugDB
- Returns list of dicts
- Rejects write operations

**`semantic_search(query, entity_type, top_k=10)`**
- Embeds query locally with sentence-transformers
- Searches LadybugDB native HNSW index for `entity_type`
- Returns top_k entities with score
- `entity_type` allowlist includes graph dimensions used by the retrieval layer

**`aggregate_measures(group_by, measure, filter_cypher="")`**
- Executes aggregation via LadybugDB
- Returns `{group_value: {mean, median, p75, count}}`

Canonical tool list remains exactly: `graph_query`, `semantic_search`, `aggregate_measures`.

### F4 - Orchestrator

- Claude Agent SDK route via OAuth session from Claude Code
- No 1Password op-fetch injection in the runtime auth path
- Response should include inline citations grounded in retrieved entities/runs

### F5 - Evaluation harness

Offline retrieval eval:
- 20 query→expected-entity fixtures
- Measures recall@5 and recall@10 per tool type

Online LLM judge:
- Scores grounding, reasoning, completeness
- Uses `claude-haiku-4-5-20251001`
- Sampled at 5 fixtures per run

### F6 - Streamlit UI

- Single-page local app (`streamlit run src/ui/app.py`)
- Panels: query input, tool trace, answer with citations, eval score

---

## Non-functional requirements

| Requirement | Target |
|---|---|
| Ingestion time (full scope) | ~90 min on M-series MacBook |
| Query latency (typical) | < 15 sec |
| Python version | 3.11+ |
| No network calls at query time | Except Claude OAuth/session behaviour |

---

## Build phases and agent passes

Early pass history remains useful as provenance, but this spec is authoritative for pass-29 runtime behaviour.

---

## Definition of done

The project is shippable when:

1. Async ingestion completes for the intended corpus without structural errors.
2. Queries return grounded answers with citations.
3. Tests and smoke checks pass.
4. Streamlit app runs locally and shows tool trace + cited answer.
