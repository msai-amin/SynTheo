"""Tier orchestration. M2: Tier 2 — verified Best-of-N.

run_tier2 is an async generator of event dicts so the CLI (now) and the SSE API
(M5) consume the same stream: routing/sampling/verifying/judging/result events.

Selection cascade [LOCKED]:
  1. mechanical verification passes on >=1 sample -> majority among verified only
  2. none verifiable but votable -> self-consistency majority + agreement ratio
  3. else -> cross-family judge rerank, top score attached
Confidence is verified | consensus | judged and is never collapsed to one number.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from core.extract import answers_equivalent, extract_answer
from core.llm import LLMClient, LLMError, Sample
from core.store import Store
from core.verify.execute import verify_by_execution
from core.verify.judge import JudgeError, judge_sample
from core.verify.logic_z3 import check_logic, extract_smt

TIER2_PROMPT = """Solve this problem step by step.

{problem}

Requirements for your response, in this order:
1. Your reasoning.
2. A ```python code block that INDEPENDENTLY verifies your answer (recompute it \
a different way if possible) and prints the final answer as the last line.{smt_clause}
3. A fenced answer block containing ONLY the final answer:
```answer
<final answer>
```"""

SMT_CLAUSE = """
2b. A ```smtlib code block encoding the problem's constraints in SMT-LIB2 \
(declare-const each unknown; assert only stated constraints; also declare a \
constant `answer` equal to the queried quantity; no check-sat)."""

_EXEC_CONCURRENCY = 4  # docker sandbox runs in flight at once


@dataclass
class SampleRecord:
    """One candidate: the sample plus everything the verifiers said about it."""
    index: int
    model: str
    temp: float
    text: str
    extracted: str | None
    tokens_in: int
    tokens_out: int
    latency_ms: float
    verifications: list[dict] = field(default_factory=list)
    judgment: dict | None = None
    error: str | None = None

    @property
    def verified(self) -> bool:
        return any(v["verified"] for v in self.verifications)


def cluster_answers(records: list[SampleRecord]) -> list[list[SampleRecord]]:
    """Group records whose extracted answers are equivalent; largest first."""
    clusters: list[list[SampleRecord]] = []
    for rec in records:
        if rec.extracted is None:
            continue
        for cluster in clusters:
            if answers_equivalent(rec.extracted, cluster[0].extracted):
                cluster.append(rec)
                break
        else:
            clusters.append([rec])
    clusters.sort(key=len, reverse=True)
    return clusters


def select_answer(records: list[SampleRecord], votable: bool) -> dict | None:
    """Stages 1-2 of the cascade (pure function; judge stage needs the LLM).

    Returns {answer, confidence_type, detail, winner_index} or None if the
    caller must fall through to judge rerank.
    """
    usable = [r for r in records if r.error is None]

    verified = [r for r in usable if r.verified]
    if verified:
        clusters = cluster_answers(verified)
        if clusters:
            top = clusters[0]
            return {
                "answer": top[0].extracted,
                "confidence_type": "verified",
                "detail": f"{len(top)}/{len(verified)} verified samples agree",
                "winner_index": top[0].index,
            }

    if votable:
        clusters = cluster_answers(usable)
        # consensus requires an actual majority signal, not a 1-1-1 scatter
        if clusters and len(clusters[0]) >= 2:
            top = clusters[0]
            total = sum(len(c) for c in clusters)
            return {
                "answer": top[0].extracted,
                "confidence_type": "consensus",
                "detail": f"{len(top)}/{total}",
                "winner_index": top[0].index,
            }

    return None


async def _verify_record(rec: SampleRecord, domain: str,
                         sem: asyncio.Semaphore) -> None:
    """Attach mechanical verification results to one record (CPU-side, free
    while the GPU decodes — spec 1.1)."""
    if rec.extracted is None:
        rec.verifications.append(
            {"verified": False, "method": "extract", "detail": "no answer block"})
        return
    async with sem:
        result = await asyncio.to_thread(verify_by_execution, rec.text, rec.extracted)
    rec.verifications.append(result)
    if domain == "logic":
        smt = extract_smt(rec.text)
        encodings = [smt] if smt else []
        z3_result = await asyncio.to_thread(check_logic, encodings, rec.extracted)
        rec.verifications.append(z3_result)


