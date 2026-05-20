"""
Unit tests for src/ingestion/transform.py.

No network, no DB. All functions are pure transforms.
"""

from __future__ import annotations

import pytest

from src.ingestion.openml_fetch import FetchedDataset, FetchedRun
from src.ingestion.transform import (
    _dataset_dim_label,
    _dataset_imbalance_label,
    # Thin aliases — must still be importable for backwards compatibility.
    _dataset_size_label,
    derive_algorithm_family,
    derive_date_row,
    derive_dim_bucket,
    derive_display_name,
    derive_imbalance_bucket,
    derive_is_ensemble,
    derive_paradigm,
    derive_size_bucket,
    derive_training_cost_class,
    extract_accuracy,
    synthesise_description,
    transform_algorithm,
    transform_dataset,
    transform_run,
    transform_task,
)

# ── derive_display_name ──────────────────────────────────────────────────────


def test_derive_display_name_sklearn_fqcn():
    assert (
        derive_display_name("sklearn.ensemble.forest.RandomForestClassifier")
        == "RandomForestClassifier"
    )


def test_derive_display_name_weka_short():
    assert derive_display_name("weka.classifiers.trees.RandomForest") == "RandomForest"


def test_derive_display_name_two_part():
    assert derive_display_name("classif.boosting") == "boosting"


def test_derive_display_name_pipeline_with_args():
    fqcn = (
        "sklearn.pipeline.Pipeline(imputation=foo,"
        "classifier=sklearn.ensemble.weight_boosting.AdaBoostClassifier(base_estimator=bar))"
    )
    assert derive_display_name(fqcn) == "Pipeline"


def test_derive_display_name_weka_libsvm():
    assert derive_display_name("weka.LibSVM") == "LibSVM"


def test_derive_display_name_mlr_dotted():
    assert derive_display_name("mlr.classif.kknn") == "kknn"


def test_derive_display_name_compound_no_underscore():
    assert derive_display_name("weka.MultilayerPerceptronCS") == "MultilayerPerceptronCS"


def test_derive_display_name_adaboost():
    assert (
        derive_display_name("sklearn.ensemble.weight_boosting.AdaBoostClassifier")
        == "AdaBoostClassifier"
    )


def test_derive_display_name_underscore_chain_takes_last():
    fqcn = "weka.AttributeSelectedClassifier_CfsSubsetEval_BestFirst_MLPClassifier"
    assert derive_display_name(fqcn) == "MLPClassifier"


# ── derive_algorithm_family ───────────────────────────────────────────────────

def test_derive_algorithm_family_random_forest():
    assert derive_algorithm_family("sklearn.ensemble.RandomForestClassifier") == "tree_ensemble"


def test_derive_algorithm_family_extra_trees():
    assert derive_algorithm_family("sklearn.ensemble.ExtraTreesClassifier") == "tree_ensemble"


def test_derive_algorithm_family_decision_tree():
    assert derive_algorithm_family("sklearn.tree.DecisionTreeClassifier") == "tree_ensemble"


def test_derive_algorithm_family_xgboost():
    assert derive_algorithm_family("XGBClassifier(...)") == "gradient_boosting"


def test_derive_algorithm_family_lightgbm():
    assert derive_algorithm_family("LightGBMClassifier") == "gradient_boosting"


def test_derive_algorithm_family_svm():
    assert derive_algorithm_family("sklearn.svm.SVC") == "svm"


def test_derive_algorithm_family_svr():
    assert derive_algorithm_family("sklearn.svm.SVR") == "svm"


def test_derive_algorithm_family_knn():
    assert derive_algorithm_family("sklearn.neighbors.KNeighborsClassifier") == "knn"


def test_derive_algorithm_family_logistic():
    assert derive_algorithm_family("sklearn.linear_model.LogisticRegression") == "linear"


def test_derive_algorithm_family_linear_svc():
    assert derive_algorithm_family("sklearn.svm.LinearSVC") == "linear"


def test_derive_algorithm_family_mlp():
    assert derive_algorithm_family("sklearn.neural_network.MLPClassifier") == "neural"


