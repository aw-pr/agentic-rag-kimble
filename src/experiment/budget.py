"""Budget guard configuration and evaluation.

Why this exists
---------------
The experiment scaffold runs on subscription routes (Claude Max +
ChatGPT) so there is no per-token bill to cap. The guards in this
module exist to catch a misbehaving *task* before it wedges the
quota: runaway recursion, a phase that has overstayed its budget,
or a 24h burn rate that looks unhealthy. All three are warnings
or soft brakes, never silent hard kills — section F of the plan.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class BudgetConfig:
    """Defaults match runs/experiment/budget.yaml.example. The dataclass
    is frozen so the tick cannot accidentally mutate budgets mid-run."""

    per_task_subagent_cap: int = 3
    daily_spawn_threshold: int = 50
    phase_overspend_multiplier: float = 2.0
    max_parallel_workers: int = 3
    worker_stall_minutes: int = 12
    tick_interval_minutes: int = 5

    @classmethod
    def load(cls, path: Path) -> "BudgetConfig":
        """Read from YAML. Missing keys fall back to dataclass defaults
        so a partial budget.yaml is valid."""
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text()) or {}
        fields = {f for f in cls.__dataclass_fields__}
        known = {k: v for k, v in raw.items() if k in fields}
        return cls(**known)


@dataclass(frozen=True)
class BudgetVerdict:
    """Output of evaluating a single guard. `ok=False` means the Mayor
    should flag this and (for soft brakes) the tick should pause new
    spawns until acknowledged."""

    ok: bool
    name: str
    message: str


def check_subagent_cap(observed: int, cfg: BudgetConfig) -> BudgetVerdict:
    """Per-task runaway-subagent guard. Caller passes the count of
    *nested* subagents the worker spawned during this run."""
    if observed > cfg.per_task_subagent_cap:
        return BudgetVerdict(
            ok=False,
            name="per_task_subagent_cap",
            message=(
                f"runaway subagents: observed {observed} > "
                f"cap {cfg.per_task_subagent_cap}"
            ),
        )
    return BudgetVerdict(ok=True, name="per_task_subagent_cap", message="ok")


def check_phase_wallclock(
    llm_active_hours: float, expected_llm_hours: float, cfg: BudgetConfig
) -> BudgetVerdict:
    """Per-phase overspend guard. Trips at multiplier * expected."""
    limit = expected_llm_hours * cfg.phase_overspend_multiplier
    if llm_active_hours > limit:
        return BudgetVerdict(
            ok=False,
            name="phase_overspend",
            message=(
                f"phase LLM-active {llm_active_hours:.1f}h > "
                f"{cfg.phase_overspend_multiplier:.1f}x expected "
                f"({expected_llm_hours:.1f}h, limit {limit:.1f}h)"
            ),
        )
    return BudgetVerdict(ok=True, name="phase_overspend", message="ok")


def check_daily_spawns(rolling_24h_spawns: int, cfg: BudgetConfig) -> BudgetVerdict:
    """Rolling-24h visibility threshold. Pure warning, no automatic
    action — the Mayor surfaces it; the tick keeps running."""
    if rolling_24h_spawns > cfg.daily_spawn_threshold:
        return BudgetVerdict(
            ok=False,
            name="daily_spawns",
            message=(
                f"burning fast: {rolling_24h_spawns} spawns in last 24h "
                f"> threshold {cfg.daily_spawn_threshold}"
            ),
        )
    return BudgetVerdict(ok=True, name="daily_spawns", message="ok")
