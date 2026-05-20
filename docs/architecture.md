## Updated as of pass 21 (2026-05-11)

The original design below describes the pass-09 architecture. The following changes are now in effect:

| Component | Original (pass 09) | Current (pass 21) |
|---|---|---|
| Property graph | Kùzu 0.11.3 | LadybugDB 0.16+ (community continuation of Kùzu post Apple acqui-hire, early 2025) |
| Vector store | ChromaDB (separate embedded store) | LadybugDB native HNSW via VECTOR extension — single store, no ChromaDB |
| Embeddings | all-MiniLM-L6-v2 (~90MB) | BAAI/bge-small-en-v1.5 (384-dim, ~130MB) |
| Agent SDK | Raw `anthropic` SDK with hand-rolled tool-use loop | `claude-agent-sdk` 0.1.81 — `@tool`-decorated functions, SDK handles loop |
| Auth | op-fetch + CLAUDE_CODE_OAUTH_TOKEN env var | Automatic via Claude Code CLI session; no env vars needed |
| LLM judge | Stub (always null) | Live `claude-haiku-4-5` via SDK, sampled 5 fixtures per run |
| Corpus size | ~600 runs (smoke DB) | 171,250 runs / 1,588 algorithms / 500 datasets / 568 tasks |

The `ChromaDB` box in the local stack diagram below no longer exists. All three agent tools hit LadybugDB only. The ingestion layer now uses `loader_async` (pass 13) rather than `loader.py`.

---

# Architecture: Agentic RAG with Kimball-Structured Knowledge Graph

## Concept

Traditional RAG treats all content as a flat bag of documents. This project applies Ralph Kimball's dimensional modelling principles to the knowledge graph, giving the retrieval layer principled structure before semantics are added.

In a Kimball star schema:
- **Fact tables** record what happened (a training run, an experiment result)
- **Dimension tables** record stable context (which algorithm, which dataset, which task type)
- **Measures** are the numeric outcomes on the fact (accuracy, runtime, cost)

Mapped to a property graph for agentic RAG:
- **Fact nodes** = experimental runs (centre of each star)
- **Dimension nodes** = Algorithm, Dataset, Task, Evaluator (the points)
- **Semantic layer** = vector embeddings stored on dimension node descriptions
- **Measures** = edge or node properties on fact nodes (accuracy, F1, AUC, runtime_sec)

The agent then selects a retrieval strategy per query type rather than always doing vector similarity:

| Query type | Tool used | Example |
|---|---|---|
| Structured lookup | `graph_query(cypher)` | "All Random Forest runs on imbalanced datasets" |
| Semantic similarity | `semantic_search(query)` | "Tasks similar to medical record classification" |
| Aggregate analysis | `aggregate_measures(filter)` | "Mean accuracy by algorithm family on tabular tasks" |
| Multi-hop reasoning | graph_query → semantic_search chain | "Given this new dataset's characteristics, what algorithm families emerged as dominant on historically similar tasks?" |

---

## Data source: OpenML

OpenML is a public repository of machine learning experiments.

- 3,800+ tasks, 4,000+ datasets, 18M+ runs
- Free Python client (`pip install openml`), no authentication required for reads
- Native dimensional structure that maps directly to the Kimball schema

### Kimball mapping

```
[Algorithm]──────[Run (fact)]──────[Dataset]
                     │
              ┌──────┴──────┐
           [Task]       [Evaluator]

Measures on Run: accuracy, f1, auc, runtime_sec, memory_mb
```

**Fact:** `Run` — one row per experiment
**Dimensions:**
- `Algorithm` — name, family, hyperparameter schema, description
- `Dataset` — name, n_rows, n_features, n_classes, imbalance_ratio, domain_tags
- `Task` — task_type (classification/regression), target_feature, evaluation_measure
- `Evaluator` — uploader, institution (sparse, used where available)

**Measures:** accuracy, weighted_F1, AUC_ROC, runtime_sec, memory_mb (not all present on every run)

---

## Local stack

All components run in-process or as local processes. No cloud services.

