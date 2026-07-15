"""Tier router. [LOCKED — spec §3.1]

One fast call, <=200 completion tokens, JSON: {domain, difficulty, votable, rationale}.

Routing rule [LOCKED]:
    metaphysics                 -> formal (Isabelle/HOL proving; see run_tier_formal)
    philosophy | mixed          -> tier 3
    difficulty <= 2 and votable -> tier 1
    else                        -> tier 2
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from core.llm import LLMClient, LLMError

ROUTER_PROMPT = """Classify this problem. Output ONLY a single JSON object, no
other text, no reasoning, no markdown fences.

The "domain" field must be exactly one of these five words: math, logic,
philosophy, metaphysics, mixed. Never output the list itself or a placeholder — pick one.
Use "metaphysics" ONLY for questions whose answer is a formal higher-order-modal
theorem to be proved or refuted mechanically — existence-of-God / ontological
arguments, modal collapse, necessity of a property. Ordinary open-ended value or
meaning questions are "philosophy", not "metaphysics".

Example output for "What is 12% of 250?":
{{"domain": "math", "difficulty": 1, "votable": true, "rationale": "simple arithmetic"}}

Now classify this problem in the same format:
{{"domain": <one of: math, logic, philosophy, metaphysics, mixed>, "difficulty": <1-5 integer>,
  "votable": <true if independent solvers would converge on one short
  canonical answer (a number, a word, a letter); false if open-ended prose>,
  "rationale": "<one short sentence>"}}

Problem:
{problem}"""

_VALID_DOMAINS = {"math", "logic", "philosophy", "metaphysics", "mixed"}


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
    start = raw.find("{")
    if start == -1:
        raise RouteError(f"router produced no JSON: {raw[:200]!r}")
    try:
        # raw_decode stops at the first complete object, ignoring anything the
        # model appended after it (a second object, trailing prose, etc.)
        data, _ = json.JSONDecoder().raw_decode(raw[start:])
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
    """Pure routing decision — the LOCKED rule, isolated for testing.

    metaphysics maps to the formal tier marker (config routing.metaphysics_tier);
    run_auto dispatches it to run_tier_formal by domain, not by this number, so the
    number is only the label shown in the `routed` event."""
    routing = cfg["routing"]
    if domain == "metaphysics":
        return routing["metaphysics_tier"]
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
            max_tokens=router_cfg["max_completion_tokens"], run_id=run_id,
            # the fast model is a reasoning model; a long think would blow the
            # 200-token [LOCKED] budget before ever emitting the JSON
            extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    except LLMError as exc:
        raise RouteError(f"router call failed: {exc}") from exc

    data = _parse(samples[0].text or samples[0].reasoning)
    tier = decide_tier(data["domain"], data["difficulty"], data["votable"], cfg)
    return Route(domain=data["domain"], difficulty=data["difficulty"],
                votable=data["votable"], rationale=data["rationale"], tier=tier)
