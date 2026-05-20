"""
LLM-as-judge for full agent response quality.

Uses the Claude Agent SDK (same OAuth auth path as the orchestrator).
No env vars or API keys required — auth is automatic via the Claude Code
OAuth session.

Judge model: claude-haiku-4-5-20251001 (fast, cheap, adequate for scoring).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass


@dataclass
class JudgeScore:
    grounding: int        # 1-5: claims traceable to retrieved context
    reasoning: int        # 1-5: logical chain from evidence to conclusion
    completeness: int     # 1-5: query fully answered
    overall: float        # mean across dimensions, 1 decimal place
    flags: list[str]      # specific hallucination signals
    verdict: str          # 2-3 sentence summary


JUDGE_RUBRIC = """
You are evaluating an AI research assistant's response. Score on three dimensions 1-5:

grounding: Are claims traceable to the retrieved context provided? (1=invented, 5=every claim cited)
reasoning: Is the logical chain from evidence to conclusion sound? (1=non sequitur, 5=rigorous)
completeness: Is the original query fully addressed? (1=ignored, 5=complete answer)

Flag any specific factual claims that appear unsupported or contradicted by the context.
Write a 2-3 sentence verdict.

Respond with ONLY a <json> block.
Schema: {"grounding": 0, "reasoning": 0, "completeness": 0, "flags": [], "verdict": ""}
"""

# Regex patterns for extracting JSON from various response shapes
_JSON_TAG_RE = re.compile(r"<json>\s*(.*?)\s*</json>", re.DOTALL | re.IGNORECASE)
_JSON_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_JSON_RAW_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_judge_response(raw: str) -> JudgeScore:
    """
    Parse the judge's raw text into a JudgeScore.

    Accepts any of:
      - <json>{...}</json>
      - ```json\\n{...}\\n```
      - Raw {...} if Claude skipped the wrapper

    On parse failure: returns a degraded JudgeScore with flags=["parse failure"].
    Does not raise — eval continues with the failed row marked.
    """
    text = raw.strip()

    # Try <json>...</json> first
    m = _JSON_TAG_RE.search(text)
    if m:
        json_str = m.group(1)
    else:
        # Try ```json...``` fence
        m = _JSON_FENCE_RE.search(text)
        if m:
            json_str = m.group(1)
        else:
            # Try raw JSON object
            m = _JSON_RAW_RE.search(text)
            if m:
                json_str = m.group(0)
            else:
                json_str = None

    if json_str is None:
        return JudgeScore(
            grounding=0,
            reasoning=0,
            completeness=0,
            overall=0.0,
            flags=["parse failure"],
            verdict=raw[:200] if raw else "No response from judge.",
        )

    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return JudgeScore(
            grounding=0,
            reasoning=0,
            completeness=0,
            overall=0.0,
            flags=["parse failure"],
            verdict=raw[:200],
        )

    grounding = int(data.get("grounding", 0))
    reasoning = int(data.get("reasoning", 0))
    completeness = int(data.get("completeness", 0))
    overall = round((grounding + reasoning + completeness) / 3, 1)

    return JudgeScore(
        grounding=grounding,
        reasoning=reasoning,
        completeness=completeness,
        overall=overall,
        flags=data.get("flags", []),
        verdict=data.get("verdict", ""),
    )


async def _score_async(
    query_text: str, retrieved_context: str, response_text: str, model: str
) -> str:
    """Call the judge model via Claude Agent SDK and return the raw text response."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
    )
    from claude_agent_sdk import (
        query as sdk_query,
    )

    user_prompt = (
        f"Query: {query_text}\n\n"
        f"Retrieved context:\n{retrieved_context}\n\n"
        f"Agent response:\n{response_text}\n\n"
        "Score this response according to the rubric. Return ONLY a <json> block."
    )

    options = ClaudeAgentOptions(
        model=model,
        system_prompt=JUDGE_RUBRIC,
        permission_mode="bypassPermissions",
    )

    raw = ""
    async for msg in sdk_query(prompt=user_prompt, options=options):
        if isinstance(msg, ResultMessage) and msg.result:
            raw = msg.result
        elif isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text:
                    raw = block.text  # last non-empty wins
    return raw


def score_response(
    query: str,
    retrieved_context: str,
    response: str,
    api_key: str | None = None,
    config=None,
) -> JudgeScore:
    """
    Score a response with LLM-as-judge.

    Parameters
    ----------
    query:              The original user question.
    retrieved_context:  The context surfaced by the retrieval layer.
    response:           The agent's full response to score.
    api_key:            Kept for backwards-compatibility only. No longer routed
                        anywhere — the Claude Agent SDK handles auth automatically
                        via the Claude Code OAuth session. Pass None (default).
    config:             Config instance. If None → stub placeholder (no network call).
                        If provided → live judge via Claude Agent SDK (uses Max quota).

    Returns
    -------
    JudgeScore — real scores when config is provided, placeholder in stub mode.
    """
    if config is None:
        return JudgeScore(
            grounding=0,
            reasoning=0,
            completeness=0,
            overall=0.0,
            flags=["stub mode"],
            verdict="Not scored.",
        )

    raw = asyncio.run(
        _score_async(query, retrieved_context, response, config.eval_judge_model)
    )
    return _parse_judge_response(raw)
