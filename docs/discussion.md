# Discussion — architectural notes on structured agentic RAG

A standalone discussion of the design choices, scaling properties, and market context behind this project. The README covers what the system is and how to run it; this document covers *why it is shaped the way it is* and how that shape relates to the wider state of retrieval-augmented systems in 2026.

The intended audience is a technical reader evaluating retrieval architectures — not someone reading the README to get a demo running.

---

## Architectural discussion

### Ingest at scale

Encoding structure into a retrieval store is upfront work. Three costs dominate the ingest of any sufficiently large corpus into a property graph: source-API throughput, embedding generation, and graph-write throughput. Each has a different scaling story.

Source-API cost scales with the number of distinct entities, not the number of fact rows. A graph that backs onto an external system (REST API, data warehouse, document corpus) will hit per-entity rate limits long before it hits write throughput limits on the local store. Two design moves help materially. First, use bulk endpoints where the source API exposes them — a single call that returns 10,000 rows is two to three orders of magnitude cheaper than 10,000 single-row calls under almost any rate-limit regime. Second, cache aggressively at the boundary, because retries on transient errors are far cheaper when the prior response is on disk.

Embedding generation is CPU-bound by default on small dimension corpora and GPU-amenable when the count gets large. `BAAI/bge-small-en-v1.5` (the embedding model used here) supports Apple Metal acceleration via `sentence-transformers` with `device="mps"`, which gives roughly a 3-5x speed-up over CPU on M-series hardware for batches of a few thousand. For genuinely large dimension counts the better answer is a remote embedding API (Voyage, OpenAI, Cohere) — at that point network latency dominates and the embedding model becomes a service, not a library.

Graph-write throughput is the asymptote. Once API fetch and embedding are off the critical path, the question becomes how quickly the store can ingest fact rows with all their edges. Single-writer embedded stores (LadybugDB, Kùzu, DuckDB-graph) max out at the low hundreds of thousands of rows per minute. Distributed graph stores (Neo4j cluster, Memgraph, RedisGraph) scale further but introduce operational overhead that smaller deployments do not need.

The practical implication is that ingest of a few hundred thousand fact rows is comfortably a laptop-scale job; ingest of a few million is feasible on the same hardware with patience; ingest of tens of millions begins to need either an upstream snapshot (database dump rather than API replay) or a different storage tier. Each tier represents an order of magnitude in operational complexity. Picking the tier early in the project lifecycle matters more than optimising within a tier.

### Schema evolution — dimensions that drift

Real-world structured data changes on three axes that any dimensional store has to absorb without invalidating existing facts.

Dimension additions are the cheapest case. A new entity appears, a new dimension node is created, edges from new facts point at it. Existing facts are untouched. Dimension *attribute* changes are slower: when an attribute that participates in an embedding or index gets rewritten, the embedding must be regenerated and the index updated. Most embedded graph stores cannot update indexed columns in place — the workaround is delete-and-recreate on the affected dimension row. Cost scales with the number of edited dimensions, not the number of facts pointing at them.

Adding a wholly new dimension (a previously-unrecognised type of context) is supported by appending a new node label and back-edging from existing fact rows — the fact properties do not need to change. Kimball's slowly-changing-dimension type 2 (each historical version preserved as a separate dimension row) maps cleanly onto property graphs: the SCD2 surrogate key becomes a new node, and facts continue to point at the version current when they occurred.

The dimensional model starts to hurt when dimension descriptions need to be multilingual, when a free-form text field on a dimension grows large enough to need its own chunking strategy, or when the boundary between fact and dimension becomes ambiguous (long-running events, hierarchical taxonomies that flex). At that point a hybrid model — structured dimensions plus a parallel document index — becomes the more natural fit. The choice between "lift the boundary into the schema" and "fall back to text search" is the single most important schema decision in a graph RAG system, and it should be made deliberately rather than emergently.

### Models in the query loop — does it scale?

A single query through an agent loop costs a bounded amount of work regardless of corpus size, because the agent only ever sees what its tools return. This is the most important difference between agentic-over-tools and flat retrieval-augmented prompting.

In flat RAG, the model's token budget per query grows with the retrieved-context volume, which often grows with corpus breadth: a broader corpus encourages broader retrieval to maintain recall, and retrieval cost is paid in tokens on every query. In structured retrieval, the tool result is bounded by query shape — a 1-10KB Cypher result, a 1-5KB semantic-search top-k, a 1KB aggregate measure — regardless of underlying corpus size. The model's token budget grows with reasoning depth, not data volume.

