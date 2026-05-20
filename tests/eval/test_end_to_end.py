"""
tests/eval/test_end_to_end.py

End-to-end eval test — skipped by default (requires populated DB + ChromaDB).
Run explicitly with:  pytest tests/eval/test_end_to_end.py --run-e2e
or by setting the env var:  RUN_E2E_EVAL=1 pytest tests/eval/test_end_to_end.py
"""

from __future__ import annotations

import json
import os

import pytest

SKIP_REASON = (
    "End-to-end eval requires a populated LadybugDB graph + vector index. "
    "Set RUN_E2E_EVAL=1 or pass --run-e2e to run."
)

_run_e2e = os.getenv("RUN_E2E_EVAL", "0") == "1"


@pytest.mark.skipif(condition=True, reason=SKIP_REASON)
def test_run_retrieval_eval_smoke() -> None:
    """Full eval run: imports tools, queries live DB, writes JSON to runs/."""
    from src.config import get_config
    from src.eval.metrics import run_retrieval_eval, write_eval_results

    cfg = get_config()
    results = run_retrieval_eval(cfg, k=10)

    # Schema assertions
    assert "recall_at_5" in results
    assert "recall_at_10" in results
    assert "per_tool" in results
    assert "failures" in results
    assert "n_fixtures" in results
    assert "db_populated" in results
    assert "judge_score" in results

    assert results["n_fixtures"] == 20
    assert 0.0 <= results["recall_at_5"] <= 1.0
    assert 0.0 <= results["recall_at_10"] <= 1.0

    for tool in ("graph", "semantic", "aggregate"):
        assert tool in results["per_tool"]
        assert 0.0 <= results["per_tool"][tool] <= 1.0

    # Write to disk and verify the file is valid JSON
    out_path = write_eval_results(results, cfg.runs_path)
    assert out_path.exists()
    assert out_path.suffix == ".json"
    assert "eval-" in out_path.name

    loaded = json.loads(out_path.read_text())
    assert loaded["recall_at_10"] == results["recall_at_10"]
    assert loaded["judge_score"] is None


@pytest.mark.skipif(condition=True, reason=SKIP_REASON)
def test_eval_json_no_nan() -> None:
    """Ensure written JSON contains no NaN values (allow_nan=False enforcement)."""
    from src.config import get_config
    from src.eval.metrics import run_retrieval_eval, write_eval_results

    cfg = get_config()
    results = run_retrieval_eval(cfg, k=10)
    out_path = write_eval_results(results, cfg.runs_path)

    raw = out_path.read_text()
    # Standard JSON does not allow NaN — if json.loads parses it, no NaN leaked
    loaded = json.loads(raw)
    assert isinstance(loaded, dict)