def test_derive_algorithm_family_unknown():
    assert derive_algorithm_family("some_weird_custom_thing") == "other"


def test_derive_algorithm_family_gradient_boosting():
    assert (
        derive_algorithm_family("sklearn.ensemble.GradientBoostingClassifier")
        == "gradient_boosting"
    )


# Weka convention — new families added in pass 18

def test_derive_algorithm_family_weka_ibk():
    assert derive_algorithm_family("weka.IBk") == "knn"


def test_derive_algorithm_family_weka_ib1():
    assert derive_algorithm_family("weka.IB1") == "knn"


def test_derive_algorithm_family_weka_multilayerperceptron():
    assert derive_algorithm_family("weka.MultilayerPerceptron") == "neural"


def test_derive_algorithm_family_weka_multilayerperceptroncs():
    assert derive_algorithm_family("weka.MultilayerPerceptronCS") == "neural"


def test_derive_algorithm_family_weka_j48():
    assert derive_algorithm_family("weka.J48") == "decision_tree"


def test_derive_algorithm_family_weka_reptree():
    assert derive_algorithm_family("weka.REPTree") == "decision_tree"


def test_derive_algorithm_family_weka_randomtree():
    assert derive_algorithm_family("weka.RandomTree") == "decision_tree"


def test_derive_algorithm_family_weka_naivebayes():
    assert derive_algorithm_family("weka.NaiveBayes") == "bayes"


def test_derive_algorithm_family_weka_bayesnet():
    assert derive_algorithm_family("weka.BayesNet") == "bayes"


def test_derive_algorithm_family_sklearn_decisiontree_stays_tree_ensemble():
    # sklearn's DecisionTreeClassifier must NOT fall through to decision_tree.
    # The "DecisionTree" token in tree_ensemble fires first.
    assert derive_algorithm_family("sklearn.tree.DecisionTreeClassifier") == "tree_ensemble"


def test_derive_algorithm_family_sklearn_randomforest_unchanged():
    assert derive_algorithm_family("sklearn.ensemble.RandomForestClassifier") == "tree_ensemble"


# ── extract_accuracy ─────────────────────────────────────────────────────────

def test_extract_accuracy_uses_predictive_accuracy():
    assert extract_accuracy({"predictive_accuracy": 0.92, "accuracy": 0.91}) == 0.92


def test_extract_accuracy_falls_back_to_accuracy():
    assert extract_accuracy({"accuracy": 0.88}) == 0.88


def test_extract_accuracy_returns_none_when_absent():
    assert extract_accuracy({"f1": 0.77}) is None


def test_extract_accuracy_empty_dict():
    assert extract_accuracy({}) is None


# ── synthesise_description ───────────────────────────────────────────────────

def test_synthesise_description_algorithm():
    desc = synthesise_description("Algorithm", {"name": "RandomForest", "family": "tree_ensemble"})
    assert "RandomForest" in desc and "tree_ensemble" in desc


def test_synthesise_description_algorithm_knn_has_synonym():
    desc = synthesise_description(
        "Algorithm",
        {"name": "weka.IBk", "display_name": "IBk", "family": "knn"},
    )
    assert "k-nearest neighbour" in desc
    assert "instance-based" in desc
    assert "lazy learner" in desc


def test_synthesise_description_algorithm_svm_has_synonym():
    desc = synthesise_description(
        "Algorithm",
        {"name": "sklearn.svm.SVC", "display_name": "SVC", "family": "svm"},
    )
    assert "support vector machine" in desc
    assert "kernel methods" in desc


def test_synthesise_description_algorithm_tree_ensemble_has_synonym():
    desc = synthesise_description(
        "Algorithm",
        {
            "name": "sklearn.ensemble.RandomForestClassifier",
            "display_name": "RandomForestClassifier",
            "family": "tree_ensemble",
        },
    )
    assert "random forest" in desc
    assert "bagging" in desc


def test_synthesise_description_algorithm_decision_tree_has_synonym():
    desc = synthesise_description(
        "Algorithm",
        {"name": "weka.J48", "display_name": "J48", "family": "decision_tree"},
    )
    assert "decision tree" in desc
    assert "recursive partitioning" in desc