async def run_tier2(
    client: LLMClient,
    store: Store | None,
    problem: str,
    *,
    domain: str = "math",
    votable: bool = True,
    n: int | None = None,
    problem_id: int | None = None,
    strategy: str = "tier2",
) -> AsyncIterator[dict[str, Any]]:
    """Verified Best-of-N. Yields progress events; the last event is type=result."""
    cfg = client.config
    tier_cfg = cfg["tiers"]["tier2"]
    n = n or tier_cfg["n"]
    split = tier_cfg["split"]
    t_lo, t_hi = tier_cfg["temperature_range"]

    # apportion n across models by the configured 5:3 ratio
    total_share = sum(split.values())
    plan: list[tuple[str, float]] = []
    aliases = list(split)
    for alias in aliases:
        k = round(n * split[alias] / total_share)
        plan.extend((alias, 0.0) for _ in range(k))
    plan = plan[:n] or [(aliases[0], 0.0)]
    plan = [(alias, t_lo + (t_hi - t_lo) * i / max(len(plan) - 1, 1))
            for i, (alias, _) in enumerate(plan)]

    smt_clause = SMT_CLAUSE if domain == "logic" else ""
    prompt = TIER2_PROMPT.format(problem=problem, smt_clause=smt_clause)
    messages = [{"role": "user", "content": prompt}]

    run_id = None
    if store is not None:
        if problem_id is None:
            problem_id = store.add_problem(problem, domain)
        run_id = store.start_run(problem_id, tier=2, strategy=strategy, config={
            "n": n, "split": split, "temperature_range": [t_lo, t_hi],
            "domain": domain, "votable": votable,
            "models": {a: cfg["models"][a]["repo"] for a in split},
        })

    started = time.monotonic()
    yield {"type": "sampling", "n": len(plan),
           "plan": [{"model": a, "temp": round(t, 2)} for a, t in plan]}

    # one batched burst: all requests in flight at once; vLLM batches server-side
    async def _one(i: int, alias: str, temp: float) -> SampleRecord:
        try:
            samples: list[Sample] = await client.complete(
                alias, messages, temperature=temp, max_tokens=8192,
                run_id=str(run_id) if run_id else None)
            s = samples[0]
            return SampleRecord(
                index=i, model=alias, temp=temp, text=s.text or s.reasoning,
                extracted=extract_answer(s.text or s.reasoning),
                tokens_in=s.tokens_in, tokens_out=s.tokens_out,
                latency_ms=s.latency_ms)
        except LLMError as exc:
            return SampleRecord(
                index=i, model=alias, temp=temp, text="", extracted=None,
                tokens_in=0, tokens_out=0, latency_ms=0.0, error=str(exc))

    records: list[SampleRecord] = []
    tasks = [asyncio.create_task(_one(i, a, t)) for i, (a, t) in enumerate(plan)]
    for done in asyncio.as_completed(tasks):
        rec = await done
        records.append(rec)
        yield {"type": "sample", "index": rec.index, "model": rec.model,
               "temp": round(rec.temp, 2), "extracted": rec.extracted,
               "error": rec.error, "k": len(records), "n": len(plan)}

    records.sort(key=lambda r: r.index)
    ok_records = [r for r in records if r.error is None]

    # Mechanical verification only applies to votable (recomputable) answers.
    # An open-ended answer has nothing a program can recompute — models will
    # happily "verify" one by printing it back (observed in the M2 gate), so
    # non-votable runs go straight to the judge.
    if votable:
        yield {"type": "verifying", "count": len(ok_records)}
        sem = asyncio.Semaphore(_EXEC_CONCURRENCY)
        await asyncio.gather(*(_verify_record(r, domain, sem) for r in ok_records))
        for rec in ok_records:
            yield {"type": "verification", "index": rec.index,
                   "verified": rec.verified, "methods": rec.verifications}

    selection = select_answer(records, votable)

    if selection is None and ok_records:
        yield {"type": "judging", "count": len(ok_records)}
        for rec in ok_records:
            try:
                j = await judge_sample(
                    client, problem, rec.text, generator_alias=rec.model,
                    run_id=str(run_id) if run_id else None)
                rec.judgment = {"judge_model": j.judge_alias, "score": j.score,
                                "strongest_flaw": j.strongest_flaw, "raw": j.raw}
            except (JudgeError, LLMError) as exc:
                rec.judgment = {"judge_model": "", "score": 0.0,
                                "strongest_flaw": f"judge failed: {exc}", "raw": ""}
            yield {"type": "judgment", "index": rec.index,
                   "score": rec.judgment["score"]}
        best = max(ok_records, key=lambda r: r.judgment["score"] if r.judgment else 0)
        selection = {
            "answer": best.extracted or best.text[-200:],
            "confidence_type": "judged",
            "detail": f"score {best.judgment['score']:.1f}",
            "winner_index": best.index,
        }

    wall_ms = (time.monotonic() - started) * 1000
    tokens_in = sum(r.tokens_in for r in records)
    tokens_out = sum(r.tokens_out for r in records)

    if selection is None:
        selection = {"answer": None, "confidence_type": "failed",
                     "detail": "no usable samples", "winner_index": None}

    if store is not None and run_id is not None:
        for rec in records:
            sample_id = store.add_sample(
                run_id, rec.model, rec.temp, rec.text or (rec.error or ""),
                rec.extracted, rec.tokens_out, rec.latency_ms)
            for v in rec.verifications:
                store.add_verification(sample_id, v["method"], v["verified"],
                                       v["detail"])
            if rec.judgment:
                store.add_judgment(sample_id, rec.judgment["judge_model"],
                                   rec.judgment["score"],
                                   rec.judgment["strongest_flaw"],
                                   rec.judgment["raw"])
        store.finish_run(
            run_id,
            verdict=selection["confidence_type"],
            answer=selection["answer"],
            confidence_type=selection["confidence_type"],
            tokens_in=tokens_in, tokens_out=tokens_out, wall_ms=wall_ms)

    yield {"type": "result", "run_id": run_id, **selection,
           "tokens_in": tokens_in, "tokens_out": tokens_out,
           "wall_ms": round(wall_ms), "n_samples": len(records),
           "n_errors": len(records) - len(ok_records)}
