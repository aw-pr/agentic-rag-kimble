"""
transform.py — Maps OpenML objects to the Kimball star schema.

All functions are pure (no network, no DB). INT64 casts happen here,
not in the loader.
"""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime as _datetime

from src.ingestion.openml_fetch import FetchedDataset, FetchedRun

# ── Algorithm family detection ──────────────────────────────────────────────


_FAMILY_RULES: list[tuple[list[str], str]] = [
    (["RandomForest", "ExtraTree", "DecisionTree"], "tree_ensemble"),
    # Weka single-tree learners — placed AFTER tree_ensemble so sklearn's
    # DecisionTreeClassifier (matched by "DecisionTree" above) stays in tree_ensemble.
    (["J48", "REPTree", "RandomTree"], "decision_tree"),
    # Gradient boosting: sklearn, XGBoost, LightGBM, CatBoost, plus MLR/R short names
    (["GradientBoosting", "XGB", "LightGBM", "CatBoost", "AdaBoost",
      "classif.boosting", "classif.gbm", "classif.xgboost"], "gradient_boosting"),
    # Neural: BEFORE linear so that MultilayerPerceptron is not caught by "Perceptron"
    (["MLP", "Neural", "MultilayerPerceptron", "KerasClassifier",
      "classif.nnet", "classif.neuralnet", "classif.dbnDNN",
      "rm.process.neural_net", "neuralnet", "dbnDNN"], "neural"),
    # Linear: LinearSVC must be checked before the generic SVC/SVM rule
    (["LogisticRegression", "LinearSVC", "SGDClassifier", "RidgeClassifier",
      "Perceptron", "classif.glm", "classif.logreg", "classif.lda"], "linear"),
    # SVM: sklearn plus MLR/R short names — must come AFTER LinearSVC
    (["SVM", "SVC", "SVR", "classif.svm", "classif.ksvm"], "svm"),
    # KNN: sklearn, Weka, MLR/R — kknn, knn
    (["KNeighbors", "KNN", "IBk", "IB1",
      "classif.kknn", "classif.knn", "mlr.classif.kknn", "NearestCentroid"], "knn"),
    (["NaiveBayes", "BayesNet", "GaussianNB", "BernoulliNB", "MultinomialNB",
      "classif.naiveBayes", "classif.gaussNB"], "bayes"),
]


# ── Algorithm family synonym phrases ────────────────────────────────────────

_FAMILY_SYNONYMS: dict[str, str] = {
    "tree_ensemble":     "ensemble of decision trees, bagging, random forest, boosted trees",
    "decision_tree":     "decision tree, recursive partitioning, splitting rules",
    "gradient_boosting": "gradient boosting, boosting, weak learners, XGBoost LightGBM CatBoost",
    "linear":            "linear model, logistic regression, generalised linear, "
                         "regularised linear",
    "svm":               "support vector machine, kernel methods, maximum margin classifier",
    "knn":               "k-nearest neighbour, instance-based, lazy learner, distance-based",
    "neural":            "neural network, deep learning, multilayer perceptron, feedforward",
    "bayes":             "naive Bayes, Bayesian classifier, probabilistic model, "
                         "conditional probability",
    "other":             "",
}

# ── Dataset size/shape bucketing ─────────────────────────────────────────────


def derive_size_bucket(n_rows: int) -> str:
    """Return a size label for a dataset based on row count."""
    if n_rows < 1000:
        return "small dataset"
    if n_rows <= 100_000:
        return "medium dataset"
    return "large dataset"


def derive_dim_bucket(n_features: int) -> str:
    """Return a dimensionality label for a dataset based on feature count."""
    if n_features < 20:
        return "low-dimensional"
    if n_features <= 100:
        return "medium-dimensional"
    return "high-dimensional"


def derive_imbalance_bucket(imbalance_ratio: float) -> str:
    """Return a class-imbalance label for a dataset based on majority/minority ratio."""
    if imbalance_ratio < 1.5:
        return "balanced classes"
    if imbalance_ratio <= 5.0:
        return "moderately imbalanced"
    return "severe class imbalance"


# Thin aliases for backwards compatibility — private names still importable.
_dataset_size_label = derive_size_bucket
_dataset_dim_label = derive_dim_bucket
_dataset_imbalance_label = derive_imbalance_bucket


# ── Algorithm dimension attributes ───────────────────────────────────────────

# Families that map to each paradigm value.
_PARADIGM_MAP: dict[str, str] = {
    "tree_ensemble":     "tree",
    "decision_tree":     "tree",
    "gradient_boosting": "ensemble_meta",
    "neural":            "neural",
    "linear":            "linear",
    "svm":               "distance",
    "knn":               "distance",
    "bayes":             "probabilistic",
}

# Families treated as ensembles.
_ENSEMBLE_FAMILIES: frozenset[str] = frozenset({"tree_ensemble", "gradient_boosting"})

