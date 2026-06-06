#!/usr/bin/env python3
"""
Pass-15 end-to-end spike: LadybugDB native vector index.

Creates a tempdir DB, inserts 20 fake Algorithm rows with descriptions,
calls VectorStore.index_entities(), then searches and asserts the expected
item is in the top 3.

Usage: python3 scripts/spike-pass15-vec-e2e.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Add repo root to path when run as a script.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import Config
from src.graph.db import GraphDB
from src.retrieval.embedder import Embedder
from src.retrieval.vector_store import VectorStore

# ── 20 fake Algorithm rows ─────────────────────────────────────────────────────
FAKE_ALGORITHMS = [
    (1,  "RandomForestClassifier",  "A tree ensemble that builds many decision trees and aggregates their predictions via majority vote."),
    (2,  "GradientBoostingClassifier", "Sequential ensemble that fits decision trees to residuals using gradient descent on the loss function."),
    (3,  "SVC",                     "Support vector machine classifier with kernel trick for non-linear decision boundaries."),
    (4,  "LogisticRegression",      "Linear model that estimates class probabilities via a sigmoid function and cross-entropy loss."),
    (5,  "KNeighborsClassifier",    "Instance-based learner that classifies by majority vote of the k nearest training examples."),
    (6,  "MLPClassifier",           "Feed-forward neural network with backpropagation for multi-class classification."),
    (7,  "AdaBoostClassifier",      "Adaptive boosting ensemble that re-weights misclassified samples across weak learners."),
    (8,  "ExtraTreesClassifier",    "Extremely randomised tree ensemble that uses random splits for speed and regularisation."),
    (9,  "BaggingClassifier",       "Bootstrap aggregating ensemble that trains base classifiers on random subsets of data."),
    (10, "DecisionTreeClassifier",  "Single decision tree grown by recursive partitioning using Gini impurity or information gain."),
    (11, "NaiveBayes",              "Probabilistic classifier based on Bayes theorem with strong feature independence assumption."),
    (12, "XGBoostClassifier",       "Optimised gradient boosting with regularisation, parallel tree construction, and shrinkage."),
    (13, "LightGBMClassifier",      "Gradient boosting using histogram-based splits and leaf-wise tree growth for speed."),
    (14, "CatBoostClassifier",      "Gradient boosting with native categorical feature handling and ordered boosting."),
    (15, "RidgeClassifier",         "Linear classifier with L2 regularisation, equivalent to ridge regression on one-hot labels."),
    (16, "SGDClassifier",           "Linear classifier trained with stochastic gradient descent — fast on very large datasets."),
    (17, "PassiveAggressiveClassifier", "Online linear classifier that aggressively corrects only on misclassified examples."),
    (18, "LinearDiscriminantAnalysis", "Dimensionality reduction and linear classification assuming equal covariance per class."),
    (19, "QuadraticDiscriminantAnalysis", "Quadratic classifier that fits class-conditional Gaussians with separate covariances."),
    (20, "GaussianProcessClassifier", "Bayesian non-parametric classifier using Gaussian process priors over latent functions."),
]


def main():
    with tempfile.TemporaryDirectory(prefix="lb-vec-spike-pass15-") as tmp:
        tmp_path = Path(tmp)
        cfg = Config(ladybug_db_path=tmp_path / "ladybug_db")

        print(f"Tempdir: {tmp_path}")

        # ── 1. Create schema ───────────────────────────────────────────────
        print("Step 1: creating schema...")
        with GraphDB(cfg) as db:
            db.initialise_schema()
            print(f"  Schema created. Node count Algorithm={db.node_count('Algorithm')}")

            # ── 2. Insert 20 fake Algorithm rows ───────────────────────────
            print("Step 2: inserting 20 fake Algorithm rows...")
            for flow_id, name, description in FAKE_ALGORITHMS:
                db.execute_write(
                    f"CREATE (a:Algorithm {{flow_id: {flow_id}, name: '{name}', "
                    f"display_name: '{name}', family: 'test', "
                    f"description: \"{description}\", description_embedding: null}})"
                )
            print(f"  Inserted. Node count Algorithm={db.node_count('Algorithm')}")

        # ── 3. Index entities via VectorStore ──────────────────────────────
        print("Step 3: indexing via VectorStore.index_entities()...")
        embedder = Embedder(cfg)
        store = VectorStore(cfg, embedder)
        store.connect()

        entities = [
            {"flow_id": fid, "description": desc, "name": name}
            for fid, name, desc in FAKE_ALGORITHMS
        ]
        store.index_entities("Algorithm", entities)
        count = store.collection_count("Algorithm")
        print(f"  Indexed. collection_count={count}")
        assert count == 20, f"Expected 20, got {count}"

        # ── 4. Search ──────────────────────────────────────────────────────
        print("Step 4: searching for 'random forest tree ensemble'...")
        results = store.search("Algorithm", "random forest tree ensemble", top_k=5)
        print(f"  Top 5 results:")
        for i, r in enumerate(results[:5], 1):
            print(f"    {i}. {r['name']!r}  score={r['score']:.4f}")

        # ── 5. Assert RandomForestClassifier in top 3 ─────────────────────
        top3_names = [r["name"] for r in results[:3]]
        assert "RandomForestClassifier" in top3_names, (
            f"Expected 'RandomForestClassifier' in top 3, got: {top3_names}"
        )
        print(f"\nAssertion passed: 'RandomForestClassifier' found in top 3.")

        # ── Bonus: test a second query ─────────────────────────────────────
        print("\nBonus: searching for 'support vector machine kernel'...")
        svm_results = store.search("Algorithm", "support vector machine kernel", top_k=5)
        for i, r in enumerate(svm_results[:3], 1):
            print(f"    {i}. {r['name']!r}  score={r['score']:.4f}")
        top3_svm = [r["name"] for r in svm_results[:3]]
        assert "SVC" in top3_svm, f"Expected 'SVC' in top 3, got: {top3_svm}"
        print("Assertion passed: 'SVC' found in top 3.")

        print("\nSpike PASSED — LadybugDB native vector index works end-to-end.")


if __name__ == "__main__":
    main()
