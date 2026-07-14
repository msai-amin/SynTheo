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
from core.tiers import run_tier2


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
        print(f"\n=== {ev['confidence_type'].upper()} ({ev['detail']}) ===")
        print(f"answer: {ev['answer']}")
        print(f"tokens in/out: {ev['tokens_in']}/{ev['tokens_out']}  "
              f"wall: {ev['wall_ms']}ms  run_id: {ev['run_id']}")
    elif t in ("verifying", "judging"):
        print(f"[{t}] {ev['count']} samples...")


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

    args = parser.parse_args(argv)
    if args.problem.startswith("@"):
        args.problem = Path(args.problem[1:]).read_text().strip()
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
