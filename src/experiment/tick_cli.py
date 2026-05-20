"""Command-line entry point for the heartbeat tick.

Invoked by scripts/experiment-tick.sh. Thin wrapper: parses args,
calls tick(), writes a one-line summary to the events log, prints
JSON summary on stdout for the dashboard to scrape.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from src.experiment.tick import tick


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Experiment heartbeat tick.")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--exp-dir", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    args = parser.parse_args(argv)

    report = tick(
        state_path=args.state,
        budget_path=args.budget,
        exp_dir=args.exp_dir,
    )

    # One-line summary to the events log, JSON dump to stdout.
    summary = (
        f"spawned={len(report.spawned)} "
        f"completed={len(report.completed)} "
        f"blocked={len(report.blocked)} "
        f"paused={report.paused} "
        f"stopped={report.stopped} "
        f"errors={len(report.errors)}"
    )
    with args.events.open("a") as f:
        f.write(f"  summary {summary}\n")
        for line in report.transitions:
            f.write(f"  {line}\n")
    json.dump(asdict(report), sys.stdout, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
