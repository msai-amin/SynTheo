#!/usr/bin/env python3
"""M2 acceptance gate: 10 problems through the Tier-2 pipeline; the store must
hold complete traces and verified/consensus/judged must all be exercised."""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm import REPO_ROOT, LLMClient, load_config  # noqa: E402
from core.store import Store  # noqa: E402
from core.tiers import run_tier2  # noqa: E402

# (problem, domain, votable) — mix engineered to hit all three cascade stages
PROBLEMS = [
    ("Compute 17 * 23.", "math", True),
    ("What is the sum of the first 100 positive integers?", "math", True),
    ("Find the smallest prime greater than 200.", "math", True),
    ("A number leaves remainder 2 mod 3, 3 mod 5, and 2 mod 7. "
     "What is the smallest such positive integer?", "math", True),
    ("What is the units digit of 7^2026?", "math", True),
    ("Is the statement 'this sentence is false' true, false, or paradoxical? "
     "Answer with one word.", "logic", True),
    ("Alice is twice as old as Bob. Their ages sum to 36. How old is Alice?",
     "logic", True),
    ("If all bloops are razzies and no razzies are lazzies, can a bloop be a "
     "lazzy? Answer yes or no.", "logic", True),
    # judged-path bait: open-ended, no single canonical answer, not votable
    ("In one sentence, state the key idea behind the proof that sqrt(2) is "
     "irrational.", "math", False),
    ("Briefly explain why the halting problem is undecidable.", "logic", False),
]


async def main() -> None:
    cfg = load_config()
    db_path = REPO_ROOT / "data" / "m2_gate.sqlite3"
    db_path.unlink(missing_ok=True)
    Path(str(db_path) + "-wal").unlink(missing_ok=True)

    confidence_seen: Counter[str] = Counter()
    run_ids: list[int] = []

    async with LLMClient(config=cfg) as client:
        with Store(db_path) as store:
            for i, (problem, domain, votable) in enumerate(PROBLEMS, 1):
                print(f"\n### problem {i}/10 [{domain}] {problem[:60]}")
                result = None
                async for ev in run_tier2(client, store, problem,
                                          domain=domain, votable=votable):
                    if ev["type"] == "sample":
                        status = "ERR" if ev["error"] else repr(ev["extracted"])[:40]
                        print(f"  sample {ev['k']}/{ev['n']} {ev['model']}: {status}")
                    elif ev["type"] == "verification":
                        marks = " ".join(f"{'✓' if m['verified'] else '✗'}{m['method']}"
                                         for m in ev["methods"])
                        print(f"  verify #{ev['index']}: {marks}")
                    elif ev["type"] == "result":
                        result = ev
                print(f"  => {result['confidence_type']} ({result['detail']}) "
                      f"answer={str(result['answer'])[:60]!r} "
                      f"wall={result['wall_ms']}ms")
                confidence_seen[result["confidence_type"]] += 1
                run_ids.append(result["run_id"])

            # --- gate checks ---
            print("\n=== gate checks ===")
            failures = []
            for rid in run_ids:
                trace = store.get_run(rid)
                if not trace["samples"]:
                    failures.append(f"run {rid}: no samples stored")
                if trace["run"]["finished"] is None:
                    failures.append(f"run {rid}: never finished")
                if not any(s["verifications"] for s in trace["samples"]):
                    failures.append(f"run {rid}: no verifications stored")

            print(f"confidence types seen: {dict(confidence_seen)}")
            for ct in ("verified", "consensus", "judged"):
                if confidence_seen.get(ct, 0) == 0:
                    failures.append(f"confidence type {ct!r} never exercised")

            if failures:
                print("GATE FAILED:")
                for f in failures:
                    print(" -", f)
                sys.exit(1)
            print(f"complete traces for all {len(run_ids)} runs")
            print("GATE PASSED")


if __name__ == "__main__":
    asyncio.run(main())