What scales with corpus size is the tool-side workload. Graph queries against a 5M-node store take longer to plan and execute than against 167k; HNSW vector search remains sub-linear but the index footprint grows linearly with embedded-dimension count; aggregate measures over wider date ranges scan more rows. The agent loop itself is insulated from this — the model sees the same shape of result regardless of how much work the tool did to produce it.

Token-cost economics in this design are dominated by question complexity. A question that requires three retrieval rounds and a final synthesis turn costs roughly the same against 167k facts as against 5M. The break-even between OAuth-quota access (subscription, no per-token charge) and pay-per-token API access depends on query volume, not corpus size — for evaluation and judge workloads run a few times a day the subscription path is comfortably ahead; for an always-on production deployment with thousands of queries per day, the API path becomes more predictable and observable.

---

## Comparison

### Flat RAG vs structured RAG vs long-context-only

Three retrieval architectures coexist in 2026, each with a different failure mode.

**Flat RAG** — fixed-size chunking of documents, dense vector embedding, top-k similarity, optional rerank — was the dominant pattern from 2023 through 2024. It works when documents are paragraph-coherent and questions are answered by a small number of contiguous passages. It fails when the answer requires schema-bound aggregation (counting, filtering by structured attributes, joining across entities), when chunk boundaries sever cross-reference dependencies, or when embedding similarity ranks by surface resemblance instead of evidential usefulness. Microsoft and FalkorDB have published benchmarks showing vector RAG accuracy collapsing to near-zero on queries touching ten or more entities, while graph variants stay in the 70-90% range. The figures are vendor-published and worth treating accordingly, but the directional finding is widely reproduced.

**Structured RAG** — including graph RAG, SQL-over-document, and Kimball-on-graph approaches — encodes corpus structure into the retrieval layer rather than letting embeddings approximate it. The trade-off is upfront: ingestion is more expensive, schema decisions are load-bearing, and adding a new data shape may require a model update rather than just re-embedding. The payoff is that aggregate, counting, and join-shaped questions have a correct answer path that does not depend on the model reconstructing structure from prose. Microsoft's GraphRAG (June 2024) and LazyGraphRAG (November 2024, claiming over 700x lower per-query cost than full GraphRAG global search) are the canonical references.

**Long-context-only** — skip retrieval, load the corpus into context, let the model reason directly — became economically plausible in 2025 as million-token windows became commodity-priced. Anthropic Sonnet 4.6 at 1M tokens, Gemini 1.5 / 2.5 Pro, GPT-5 with extended context all support this. The approach works well on bounded corpora that fit (a deal room, a single codebase, one set of filings) and breaks on enterprise-scale corpora where "fit in context" is not on the table at any price. Recall on long context has improved sharply but is not yet at parity with targeted retrieval for needle-in-haystack questions; Opus 4.6 scoring 76% on 8-needle 1M-token MRCR v2 (versus Sonnet 4.5 at 18.5%) is the best recent benchmark.

The structured-RAG camp is the bet for dimensional, aggregate-heavy data — experiment results, transactions, observations, anything Kimball would model. The flat-RAG camp remains viable for paragraph-coherent prose where similarity is a reasonable proxy for relevance. The long-context camp covers bounded corpora where the cost of stuffing wins on simplicity. The three are not mutually exclusive; production systems increasingly blend all three.

---

## Market context

### Deficiencies of flat RAG that are now visible

Five recur in 2025-2026 engineering write-ups:

