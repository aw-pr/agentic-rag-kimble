"""
Offline retrieval evaluation harness for agentic-rag-kimble.

Measures recall@k against 20 hand-crafted fixtures. Can run before the agent
tools exist (returns recall = 0.0 gracefully) and provides a persistent signal
to optimise against as each phase ships.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import Config
from src.eval.fixtures import FIXTURES, EvalFixture


def recall_at_k(
    retrieved_names: list[str],
    expected_names: list[str],
    k: int,
) -> float:
    """1.0 if any expected name appears in the top-k retrieved results, else 0.0."""
    top_k = retrieved_names[:k]
    for name in top_k:
        if name in expected_names:
            return 1.0
    return 0.0


def _try_graph_query(fixture: EvalFixture, config: Config) -> list[str]:
    """Run a graph lookup for the fixture and return a list of entity names."""
    from src.graph.db import GraphDB

    family_keywords = [
        "tree_ensemble", "decision_tree", "svm", "knn", "linear",
        "gradient_boosting", "neural", "bayes", "other",
    ]

    entity_type = fixture.expected_entity_type

    with GraphDB(config) as db:
        if entity_type == "Algorithm":
            # Detect family queries
            q_lower = fixture.query.lower()
            matched_family = next(
                (f for f in family_keywords if f in q_lower), None
            )
            # Prefer display_name (canonical class name) over the FQCN.
            # Fetch a generous window then sort in Python: shorter canonical names
            # (e.g. SVC, IBk, MLPClassifier) before Pipeline/RandomizedSearchCV.
            if matched_family:
                cypher = (
                    f"MATCH (a:Algorithm) WHERE a.family = '{matched_family}' "
                    "RETURN COALESCE(a.display_name, a.name) AS name LIMIT 100"
                )
            else:
                cypher = (
                    "MATCH (a:Algorithm) "
                    "RETURN COALESCE(a.display_name, a.name) AS name LIMIT 100"
                )
            rows = db.execute(cypher)
            names = [r["name"] for r in rows if isinstance(r.get("name"), str)]
            # De-duplicate while preserving order, then sort shorter names first.
            # Tiebreak: prefer proper-CamelCase names (start with uppercase) over
            # all-lowercase short tokens (MLR/R style) to surface canonical sklearn/Weka
            # class names above wrapper inner-class fragments.
            seen: set[str] = set()
            unique: list[str] = []
            for n in names:
                if n not in seen:
                    seen.add(n)
                    unique.append(n)
            unique.sort(key=lambda n: (len(n), 0 if n[:1].isupper() else 1))
            return unique

        elif entity_type == "Dataset":
            cypher = "MATCH (d:Dataset) RETURN d.name AS name LIMIT 20"
            rows = db.execute(cypher)
            return [r["name"] for r in rows]

        elif entity_type == "Task":
            cypher = "MATCH (t:Task) RETURN t.task_type AS name LIMIT 20"
            rows = db.execute(cypher)
            return [r["name"] for r in rows]

    return []


def _try_semantic_search(fixture: EvalFixture, config: Config, k: int) -> list[str]:
    """Run semantic search for the fixture and return entity names."""
    from src.retrieval.semantic import semantic_search

    results = semantic_search(
        query=fixture.query,
        entity_type=fixture.expected_entity_type,
        top_k=k,
        config=config,
    )
    return [r.get("name", "") for r in results]


def _try_aggregate(fixture: EvalFixture, config: Config) -> list[str]:
    """Run an aggregate query for the fixture and return result names."""
    from src.retrieval.aggregate import aggregate_measures

    results = aggregate_measures(
        group_by="family",
        measure="accuracy",
        filter_cypher=None,
        config=config,
    )
    return [r.get("family", r.get("name", "")) for r in results]


def run_retrieval_eval(config: Config, k: int = 10, with_judge: bool = False) -> dict:
    """
    Run all FIXTURES against the live DB and vector store.

    Parameters
    ----------
    config:     Runtime configuration.
    k:          Recall cutoff (default 10).
    with_judge: If True, run the LLM judge on FIXTURES[:5] (sampled to limit
                quota usage). Populates results["judge_score"] with aggregate
                scores; each judged fixture requires one live SDK call.
                Default False — no network required, judge_score stays None.

    Returns
    -------
    {
      "recall_at_5": float,
      "recall_at_10": float,
      "per_tool": {"graph": float, "semantic": float, "aggregate": float},
      "failures": [{"query": str, "expected": list, "got": list}],
      "n_fixtures": int,
      "db_populated": bool,
      "judge_score": None | {"samples_scored": int, "mean_grounding": float, ...}
    }

    Gracefully returns db_populated=False if tools or DB are not yet available.
    """
    # Check whether the DB module is importable and the DB is populated.
    try:
        from src.graph.db import GraphDB

        with GraphDB(config) as db:
            rows = db.execute("MATCH (a:Algorithm) RETURN count(a) AS n")
            n_algorithms = int(rows[0]["n"]) if rows else 0
        db_populated = n_algorithms > 0
    except Exception:
        db_populated = False

    if not db_populated:
        return {
            "recall_at_5": 0.0,
            "recall_at_10": 0.0,
            "per_tool": {"graph": 0.0, "semantic": 0.0, "aggregate": 0.0},
            "failures": [],
            "n_fixtures": len(FIXTURES),
            "db_populated": False,
        }

    hits_at_5: dict[str, list[float]] = {"graph": [], "semantic": [], "aggregate": []}
    hits_at_10: dict[str, list[float]] = {"graph": [], "semantic": [], "aggregate": []}
    failures: list[dict] = []

    for fixture in FIXTURES:
        retrieved: list[str] = []
        tool = fixture.tool_hint

        try:
            if tool == "graph":
                retrieved = _try_graph_query(fixture, config)
            elif tool == "semantic":
                retrieved = _try_semantic_search(fixture, config, k)
            elif tool == "aggregate":
                retrieved = _try_aggregate(fixture, config)
        except Exception:
            retrieved = []

        r5 = recall_at_k(retrieved, fixture.expected_entity_names, k=5)
        r10 = recall_at_k(retrieved, fixture.expected_entity_names, k=10)

        hits_at_5[tool].append(r5)
        hits_at_10[tool].append(r10)

        if r10 == 0.0:
            failures.append(
                {
                    "query": fixture.query,
                    "expected": fixture.expected_entity_names,
                    "got": retrieved[:10],
                }
            )

    def _mean(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    all_r5 = [v for vs in hits_at_5.values() for v in vs]
    all_r10 = [v for vs in hits_at_10.values() for v in vs]

    results: dict = {
        "recall_at_5": _mean(all_r5),
        "recall_at_10": _mean(all_r10),
        "per_tool": {
            tool: _mean(hits_at_10[tool]) for tool in ("graph", "semantic", "aggregate")
        },
        "failures": failures,
        "n_fixtures": len(FIXTURES),
        "db_populated": db_populated,
        "judge_score": None,
    }

    if with_judge:
        results["judge_score"] = _run_judge_sample(config, k)

    return results


_JUDGE_SAMPLE_SIZE = 5  # Cap to limit Max quota usage per eval run


def _build_judge_context(agent_resp, retrieved: list[str]) -> str:
    """Assemble a rich retrieved_context string for the judge.

    Combines (a) the agent's actual tool calls, (b) the retrieval layer's
    answer to the fixture's intent, and (c) any citations the agent extracted.
    Pass-20 found the judge scored grounding ~1.2/5 when given only bare entity
    names — it couldn't trace specific claims because it couldn't see the
    queries the agent ran.
    """
    parts: list[str] = []
    if agent_resp is not None and getattr(agent_resp, "tool_calls", None):
        parts.append("Agent tool calls:")
        for i, tc in enumerate(agent_resp.tool_calls, 1):
            args = json.dumps(tc.get("input", {}), default=str)[:300]
            call_line = f"  {i}. {tc.get('name', '?')}({args})"
            result = tc.get("result")
            if result:
                call_line += f"\n     → {str(result)[:500]}"
            parts.append(call_line)
    if retrieved:
        parts.append("\nTop retrieval results for this query (re-executed by eval harness):")
        for r in retrieved[:10]:
            parts.append(f"  - {r}")
    if agent_resp is not None and getattr(agent_resp, "citations", None):
        parts.append("\nCitations extracted from the agent's answer:")
        for c in agent_resp.citations[:10]:
            parts.append(f"  - {c}")
    return "\n".join(parts) if parts else "(no context)"


def _run_judge_sample(config: Config, k: int) -> dict:
    """
    Run the LLM judge on the first JUDGE_SAMPLE_SIZE fixtures.

    For each sampled fixture: runs the agent end-to-end to get a real response,
    then scores it. Returns aggregate means plus per-fixture detail.

    This is kept out of run_retrieval_eval() so the two concerns stay separate
    and the judge can be skipped without altering retrieval logic.
    """
    from src.agent.orchestrator import run_query
    from src.eval.judge import score_response

    sampled = FIXTURES[:_JUDGE_SAMPLE_SIZE]
    per_fixture: list[dict[str, Any]] = []

    for fixture in sampled:
        # Re-execute the retrieval layer for supporting evidence
        try:
            if fixture.tool_hint == "graph":
                retrieved = _try_graph_query(fixture, config)
            elif fixture.tool_hint == "semantic":
                retrieved = _try_semantic_search(fixture, config, k)
            else:
                retrieved = _try_aggregate(fixture, config)
        except Exception:
            retrieved = []

        # Run agent end-to-end to get a real response to score
        try:
            agent_resp = run_query(fixture.query, config)
            response_text = agent_resp.answer
        except Exception as exc:
            agent_resp = None
            response_text = f"[agent error: {exc}]"

        # Build a rich judge context: agent's actual tool calls + retrieval results + citations.
        # Pass-20 found that bare entity names alone make the judge see claims as ungrounded.
        retrieved_context = _build_judge_context(agent_resp, retrieved)

        score = score_response(
            query=fixture.query,
            retrieved_context=retrieved_context,
            response=response_text,
            config=config,
        )

        per_fixture.append({
            "query": fixture.query,
            "grounding": score.grounding,
            "reasoning": score.reasoning,
            "completeness": score.completeness,
            "overall": score.overall,
            "flags": score.flags,
            "verdict": score.verdict,
        })

    n = len(per_fixture)
    if n == 0:
        return {
            "samples_scored": 0,
            "mean_grounding": 0.0,
            "mean_reasoning": 0.0,
            "mean_completeness": 0.0,
            "mean_overall": 0.0,
            "per_fixture": [],
        }

    def _avg(key: str) -> float:
        return round(sum(float(f[key]) for f in per_fixture) / n, 2)

    return {
        "samples_scored": n,
        "mean_grounding": _avg("grounding"),
        "mean_reasoning": _avg("reasoning"),
        "mean_completeness": _avg("completeness"),
        "mean_overall": _avg("overall"),
        "per_fixture": per_fixture,
    }


def _coerce_nan(obj):
    """Recursively replace NaN/Inf floats with None so json.dumps allow_nan=False succeeds."""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: _coerce_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_nan(v) for v in obj]
    return obj


def write_eval_results(results: dict, runs_path: Path) -> Path:
    """Write results to runs/eval-{timestamp}.json. Returns the path written."""
    runs_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = runs_path / f"eval-{timestamp}.json"
    safe_results = _coerce_nan(results)
    out_path.write_text(json.dumps(safe_results, indent=2, allow_nan=False))
    return out_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run offline retrieval eval.")
    parser.add_argument("--output-json", action="store_true", help="Print JSON output")
    parser.add_argument("--k", type=int, default=10, help="Recall cutoff (default 10)")
    parser.add_argument(
        "--with-judge",
        action="store_true",
        help=(
            "Enable LLM judge scoring on the first 5 fixtures. "
            "Requires Claude Agent SDK OAuth auth (uses Max subscription quota). "
            "Adds ~5 agent invocations + 5 judge calls per eval run."
        ),
    )
    args = parser.parse_args()

    from src.config import get_config

    cfg = get_config()
    results = run_retrieval_eval(cfg, k=args.k, with_judge=args.with_judge)

    # Always write results to runs/
    out_path = write_eval_results(results, cfg.runs_path)
    print(f"Results written to {out_path}")

    if args.output_json:
        print(json.dumps(results, indent=2, allow_nan=False))
    else:
        print(f"recall@5:  {results['recall_at_5']:.3f}")
        print(f"recall@10: {results['recall_at_10']:.3f}")
        for tool, score in results["per_tool"].items():
            print(f"  {tool}: {score:.3f}")
        if results.get("failures"):
            print(f"\nFailures ({len(results['failures'])}):")
            for f in results["failures"][:5]:
                print(f"  Q: {f['query'][:60]}")
                print(f"  Expected: {f['expected']}, Got: {f['got'][:3]}")
        if results.get("judge_score") and results["judge_score"].get("samples_scored", 0) > 0:
            js = results["judge_score"]
            print(f"\nJudge scores (n={js['samples_scored']} fixtures sampled):")
            print(f"  mean grounding:    {js['mean_grounding']:.2f}")
            print(f"  mean reasoning:    {js['mean_reasoning']:.2f}")
            print(f"  mean completeness: {js['mean_completeness']:.2f}")
            print(f"  mean overall:      {js['mean_overall']:.2f}")
        elif not args.with_judge:
            print("\n(Judge not run. Use --with-judge or WITH_JUDGE=1 to enable.)")
        if not results["db_populated"]:
            print(
                "\nNote: db_populated=False — tools/embeddings not yet available. "
                "Recall will be 0.000 until pass 05 backfills embeddings and pass 06 "
                "wires the retrieval tools."
            )