def test_synthesise_description_algorithm_gradient_boosting_has_synonym():
    desc = synthesise_description(
        "Algorithm",
        {"name": "XGBClassifier", "display_name": "XGBClassifier", "family": "gradient_boosting"},
    )
    assert "gradient boosting" in desc
    assert "XGBoost" in desc


def test_synthesise_description_algorithm_linear_has_synonym():
    desc = synthesise_description(
        "Algorithm",
        {
            "name": "sklearn.linear_model.LogisticRegression",
            "display_name": "LogisticRegression",
            "family": "linear",
        },
    )
    assert "logistic regression" in desc
    assert "linear model" in desc


def test_synthesise_description_algorithm_neural_has_synonym():
    desc = synthesise_description(
        "Algorithm",
        {
            "name": "weka.MultilayerPerceptron",
            "display_name": "MultilayerPerceptron",
            "family": "neural",
        },
    )
    assert "neural network" in desc
    assert "multilayer perceptron" in desc


def test_synthesise_description_algorithm_bayes_has_synonym():
    desc = synthesise_description(
        "Algorithm",
        {"name": "weka.NaiveBayes", "display_name": "NaiveBayes", "family": "bayes"},
    )
    assert "naive Bayes" in desc
    assert "Bayesian classifier" in desc


def test_synthesise_description_algorithm_other_no_dash():
    desc = synthesise_description(
        "Algorithm",
        {"name": "some.CustomAlgo", "display_name": "CustomAlgo", "family": "other"},
    )
    # other family has no synonyms — should not have trailing dash
    assert " — " not in desc
    assert "CustomAlgo" in desc


def test_synthesise_description_dataset():
    props = {
        "name": "iris",
        "n_rows": 150,
        "n_features": 4,
        "n_classes": 3,
        "imbalance_ratio": 1.0,
    }
    desc = synthesise_description("Dataset", props)
    assert "iris" in desc
    assert "150" in desc
    assert "4" in desc
    assert "3" in desc


def test_synthesise_description_dataset_small_low_dim_balanced():
    props = {"name": "iris", "n_rows": 150, "n_features": 4, "n_classes": 3, "imbalance_ratio": 1.0}
    desc = synthesise_description("Dataset", props)
    assert "small dataset" in desc.lower()
    assert "low-dimensional" in desc
    assert "balanced classes" in desc


def test_synthesise_description_dataset_large_high_dim_severe_imbalance():
    props = {
        "name": "bigdata",
        "n_rows": 500_000,
        "n_features": 500,
        "n_classes": 2,
        "imbalance_ratio": 10.0,
    }
    desc = synthesise_description("Dataset", props)
    assert "large dataset" in desc.lower()
    assert "high-dimensional" in desc
    assert "severe class imbalance" in desc


def test_synthesise_description_dataset_medium_medium_dim_moderate_imbalance():
    props = {
        "name": "mid",
        "n_rows": 5000,
        "n_features": 50,
        "n_classes": 2,
        "imbalance_ratio": 3.0,
    }
    desc = synthesise_description("Dataset", props)
    assert "medium dataset" in desc.lower()
    assert "medium-dimensional" in desc
    assert "moderately imbalanced" in desc


def test_synthesise_description_task():
    props = {
        "task_type": "Supervised Classification",
        "target_feature": "class",
        "evaluation_measure": "predictive_accuracy",
    }
    desc = synthesise_description("Task", props)
    assert "Supervised Classification" in desc
    assert "class" in desc
    assert "predictive_accuracy" in desc


def test_synthesise_description_unknown_type():
    with pytest.raises(ValueError):
        synthesise_description("Unknown", {})


# ── dataset bucket helpers (private aliases — backwards compatibility) ────────

def test_dataset_size_label_small():
    assert _dataset_size_label(100) == "small dataset"

def test_dataset_size_label_boundary_1000():
    assert _dataset_size_label(999) == "small dataset"
    assert _dataset_size_label(1000) == "medium dataset"

