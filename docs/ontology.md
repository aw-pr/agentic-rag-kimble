# Domain Ontology: agentic-rag-kimble

Prepended by `scripts/run-build.sh` to the build-agent and tuning-agent prompts. The runtime agent does not load it.
Single source of truth. If a term is defined here, use this definition everywhere.

---

## Entities

### Run (fact node)
A single ML experiment execution. One algorithm applied to one dataset for one task.
The centre of the Kimball star. All measures live here.
- `run_id`: OpenML's unique integer ID for this execution
- `setup_id`: groups runs that used the same algorithm + hyperparameter configuration
- `accuracy`: predictive_accuracy from OpenML evaluations; preferred over `accuracy` when both present
- `f1`: weighted F1 score; often absent, treat as nullable
- `auc`: ROC AUC; often absent, treat as nullable
- `runtime_sec`: wall-clock seconds for training + prediction; often absent
- `memory_mb`: peak memory in MB; sparse, treat as nullable

A run with no accuracy value is still a valid node; it may have f1 or auc.

### Algorithm (dimension node)
An ML flow registered in OpenML, typically a scikit-learn class or pipeline.
- `flow_id`: OpenML's integer ID for the flow
- `name`: the full flow name as registered (e.g. `sklearn.ensemble.RandomForestClassifier`)
- `family`: canonical grouping (see Family taxonomy below). Derived, not from OpenML.
- `description`: synthesised plain-English string used for embedding. Format: `"{name} is a {family} algorithm."`

One Algorithm node per unique `flow_id`. Multiple runs may share an algorithm.

### Dataset (dimension node)
A tabular dataset registered in OpenML.
- `dataset_id`: OpenML's integer ID
- `name`: display name (e.g. `iris`, `kddcup99`, `adult`)
- `n_rows`: NumberOfInstances from OpenML qualities, cast to int
- `n_features`: NumberOfFeatures from qualities, cast to int
- `n_classes`: NumberOfClasses from qualities, cast to int
- `imbalance_ratio`: MajorityClassSize / MinorityClassSize. Value of 1.0 means perfectly balanced. Default 1.0 when qualities unavailable.
- `domain_tags`: comma-separated string, derived from dataset name heuristics; may be empty
- `description`: synthesised: `"Dataset {name} has {n_rows} rows, {n_features} features, {n_classes} classes. Imbalance ratio: {imbalance_ratio:.1f}."`

### Date (dimension node)
A calendar date, shared by every run executed on it.
- `date_id`: integer primary key in `YYYYMMDD` form
- `date`: ISO string, `YYYY-MM-DD`
- `year`: calendar year
- `quarter`: string such as `2024-Q1`
- `month`: month number
- `month_name`: for example `March`
- `day_of_week`: for example `Monday`
- `is_weekend`: boolean

### AlgorithmFamily (dimension node)
One node per algorithm family (see Family taxonomy below).
- `family_id`: deterministic integer hash of the family slug
- `family_name`: the slug, for example `tree_ensemble`
- `display_name`: human label, for example `Tree ensembles`
- `paradigm`: modelling paradigm
- `interpretability`: one of `high`, `medium`, `low`
- `typical_use_case`: short plain-English string
- `description`: plain-English description used for embedding

### Task (dimension node)
The learning problem definition in OpenML.
- `task_id`: OpenML's integer ID
- `task_type`: always `"Supervised Classification"` in v1 scope
- `target_feature`: the column being predicted (e.g. `class`, `income`, `label`)
- `evaluation_measure`: the primary metric OpenML used for this task (e.g. `predictive_accuracy`)
- `description`: synthesised: `"A {task_type} task predicting {target_feature}. Evaluation measure: {evaluation_measure}."`

---

## Relationships

