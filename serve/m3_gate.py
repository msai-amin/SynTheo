#!/usr/bin/env python3
"""M3 acceptance gate: (1) a 12-problem routing matrix against the LIVE fast
model must hit the expected tier for each; (2) a live Tier-3 run must produce
a non-empty strongest_surviving_objection (the output contract, enforced)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm import LLMClient, load_config  # noqa: E402
from core.router import route  # noqa: E402
from core.tiers import Tier3ContractError, run_tier3  # noqa: E402

# (problem, expected_tier) — engineered to cover every branch of the LOCKED rule:
# philosophy/mixed -> 3 regardless of difficulty; easy+votable math/logic -> 1;
# everything else -> 2.
ROUTING_MATRIX = [
    ("What is 7 + 5?", 1),
    ("What is 100 divided by 4?", 1),
    ("If it is raining, the ground is wet. It is raining. Is the ground wet? "
     "Answer yes or no.", 1),
    ("What is 2 + 2 * 2?", 1),
    ("Prove that there are infinitely many prime numbers.", 2),
    ("Prove that the square root of 2 is irrational, and explain whether "
     "the argument generalizes to the square root of any prime.", 2),
    ("Using the Akra-Bazzi method, solve the recurrence T(n) = 2T(n/2) + n "
     "log n and give the asymptotic complexity with a full derivation.", 2),
    ("Prove that Dijkstra's algorithm produces correct shortest paths on a "
     "graph with non-negative edge weights.", 2),
    ("Is it ever morally permissible to lie? Discuss.", 3),
    ("What is the meaning of life?", 3),
    ("Compare utilitarianism and deontology, and also touch on whether "
     "42 is the answer to everything.", 3),
    ("Does free will exist given determinism?", 3),
]


async def gate_routing_matrix(client: LLMClient) -> list[str]:
    failures = []
    for problem, expected in ROUTING_MATRIX:
        try:
            r = await route(client, problem)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{problem[:50]!r}: router raised {exc}")
            continue
        status = "OK" if r.tier == expected else "MISMATCH"
        print(f"  [{status}] tier {r.tier} (expected {expected}) domain={r.domain} "
              f"difficulty={r.difficulty} votable={r.votable} :: {problem[:55]}")
        if r.tier != expected:
            failures.append(
                f"{problem[:50]!r}: got tier {r.tier} (domain={r.domain}, "
                f"difficulty={r.difficulty}, votable={r.votable}), expected {expected}")
    return failures


async def gate_tier3_contract(client: LLMClient) -> list[str]:
    failures = []
    problem = ("Is it morally permissible to break a promise to prevent a "
               "greater harm? Defend a position.")
    try:
        result = None
        async for ev in run_tier3(client, None, problem, domain="philosophy"):
            if ev["type"] in ("proved", "skepticized", "rebutted"):
                key = {"proved": "position", "skepticized": "objection",
                      "rebutted": "rebuttal"}[ev["type"]]
                print(f"  [{ev['type']}] {ev[key][:100]}")
            if ev["type"] == "result":
                result = ev
        print(f"  answer: {result['answer'][:150]}")
        print(f"  surviving objection: {result['strongest_surviving_objection'][:150]}")
        print(f"  judge confidence: {result['judge_confidence']}/10")
        if not result["strongest_surviving_objection"].strip():
            failures.append("output contract violated: empty surviving objection "
                            "(should have raised Tier3ContractError)")
        for field_name in ("answer", "key_premises", "strongest_surviving_objection",
                          "judge_confidence"):
            if field_name not in result:
                failures.append(f"output contract violated: missing {field_name!r}")
    except Tier3ContractError as exc:
        failures.append(f"live Tier-3 run failed its own contract: {exc}")
    return failures


async def main() -> None:
    cfg = load_config()
    failures = []

    async with LLMClient(config=cfg) as client:
        print("=== routing matrix (12 problems, live fast-model router) ===")
        failures += await gate_routing_matrix(client)

        print("\n=== Tier-3 output contract (live prover/skeptic/judge) ===")
        failures += await gate_tier3_contract(client)

    print()
    if failures:
        print("GATE FAILED:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("GATE PASSED")


if __name__ == "__main__":
    asyncio.run(main())