def test_dataset_size_label_medium():
    assert _dataset_size_label(50_000) == "medium dataset"

def test_dataset_size_label_large():
    assert _dataset_size_label(200_000) == "large dataset"

def test_dataset_dim_label_low():
    assert _dataset_dim_label(4) == "low-dimensional"

def test_dataset_dim_label_medium():
    assert _dataset_dim_label(50) == "medium-dimensional"

def test_dataset_dim_label_high():
    assert _dataset_dim_label(500) == "high-dimensional"

def test_dataset_imbalance_label_balanced():
    assert _dataset_imbalance_label(1.0) == "balanced classes"

def test_dataset_imbalance_label_moderate():
    assert _dataset_imbalance_label(3.0) == "moderately imbalanced"

def test_dataset_imbalance_label_severe():
    assert _dataset_imbalance_label(9.0) == "severe class imbalance"


# ── public bucket functions (derive_size_bucket / derive_dim_bucket / etc.) ──

def test_derive_size_bucket_small():
    assert derive_size_bucket(0) == "small dataset"
    assert derive_size_bucket(999) == "small dataset"

def test_derive_size_bucket_boundary_at_1000():
    assert derive_size_bucket(1000) == "medium dataset"

def test_derive_size_bucket_medium():
    assert derive_size_bucket(100_000) == "medium dataset"

def test_derive_size_bucket_boundary_at_100001():
    assert derive_size_bucket(100_001) == "large dataset"

def test_derive_size_bucket_large():
    assert derive_size_bucket(500_000) == "large dataset"

def test_derive_dim_bucket_low():
    assert derive_dim_bucket(1) == "low-dimensional"
    assert derive_dim_bucket(19) == "low-dimensional"

def test_derive_dim_bucket_boundary_at_20():
    assert derive_dim_bucket(20) == "medium-dimensional"

def test_derive_dim_bucket_medium():
    assert derive_dim_bucket(100) == "medium-dimensional"

def test_derive_dim_bucket_boundary_at_101():
    assert derive_dim_bucket(101) == "high-dimensional"

def test_derive_dim_bucket_high():
    assert derive_dim_bucket(1000) == "high-dimensional"

def test_derive_imbalance_bucket_balanced():
    assert derive_imbalance_bucket(1.0) == "balanced classes"
    assert derive_imbalance_bucket(1.499) == "balanced classes"

def test_derive_imbalance_bucket_boundary_at_1_5():
    assert derive_imbalance_bucket(1.5) == "moderately imbalanced"

def test_derive_imbalance_bucket_moderate():
    assert derive_imbalance_bucket(5.0) == "moderately imbalanced"

def test_derive_imbalance_bucket_boundary_at_5_01():
    assert derive_imbalance_bucket(5.01) == "severe class imbalance"

def test_derive_imbalance_bucket_severe():
    assert derive_imbalance_bucket(20.0) == "severe class imbalance"

# Public names are the same objects as the private aliases.
def test_public_bucket_aliases_match_private():
    assert derive_size_bucket is _dataset_size_label
    assert derive_dim_bucket is _dataset_dim_label
    assert derive_imbalance_bucket is _dataset_imbalance_label


# ── derive_paradigm ──────────────────────────────────────────────────────────

def test_derive_paradigm_tree_ensemble():
    assert derive_paradigm("sklearn.ensemble.RandomForestClassifier", "tree_ensemble") == "tree"

def test_derive_paradigm_decision_tree():
    assert derive_paradigm("weka.J48", "decision_tree") == "tree"

def test_derive_paradigm_gradient_boosting():
    assert derive_paradigm("XGBClassifier", "gradient_boosting") == "ensemble_meta"

def test_derive_paradigm_neural():
    assert derive_paradigm("sklearn.neural_network.MLPClassifier", "neural") == "neural"

def test_derive_paradigm_linear():
    assert derive_paradigm("sklearn.linear_model.LogisticRegression", "linear") == "linear"

def test_derive_paradigm_svm():
    assert derive_paradigm("sklearn.svm.SVC", "svm") == "distance"

