"""Tier router. [LOCKED — spec §3.1]

One fast call, <=200 completion tokens, JSON: {domain, difficulty, votable, rationale}.

Routing rule [LOCKED]:
    philosophy | mixed          -> tier 3
    difficulty <= 2 and votable -> tier 1
    else                        -> tier 2
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from core.llm import LLMClient, LLMError

ROUTER_PROMPT = """Classify this problem. Respond with JSON only, no other text:
{{"domain": "math|logic|philosophy|mixed", "difficulty": <1-5 integer>,
  "votable": <true|false, whether independent solvers would converge on one
  short canonical answer (a number, a word, a letter) vs. open-ended prose>,
  "rationale": "<one short sentence>"}}

Problem:
{problem}"""

_VALID_DOMAINS = {"math", "logic", "philosophy", "mixed"}


class RouteError(ValueError):
    pass


@dataclass
class Route:
    domain: str
    difficulty: int
    votable: bool
    rationale: str
    tier: int


def _parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise RouteError(f"router produced no JSON: {raw[:200]!r}")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise RouteError(f"router JSON invalid: {exc}") from exc
    if data.get("domain") not in _VALID_DOMAINS:
        raise RouteError(f"router gave unknown domain: {data.get('domain')!r}")
    try:
        data["difficulty"] = int(data["difficulty"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RouteError(f"router gave non-integer difficulty: {exc}") from exc
    if not 1 <= data["difficulty"] <= 5:
        raise RouteError(f"difficulty out of range: {data['difficulty']}")
    data["votable"] = bool(data.get("votable", False))
    data.setdefault("rationale", "")
    return data


def decide_tier(domain: str, difficulty: int, votable: bool, cfg: dict) -> int:
    """Pure routing decision — the LOCKED rule, isolated for testing."""
    routing = cfg["routing"]
    if domain in ("philosophy", "mixed"):
        return routing["philosophy_or_mixed_tier"]
    if difficulty <= routing["reflex_max_difficulty"] and votable:
        return 1
    return 2


async def route(client: LLMClient, problem: str, run_id: str | None = None) -> Route:
    """One fast-model call producing the routing decision."""
    cfg = client.config
    router_cfg = cfg["tiers"]["router"]
    messages = [{"role": "user", "content": ROUTER_PROMPT.format(problem=problem)}]
    try:
        samples = await client.complete(
            router_cfg["model"], messages, temperature=0.0,
            max_tokens=router_cfg["max_completion_tokens"], run_id=run_id)
    except LLMError as exc:
        raise RouteError(f"router call failed: {exc}") from exc

    data = _parse(samples[0].text or samples[0].reasoning)
    tier = decide_tier(data["domain"], data["difficulty"], data["votable"], cfg)
    return Route(domain=data["domain"], difficulty=data["difficulty"],
                votable=data["votable"], rationale=data["rationale"], tier=tier)
