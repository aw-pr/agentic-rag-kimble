"""Tests for src/experiment/budget.py.

Why this exists
---------------
The three budget guards are the Mayor's early-warning system. Each
guard is one function with a clear yes/no contract, so each test
pins one boundary. If the defaults in budget.yaml.example drift from
the BudgetConfig dataclass defaults, test_defaults_match_example
catches it.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from src.experiment.budget import (
    BudgetConfig,
    check_daily_spawns,
    check_phase_wallclock,
    check_subagent_cap,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
BUDGET_EXAMPLE = REPO_ROOT / "runs" / "experiment" / "budget.yaml.example"


# ---- config loading --------------------------------------------------------


def test_defaults_match_example_yaml() -> None:
    """The committed example file is the documented default; the
    dataclass defaults must match it byte-for-byte so a user copying
    the example gets exactly the behaviour the dataclass describes."""
    documented = yaml.safe_load(BUDGET_EXAMPLE.read_text())
    code_defaults = BudgetConfig()
    for key, value in documented.items():
        assert getattr(code_defaults, key) == value, key


def test_load_partial_yaml_falls_back_to_defaults(tmp_path: Path) -> None:
    """A budget.yaml that only overrides one field keeps defaults for
    the rest. Lets the user edit one knob without copying the whole file."""
    path = tmp_path / "budget.yaml"
    path.write_text("max_parallel_workers: 5\n")
    cfg = BudgetConfig.load(path)
    assert cfg.max_parallel_workers == 5
    assert cfg.per_task_subagent_cap == BudgetConfig().per_task_subagent_cap


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    """No budget.yaml is a normal cold-start condition."""
    cfg = BudgetConfig.load(tmp_path / "absent.yaml")
    assert cfg == BudgetConfig()


def test_load_ignores_unknown_keys(tmp_path: Path) -> None:
    """Forward-compat: a budget.yaml from a newer scaffold version with
    extra keys should still load on the current version, ignoring
    fields we don't recognise."""
    path = tmp_path / "budget.yaml"
    path.write_text("max_parallel_workers: 4\nfuture_key: 99\n")
    cfg = BudgetConfig.load(path)
    assert cfg.max_parallel_workers == 4


def test_budget_config_is_immutable() -> None:
    """Frozen so the tick cannot accidentally mutate budgets mid-run."""
    cfg = BudgetConfig()
    try:
        cfg.per_task_subagent_cap = 999  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("BudgetConfig should be frozen")


# ---- subagent cap ----------------------------------------------------------


def test_subagent_cap_ok_at_cap() -> None:
    """Equal to cap is still ok; only strictly over trips."""
    cfg = BudgetConfig(per_task_subagent_cap=3)
    assert check_subagent_cap(3, cfg).ok


def test_subagent_cap_trips_above_cap() -> None:
    """One over the cap trips. Message names the cap so triage can
    read the verdict and decide whether to raise it."""
    cfg = BudgetConfig(per_task_subagent_cap=3)
    v = check_subagent_cap(4, cfg)
    assert not v.ok
    assert "4" in v.message and "3" in v.message


# ---- phase wallclock -------------------------------------------------------


def test_phase_overspend_ok_under_multiplier() -> None:
    """Active hours at exactly the multiplier are still ok; only over
    the limit trips."""
    cfg = BudgetConfig(phase_overspend_multiplier=2.0)
    assert check_phase_wallclock(
        llm_active_hours=12.0, expected_llm_hours=6.0, cfg=cfg
    ).ok


def test_phase_overspend_trips_above_multiplier() -> None:
    """Above 2x expected trips with a message that names both numbers."""
    cfg = BudgetConfig(phase_overspend_multiplier=2.0)
    v = check_phase_wallclock(
        llm_active_hours=13.0, expected_llm_hours=6.0, cfg=cfg
    )
    assert not v.ok
    assert "13" in v.message and "6" in v.message


# ---- daily spawns ----------------------------------------------------------


def test_daily_spawns_ok_at_threshold() -> None:
    """At the threshold is still ok; strictly over trips."""
    cfg = BudgetConfig(daily_spawn_threshold=50)
    assert check_daily_spawns(50, cfg).ok


def test_daily_spawns_trips_above_threshold() -> None:
    """One above the rolling-24h threshold trips a visibility warning;
    the tick does not auto-pause for this guard (warning only)."""
    cfg = BudgetConfig(daily_spawn_threshold=50)
    v = check_daily_spawns(51, cfg)
    assert not v.ok
    assert "51" in v.message