def test_derive_paradigm_knn():
    assert derive_paradigm("sklearn.neighbors.KNeighborsClassifier", "knn") == "distance"

def test_derive_paradigm_bayes():
    assert derive_paradigm("weka.NaiveBayes", "bayes") == "probabilistic"

def test_derive_paradigm_other():
    assert derive_paradigm("some.CustomThing", "other") == "other"

def test_derive_paradigm_automl_from_flow_name():
    assert derive_paradigm("auto-sklearn.AutoSklearnClassifier", "other") == "automl"

def test_derive_paradigm_automl_overrides_family():
    # Even if family is known, "auto" in flow_name wins.
    assert derive_paradigm("autoweka.AutoWEKAClassifier", "tree_ensemble") == "automl"


# ── derive_is_ensemble ───────────────────────────────────────────────────────

def test_derive_is_ensemble_tree_ensemble():
    assert derive_is_ensemble("sklearn.ensemble.RandomForestClassifier", "tree_ensemble") is True

def test_derive_is_ensemble_gradient_boosting():
    assert derive_is_ensemble("XGBClassifier", "gradient_boosting") is True

def test_derive_is_ensemble_automl():
    assert derive_is_ensemble("auto-sklearn.AutoSklearnClassifier", "other") is True

def test_derive_is_ensemble_linear_false():
    assert derive_is_ensemble("sklearn.linear_model.LogisticRegression", "linear") is False

def test_derive_is_ensemble_knn_false():
    assert derive_is_ensemble("weka.IBk", "knn") is False

def test_derive_is_ensemble_neural_false():
    assert derive_is_ensemble("sklearn.neural_network.MLPClassifier", "neural") is False

def test_derive_is_ensemble_svm_false():
    assert derive_is_ensemble("sklearn.svm.SVC", "svm") is False

def test_derive_is_ensemble_bayes_false():
    assert derive_is_ensemble("weka.NaiveBayes", "bayes") is False

def test_derive_is_ensemble_decision_tree_false():
    assert derive_is_ensemble("weka.J48", "decision_tree") is False


# ── derive_training_cost_class ────────────────────────────────────────────────

def test_derive_training_cost_class_linear_cheap():
    assert (
        derive_training_cost_class("sklearn.linear_model.LogisticRegression", "linear")
        == "cheap"
    )

def test_derive_training_cost_class_knn_cheap():
    assert derive_training_cost_class("weka.IBk", "knn") == "cheap"

def test_derive_training_cost_class_bayes_cheap():
    assert derive_training_cost_class("weka.NaiveBayes", "bayes") == "cheap"

def test_derive_training_cost_class_decision_tree_moderate():
    assert derive_training_cost_class("weka.J48", "decision_tree") == "moderate"

def test_derive_training_cost_class_svm_moderate():
    assert derive_training_cost_class("sklearn.svm.SVC", "svm") == "moderate"

def test_derive_training_cost_class_tree_ensemble_expensive():
    assert (
        derive_training_cost_class("sklearn.ensemble.RandomForestClassifier", "tree_ensemble")
        == "expensive"
    )

def test_derive_training_cost_class_gradient_boosting_expensive():
    assert derive_training_cost_class("XGBClassifier", "gradient_boosting") == "expensive"

def test_derive_training_cost_class_neural_expensive():
    assert (
        derive_training_cost_class("sklearn.neural_network.MLPClassifier", "neural")
        == "expensive"
    )

def test_derive_training_cost_class_automl_expensive():
    assert derive_training_cost_class("auto-sklearn.AutoSklearnClassifier", "other") == "expensive"

def test_derive_training_cost_class_unknown_defaults_moderate():
    assert derive_training_cost_class("some.WeirdThing", "other") == "moderate"


# ── transform_dataset now includes bucket columns ────────────────────────────

def test_transform_dataset_includes_bucket_columns():
    fetched = FetchedDataset(
        dataset_id=99,
        name="testset",
        qualities={
            "NumberOfInstances": 500.0,
            "NumberOfFeatures": 10.0,
            "NumberOfClasses": 2.0,
            "MajorityClassSize": 400.0,
            "MinorityClassSize": 100.0,
        },
    )
    result = transform_dataset(fetched)
    assert result["size_bucket"] == "small dataset"
    assert result["dim_bucket"] == "low-dimensional"
    assert result["imbalance_bucket"] == "moderately imbalanced"


