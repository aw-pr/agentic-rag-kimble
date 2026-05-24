## Updated as of pass 29 (2026-05-21)

Last reviewed: 2026-05-21 (pass-29)

The original design below describes the pass-09 architecture. The following changes are now in effect:

| Component | Original (pass 09) | Current (pass 29) |
|---|---|---|
| Property graph | Kùzu 0.11.3 | LadybugDB 0.16+ (community continuation of Kùzu post Apple acqui-hire, early 2025) |
| Vector store | ChromaDB (separate embedded store) | LadybugDB native HNSW via VECTOR extension - single store, no ChromaDB |
| Embeddings | all-MiniLM-L6-v2 (~90MB) | BAAI/bge-small-en-v1.5 (384-dim, ~130MB) |
| Agent SDK | Raw `anthropic` SDK with hand-rolled tool-use loop | `claude-agent-sdk` with `@tool`-decorated functions, SDK handles loop |
| Auth | op-fetch + env var injection | Automatic via Claude Code OAuth session; no env vars needed |
| Kimball dimensions | Algorithm, Dataset, Task, Evaluator | Algorithm, Dataset, Task, Date + AlgorithmFamily snowflake outrigger |
| Corpus size | ~600 runs (smoke DB) | 171,250 runs / 1,588 algorithms / 500 datasets / 568 tasks |

The `ChromaDB` box in the historical stack diagram is obsolete. All three agent tools hit LadybugDB only. The ingestion layer uses the async path (`openml_fetch_async` + `loader_async`).

---

# Architecture: Agentic RAG with Kimball-Structured Knowledge Graph

## Concept

Traditional RAG treats all content as a flat bag of documents. This project applies Ralph Kimball's dimensional modelling principles to the knowledge graph, giving the retrieval layer principled structure before semantics are added.

In a Kimball star/snowflake schema:
- **Fact nodes** record what happened (a training run, an experiment result)
- **Dimension nodes** record stable context (`Algorithm`, `Dataset`, `Task`, `Date`)
- **Snowflake outrigger** captures algorithm family as its own dimension (`AlgorithmFamily`)
- **Measures** are numeric outcomes on `Run` (accuracy, F1, AUC, runtime_sec)

The agent selects retrieval strategy per query type rather than always doing vector similarity:

| Query type | Tool used | Example |
|---|---|---|
| Structured lookup | `graph_query(cypher)` | "All Random Forest runs on imbalanced datasets" |
| Semantic similarity | `semantic_search(query)` | "Tasks similar to medical record classification" |
| Aggregate analysis | `aggregate_measures(filter)` | "Mean accuracy by algorithm family on tabular tasks" |
| Multi-hop reasoning | graph_query -> semantic_search chain | "Given this new dataset's characteristics, what algorithm families emerged as dominant on historically similar tasks?" |

---

## Data source: OpenML

OpenML is a public repository of machine learning experiments.

- 3,800+ tasks, 4,000+ datasets, 18M+ upstream runs
- Free Python client (`pip install openml`), no auth required for reads
- Native dimensional structure that maps directly to the Kimball model

### Kimball mapping (current)

```
[AlgorithmFamily]
        |
BELONGS_TO_FAMILY
        |
[Algorithm]----[Run (fact)]----[Dataset]
                    |  \
               [Task] [Date]
```

**Fact:** `Run`  
**Dimensions:** `Algorithm`, `Dataset`, `Task`, `Date`  
**Snowflake outrigger:** `AlgorithmFamily` off `Algorithm`  
**Measures:** accuracy, weighted_F1, AUC_ROC, runtime_sec (sparse by run)

---

## Local stack

All components run in-process or as local processes. No cloud services.

```
OpenML API
    |
    v
[Ingestion layer]          src/ingestion/
    |  openml_fetch_async.py  <- pulls runs/datasets/tasks via API
    |  transform.py           <- maps to Kimball schema
    |  loader_async.py        <- writes to LadybugDB graph
    v
[LadybugDB graph]          data/ladybug_db/
    |  Property graph         <- Cypher query interface
    |  Native HNSW index      <- VECTOR extension on dimension descriptions
    |  Kimball snowflake      <- Run(fact) + dimensions + AlgorithmFamily outrigger
    v
[Agent layer]              src/agent/
    |  orchestrator.py        <- Claude Agent SDK via OAuth session
    |  tools.py               <- graph_query / semantic_search / aggregate_measures
    |  prompts.py             <- system prompt, tool descriptions
    v
[Eval harness]             src/eval/
    |  judge.py               <- LLM-as-judge scoring
    |  metrics.py             <- retrieval recall, tool selection accuracy
    v
[Streamlit UI]             src/ui/
       app.py               <- local demo, query -> tool trace -> answer
```

---

## Agent design

### Tool definitions

```python
graph_query(cypher: str, explain: str) -> list[dict]

semantic_search(
    query: str,
    entity_type: Literal["Algorithm", "Dataset", "Task", "AlgorithmFamily"],
    top_k: int = 10
) -> list[dict]

aggregate_measures(
    group_by: str,
    measure: str,
    filter_cypher: str = ""
) -> dict
```

Three canonical tools: `graph_query`, `semantic_search`, `aggregate_measures`.

---

## Evaluation design

Two layers:

**Retrieval eval** (offline, automated)
- 20 known query→entity fixtures
- Metrics: recall@5 and recall@10 by tool type

**Response eval** (LLM-as-judge)
- Dimensions: grounding, reasoning quality, completeness
- Judge model: `claude-haiku-4-5-20251001`
- Sampled 5 fixtures per run

---

## Scope boundaries

**In scope for v1:**
- OpenML data only
- Classification-heavy workload
- CPU-only local embedding inference
- Single-user local demo

---

## Why this remains architecturally interesting

The Kimball structure and snowflaked outrigger give stronger grounding than flat-chunk RAG:

1. **Dimensional consistency** - the same entities are reused across all runs
2. **Measure-aware retrieval** - aggregates operate over fact measures, not generated text
3. **Schema-enforced grounding** - claims should trace to fact nodes and named entities