1. **Retrieval recall ceiling.** Embedding similarity ranks by surface resemblance, not answer usefulness. Useful evidence often lands late in the retrieved set where the model's attention is weaker.
2. **Chunk-boundary semantic loss.** Fixed-size chunking is, in [Bustamante's phrasing](https://www.nicolasbustamante.com/p/the-rag-obituary-killed-by-agents), "indifferent to what the text is saying." Cross-chunk dependencies — definitions, constraints, prerequisites — get severed. Overlap mitigates but inflates token cost and duplicates noise.
3. **No structural grounding.** Vector top-k cannot enforce schema, relationships, or aggregations. Any question of the form "how many", "compare across", or "rank by" sits outside what flat retrieval can answer correctly.
4. **Hallucination despite correct retrieval.** Re-annotation of RAGTruth in 2025 uncovered 1.68x more hallucination cases than originally labelled, implying benchmarks understate the rate at which models contradict or fabricate over correctly-retrieved context. The Stanford "up to 40% hallucination in poorly evaluated RAG" figure circulates widely but is hard to trace to a primary citation.
5. **Token-budget arithmetic flipping.** Anthropic removed its >200k-context surcharge in March 2026 (flat $3 / $15 per 1M tokens for Sonnet 4.6 at 1M); Gemini Flash sits near $0.075 per 1M input. The "RAG saves tokens" argument weakens fast when long context is priced commodity-style.

### How vendors are reacting

- **Anthropic / Claude Code (May 2025)**: dropped vector RAG with embeddings in favour of agentic search using `glob`, `grep`, and file reads. The framing on [Latent Space](https://www.latent.space/p/claude-code) was that agentic search "outperformed [RAG] by a lot" for code, where exact match beats semantic similarity, and that the index-staleness and codebase-upload risks were not worth the marginal recall.
- **Microsoft Research**: released [GraphRAG](https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/) (June 2024) and LazyGraphRAG (November 2024) — graph-structured community summarisation for global queries that vector top-k cannot answer.
- **Contextual AI, LlamaIndex**: rebranded as "context engineering" rather than retrieval, absorbing the criticism into the category. Anthropic published [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) framing retrieval as just-in-time tool calls rather than pre-indexed similarity.
- **Memory systems (Mem0, Letta, Zep)**: position persistent agent memory as a complementary layer over RAG and tool calls, not a replacement. The practitioner consensus is hybrid: factual long-term memory plus working short-term context plus targeted retrieval.
- **Glean** and other enterprise-search incumbents continue to ship RAG-first but market under "hybrid contextual grounding" language — vendor marketing absorbing the shift without restructuring.

### Enterprise adoption and re-architecture

The 2025 [Menlo Ventures *State of Generative AI in the Enterprise*](https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/) (≈500 US enterprise decision-makers, November 2025) reports RAG remains the second-most-used technique after prompt design, but only 16% of enterprise and 27% of startup deployments qualify as true agents — most are fixed-sequence workflows around a single model call. Enterprise AI spend reached $37B in 2025, approximately 3x year-on-year.

[VentureBeat's "Retrieval Rebuild" 2025 analysis](https://venturebeat.com/data/the-retrieval-rebuild-why-hybrid-retrieval-intent-tripled-as-enterprise-rag-programs-hit-the-scale-wall) reports that the share of respondents *not* expecting large-scale RAG deployments by year-end grew from 3.4% to 15.6% — a roughly 5x increase, framed as re-architecture toward hybrid retrieval rather than outright abandonment.

Gartner's February 2025 release projected that 60% of AI projects would be abandoned by 2026 without AI-ready data; a separate June 2025 release projected over 40% of agentic AI projects would be cancelled by end of 2027. Both figures circulate widely but trace to Gartner press releases rather than independent surveys, and should be cited as such.

### Where the market is heading

The directional consensus, across vendor blogs and 2026 practitioner writing, is that flat chunk-based RAG is no longer the default architecture for new builds. The replacements are not one thing — they are a portfolio: hybrid retrieval (BM25 + dense + rerank) as the new floor; graph or schema-aware retrieval for structurally-shaped data; long-context-only for bounded corpora that fit; agentic navigation (tool calls, file reads, structured queries) where the question shape benefits from multi-step reasoning. Memory systems sit alongside, not in place.

---

## Sources

- Cherny / Claude Code on Latent Space: <https://www.latent.space/p/claude-code>
- Bustamante, "The RAG Obituary": <https://www.nicolasbustamante.com/p/the-rag-obituary-killed-by-agents>
- Contextual AI counter, "Is RAG dead yet?": <https://contextual.ai/blog/is-rag-dead-yet>
- Microsoft LazyGraphRAG: <https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/>
- Anthropic, "Effective context engineering": <https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents>
- VentureBeat, "The Retrieval Rebuild": <https://venturebeat.com/data/the-retrieval-rebuild-why-hybrid-retrieval-intent-tripled-as-enterprise-rag-programs-hit-the-scale-wall>
- Menlo Ventures 2025 State of GenAI: <https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/>
- Gartner Feb 2025 release: <https://www.gartner.com/en/newsroom/press-releases/2025-02-26-lack-of-ai-ready-data-puts-ai-projects-at-risk>
- U-NIAH long-context paper: <https://arxiv.org/pdf/2503.00353>