# ── transform_algorithm now includes paradigm / is_ensemble / cost ───────────

def test_transform_algorithm_includes_paradigm():
    algo = transform_algorithm(1, "sklearn.ensemble.RandomForestClassifier")
    assert algo["paradigm"] == "tree"

def test_transform_algorithm_includes_is_ensemble_true():
    algo = transform_algorithm(2, "sklearn.ensemble.RandomForestClassifier")
    assert algo["is_ensemble"] is True

def test_transform_algorithm_includes_is_ensemble_false():
    algo = transform_algorithm(3, "sklearn.linear_model.LogisticRegression")
    assert algo["is_ensemble"] is False

def test_transform_algorithm_includes_training_cost_class():
    algo = transform_algorithm(4, "XGBClassifier")
    assert algo["training_cost_class"] == "expensive"


# ── transform_dataset ────────────────────────────────────────────────────────

def test_transform_dataset_maps_qualities():
    fetched = FetchedDataset(
        dataset_id=1,
        name="iris",
        qualities={
            "NumberOfInstances": 150.0,
            "NumberOfFeatures": 4.0,
            "NumberOfClasses": 3.0,
            "MajorityClassSize": 50.0,
            "MinorityClassSize": 50.0,
        },
    )
    result = transform_dataset(fetched)
    assert result["dataset_id"] == 1
    assert result["name"] == "iris"
    assert result["n_rows"] == 150
    assert result["n_features"] == 4
    assert result["n_classes"] == 3
    assert result["imbalance_ratio"] == 1.0
    assert "description" in result
    assert "iris" in result["description"]


def test_transform_dataset_casts_to_int():
    fetched = FetchedDataset(
        dataset_id=42,
        name="test",
        qualities={
            "NumberOfInstances": 1000.0,
            "NumberOfFeatures": 10.0,
            "NumberOfClasses": 2.0,
        },
    )
    result = transform_dataset(fetched)
    assert isinstance(result["n_rows"], int)
    assert isinstance(result["n_features"], int)
    assert isinstance(result["n_classes"], int)
    assert result["n_rows"] == 1000


def test_transform_dataset_imbalance_default():
    """When MajorityClassSize/MinorityClassSize are absent, default to 1.0."""
    fetched = FetchedDataset(
        dataset_id=2,
        name="noclass",
        qualities={"NumberOfInstances": 500.0, "NumberOfFeatures": 5.0},
    )
    result = transform_dataset(fetched)
    assert result["imbalance_ratio"] == 1.0


def test_transform_dataset_imbalance_ratio_calculated():
    fetched = FetchedDataset(
        dataset_id=3,
        name="imbalanced",
        qualities={
            "NumberOfInstances": 100.0,
            "NumberOfFeatures": 5.0,
            "NumberOfClasses": 2.0,
            "MajorityClassSize": 90.0,
            "MinorityClassSize": 10.0,
        },
    )
    result = transform_dataset(fetched)
    assert result["imbalance_ratio"] == pytest.approx(9.0)


# ── transform_run / transform_algorithm ─────────────────────────────────────

def test_transform_run_returns_tuple():
    fetched = FetchedRun(
        run_id=100,
        setup_id=5,
        flow_id=77,
        dataset_id=1,
        task_id=10,
        evaluations={"predictive_accuracy": 0.95},
    )
    run_dict, algo_dict = transform_run(fetched, "sklearn.ensemble.RandomForestClassifier")
    assert run_dict["run_id"] == 100
    assert run_dict["accuracy"] == pytest.approx(0.95)
    assert run_dict["setup_id"] == 5
    assert algo_dict["flow_id"] == 77
    assert algo_dict["family"] == "tree_ensemble"