# Training-cost classification per family.
_COST_MAP: dict[str, str] = {
    "linear":            "cheap",
    "knn":               "cheap",
    "bayes":             "cheap",
    "decision_tree":     "moderate",
    "svm":               "moderate",
    "tree_ensemble":     "expensive",
    "gradient_boosting": "expensive",
    "neural":            "expensive",
}


def derive_paradigm(flow_name: str, family: str) -> str:
    """Map an algorithm to a coarse paradigm label.

    Returns one of: tree, linear, neural, distance, probabilistic,
    ensemble_meta, automl, rule, other.
    """
    if "auto" in flow_name.lower():
        return "automl"
    return _PARADIGM_MAP.get(family, "other")


def derive_is_ensemble(flow_name: str, family: str) -> bool:
    """Return True when the algorithm is an ensemble or AutoML meta-learner."""
    if "auto" in flow_name.lower():
        return True
    return family in _ENSEMBLE_FAMILIES


def derive_training_cost_class(flow_name: str, family: str) -> str:
    """Return an estimated training-cost class: cheap, moderate, or expensive."""
    if "auto" in flow_name.lower():
        return "expensive"
    return _COST_MAP.get(family, "moderate")


def derive_display_name(flow_name: str) -> str:
    """Derive a short canonical name from a fully-qualified OpenML flow name.

    Strategy:
    1. Strip everything from the first '(' onward (drops pipeline args).
    2. Split on '.' and take the last token.
    3. If the token contains '_', split on '_' and take the *last* segment
       (handles Weka-style Foo_Bar_MLPClassifier chains).
    4. Strip trailing whitespace.
    """
    # Step 1 — strip parametric args
    name = flow_name.split("(")[0]
    # Step 2 — take last dot-separated token
    name = name.split(".")[-1]
    # Step 3 — for underscore-chained names, take the last segment
    if "_" in name:
        name = name.split("_")[-1]
    return name.strip()


def derive_algorithm_family(flow_name: str) -> str:
    """Extract algorithm family from an OpenML flow name.

    Checks the full flow name (case-sensitive substring) against known tokens
    so that e.g. 'sklearn.ensemble.RandomForestClassifier' maps to 'tree_ensemble'.
    """
    for tokens, family in _FAMILY_RULES:
        for token in tokens:
            if token in flow_name:
                return family
    return "other"


# ── Description synthesis ────────────────────────────────────────────────────


def synthesise_description(node_type: str, properties: dict) -> str:
    """Build a plain-English description for embedding.

    node_type must be one of: 'Algorithm', 'Dataset', 'Task'.
    """
    if node_type == "Algorithm":
        display = properties.get("display_name") or properties["name"]
        fqcn = properties["name"]
        family = properties["family"]
        synonym_phrase = _FAMILY_SYNONYMS.get(family, "")
        if display != fqcn:
            base = f"{display} ({fqcn}) is a {family} algorithm"
        else:
            base = f"{display} is a {family} algorithm"
        if synonym_phrase:
            return f"{base} — {synonym_phrase}."
        return f"{base}."
    if node_type == "Dataset":
        name = properties["name"]
        n_rows = properties["n_rows"]
        n_features = properties["n_features"]
        n_classes = properties["n_classes"]
        imbalance_ratio = properties["imbalance_ratio"]
        size_label = _dataset_size_label(n_rows)
        dim_label = _dataset_dim_label(n_features)
        imbalance_label = _dataset_imbalance_label(imbalance_ratio)
        return (
            f"Dataset {name}: {n_rows} rows, {n_features} features, {n_classes} classes. "
            f"{size_label.capitalize()}, {dim_label}, {imbalance_label}. "
            f"Imbalance ratio: {imbalance_ratio:.1f}."
        )
    if node_type == "Task":
        return (
            f"A {properties['task_type']} task predicting "
            f"{properties['target_feature']}. "
            f"Evaluation measure: {properties['evaluation_measure']}."
        )
    raise ValueError(f"Unknown node_type: {node_type!r}")


# ── Date dimension ───────────────────────────────────────────────────────────

_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def derive_date_row(iso_or_dt) -> dict | None:
    """Return a Date dimension dict for a given date value.

    Accepts an ISO date string ("YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS"), a
    ``datetime.date``, or a ``datetime.datetime``.  Returns ``None`` for
    ``None`` or any value that cannot be parsed.

    The ``date_id`` is the integer YYYYMMDD (e.g. 20240315).
    """
    if iso_or_dt is None:
        return None

    d: _date | None = None
    if isinstance(iso_or_dt, _datetime):
        d = iso_or_dt.date()
    elif isinstance(iso_or_dt, _date):
        d = iso_or_dt
    else:
        raw = str(iso_or_dt).strip()
        if not raw:
            return None
        # Accept "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS" (OpenML upload_time format).
        try:
            d = _date.fromisoformat(raw[:10])
        except (ValueError, TypeError):
            return None

    if d is None:
        return None

    month = d.month
    quarter_num = (month - 1) // 3 + 1
    dow = d.weekday()  # 0=Monday … 6=Sunday

    return {
        "date_id": int(d.strftime("%Y%m%d")),
        "date": d.isoformat(),
        "year": d.year,
        "quarter": f"{d.year}-Q{quarter_num}",
        "month": month,
        "month_name": _MONTH_NAMES[month],
        "day_of_week": _DOW_NAMES[dow],
        "is_weekend": dow >= 5,
    }


