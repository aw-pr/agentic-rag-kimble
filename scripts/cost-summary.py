#!/usr/bin/env python3
"""
cost-summary.py — roll up `runs/cost-log.jsonl` into a human report.

Usage:
    python3 scripts/cost-summary.py                  # full log
    python3 scripts/cost-summary.py --last 20        # last N queries
    python3 scripts/cost-summary.py --since 2026-05  # ISO-prefix filter on ts
    python3 scripts/cost-summary.py --json           # machine-readable dump
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_LOG = Path("runs/cost-log.jsonl")


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        sys.exit(f"No cost log found at {path}. Run a query first.")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rows.append(json.loads(raw))
            except json.JSONDecodeError:
                # Skip malformed lines rather than die on a corrupted append.
                continue
    return rows


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"queries": 0}

    in_toks = [r["usage"].get("input_tokens", 0) for r in rows if r.get("usage")]
    out_toks = [r["usage"].get("output_tokens", 0) for r in rows if r.get("usage")]
    cache_read = [r["usage"].get("cache_read_input_tokens", 0) for r in rows if r.get("usage")]
    durations = [r.get("duration_ms", 0) for r in rows]
    turns = [r.get("num_turns", 0) for r in rows]
    tool_counts = [r.get("total_tool_calls", 0) for r in rows]

    # Per-tool aggregates
    tool_totals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "durations": []}
    )
    for r in rows:
        for tc in r.get("tool_calls") or []:
            name = tc.get("name") or "?"
            tool_totals[name]["calls"] += 1
            d = tc.get("duration_ms")
            if d is not None:
                tool_totals[name]["durations"].append(d)

    tools_report = {}
    for name, agg in tool_totals.items():
        ds = agg["durations"]
        tools_report[name] = {
            "calls": agg["calls"],
            "mean_ms": int(statistics.mean(ds)) if ds else None,
            "p95_ms": int(_p(ds, 0.95)) if ds else None,
            "max_ms": max(ds) if ds else None,
        }

    # Per-model aggregates (from SDK model_usage when present)
    model_totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {"input_tokens": 0, "output_tokens": 0, "calls": 0}
    )
    for r in rows:
        mu = r.get("model_usage") or {}
        for model_id, breakdown in mu.items():
            agg = model_totals[model_id]
            agg["calls"] += 1
            agg["input_tokens"] += int(breakdown.get("input_tokens", 0) or 0)
            agg["output_tokens"] += int(breakdown.get("output_tokens", 0) or 0)

    return {
        "queries": len(rows),
        "totals": {
            "input_tokens": sum(in_toks),
            "output_tokens": sum(out_toks),
            "cache_read_input_tokens": sum(cache_read),
            "duration_ms": sum(durations),
        },
        "per_query": {
            "mean_input_tokens": int(statistics.mean(in_toks)) if in_toks else 0,
            "mean_output_tokens": int(statistics.mean(out_toks)) if out_toks else 0,
            "mean_duration_s": round(statistics.mean(durations) / 1000, 2) if durations else 0,
            "mean_turns": round(statistics.mean(turns), 1) if turns else 0,
            "mean_tool_calls": round(statistics.mean(tool_counts), 1) if tool_counts else 0,
        },
        "tools": tools_report,
        "model_usage": dict(model_totals),
    }


def _p(xs: list[int], q: float) -> float:
    if not xs:
        return 0
    xs_sorted = sorted(xs)
    k = max(0, min(len(xs_sorted) - 1, int(round(q * (len(xs_sorted) - 1)))))
    return xs_sorted[k]


def render(summary: dict[str, Any]) -> str:
    if summary["queries"] == 0:
        return "No queries logged."

    t = summary["totals"]
    pq = summary["per_query"]
    lines = [
        f"Cost summary — {summary['queries']} queries",
        "",
        "Totals",
        f"  input tokens          {t['input_tokens']:>12,}",
        f"  output tokens         {t['output_tokens']:>12,}",
        f"  cache-read tokens     {t['cache_read_input_tokens']:>12,}",
        f"  wall-clock total      {t['duration_ms'] / 1000:>12.1f}s",
        "",
        "Per query (mean)",
        f"  input tokens          {pq['mean_input_tokens']:>12,}",
        f"  output tokens         {pq['mean_output_tokens']:>12,}",
        f"  duration              {pq['mean_duration_s']:>12.2f}s",
        f"  turns                 {pq['mean_turns']:>12}",
        f"  tool calls            {pq['mean_tool_calls']:>12}",
    ]
    if summary["tools"]:
        lines += ["", "Tools"]
        for name, agg in sorted(summary["tools"].items(), key=lambda kv: -kv[1]["calls"]):
            lines.append(
                f"  {name:<24} {agg['calls']:>4} calls  "
                f"mean {agg['mean_ms'] or 0:>5}ms  "
                f"p95 {agg['p95_ms'] or 0:>5}ms  "
                f"max {agg['max_ms'] or 0:>5}ms"
            )
    if summary["model_usage"]:
        lines += ["", "Per model"]
        for model_id, agg in summary["model_usage"].items():
            lines.append(
                f"  {model_id:<32} {int(agg['calls']):>4} calls  "
                f"in {int(agg['input_tokens']):>10,}  "
                f"out {int(agg['output_tokens']):>8,}"
            )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG, help="Path to cost-log.jsonl")
    ap.add_argument("--last", type=int, help="Only summarise the last N entries")
    ap.add_argument("--since", help="ISO-prefix filter applied to the 'ts' field (e.g. 2026-05)")
    ap.add_argument("--json", action="store_true", help="Emit summary as JSON")
    args = ap.parse_args()

    rows = load(args.log)
    if args.since:
        rows = [r for r in rows if str(r.get("ts", "")).startswith(args.since)]
    if args.last:
        rows = rows[-args.last:]

    summary = summarise(rows)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(render(summary))


if __name__ == "__main__":
    main()