def test_transform_run_missing_accuracy_is_none():
    fetched = FetchedRun(
        run_id=200,
        setup_id=0,
        flow_id=99,
        dataset_id=1,
        task_id=10,
        evaluations={"f1": 0.8},
    )
    run_dict, _ = transform_run(fetched, "some_flow")
    assert run_dict["accuracy"] is None


def test_transform_algorithm_family():
    algo = transform_algorithm(77, "XGBClassifier")
    assert algo["flow_id"] == 77
    assert algo["family"] == "gradient_boosting"
    assert "description" in algo


# ── transform_task ───────────────────────────────────────────────────────────

def test_transform_task_fields():
    result = transform_task(
        task_id=10,
        task_type="Supervised Classification",
        target_feature="class",
        evaluation_measure="predictive_accuracy",
    )
    assert result["task_id"] == 10
    assert result["task_type"] == "Supervised Classification"
    assert result["target_feature"] == "class"
    assert result["evaluation_measure"] == "predictive_accuracy"
    assert "description" in result
    assert "class" in result["description"]


# ── derive_date_row ───────────────────────────────────────────────────────────

def test_derive_date_row_iso_string():
    row = derive_date_row("2024-03-15")
    assert row is not None
    assert row["date_id"] == 20240315
    assert row["date"] == "2024-03-15"
    assert row["year"] == 2024
    assert row["month"] == 3
    assert row["month_name"] == "March"
    assert row["quarter"] == "2024-Q1"


def test_derive_date_row_datetime_with_time_component():
    row = derive_date_row("2023-07-04 12:34:56")
    assert row is not None
    assert row["date_id"] == 20230704
    assert row["year"] == 2023
    assert row["month"] == 7
    assert row["quarter"] == "2023-Q3"


def test_derive_date_row_datetime_object():
    from datetime import datetime
    row = derive_date_row(datetime(2021, 1, 10, 8, 0, 0))
    assert row is not None
    assert row["date_id"] == 20210110
    assert row["year"] == 2021
    assert row["month"] == 1
    assert row["month_name"] == "January"


def test_derive_date_row_date_object():
    from datetime import date
    row = derive_date_row(date(2020, 6, 6))
    assert row is not None
    assert row["date_id"] == 20200606
    assert row["year"] == 2020


def test_derive_date_row_none_returns_none():
    assert derive_date_row(None) is None


def test_derive_date_row_malformed_string_returns_none():
    assert derive_date_row("not-a-date") is None


def test_derive_date_row_empty_string_returns_none():
    assert derive_date_row("") is None


def test_derive_date_row_weekend():
    # 2024-03-16 is a Saturday
    row = derive_date_row("2024-03-16")
    assert row is not None
    assert row["is_weekend"] is True
    assert row["day_of_week"] == "Saturday"


def test_derive_date_row_weekday():
    # 2024-03-15 is a Friday
    row = derive_date_row("2024-03-15")
    assert row is not None
    assert row["is_weekend"] is False
    assert row["day_of_week"] == "Friday"


def test_derive_date_row_sunday_is_weekend():
    # 2024-03-17 is a Sunday
    row = derive_date_row("2024-03-17")
    assert row is not None
    assert row["is_weekend"] is True
    assert row["day_of_week"] == "Sunday"


def test_derive_date_row_quarter_boundary_march_31_is_q1():
    row = derive_date_row("2024-03-31")
    assert row is not None
    assert row["quarter"] == "2024-Q1"


def test_derive_date_row_quarter_boundary_april_1_is_q2():
    row = derive_date_row("2024-04-01")
    assert row is not None
    assert row["quarter"] == "2024-Q2"


def test_derive_date_row_quarter_july_is_q3():
    row = derive_date_row("2022-07-15")
    assert row is not None
    assert row["quarter"] == "2022-Q3"


def test_derive_date_row_quarter_october_is_q4():
    row = derive_date_row("2022-10-01")
    assert row is not None
    assert row["quarter"] == "2022-Q4"


def test_derive_date_row_returns_all_required_keys():
    row = derive_date_row("2023-11-22")
    assert row is not None
    required = {
        "date_id", "date", "year", "quarter", "month", "month_name", "day_of_week", "is_weekend"
    }
    assert required <= set(row.keys())