```
OpenML API
    │
    ▼
[Ingestion layer]          src/ingestion/
    │  openml_fetch.py      ← pulls runs/datasets/tasks via API
    │  transform.py         ← maps to Kimball schema
    │  loader.py            ← writes to Kùzu graph
    ▼
[Kùzu graph]               data/kuzu_db/
    │  Property graph       ← Cypher query interface
    │  Built-in vector idx  ← on Algorithm.description, Dataset.description
    │  Kimball star schema  ← Run(fact) ↔ Algorithm/Dataset/Task(dims)
    ▼
[ChromaDB]                 data/chroma_db/
    │  Semantic fallback    ← dense passage retrieval on dimension descriptions
    │  sentence-transformers ← all-MiniLM-L6-v2 (local, ~90MB, CPU-viable)
    ▼
[Agent layer]              src/agent/
    │  orchestrator.py      ← Claude via OAuth (Max subscription, no API cost)
    │  tools.py             ← graph_query / semantic_search / aggregate_measures
    │  prompts.py           ← system prompt, tool descriptions
    ▼
[Eval harness]             src/eval/
    │  judge.py             ← LLM-as-judge scoring (coverage, grounding, reasoning)
    │  metrics.py           ← retrieval recall, tool selection accuracy
    ▼
[Streamlit UI]             src/ui/
       app.py               ← local demo, query → tool trace → answer
```

---

## Agent design

### Orchestrator system prompt (intent)

The agent is framed as an ML engineering advisor. It knows it has access to 18M historical experiments and should reason from evidence, not prior knowledge. It must cite specific run IDs or dataset names when making claims.

### Tool definitions

```python
graph_query(
    cypher: str,
    explain: str  # why this query answers the user's question
) -> list[dict]

semantic_search(
    query: str,
    entity_type: Literal["Algorithm", "Dataset", "Task"],
    top_k: int = 10
) -> list[dict]

aggregate_measures(
    group_by: str,           # e.g. "algorithm.family"
    measure: str,            # e.g. "accuracy"
    filter_cypher: str = ""  # optional WHERE clause
) -> dict  # {group: {mean, median, p75, count}}
```

### Tool selection logic (agent responsibility, not hardcoded)

The agent decides. Guidelines embedded in system prompt:
- Exact entity lookups → `graph_query`
- "Similar to X" / "like Y" → `semantic_search` first, then `graph_query` to get runs
- "Best performing" / "typical" / "historically" → `aggregate_measures`
- Complex questions → chain: semantic search → graph traversal → aggregate

---

## Evaluation design

Two layers:

**Retrieval eval** (offline, automated)
- Sample 50 known query→answer pairs from OpenML documentation
- Measure: did the right algorithm/dataset appear in retrieved context?
- Metric: recall@10 per tool type

**Response eval** (LLM-as-judge, same pattern as research-sweeper)
- Dimensions: grounding (claims traceable to retrieved data), reasoning quality, answer completeness
- Judge model: claude-haiku (cheap, single call)
- Output appended to `runs/eval.json`

---

## Output specification

### What a complete run produces

Given query: *"I have a tabular classification dataset, 80k rows, 45 features, moderate class imbalance (~1:8). What does the evidence suggest about algorithm selection?"*

1. **Tool trace** (shown in UI) — which tools fired, in what order, what they returned
2. **Retrieved context** — the specific runs, algorithms, datasets that informed the answer
3. **Synthesised answer** — Claude's reasoning grounded in retrieved evidence, with citations (`Run #123456, RandomForest, accuracy=0.847`)
4. **Eval score** — grounding/reasoning/completeness scored 1-5

### Streamlit UI panels

```
┌─────────────────────────────────────────────────────┐
│  Query input                                        │
├──────────────────┬──────────────────────────────────┤
│  Tool trace      │  Answer                          │
│  (step by step)  │  (with inline citations)         │
├──────────────────┴──────────────────────────────────┤
│  Eval score  │  Run metadata  │  Graph visualisation │
└─────────────────────────────────────────────────────┘
```

---

## Scope boundaries

**In scope for v1:**
- OpenML data only (no Wikipedia, no Wolfram in v1)
- Classification tasks only (regression is a v2 extension)
- CPU-only inference (sentence-transformers on CPU, no GPU required)
- Single-user local demo

**Extensions (post-v1):**
- Wolfram Alpha tool for on-demand statistical calculations
- OpenClaw usage logs as a second star (AI assistant interaction patterns)
- Game of Life / cellular automata patterns as a third star (emergence angle)
- Public deployment via Fly.io or Modal (zero-cost tiers available)

---

## Why this is architecturally interesting

Most production RAG systems have no principled structure in the knowledge graph — documents are chunked, embedded, and retrieved by cosine similarity. The Kimball structure here means:

1. **Dimensional consistency** — the same Algorithm entity appears across all its runs; there is no duplication or drift between chunks
2. **Measure-aware retrieval** — the agent can aggregate over outcomes, not just retrieve text
3. **Schema-enforced grounding** — claims must trace to a fact node with a run ID; hallucination requires fabricating a specific run

That third point is the strongest credibility argument: the system is structurally harder to hallucinate from than flat document RAG.
