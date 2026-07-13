#!/usr/bin/env python3
"""M0 acceptance gate: three healthy endpoints + one logged batched call of 8
parallel samples (5 heavy, 3 mid — the Tier-2 split) with per-sample latency."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm import LLMClient  # noqa: E402
from serve.healthcheck import load_config, wait_all  # noqa: E402


async def main() -> None:
    cfg = load_config()
    print("--- waiting for heavy/mid/fast to become healthy ---")
    status = wait_all(cfg, timeout_s=1800)
    if not all(v == "green" for v in status.values()):
        print("GATE FAILED: not all endpoints healthy:", status)
        sys.exit(1)
    print("all healthy:", status)

    prompt = [{"role": "user", "content": "What is 17 * 23? Answer with just the number."}]
    plan = [("heavy", prompt, {"n": 5, "temperature": 0.9, "max_tokens": 64})] + [
        ("mid", prompt, {"n": 3, "temperature": 0.9, "max_tokens": 64})
    ]

    async with LLMClient(config=cfg) as client:
        started = time.monotonic()
        samples = await client.batch_complete(plan, run_id="m0-gate")
        wall_ms = (time.monotonic() - started) * 1000

    print(f"\n--- {len(samples)} samples in {wall_ms:.0f} ms wall time ---")
    ok = 0
    for s in samples:
        if isinstance(s, Exception):
            print(f"ERROR: {s}")
            continue
        ok += 1
        print(f"[{s.alias}] {s.latency_ms:.0f}ms  tokens_in={s.tokens_in} tokens_out={s.tokens_out}  text={s.text.strip()[:80]!r}")

    if ok != 8:
        print(f"GATE FAILED: expected 8 successful samples, got {ok}")
        sys.exit(1)
    print("\nGATE PASSED")


if __name__ == "__main__":
    asyncio.run(main())
