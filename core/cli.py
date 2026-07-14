"""CLI for running tiers against the live model stack.

    python -m core.cli tier2 "What is 17*23?" --domain math
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from core.llm import REPO_ROOT, LLMClient, load_config
from core.store import Store
from core.tiers import run_auto, run_tier1, run_tier2, run_tier3


def _print_event(ev: dict) -> None:
    t = ev["type"]
    if t == "sampling":
        plan = ", ".join(f"{p['model']}@{p['temp']}" for p in ev["plan"])
        print(f"[sampling] {ev['n']} samples: {plan}")
    elif t == "sample":
        status = f"error: {ev['error']}" if ev["error"] else f"answer: {ev['extracted']!r}"
        print(f"[sample {ev['k']}/{ev['n']}] {ev['model']} -> {status}")
    elif t == "verification":
        marks = " ".join(
            f"{'✓' if m['verified'] else '✗'}{m['method']}" for m in ev["methods"])
        print(f"[verify] sample {ev['index']}: {marks}")
    elif t == "judgment":
        print(f"[judge] sample {ev['index']}: {ev['score']:.1f}")
    elif t == "result":
        print(f"\n=== {ev['confidence_type'].upper()} ({ev.get('detail', '')}) ===")
        print(f"answer: {ev['answer']}")
        if "strongest_surviving_objection" in ev:
            print(f"key premises: {ev['key_premises']}")
            print(f"strongest surviving objection: {ev['strongest_surviving_objection']}")
            print(f"judge confidence: {ev['judge_confidence']}/10")
        print(f"tokens in/out: {ev['tokens_in']}/{ev['tokens_out']}  "
              f"wall: {ev['wall_ms']}ms  run_id: {ev['run_id']}")
    elif t in ("verifying", "judging"):
        print(f"[{t}] {ev['count']} samples...")
    elif t == "routed":
        print(f"[routed] domain={ev['domain']} difficulty={ev['difficulty']} "
              f"votable={ev['votable']} -> tier {ev['tier']} ({ev['rationale']})")
    elif t == "escalation":
        print(f"[escalation] tier {ev['from_tier']} -> tier {ev['to_tier']}: {ev['reason']}")
    elif t in ("proving", "skepticizing", "rebutting"):
        print(f"[{t}] {ev['model']}...")
    elif t == "proved":
        print(f"[proved] {ev['position'][:200]}")
    elif t == "skepticized":
        print(f"[skepticized] {ev['objection'][:200]}")
    elif t == "rebutted":
        print(f"[rebutted] {ev['rebuttal'][:200]}")
    elif t == "route_failed":
        print(f"[route_failed] {ev['error']}")


async def _tier2(args: argparse.Namespace) -> None:
    cfg = load_config()
    db_path = args.db or REPO_ROOT / cfg["paths"]["episode_db"]
    async with LLMClient(config=cfg) as client:
        with Store(db_path) as store:
            async for ev in run_tier2(
                client, store, args.problem,
                domain=args.domain, votable=not args.not_votable, n=args.n,
            ):
                if args.json:
                    print(json.dumps(ev))
                else:
                    _print_event(ev)


async def _run_stream(args: argparse.Namespace, gen_factory) -> None:
    cfg = load_config()
    db_path = args.db or REPO_ROOT / cfg["paths"]["episode_db"]
    async with LLMClient(config=cfg) as client:
        with Store(db_path) as store:
            async for ev in gen_factory(client, store):
                if args.json:
                    print(json.dumps(ev))
                else:
                    _print_event(ev)


async def _tier1(args: argparse.Namespace) -> None:
    await _run_stream(args, lambda client, store: run_tier1(
        client, store, args.problem, domain=args.domain,
        votable=not args.not_votable))


async def _tier3(args: argparse.Namespace) -> None:
    await _run_stream(args, lambda client, store: run_tier3(
        client, store, args.problem, domain=args.domain))


async def _auto(args: argparse.Namespace) -> None:
    await _run_stream(args, lambda client, store: run_auto(
        client, store, args.problem))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="syntheo")
    sub = parser.add_subparsers(dest="command", required=True)

    p2 = sub.add_parser("tier2", help="verified Best-of-N")
    p2.add_argument("problem", help="problem text, or @path/to/file")
    p2.add_argument("--domain", choices=["math", "logic", "philosophy", "mixed"],
                    default="math")
    p2.add_argument("--n", type=int, default=None)
    p2.add_argument("--not-votable", action="store_true",
                    help="disable self-consistency fallback")
    p2.add_argument("--db", type=Path, default=None)
    p2.add_argument("--json", action="store_true", help="raw event stream")
    p2.set_defaults(func=_tier2)

    p1 = sub.add_parser("tier1", help="reflex: single fast-model shot")
    p1.add_argument("problem", help="problem text, or @path/to/file")
    p1.add_argument("--domain", choices=["math", "logic", "philosophy", "mixed"],
                    default="math")
    p1.add_argument("--not-votable", action="store_true")
    p1.add_argument("--db", type=Path, default=None)
    p1.add_argument("--json", action="store_true")
    p1.set_defaults(func=_tier1)

    p3 = sub.add_parser("tier3", help="adversarial: prover/skeptic/judge")
    p3.add_argument("problem", help="problem text, or @path/to/file")
    p3.add_argument("--domain", choices=["math", "logic", "philosophy", "mixed"],
                    default="philosophy")
    p3.add_argument("--db", type=Path, default=None)
    p3.add_argument("--json", action="store_true")
    p3.set_defaults(func=_tier3)

    pa = sub.add_parser("auto", help="route, run, escalate")
    pa.add_argument("problem", help="problem text, or @path/to/file")
    pa.add_argument("--db", type=Path, default=None)
    pa.add_argument("--json", action="store_true")
    pa.set_defaults(func=_auto)

    args = parser.parse_args(argv)
    if args.problem.startswith("@"):
        args.problem = Path(args.problem[1:]).read_text().strip()
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
