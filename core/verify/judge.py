"""Rubric judge with bias guards. [LOCKED — spec §3.3]

Guards, enforced here and tested in tests/test_judge.py:
- a judge must be a DIFFERENT model family than the sample's generator
- judging is blind: generator identity is stripped from what the judge sees
- contentless verdicts ("looks correct") are rejected and re-asked once
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Family map: alias -> model family. Extend when the roster changes; judge
# selection must consult THIS, never the alias string.
FAMILIES = {
    "heavy": "gpt-oss",
    "mid": "qwen",
    "fast": "nemotron",
}

RUBRIC_PROMPT = """You are grading one candidate solution. The author is anonymous.

Problem:
{problem}

Candidate solution:
{sample}

Score 1-10 against this rubric:
1-2 wrong or incoherent; 3-4 major gaps or an unjustified leap; 5-6 right idea,
flawed execution; 7-8 correct with minor blemishes; 9-10 correct, complete, clear.

Respond with JSON only:
{{"score": <1-10>, "strongest_flaw": "<the single most serious specific flaw>",
  "verdict_reason": "<why this score, citing specifics of the argument>"}}"""

STERN_RETRY_PROMPT = """Your previous verdict was rejected as contentless. Generic
phrases ("looks correct", "seems fine", "well reasoned") are not analysis.
Re-grade. Name a concrete step, equation, or claim in the candidate and say what
is right or wrong about it. Same JSON format."""

_GENERIC_PAT = re.compile(
    r"^\s*(looks?\s+(correct|good|fine|right)|seems?\s+(correct|good|fine|right)|"
    r"(is\s+)?(correct|good|fine|well.?reasoned|solid)|no\s+flaws?( found)?|"
    r"n/?a|none)\s*\.?\s*$",
    re.IGNORECASE,
)


@dataclass
class Judgment:
    score: float
    strongest_flaw: str
    verdict_reason: str
    judge_alias: str
    retried: bool
    raw: str


class JudgeError(ValueError):
    """Raised when no valid judgment can be produced."""


def eligible_judges(generator_alias: str, available: list[str] | None = None) -> list[str]:
    """Aliases allowed to judge a sample from `generator_alias` (different family)."""
    pool = available if available is not None else list(FAMILIES)
    gen_family = FAMILIES[generator_alias]
    return [a for a in pool if FAMILIES[a] != gen_family]


def is_contentless(verdict: dict) -> bool:
    flaw = str(verdict.get("strongest_flaw", "")).strip()
    reason = str(verdict.get("verdict_reason", "")).strip()
    if not flaw or not reason:
        return True
    if _GENERIC_PAT.match(flaw) and len(reason) < 80:
        return True
    if _GENERIC_PAT.match(reason):
        return True
    return False


def parse_verdict(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        verdict = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    try:
        score = float(verdict["score"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 1 <= score <= 10:
        return None
    verdict["score"] = score
    return verdict


async def judge_sample(
    client,
    problem: str,
    sample_text: str,
    generator_alias: str,
    available: list[str] | None = None,
    run_id: str | None = None,
) -> Judgment:
    """Judge one sample blind, cross-family, with one stern retry on contentless."""
    candidates = eligible_judges(generator_alias, available)
    if not candidates:
        raise JudgeError(f"no cross-family judge available for {generator_alias}")
    judge_alias = candidates[0]

    messages = [{"role": "user", "content": RUBRIC_PROMPT.format(
        problem=problem, sample=sample_text)}]

    retried = False
    for attempt in range(2):
        samples = await client.complete(
            judge_alias, messages, temperature=0.2, max_tokens=1024, run_id=run_id
        )
        raw = samples[0].text
        verdict = parse_verdict(raw)
        if verdict is not None and not is_contentless(verdict):
            return Judgment(
                score=verdict["score"],
                strongest_flaw=str(verdict["strongest_flaw"]),
                verdict_reason=str(verdict["verdict_reason"]),
                judge_alias=judge_alias,
                retried=retried,
                raw=raw,
            )
        # one stern retry, then give up
        retried = True
        messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": STERN_RETRY_PROMPT},
        ]

    raise JudgeError(f"judge {judge_alias} produced no contentful verdict after retry")
