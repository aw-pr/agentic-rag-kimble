"""
Reusable Cypher query fragments for the agentic-rag-kimble graph.

The read fragments below are all read-only (MATCH / RETURN). The graph_query
tool enforces this by rejecting writes, but we keep it clean here too.

The write fragments at the bottom (SET_*) are parameterised and intended only
for ingestion / backfill via GraphDB.execute_write(query, params). They bind
the free-form description as a $parameter rather than interpolating it into a
Cypher string literal — this avoids quote/backslash-escaping bugs, is injection
safe, and keeps long non-ASCII OpenML text off the Cypher parser entirely.
"""

# ── Core queries (from spec) ───────────────────────────────────────────────

RUNS_BY_ALGORITHM = """
MATCH (r:Run)-[:USED_ALGORITHM]->(a:Algorithm)
WHERE a.name = $algorithm_name
RETURN r.run_id, r.accuracy, r.f1, r.auc, r.runtime_sec
ORDER BY r.accuracy DESC
"""

TOP_ALGORITHMS_FOR_DATASET = """
MATCH (r:Run)-[:USED_ALGORITHM]->(a:Algorithm),
      (r)-[:ON_DATASET]->(d:Dataset)
WHERE d.dataset_id = $dataset_id
RETURN a.name, a.family, avg(r.accuracy) AS mean_acc, count(r) AS n_runs
ORDER BY mean_acc DESC
LIMIT $limit
"""

DATASET_PROFILE = """
MATCH (d:Dataset)-[:PART_OF_TASK]->(t:Task)
WHERE d.dataset_id = $dataset_id
RETURN d.name, d.n_rows, d.n_features, d.n_classes, d.imbalance_ratio,
       d.domain_tags, t.task_type, t.evaluation_measure
"""

ALGORITHM_FAMILIES = """
MATCH (a:Algorithm)
RETURN DISTINCT a.family, count(a) AS algorithm_count
ORDER BY algorithm_count DESC
"""

# ── Additional queries ─────────────────────────────────────────────────────

# Best-performing runs overall (useful for leaderboard views)
TOP_RUNS_OVERALL = """
MATCH (r:Run)-[:USED_ALGORITHM]->(a:Algorithm),
      (r)-[:ON_DATASET]->(d:Dataset)
RETURN r.run_id, a.name AS algorithm, d.name AS dataset,
       r.accuracy, r.f1, r.auc, r.runtime_sec
ORDER BY r.accuracy DESC
LIMIT $limit
"""

# All algorithms tested on a given task type
ALGORITHMS_BY_TASK_TYPE = """
MATCH (r:Run)-[:USED_ALGORITHM]->(a:Algorithm),
      (r)-[:FOR_TASK]->(t:Task)
WHERE t.task_type = $task_type
RETURN DISTINCT a.name, a.family, count(r) AS run_count
ORDER BY run_count DESC
"""

# Performance distribution for an algorithm family across datasets
FAMILY_ACCURACY_DISTRIBUTION = """
MATCH (r:Run)-[:USED_ALGORITHM]->(a:Algorithm),
      (r)-[:ON_DATASET]->(d:Dataset)
WHERE a.family = $family
RETURN d.name, d.n_rows, d.n_features,
       avg(r.accuracy) AS mean_acc,
       min(r.accuracy) AS min_acc,
       max(r.accuracy) AS max_acc,
       count(r) AS n_runs
ORDER BY mean_acc DESC
"""

# Datasets with many features and high class imbalance (harder problems)
HARD_DATASETS = """
MATCH (d:Dataset)
WHERE d.n_features >= $min_features
  AND d.imbalance_ratio >= $min_imbalance
RETURN d.dataset_id, d.name, d.n_rows, d.n_features,
       d.n_classes, d.imbalance_ratio
ORDER BY d.imbalance_ratio DESC
LIMIT $limit
"""

# Runs for a specific setup_id (to inspect hyperparameter configurations)
RUNS_BY_SETUP = """
MATCH (r:Run)-[:USED_ALGORITHM]->(a:Algorithm),
      (r)-[:ON_DATASET]->(d:Dataset)
WHERE r.setup_id = $setup_id
RETURN r.run_id, a.name AS algorithm, d.name AS dataset,
       r.accuracy, r.f1, r.auc, r.runtime_sec, r.memory_mb
ORDER BY r.accuracy DESC
"""

# Summary stats per dataset: how many algorithms have been tried
DATASET_COVERAGE = """
MATCH (r:Run)-[:ON_DATASET]->(d:Dataset)
WITH d, count(DISTINCT r.setup_id) AS unique_setups, count(r) AS total_runs,
     avg(r.accuracy) AS mean_acc
RETURN d.dataset_id, d.name, unique_setups, total_runs, mean_acc
ORDER BY total_runs DESC
LIMIT $limit
"""

# ── Write fragments (ingestion / backfill only — via execute_write) ─────────
# Parameterised: pass {"flow_id"/"dataset_id": int, "description": str}. The
# description binds as $description, so quotes, backslashes, and long non-ASCII
# OpenML text never reach the Cypher parser. Setting description_embedding to
# NULL marks the row for re-embedding by build-index.

SET_ALGORITHM_DESCRIPTION = """
MATCH (a:Algorithm {flow_id: $flow_id})
SET a.description = $description,
    a.description_embedding = NULL
"""

SET_DATASET_DESCRIPTION = """
MATCH (d:Dataset {dataset_id: $dataset_id})
SET d.description = $description,
    d.description_embedding = NULL
"""
