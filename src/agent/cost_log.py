"""
cost_log.py — append-only JSONL telemetry for agent queries.

One line per `run_query` call, captured at `runs/cost-log.jsonl`. Fields are
chosen so the file can be summarised offline by `scripts/cost-summary.py`
without needing the full orchestrator import path.

Schema (per line):
    ts            ISO-8601 UTC timestamp
    query         the user query verbatim
    model         the orchestrator's claude_model at call time
    num_turns     SDK turn count from ResultMessage
    duration_ms   SDK-reported wall-clock for the agent loop
    tool_calls    list of {name, duration_ms} — one entry per ToolUseBlock
    total_tool_calls
    usage         {input_tokens, output_tokens, cache_*_tokens, ...} (SDK shape)
    model_usage   {model_id: {tokens breakdown}} as the SDK reports it

The writer is best-effort: an OSError on the write is swallowed by the caller
in orchestrator.py so a missing `runs/` directory or a read-only volume never
breaks a query.

The destination is `runs/cost-log.jsonl` by default; set `COST_LOG_PATH` to
redirect it. The test suite points it at a tmp file so unit runs never append
to the real telemetry log.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent.orchestrator import AgentResponse


DEFAULT_LOG_PATH = Path("runs/cost-log.jsonl")


def _log_path() -> Path:
    """Resolve the log destination at call time, honouring COST_LOG_PATH."""
    if override := os.getenv("COST_LOG_PATH"):
        return Path(override)
    return DEFAULT_LOG_PATH


def log_response(response: AgentResponse, *, model: str) -> None:
    """Append one JSON line capturing the cost/usage of a single agent run."""
    tool_calls_compact = [
        {"name": tc.get("name"), "duration_ms": tc.get("duration_ms")}
        for tc in response.tool_calls
    ]
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "query": response.query,
        "model": model,
        "num_turns": response.num_turns,
        "duration_ms": response.duration_ms,
        "tool_calls": tool_calls_compact,
        "total_tool_calls": response.total_tool_calls,
        "usage": response.usage,
        "model_usage": response.model_usage,
    }
    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
