"""
family_metadata.py — Curated metadata for algorithm family nodes (AlgorithmFamily).

Each entry maps a family slug (the string stored in Algorithm.family) to a dict
containing attributes suitable for the AlgorithmFamily node table.  The
description field is plain prose intended for embedding with BAAI/bge-small-en-v1.5.

family_id is a deterministic INT64 hash of the family slug (STRING primary keys
are not supported by LadybugDB).  The hash is stable across runs and processes.
"""

from __future__ import annotations

import hashlib


def _family_id(slug: str) -> int:
    """Return a stable, positive INT64 derived from the family slug.

    Uses the first 8 bytes of the SHA-256 digest, interpreted as an unsigned
    64-bit integer, then reduced to a positive signed INT64 range by masking
    the top bit.
    """
    digest = hashlib.sha256(slug.encode()).digest()
    raw = int.from_bytes(digest[:8], "big")
    return raw & 0x7FFFFFFFFFFFFFFF  # keep within signed INT64 positive range


FAMILY_METADATA: dict[str, dict] = {
    "tree_ensemble": {
        "display_name": "Tree ensembles",
        "paradigm": "tree",
        "interpretability": "medium",
        "typical_use_case": (
            "General-purpose tabular classification and regression. "
            "Works well across a wide range of dataset sizes and feature types "
            "with minimal preprocessing."
        ),
        "description": (
            "Tree ensemble methods combine many individual decision trees to produce "
            "a stronger, more robust predictor. Random forests use bagging: each tree "
            "is trained on a bootstrap sample with random feature subsets at each split, "
            "reducing variance without significantly increasing bias. Extra-trees add "
            "further randomness by selecting split thresholds at random. Ensembles of "
            "this type are resistant to overfitting, handle mixed feature types well, "
            "and provide feature importance estimates, making them a reliable baseline "
            "for tabular data."
        ),
    },
    "decision_tree": {
        "display_name": "Decision trees",
        "paradigm": "tree",
        "interpretability": "high",
        "typical_use_case": (
            "Tasks requiring transparent, rule-based predictions that practitioners "
            "can inspect and explain directly, such as medical decision support or "
            "regulatory compliance settings."
        ),
        "description": (
            "A single decision tree partitions the feature space by recursively "
            "selecting the split that best separates the target classes (e.g. by "
            "information gain or Gini impurity). The resulting model is a binary tree "
            "of if-then rules that is straightforward to visualise and interpret. "
            "Decision trees are prone to overfitting on training data unless pruning "
            "or depth limits are applied, and they can be unstable: small changes to "
            "training data may produce structurally different trees. Despite these "
            "limitations, their interpretability makes them useful as baselines and "
            "in ensemble building blocks."
        ),
    },
    "gradient_boosting": {
        "display_name": "Gradient boosting",
        "paradigm": "ensemble_meta",
        "interpretability": "low",
        "typical_use_case": (
            "Structured/tabular data competitions and production models where "
            "predictive accuracy is the primary goal, particularly when the dataset "
            "is large enough to support many boosting rounds."
        ),
        "description": (
            "Gradient boosting builds an additive model in a stage-wise fashion: "
            "each new weak learner (typically a shallow decision tree) is trained to "
            "predict the negative gradient of the loss function on the residuals left "
            "by the current ensemble. Implementations such as XGBoost, LightGBM, and "
            "CatBoost add regularisation, second-order gradient approximations, and "
            "specialised data structures to achieve high throughput on large datasets. "
            "Gradient boosting consistently achieves state-of-the-art results on "
            "tabular benchmarks but is sensitive to hyperparameters and can overfit "
            "without careful tuning."
        ),
    },
    "linear": {
        "display_name": "Linear models",
        "paradigm": "linear",
        "interpretability": "high",
        "typical_use_case": (
            "Baseline classification or regression on linearly separable problems, "
            "high-dimensional sparse text data, or settings where model coefficients "
            "must be auditable."
        ),
        "description": (
            "Linear models learn a weighted combination of input features, with the "
            "decision boundary (for classification) or prediction surface (for "
            "regression) constrained to be a hyperplane. Logistic regression, linear "
            "SVM, ridge regression, and perceptrons all belong to this family. "
            "Regularisation (L1 or L2) controls model complexity and can induce "
            "sparsity. Linear models are fast to train and to score, scale to very "
            "high feature counts, and produce directly interpretable coefficients, "
            "but cannot capture non-linear interactions without explicit feature "
            "engineering."
        ),
    },
    "svm": {
        "display_name": "Support vector machines",
        "paradigm": "distance",
        "interpretability": "low",
        "typical_use_case": (
            "Medium-sized datasets with high-dimensional features, image kernels, "
            "or tasks where maximising the margin between classes is desirable."
        ),
        "description": (
            "Support vector machines find the maximum-margin hyperplane separating "
            "two classes in a (possibly kernel-transformed) feature space. The kernel "
            "trick allows SVMs to learn non-linear decision boundaries by implicitly "
            "mapping inputs into high-dimensional spaces via dot-product kernels such "
            "as the radial basis function (RBF) or polynomial kernel. Training "
            "complexity is quadratic to cubic in the number of samples, making SVMs "
            "impractical for very large datasets without approximations. Prediction "
            "depends on support vectors only, giving a compact model, though "
            "interpreting the learned representation is not straightforward."
        ),
    },
    "knn": {
        "display_name": "k-nearest neighbours",
        "paradigm": "distance",
        "interpretability": "high",
        "typical_use_case": (
            "Small to medium datasets where local structure matters, or as a "
            "non-parametric baseline when no assumptions can be made about the "
            "functional form of the decision boundary."
        ),
        "description": (
            "k-nearest neighbour methods classify a new instance by a majority vote "
            "among its k closest training examples in feature space, using a distance "
            "metric such as Euclidean or cosine distance. The algorithm is non-parametric "
            "and instance-based: the entire training set is retained and consulted at "
            "prediction time, making inference slow on large datasets. KNN is sensitive "
            "to irrelevant or poorly scaled features and to the choice of k. Its main "
            "advantage is simplicity and the ability to capture arbitrarily complex "
            "local decision boundaries."
        ),
    },
    "neural": {
        "display_name": "Neural networks",
        "paradigm": "neural",
        "interpretability": "low",
        "typical_use_case": (
            "Large datasets where raw features are complex (images, text, audio) or "
            "where non-linear feature interactions are expected. Multi-layer perceptrons "
            "are also used as deep tabular learners."
        ),
        "description": (
            "Neural networks are function approximators composed of stacked layers of "
            "parameterised affine transformations followed by non-linear activations. "
            "The universal approximation theorem guarantees that a sufficiently wide "
            "network can approximate any continuous function; in practice, depth "
            "enables hierarchical feature learning. Training by stochastic gradient "
            "descent with backpropagation is computationally intensive but scales to "
            "billions of parameters. Multilayer perceptrons (MLPs) and feedforward "
            "networks cover the family seen in tabular ML benchmarks. Neural models "
            "typically require more data and tuning than tree-based methods to reach "
            "competitive performance on structured tabular data."
        ),
    },
    "bayes": {
        "display_name": "Bayesian classifiers",
        "paradigm": "probabilistic",
        "interpretability": "high",
        "typical_use_case": (
            "Text classification (naive Bayes), streaming data requiring fast "
            "incremental updates, or any setting where calibrated probability "
            "estimates and transparent probabilistic reasoning are required."
        ),
        "description": (
            "Bayesian classifiers apply Bayes' theorem to compute the posterior "
            "probability of each class given the observed features. Naive Bayes "
            "simplifies the joint likelihood by assuming conditional independence "
            "of features given the class label. Despite this strong assumption, "
            "it performs well in high-dimensional discrete spaces such as bag-of-words "
            "text representations. Gaussian Naive Bayes extends the approach to "
            "continuous features by modelling each feature's class-conditional "
            "distribution as a Gaussian. Bayesian networks drop the independence "
            "assumption and represent dependencies as a directed acyclic graph. "
            "These models are computationally cheap and naturally produce calibrated "
            "probabilities."
        ),
    },
    "other": {
        "display_name": "Other / unclassified",
        "paradigm": "other",
        "interpretability": "medium",
        "typical_use_case": (
            "Catch-all for algorithms that do not map cleanly to any recognised "
            "family based on their OpenML flow name. May include rule-based "
            "learners, meta-learners, or AutoML pipelines."
        ),
        "description": (
            "This family groups algorithms whose OpenML flow name did not match any "
            "of the known pattern rules. It serves as a catch-all category and "
            "may contain rule-based learners, meta-classifiers, AutoML pipelines, "
            "wrapper methods, or experimental flows. The heterogeneous nature of "
            "this group means that aggregate statistics for 'other' algorithms "
            "should be interpreted with caution. Reviewing the constituent flows "
            "on the Dimensions page can help identify candidates for new family "
            "classification rules."
        ),
    },
}


def get_family_row(slug: str) -> dict:
    """Return the full property dict for an AlgorithmFamily node.

    Includes family_id (deterministic INT64 hash) and all curated metadata.
    Falls back to the 'other' entry if the slug is not recognised.
    The description_embedding column is intentionally excluded — it is computed
    and written by the embedding pipeline, not during ingestion.

    Parameters
    ----------
    slug:
        A family slug string, e.g. 'tree_ensemble'.  Unknown slugs fall back
        to the 'other' metadata but retain the requested slug as family_name
        and derive a family_id from it.

    Returns
    -------
    dict with keys: family_id, family_name, display_name, paradigm,
    interpretability, typical_use_case, description.
    """
    meta = FAMILY_METADATA.get(slug, FAMILY_METADATA["other"])
    return {
        "family_id": _family_id(slug),
        "family_name": slug,
        "display_name": meta["display_name"],
        "paradigm": meta["paradigm"],
        "interpretability": meta["interpretability"],
        "typical_use_case": meta["typical_use_case"],
        "description": meta["description"],
    }