# ── Metric extraction ────────────────────────────────────────────────────────


def extract_accuracy(evaluations: dict) -> float | None:
    """Return predictive_accuracy, then accuracy, or None if neither present."""
    if "predictive_accuracy" in evaluations:
        return float(evaluations["predictive_accuracy"])
    if "accuracy" in evaluations:
        return float(evaluations["accuracy"])
    return None


def _safe_float(value) -> float | None:
    """Convert value to float, return None on failure or NaN."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _safe_int(value, default: int = 0) -> int:
    """Convert value to int via float, returning `default` on None/NaN/failure."""
    f = _safe_float(value)
    return default if f is None else int(f)


# ── Node transformers ────────────────────────────────────────────────────────


def transform_algorithm_family(slug: str) -> dict:
    """Return the property dict for an AlgorithmFamily node (no embedding column).

    Delegates to ``family_metadata.get_family_row``.  Unknown slugs fall back
    to the 'other' entry while preserving the provided slug as family_name.

    Parameters
    ----------
    slug:
        A family slug string, e.g. 'tree_ensemble'.

    Returns
    -------
    dict matching the AlgorithmFamily node schema (excluding description_embedding).
    """
    from src.ingestion.family_metadata import get_family_row  # local import avoids circularity
    return get_family_row(slug)


def transform_dataset(fetched: FetchedDataset) -> dict:
    """Return a dict matching the Dataset node schema (no embedding column)."""
    q = fetched.qualities

    n_rows = _safe_int(q.get("NumberOfInstances"))
    n_features = _safe_int(q.get("NumberOfFeatures"))
    n_classes = _safe_int(q.get("NumberOfClasses"))

    maj = _safe_float(q.get("MajorityClassSize"))
    minn = _safe_float(q.get("MinorityClassSize"))
    if maj is not None and minn is not None and minn > 0:
        imbalance_ratio = round(maj / minn, 6)
    else:
        imbalance_ratio = 1.0

    props = {
        "dataset_id": int(fetched.dataset_id),
        "name": str(fetched.name),
        "n_rows": n_rows,
        "n_features": n_features,
        "n_classes": n_classes,
        "imbalance_ratio": imbalance_ratio,
        "size_bucket": derive_size_bucket(n_rows),
        "dim_bucket": derive_dim_bucket(n_features),
        "imbalance_bucket": derive_imbalance_bucket(imbalance_ratio),
        "domain_tags": "",
    }
    props["description"] = synthesise_description("Dataset", props)
    return props


def transform_algorithm(flow_id: int, flow_name: str) -> dict:
    """Return a dict matching the Algorithm node schema (no embedding column)."""
    family = derive_algorithm_family(flow_name)
    props = {
        "flow_id": int(flow_id),
        "name": str(flow_name),
        "display_name": derive_display_name(flow_name),
        "family": family,
        "paradigm": derive_paradigm(flow_name, family),
        "is_ensemble": derive_is_ensemble(flow_name, family),
        "training_cost_class": derive_training_cost_class(flow_name, family),
    }
    props["description"] = synthesise_description("Algorithm", props)
    return props


def transform_task(
    task_id: int, task_type: str, target_feature: str, evaluation_measure: str
) -> dict:
    """Return a dict matching the Task node schema (no embedding column)."""
    props = {
        "task_id": int(task_id),
        "task_type": str(task_type),
        "target_feature": str(target_feature or ""),
        "evaluation_measure": str(evaluation_measure or ""),
    }
    props["description"] = synthesise_description("Task", props)
    return props


def transform_run(fetched: FetchedRun, flow_name: str) -> tuple[dict, dict]:
    """Return (run_dict, algorithm_dict).

    run_dict matches the Run node schema.
    algorithm_dict matches the Algorithm node schema.
    """
    accuracy = extract_accuracy(fetched.evaluations)

    evals = fetched.evaluations
    f1 = _safe_float(evals.get("f1") or evals.get("f_measure"))
    auc = _safe_float(evals.get("area_under_roc_curve") or evals.get("auc"))

    run_dict = {
        "run_id": int(fetched.run_id),
        "setup_id": int(fetched.setup_id),
        "accuracy": accuracy,
        "f1": f1,
        "auc": auc,
        "runtime_sec": _safe_float(evals.get("usercpu_time_millis_training")),
        "memory_mb": None,
    }
    algorithm_dict = transform_algorithm(fetched.flow_id, flow_name)
    return run_dict, algorithm_dict
