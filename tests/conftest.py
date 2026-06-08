"""Shared pytest fixtures for the whole test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_cost_log(tmp_path, monkeypatch):
    """Redirect the agent cost log to a tmp file for every test.

    `run_query` writes one telemetry line per call via cost_log.log_response.
    Without this, unit runs append fixture queries to the real
    `runs/cost-log.jsonl`, polluting the experiment telemetry.
    """
    monkeypatch.setenv("COST_LOG_PATH", str(tmp_path / "cost-log.jsonl"))
