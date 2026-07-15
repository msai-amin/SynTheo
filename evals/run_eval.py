#!/usr/bin/env python3
"""Eval harness: suite × strategy → scored JSON report.

    python evals/run_eval.py --suite core --strategy tier2 --n 8
    python evals/run_eval.py --suite core --strategy all --limit-per-domain 3

Suites are frozen JSONL under evals/suites/ — the app NEVER writes there.
Problems are imported into the episode store with is_eval=1, which the
contamination firewall (store.NON_EVAL_FILTER) uses to keep them out of
every training export.

Scoring: math/logic via mathcheck against the gold answer; philosophy via a
cross-family rubric judge (score >= 7 counts as pass; mean score reported).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm import REPO_ROOT, LLMClient, load_config  # noqa: E402
from core.store import Store  # noqa: E402
from core.tiers import (  # noqa: E402
    run_auto,
    run_tier1,
    run_tier2,
    run_tier3,
    run_tier_formal,
)
from core.verify.judge import JudgeError, judge_sample  # noqa: E402
from core.verify.mathcheck import check_math  # noqa: E402

SUITES_DIR = Path(__file__).resolve().parent / "suites"
REPORTS_DIR = Path(__file__).resolve().parent / "reports"
STRATEGIES = ("tier1", "self-consistency", "tier2", "auto")
PHIL_PASS_SCORE = 7.0


def load_suite(name: str) -> list[dict]:
    rows = []
    for path in sorted(SUITES_DIR.glob(f"{name}_*.jsonl")):
        with open(path) as f:
            rows.extend(json.loads(line) for line in f if line.strip())
    if not rows:
        raise SystemExit(f"no suite files match {name}_*.jsonl in {SUITES_DIR}")
    return rows


def import_suite(store: Store, rows: list[dict], suite: str) -> dict[str, int]:
    """Import problems with is_eval=1 (idempotent: existing rows are reused)."""
    ids: dict[str, int] = {}
    for row in rows:
        existing = store.conn.execute(
            "SELECT id, is_eval FROM problems WHERE text = ?", (row["problem"],)
        ).fetchone()
        if existing:
            assert existing["is_eval"] == 1, (
                f"eval problem {row['id']} already in store WITHOUT is_eval=1 — "
                "contamination firewall breach, refusing to continue")
            ids[row["id"]] = existing["id"]
            continue
        ids[row["id"]] = store.add_problem(
            row["problem"], row["domain"], source=f"suite:{suite}", is_eval=True)
    return ids


async def run_strategy(client: LLMClient, store: Store, strategy: str,
                       row: dict, problem_id: int, n: int | None) -> dict:
    """Run one problem under one strategy; return its final result event."""
    domain, votable = row["domain"], "answer" in row
    kwargs = {"problem_id": problem_id}
    if strategy == "tier1":
        gen = run_tier1(client, store, row["problem"], domain=domain,
                        votable=votable, **kwargs)
    elif strategy == "self-consistency":
        gen = run_tier2(client, store, row["problem"], domain=domain,
                        votable=votable, n=n, strategy="self-consistency",
                        verify=False, **kwargs)
    elif strategy == "tier2":
        if domain == "philosophy":
            gen = run_tier2(client, store, row["problem"], domain=domain,
                            votable=False, n=n, **kwargs)
        else:
            gen = run_tier2(client, store, row["problem"], domain=domain,
                            votable=True, n=n, **kwargs)
    elif strategy == "auto":
        gen = run_auto(client, store, row["problem"])
    elif strategy == "tier3":
        gen = run_tier3(client, store, row["problem"], domain=domain, **kwargs)
    elif strategy == "formal":
        gen = run_tier_formal(client, store, row["problem"], target=row.get("target"),
                              parent_session=row.get("parent_session"), **kwargs)
    else:
        raise ValueError(strategy)

    result = None
    async for ev in gen:
        if ev["type"] == "result":
            result = ev  # run_auto may yield several; the LAST is the final one
    return result or {"answer": None, "confidence_type": "failed",
                      "tokens_in": 0, "tokens_out": 0, "wall_ms": 0}


async def score_result(client: LLMClient, row: dict, result: dict) -> dict:
    """Score one result against gold answer, kernel verdict, or rubric."""
    answer = result.get("answer")
    if row.get("domain") == "metaphysics":  # kernel verdict vs. expected polarity
        got = result.get("confidence_type")
        expected = row.get("expected")  # "verified" | "refuted"
        return {"correct": got == expected, "method": "kernel",
                "detail": f"got {got!r}, expected {expected!r} — {result.get('detail', '')}"[:200]}
    if "answer" in row:  # math / logic: mechanical comparison with gold
        if answer is None:
            return {"correct": False, "method": "gold", "detail": "no answer"}
        check = check_math(str(answer), row["answer"])
        return {"correct": check["verified"], "method": f"gold/{check['method']}",
                "detail": check["detail"]}
    # philosophy: rubric judge (cross-family vs. nothing specific — the answer
    # text is what's judged, so exclude nothing; use default judge pool).
    # Judge the full reasoning/argument, not just the terse extracted answer
    # field — a fenced answer block is 1-2 sentences by design and can't carry
    # the counterargument-engagement a philosophy rubric requires.
    if not answer:
        return {"correct": False, "score": 0.0, "method": "rubric",
                "detail": "no answer"}
    graded_text = result.get("full_text") or str(answer)
    rubric_problem = (f"{row['problem']}\n\nGrading rubric (score against "
                      f"this): {row['rubric']}")
    try:
        j = await judge_sample(client, rubric_problem, graded_text,
                               generator_alias="heavy")
        return {"correct": j.score >= PHIL_PASS_SCORE, "score": j.score,
                "method": "rubric", "detail": j.verdict_reason[:200]}
    except (JudgeError, Exception) as exc:  # noqa: BLE001
        return {"correct": False, "score": 0.0, "method": "rubric",
                "detail": f"judge failed: {exc}"}


def aggregate(rows_scored: list[dict]) -> dict:
    """Per-domain and overall accuracy / tokens / wall-time."""
    out: dict[str, dict] = {}
    for scope in ("math", "logic", "philosophy", "metaphysics", "overall"):
        subset = [r for r in rows_scored
                  if scope == "overall" or r["domain"] == scope]
        if not subset:
            continue
        n = len(subset)
        out[scope] = {
            "n": n,
            "accuracy": sum(r["score"]["correct"] for r in subset) / n,
            "mean_tokens": sum(r["result"]["tokens_in"]
                               + r["result"]["tokens_out"] for r in subset) / n,
            "mean_wall_ms": sum(r["result"]["wall_ms"] for r in subset) / n,
        }
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="core")
    ap.add_argument("--strategy", default="tier2",
                    choices=STRATEGIES + ("all", "tier3", "formal"))
    ap.add_argument("--n", type=int, default=None, help="tier2 sample count")
    ap.add_argument("--limit-per-domain", type=int, default=None,
                    help="cap problems per domain (smoke runs)")
    ap.add_argument("--db", type=Path, default=None)
    args = ap.parse_args()

    cfg = load_config()
    db_path = args.db or REPO_ROOT / cfg["paths"]["episode_db"]
    strategies = list(STRATEGIES) if args.strategy == "all" else [args.strategy]

    rows = load_suite(args.suite)
    if args.limit_per_domain:
        by_domain: dict[str, list[dict]] = {}
        for row in rows:
            by_domain.setdefault(row["domain"], []).append(row)
        rows = [r for domain_rows in by_domain.values()
                for r in domain_rows[:args.limit_per_domain]]

    report: dict = {"suite": args.suite, "started": time.time(),
                    "n_problems": len(rows), "strategies": {},
                    "config": {"n": args.n or cfg["tiers"]["tier2"]["n"]}}

    async with LLMClient(config=cfg) as client:
        with Store(db_path) as store:
            ids = import_suite(store, rows, args.suite)
            for strategy in strategies:
                print(f"\n===== strategy: {strategy} ({len(rows)} problems) =====")
                scored = []
                for i, row in enumerate(rows, 1):
                    t0 = time.monotonic()
                    result = await run_strategy(client, store, strategy, row,
                                                ids[row["id"]], args.n)
                    score = await score_result(client, row, result)
                    scored.append({"id": row["id"], "domain": row["domain"],
                                   "result": {k: result.get(k) for k in
                                              ("answer", "confidence_type",
                                               "tokens_in", "tokens_out",
                                               "wall_ms", "run_id")},
                                   "score": score})
                    mark = "✓" if score["correct"] else "✗"
                    print(f"  [{i}/{len(rows)}] {mark} {row['id']} "
                          f"{result.get('confidence_type')} "
                          f"answer={str(result.get('answer'))[:40]!r} "
                          f"({time.monotonic() - t0:.0f}s)")
                report["strategies"][strategy] = {
                    "aggregate": aggregate(scored), "problems": scored}

    report["finished"] = time.time()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"{args.suite}-{int(report['started'])}.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=1)

    print(f"\n===== report: {out_path} =====")
    print(f"{'strategy':18} {'domain':12} {'acc':>6} {'tokens':>8} {'wall_s':>7}")
    for strategy, data in report["strategies"].items():
        for domain, agg in data["aggregate"].items():
            print(f"{strategy:18} {domain:12} {agg['accuracy']:6.0%} "
                  f"{agg['mean_tokens']:8.0f} {agg['mean_wall_ms'] / 1000:7.1f}")


if __name__ == "__main__":
    asyncio.run(main())