| Edge | From | To | Meaning |
|---|---|---|---|
| `USED_ALGORITHM` | Run | Algorithm | This run executed this algorithm |
| `ON_DATASET` | Run | Dataset | This run trained/tested on this dataset |
| `FOR_TASK` | Run | Task | This run addressed this OpenML task |
| `PART_OF_TASK` | Dataset | Task | This dataset is the source data for this task |
| `RUN_ON_DATE` | Run | Date | The calendar date on which this run was executed |
| `BELONGS_TO_FAMILY` | Algorithm | AlgorithmFamily | This algorithm belongs to this family |

All edges are directional. No edge properties in v1.

---

## Family taxonomy

Canonical algorithm family values. Exhaustive: every Algorithm must have one of these nine.

| Family | Covers |
|---|---|
| `tree_ensemble` | RandomForest, ExtraTrees, DecisionTree, BaggingClassifier |
| `decision_tree` | J48, REPTree, RandomTree |
| `gradient_boosting` | GradientBoostingClassifier, XGBClassifier, LGBMClassifier, CatBoostClassifier, HistGradientBoosting |
| `svm` | SVC, SVR, LinearSVC, NuSVC |
| `linear` | LogisticRegression, LinearDiscriminantAnalysis, RidgeClassifier, Perceptron, SGDClassifier |
| `knn` | KNeighborsClassifier, RadiusNeighborsClassifier |
| `neural` | MLPClassifier, any flow name containing Neural or MLP |
| `bayes` | GaussianNB, BernoulliNB, MultinomialNB, ComplementNB |
| `other` | Anything not matched above |

Derivation is from `flow.name` only. Apply in order: first match wins.

---

## Size buckets

Used by `aggregate_measures` and in agent reasoning. Consistent across all phases.

**Dataset size (n_rows):**
- `small dataset`: < 1,000 rows
- `medium dataset`: 1,000 to 100,000 rows inclusive
- `large dataset`: > 100,000 rows

**Feature count (n_features):**
- `low-dimensional`: < 20 features
- `medium-dimensional`: 20 to 100 features inclusive
- `high-dimensional`: > 100 features

**Imbalance (imbalance_ratio):**
- `balanced classes`: < 1.5
- `moderately imbalanced`: 1.5 to 5.0 inclusive
- `severe class imbalance`: > 5.0

---

## Eval terminology

**recall@k**: for a query with expected entity names `E`, recall@k = 1.0 if any name in `E` appears in the top-k retrieved results, else 0.0. Macro-averaged across all fixtures.

**grounding**: a response claim is grounded if it is traceable to a specific retrieved entity or run. A claim about "RandomForest" is grounded only if a RandomForest node appeared in the retrieved context for that query.

**judge score**: overall = mean(grounding, reasoning, completeness), rounded to 1dp. Range 1–5.

**tuning pass**: one iteration of the self-improvement loop: eval → diagnosis → single targeted fix → re-eval.

---

## Scope constraints (v1)

- **Classification tasks only.** `task_type = "Supervised Classification"` (OpenML task_type_id = 1).
- **Top 500 datasets by run count.** Datasets with fewer than 10 runs are excluded.
- **Accuracy as primary measure.** Runs without accuracy, f1, or auc are excluded from the graph.
- **No hyperparameter detail in v1.** `setup_id` is stored but hyperparameters are not unpacked into the graph.

---

## Known data quality issues

- OpenML flow names are inconsistent across versions of scikit-learn. `RandomForestClassifier(100)` and `RandomForestClassifier(n_estimators=100)` are different flows but the same algorithm. Family derivation handles this; name matching in Cypher should use `CONTAINS` not `=`.
- `runtime_sec` is missing on ~60% of runs. Never assert its presence; always treat as optional.
- Some datasets have `n_classes = 0` or `imbalance_ratio = 0` due to OpenML quality computation errors. Treat values ≤ 0 as missing; default to 1.0 for imbalance_ratio.
- `domain_tags` is heuristic and sparse. Do not rely on it for retrieval; use it only as supplementary context.
