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

import json
import random
import re

from core.extract import answers_equivalent, extract_answer
from core.llm import LLMClient, LLMError, Sample
from core.router import RouteError, route
from core.store import Store
from core.verify.execute import verify_by_execution
from core.verify.isabelle_hol import _PROOF_SEMAPHORE, check_isabelle, extract_theory
from core.verify.judge import FAMILIES, JudgeError, judge_sample
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

# Open-ended (non-mechanically-verifiable) domains have no gold answer to
# recompute, so the math contract's code-verification step doesn't apply, and
# a single-word answer block guarantees rubric failure — the block must carry
# the actual argument.
OPEN_ENDED_PROMPT = """Answer this question directly.

{problem}

Give your reasoning, engaging the strongest counterargument you can think of.
Then end your response with a fenced answer block containing your full \
position and its core justification (2-5 sentences) — not a single word or a \
one-line verdict:
```answer
<your position and its core justification>
```"""


def _select_prompt(domain: str) -> str:
    return TIER2_PROMPT if domain in ("math", "logic") else OPEN_ENDED_PROMPT

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
                "full_text": top[0].text,
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
                "full_text": top[0].text,
                "confidence_type": "consensus",
                "detail": f"{len(top)}/{total}",
                "winner_index": top[0].index,
            }

    return None


async def _verify_record(rec: SampleRecord, domain: str,
                         sem: asyncio.Semaphore) -> None:
    """Attach mechanical verification results to one record (CPU-side, free
    while the GPU decodes — spec 1.1)."""
    # metaphysics: the sample IS an Isabelle theory (no ```answer block), verified by
    # the kernel, NOT the Python sandbox. Serialized globally (ADR-002 memory). [ADR-006]
    if domain == "metaphysics":
        theory = extract_theory(rec.text)
        if theory is None:
            rec.verifications.append(
                {"verified": False, "method": "isabelle-unverifiable",
                 "detail": "no theory block"})
            return
        async with _PROOF_SEMAPHORE:
            result = await asyncio.to_thread(check_isabelle, theory, None)
        rec.verifications.append(result)
        return
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
    verify: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """Verified Best-of-N. Yields progress events; the last event is type=result.

    verify=False turns this into the pure self-consistency baseline (majority
    vote over canonicalized answers, no mechanical verification) for run_eval.
    """
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
    prompt = _select_prompt(domain).format(problem=problem, smt_clause=smt_clause)
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
    # non-votable runs go straight to the judge. verify=False additionally
    # disables it for the self-consistency baseline strategy.
    if votable and verify:
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
            "full_text": best.text,
            "confidence_type": "judged",
            "detail": f"score {best.judgment['score']:.1f}",
            "winner_index": best.index,
        }

    wall_ms = (time.monotonic() - started) * 1000
    tokens_in = sum(r.tokens_in for r in records)
    tokens_out = sum(r.tokens_out for r in records)

    if selection is None:
        selection = {"answer": None, "full_text": None, "confidence_type": "failed",
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


# ============================================================================
# Tier 1 — reflex: single fast-model shot, verified or escalate. [M3]
# ============================================================================

async def run_tier1(
    client: LLMClient,
    store: Store | None,
    problem: str,
    *,
    domain: str = "math",
    votable: bool = True,
    problem_id: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """One fast-model sample. Yields a `result` event with confidence_type
    verified|unverified — callers (run_auto) are responsible for escalating an
    unverified result to Tier 2, per the misroute-insurance rule [LOCKED]."""
    cfg = client.config
    fast_alias = cfg["tiers"]["tier1"]["model"]
    smt_clause = SMT_CLAUSE if domain == "logic" else ""
    prompt = _select_prompt(domain).format(problem=problem, smt_clause=smt_clause)
    messages = [{"role": "user", "content": prompt}]

    run_id = None
    if store is not None:
        if problem_id is None:
            problem_id = store.add_problem(problem, domain)
        run_id = store.start_run(problem_id, tier=1, strategy="tier1", config={
            "model": fast_alias, "domain": domain, "votable": votable})

    started = time.monotonic()
    yield {"type": "sampling", "n": 1, "plan": [{"model": fast_alias, "temp": 0.0}]}
    try:
        samples = await client.complete(fast_alias, messages, temperature=0.0,
                                        max_tokens=4096,
                                        run_id=str(run_id) if run_id else None)
        s = samples[0]
        text, extracted = s.text or s.reasoning, extract_answer(s.text or s.reasoning)
        rec = SampleRecord(index=0, model=fast_alias, temp=0.0, text=text,
                           extracted=extracted, tokens_in=s.tokens_in,
                           tokens_out=s.tokens_out, latency_ms=s.latency_ms)
    except LLMError as exc:
        rec = SampleRecord(index=0, model=fast_alias, temp=0.0, text="",
                           extracted=None, tokens_in=0, tokens_out=0,
                           latency_ms=0.0, error=str(exc))

    yield {"type": "sample", "index": 0, "model": fast_alias,
           "extracted": rec.extracted, "error": rec.error, "k": 1, "n": 1}

    if rec.error is None and domain == "metaphysics":
        # A formal proof is verifiable though not "votable"; kernel-check regardless. [ADR-006]
        theory = extract_theory(rec.text)
        if theory is not None:
            async with _PROOF_SEMAPHORE:
                result = await asyncio.to_thread(check_isabelle, theory, None)
        else:
            result = {"verified": False, "method": "isabelle-unverifiable",
                      "detail": "no theory block"}
        rec.verifications.append(result)
        yield {"type": "verification", "index": 0, "verified": rec.verified,
               "methods": rec.verifications}
    elif rec.error is None and votable:
        result = await asyncio.to_thread(verify_by_execution, rec.text, rec.extracted) \
            if rec.extracted else {"verified": False, "method": "extract",
                                   "detail": "no answer block"}
        rec.verifications.append(result)
        if domain == "logic":
            smt = extract_smt(rec.text)
            z3_result = await asyncio.to_thread(
                check_logic, [smt] if smt else [], rec.extracted or "")
            rec.verifications.append(z3_result)
        yield {"type": "verification", "index": 0, "verified": rec.verified,
               "methods": rec.verifications}

    wall_ms = (time.monotonic() - started) * 1000
    # a kernel countermodel is a POSITIVE result (the claim is false), distinct from
    # an unproved goal — surface it as its own label [ADR-006].
    refuted = any(v.get("method") == "isabelle-refuted" for v in rec.verifications)
    confidence_type = ("verified" if rec.verified
                       else "refuted" if refuted else "unverified")

    if store is not None and run_id is not None:
        sample_id = store.add_sample(run_id, rec.model, rec.temp,
                                     rec.text or (rec.error or ""), rec.extracted,
                                     rec.tokens_out, rec.latency_ms)
        for v in rec.verifications:
            store.add_verification(sample_id, v["method"], v["verified"], v["detail"])
        store.finish_run(run_id, verdict=confidence_type, answer=rec.extracted,
                         confidence_type=confidence_type, tokens_in=rec.tokens_in,
                         tokens_out=rec.tokens_out, wall_ms=wall_ms)

    yield {"type": "result", "run_id": run_id, "answer": rec.extracted,
           "full_text": rec.text or None,
           "confidence_type": confidence_type,
           "detail": "single fast sample" + ("" if rec.verified else " (unverified)"),
           "tokens_in": rec.tokens_in, "tokens_out": rec.tokens_out,
           "wall_ms": round(wall_ms), "n_samples": 1, "n_errors": int(rec.error is not None)}


async def _shadow_audit(client: LLMClient, store: Store, problem: str,
                        domain: str, problem_id: int, tier1_answer: str | None) -> None:
    """1% of Tier-1 verified successes are shadow-re-run at Tier 2 in the
    background and compared — the router-audit metric [spec §3.1]. Never
    raises into the caller; failures here must not affect the served answer."""
    try:
        result = None
        async for ev in run_tier2(client, store, problem, domain=domain,
                                  votable=True, problem_id=problem_id,
                                  strategy="shadow-audit"):
            if ev["type"] == "result":
                result = ev
        if result is not None:
            agree = (tier1_answer is not None and result["answer"] is not None
                    and answers_equivalent(tier1_answer, str(result["answer"])))
            store.add_correction(
                result["run_id"],
                f"shadow-audit vs tier1 answer={tier1_answer!r}: "
                f"{'AGREE' if agree else 'DISAGREE'}")
    except Exception:
        pass  # audit is best-effort; never let it surface as a user-facing error


# ============================================================================
# Tier 3 — adversarial: prover/skeptic/rebuttal/judge. [LOCKED — spec §3.4]
# ============================================================================

PROVER_PROMPT = """State your position on this question. Give explicit,
numbered premises, then your reasoning, then your conclusion.

End your response with:
```premises
["<premise 1>", "<premise 2>", ...]
```
```answer
<your conclusion, one to two sentences>
```

Question:
{problem}"""

# [LOCKED — spec §3.4] exact role prompt
SKEPTIC_PROMPT = """You are rewarded ONLY for the strength of your attack.
Steelman the opposing position, identify the weakest premise, produce the
single strongest counterargument. Agreeing is failure.

The question was:
{problem}

The position to attack:
{position}

Respond with your steelman of the opposing view, your identification of the
weakest premise, and your strongest counterargument. End with:
```objection
<your single strongest counterargument, one to two sentences>
```"""

REBUTTAL_PROMPT = """Your position was challenged. Here is the strongest
counterargument against it:

{objection}

Respond to this specific counterargument. You may concede a point, refine your
position, or defend it — but do not ignore the objection. End with:
```answer
<your final conclusion after considering the objection, one to two sentences>
```"""

# Fresh context: the judge sees a structured digest, never the raw transcript
# (context-ceiling defense [LOCKED] — one rebuttal round, no growing transcript).
SYNTHESIS_PROMPT = """You are judging a philosophical dispute. You did not
witness the debate directly; here is a neutral digest of it.

Question: {problem}

Position (with premises):
{position}
Premises: {premises}

Strongest objection raised against the position:
{objection}

Position's response to that objection:
{rebuttal}

Score the position 1-10 and render a verdict. Your "answer" must COMMIT: name
which position on the question is more defensible and the single strongest
reason, in a clear, direct voice. Do not hedge, split the difference, survey
both sides, or restate the numeric score in the answer — residual uncertainty
belongs in the other fields (report the unresolved weakness in
strongest_surviving_objection; signal how close the call was in
judge_confidence, which may be low for a genuinely balanced question).

The surviving objection is the strongest part of the counterargument that the
rebuttal did NOT adequately answer — there is almost always one; only say
"none" if the rebuttal is airtight, which is rare. Committing to a verdict does
not mean the objection is resolved; a confident answer can still ship a real
surviving objection.

Respond with JSON only:
{{"answer": "<a committed position on the question and its core reason, 1-3
    sentences — a verdict, not a survey of both sides or a restatement of the score>",
  "key_premises": ["<premise>", ...],
  "strongest_surviving_objection": "<specific unresolved weakness, or the
    single word 'none' if truly none remains>",
  "judge_confidence": <1-10>}}"""


class Tier3ContractError(ValueError):
    """The output contract [LOCKED] was not met: every Tier-3 answer must ship
    with a surviving objection, or the run is a validation failure, not a
    quietly-degraded success."""


def _extract_fenced(text: str, tag: str) -> str | None:
    m = re.search(rf"```{tag}\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else None


def _parse_synthesis(raw: str) -> dict:
    start = raw.find("{")
    if start == -1:
        raise Tier3ContractError(f"judge produced no JSON: {raw[:200]!r}")
    try:
        # raw_decode stops at the first complete object; a greedy regex to the
        # LAST '}' breaks when the model appends anything after the object
        data, _ = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError as exc:
        raise Tier3ContractError(f"judge JSON invalid: {exc}") from exc
    for field_name in ("answer", "key_premises", "strongest_surviving_objection",
                       "judge_confidence"):
        if field_name not in data:
            raise Tier3ContractError(f"judge output missing {field_name!r}")
    objection = str(data["strongest_surviving_objection"]).strip()
    if not objection:
        raise Tier3ContractError("empty strongest_surviving_objection")
    return data


async def run_tier3(
    client: LLMClient,
    store: Store | None,
    problem: str,
    *,
    domain: str = "philosophy",
    problem_id: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Prover -> skeptic -> one rebuttal round -> fresh-context judge synthesis.
    Raises Tier3ContractError if the output contract [LOCKED] can't be met."""
    cfg = client.config
    tier3_cfg = cfg["tiers"]["tier3"]
    prover_alias = tier3_cfg["prover_model"]
    skeptic_alias = tier3_cfg["skeptic_model"]
    judge_alias = tier3_cfg["judge_model"]
    assert FAMILIES[judge_alias] not in (FAMILIES[prover_alias], FAMILIES[skeptic_alias]), \
        "Tier-3 judge must be a third family, distinct from prover and skeptic"

    run_id = None
    if store is not None:
        if problem_id is None:
            problem_id = store.add_problem(problem, domain)
        run_id = store.start_run(problem_id, tier=3, strategy="tier3", config={
            "prover": prover_alias, "skeptic": skeptic_alias, "judge": judge_alias,
            "max_rebuttal_rounds": tier3_cfg["max_rebuttal_rounds"]})

    started = time.monotonic()
    tokens_in = tokens_out = 0

    def _account(s: Sample) -> None:
        nonlocal tokens_in, tokens_out
        tokens_in += s.tokens_in
        tokens_out += s.tokens_out

    yield {"type": "proving", "model": prover_alias}
    prover_samples = await client.complete(
        prover_alias, [{"role": "user", "content": PROVER_PROMPT.format(problem=problem)}],
        temperature=0.7, max_tokens=4096, run_id=str(run_id) if run_id else None)
    prover_text = prover_samples[0].text or prover_samples[0].reasoning
    _account(prover_samples[0])
    position = _extract_fenced(prover_text, "answer") or prover_text[-500:]
    premises_raw = _extract_fenced(prover_text, "premises") or "[]"
    try:
        premises = json.loads(premises_raw)
    except json.JSONDecodeError:
        premises = [premises_raw]
    yield {"type": "proved", "position": position}

    yield {"type": "skepticizing", "model": skeptic_alias}
    skeptic_samples = await client.complete(
        skeptic_alias,
        [{"role": "user", "content": SKEPTIC_PROMPT.format(problem=problem, position=position)}],
        temperature=0.8, max_tokens=4096, run_id=str(run_id) if run_id else None)
    skeptic_text = skeptic_samples[0].text or skeptic_samples[0].reasoning
    _account(skeptic_samples[0])
    objection = _extract_fenced(skeptic_text, "objection") or skeptic_text[-500:]
    yield {"type": "skepticized", "objection": objection}

    # exactly one rebuttal round [LOCKED context-ceiling defense]
    yield {"type": "rebutting", "model": prover_alias}
    rebuttal_samples = await client.complete(
        prover_alias,
        [{"role": "user", "content": PROVER_PROMPT.format(problem=problem)},
         {"role": "assistant", "content": prover_text},
         {"role": "user", "content": REBUTTAL_PROMPT.format(objection=objection)}],
        temperature=0.7, max_tokens=4096, run_id=str(run_id) if run_id else None)
    rebuttal_text = rebuttal_samples[0].text or rebuttal_samples[0].reasoning
    _account(rebuttal_samples[0])
    rebuttal = _extract_fenced(rebuttal_text, "answer") or rebuttal_text[-500:]
    yield {"type": "rebutted", "rebuttal": rebuttal}

    # judge sees a FRESH context: a structured digest, never the raw transcript
    yield {"type": "judging", "model": judge_alias}
    digest_prompt = SYNTHESIS_PROMPT.format(
        problem=problem, position=position, premises=json.dumps(premises),
        objection=objection, rebuttal=rebuttal)
    synthesis_data = None
    last_error = None
    for attempt in range(2):  # one retry if the contract isn't met, then fail loudly
        judge_samples = await client.complete(
            judge_alias, [{"role": "user", "content": digest_prompt}],
            temperature=0.2, max_tokens=2048, run_id=str(run_id) if run_id else None)
        _account(judge_samples[0])
        try:
            synthesis_data = _parse_synthesis(judge_samples[0].text or judge_samples[0].reasoning)
            break
        except Tier3ContractError as exc:
            last_error = exc
            digest_prompt = (SYNTHESIS_PROMPT.format(
                problem=problem, position=position, premises=json.dumps(premises),
                objection=objection, rebuttal=rebuttal)
                + "\n\nYour previous response violated the output contract "
                  f"({exc}). Every field is required; strongest_surviving_objection "
                  "must be non-empty (use 'none' only if truly none remains).")

    wall_ms = (time.monotonic() - started) * 1000

    if synthesis_data is None:
        if store is not None and run_id is not None:
            store.finish_run(run_id, verdict="invalid", answer=None,
                             confidence_type="invalid", tokens_in=tokens_in,
                             tokens_out=tokens_out, wall_ms=wall_ms)
        raise Tier3ContractError(f"Tier-3 output contract violated twice: {last_error}")

    if store is not None and run_id is not None:
        store.finish_run(run_id, verdict="judged", answer=synthesis_data["answer"],
                         confidence_type="judged", tokens_in=tokens_in,
                         tokens_out=tokens_out, wall_ms=wall_ms)

    full_text = (
        f"Position: {position}\nPremises: {json.dumps(premises)}\n\n"
        f"Strongest objection: {objection}\n\n"
        f"Response to objection: {rebuttal}\n\n"
        f"Synthesis: {synthesis_data['answer']}\n"
        f"Surviving objection: {synthesis_data['strongest_surviving_objection']}"
    )

    yield {"type": "result", "run_id": run_id, "answer": synthesis_data["answer"],
           "full_text": full_text,
           "key_premises": synthesis_data["key_premises"],
           "strongest_surviving_objection": synthesis_data["strongest_surviving_objection"],
           "judge_confidence": synthesis_data["judge_confidence"],
           "confidence_type": "judged", "tokens_in": tokens_in, "tokens_out": tokens_out,
           "wall_ms": round(wall_ms)}


# ============================================================================
# Formal metaphysics — Isabelle/HOL proving. [ADR-006 Phase 1]
# ============================================================================

# The `\<...>` ASCII form of the Isabelle symbols is used throughout (Isabelle accepts
# it identically to the unicode glyphs) so the whole pipeline stays plain-ASCII.
FORMAL_PROMPT = r"""You are proving or refuting a claim in formal metaphysics with
Isabelle/HOL. Higher-order modal logic (logic KB) is embedded in HOL by the AFP entry
GoedelGod — Scott's version of Goedel's ontological argument.

Question:
{problem}
{target_clause}
Write ONE complete Isabelle theory. Rules:
- Name it `Submission` and import the embedding: imports "GoedelGod.GoedelGod".
- Embedding syntax (propositions have type \<sigma> = i\<Rightarrow>bool; use the \<...> forms):
    [p]                   p is valid (holds in every world)
    \<box> p   \<diamond> p          necessity / possibility
    m\<not>  m\<and>  m\<or>  m\<rightarrow>  m\<equiv>    lifted connectives
    \<forall>(\<lambda>x. ...)   \<exists>(\<lambda>x. ...)   lifted quantifiers (individuals or properties)
    P    positiveness      G    God-like      x mL= y   Leibniz equality
- Proved theorems you may CITE (do not re-derive from axioms):
    T1: "[\<forall>(\<lambda>\<Phi>. P \<Phi> m\<rightarrow> \<diamond> (\<exists> \<Phi>))]"     positive props are possibly exemplified
    C:  "[\<diamond> (\<exists> G)]"                       possibly, God exists
    T2: "[\<forall>(\<lambda>x. G x m\<rightarrow> G ess x)]"          God-likeness is an essence of a God
    T3: "[\<box> (\<exists> G)]"                       necessarily, God exists
    C2: "[\<exists> G]"                             God exists
    Flawlessness, Monotheism ; axioms A1a A1b A2 A3 A4 A5 sym ; defs G_def NE_def ess_def
- To PROVE a claim, state it and close it — cite the matching theorem:
    theory Submission imports "GoedelGod.GoedelGod" begin
    theorem target: "[\<box> (\<exists> G)]" using T3 by simp
    end
- To REFUTE a claim (show it is FALSE / has a countermodel), let Nitpick find it — the
  harness reads a found countermodel as a refutation:
    theory Submission imports "GoedelGod.GoedelGod" begin
    theorem target: "[m\<not> (\<exists> G)]" nitpick [user_axioms, expect = genuine] oops
    end
- PROOF SEARCH: if NO single theorem states your goal, you must PROVE it — do not give up.
  A bare `sledgehammer` does NOT close a goal in batch mode; write the actual tactic it
  would suggest. Effective closers, roughly in order to try:
    by simp        by auto        by blast        by (simp add: G_def NE_def ess_def)
    using <thms> by metis          using <thms> by (metis (full_types))
    using <thms> by (smt (verit))                (Z3, CVC5, E, Vampire, SPASS are available)
  Combine several cited theorems when needed, e.g. `using T3 Monotheism by simp` or
  `by (metis C T1 T3)`. Unfold definitions with `add: G_def` to reason under `G`.
- Never write sorry, and never add a new axiomatization/axiom — use only what the entry
  already proves. Reproduce the target statement CHARACTER-FOR-CHARACTER, including every
  parenthesis and the exact operator spelling. The lifted modal operators have NO internal
  space: write `m\<and>` `m\<or>` `m\<rightarrow>` `m\<not>` `m\<equiv>` — never `m \<and>` or
  `m \<rightarrow>`. A space splits the operator into two tokens and the goal will not match.

Output ONLY the theory in a single ```isabelle fenced block."""

REPAIR_PROMPT = r"""Your theory did not check. Isabelle reported:

{error}

Fix it and output ONLY the corrected complete ```isabelle block. Likely fixes: cite the
right existing theorem (T1/C/T2/T3/C2/Flawlessness/Monotheism); try a stronger closer —
`by (metis <lemmas>)`, `by (smt (verit))`, `by auto`, `by blast`, or `by (simp add: G_def)`;
combine several `using` lemmas; keep the import `"GoedelGod.GoedelGod"`; state the goal exactly."""


def _formal_answer(confidence_type: str, target: str | None) -> str:
    what = f" of `{target}`" if target else ""
    return {
        "verified": f"Proved{what}: kernel-checked theorem.",
        "refuted": f"Refuted{what}: a countermodel exists, so the claim is not valid.",
        "unverified": f"Unresolved{what}: no kernel proof or countermodel was produced.",
    }[confidence_type]


async def run_tier_formal(
    client: LLMClient,
    store: Store | None,
    problem: str,
    *,
    domain: str = "metaphysics",
    target: str | None = None,
    parent_session: str | None = None,
    problem_id: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Prover writes an Isabelle/Isar theory; the LCF kernel adjudicates. One repair
    round on a failed build (fed the kernel's own error). Yields progress events; the
    last is type=result with confidence_type verified|refuted|unverified. [ADR-006]"""
    cfg = client.config
    fcfg = cfg["tiers"]["formal"]
    prover_alias = fcfg["prover_model"]
    max_repair = fcfg["max_repair_rounds"]

    run_id = None
    if store is not None:
        if problem_id is None:
            problem_id = store.add_problem(problem, domain)
        run_id = store.start_run(problem_id, tier=3, strategy="formal", config={
            "prover": prover_alias, "parent_session": parent_session or fcfg["parent_session"],
            "target": target, "max_repair_rounds": max_repair})

    started = time.monotonic()
    tokens_in = tokens_out = 0
    target_clause = (f"\nProve or refute EXACTLY this statement:\n```\n{target}\n```\n"
                     if target else "\nDecide whether the claim holds; state your target theorem.\n")
    messages = [{"role": "user",
                 "content": FORMAL_PROMPT.format(problem=problem, target_clause=target_clause)}]

    verdict: dict[str, Any] = {"verified": False, "method": "isabelle-unverifiable",
                               "detail": "no theory produced"}
    theory: str | None = None

    for attempt in range(max_repair + 1):
        yield {"type": "formalizing", "model": prover_alias, "attempt": attempt + 1}
        try:
            samples = await client.complete(prover_alias, messages, temperature=0.2,
                                            max_tokens=4096,
                                            run_id=str(run_id) if run_id else None)
        except LLMError as exc:
            verdict = {"verified": False, "method": "isabelle-unverifiable",
                       "detail": f"prover call failed: {exc}"}
            break
        s = samples[0]
        tokens_in += s.tokens_in
        tokens_out += s.tokens_out
        text = s.text or s.reasoning
        theory = extract_theory(text)
        if theory is None:
            verdict = {"verified": False, "method": "isabelle-unverifiable",
                       "detail": "no ```isabelle theory block in model output"}
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "You did not emit a ```isabelle "
                          "fenced theory block. Output ONLY the complete theory in one."}]
            continue

        yield {"type": "checking", "attempt": attempt + 1}
        async with _PROOF_SEMAPHORE:
            verdict, raw = await asyncio.to_thread(
                check_isabelle, theory, target, return_raw=True)
        yield {"type": "checked", "attempt": attempt + 1,
               "method": verdict["method"], "detail": verdict["detail"]}

        if verdict["verified"] or verdict["method"] == "isabelle-refuted":
            break
        if attempt < max_repair:
            err = (raw or verdict["detail"])[-1500:]
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": REPAIR_PROMPT.format(error=err)}]
            yield {"type": "repairing", "attempt": attempt + 2}

    wall_ms = (time.monotonic() - started) * 1000
    confidence_type = ("verified" if verdict["verified"]
                       else "refuted" if verdict["method"] == "isabelle-refuted"
                       else "unverified")
    answer = _formal_answer(confidence_type, target)
    full_text = (theory or "") + f"\n\n(* verdict: {verdict['method']} — {verdict['detail']} *)"

    if store is not None and run_id is not None:
        sample_id = store.add_sample(run_id, prover_alias, 0.2, theory or "",
                                     target, tokens_out, wall_ms)
        store.add_verification(sample_id, verdict["method"], verdict["verified"],
                               verdict["detail"])
        store.finish_run(run_id, verdict=confidence_type, answer=answer,
                         confidence_type=confidence_type, tokens_in=tokens_in,
                         tokens_out=tokens_out, wall_ms=wall_ms)

    yield {"type": "result", "run_id": run_id, "answer": answer, "full_text": full_text,
           "theory": theory, "method": verdict["method"], "detail": verdict["detail"],
           "confidence_type": confidence_type, "tokens_in": tokens_in,
           "tokens_out": tokens_out, "wall_ms": round(wall_ms)}


# ============================================================================
# Auto-routing with escalation. [M3 — spec §3.1 misroute insurance]
# ============================================================================

async def run_auto(
    client: LLMClient,
    store: Store | None,
    problem: str,
) -> AsyncIterator[dict[str, Any]]:
    """Route, run the assigned tier, escalate on the LOCKED misroute-insurance
    rules, and log every escalation with its reason."""
    cfg = client.config
    problem_id = store.add_problem(problem, "unrouted") if store is not None else None

    try:
        r = await route(client, problem)
    except RouteError as exc:
        yield {"type": "route_failed", "error": str(exc)}
        # fail safe: an unclassifiable problem gets the most thorough treatment
        r = None

    if r is None:
        async for ev in run_tier2(client, store, problem, domain="math",
                                  votable=False, problem_id=problem_id,
                                  strategy="auto->tier2(route-failed)"):
            yield ev
        return

    yield {"type": "routed", "domain": r.domain, "difficulty": r.difficulty,
           "votable": r.votable, "rationale": r.rationale, "tier": r.tier}

    # metaphysics is dispatched by DOMAIN to the formal (Isabelle) flow, not by tier
    # number — it is a distinct pipeline (prove/refute with the kernel). [ADR-006]
    if r.domain == "metaphysics":
        async for ev in run_tier_formal(client, store, problem, problem_id=problem_id):
            yield ev
        return

    if r.tier == 1:
        result = None
        async for ev in run_tier1(client, store, problem, domain=r.domain,
                                  votable=r.votable, problem_id=problem_id):
            if ev["type"] == "result":
                result = ev
            yield ev
        if result["confidence_type"] != "verified":
            yield {"type": "escalation", "from_tier": 1, "to_tier": 2,
                   "reason": "tier1 result failed verification"}
            async for ev in run_tier2(client, store, problem, domain=r.domain,
                                      votable=r.votable, problem_id=problem_id,
                                      strategy="tier1->tier2"):
                yield ev
            return
        # shadow-audit: 1% of Tier-1 verified successes re-run at Tier 2 in
        # the background for the router-audit metric [spec §3.1]
        if store is not None and random.random() < cfg["routing"]["shadow_rerun_fraction"]:
            asyncio.create_task(_shadow_audit(
                client, store, problem, r.domain, problem_id, result["answer"]))
        return

    if r.tier == 2:
        result = None
        async for ev in run_tier2(client, store, problem, domain=r.domain,
                                  votable=r.votable, problem_id=problem_id,
                                  strategy="auto->tier2"):
            if ev["type"] == "result":
                result = ev
            yield ev
        if result["confidence_type"] not in ("verified", "consensus"):
            yield {"type": "escalation", "from_tier": 2, "to_tier": 3,
                   "reason": f"tier2 unverified disagreement "
                             f"({result['confidence_type']})"}
            async for ev in run_tier3(client, store, problem, domain="mixed",
                                      problem_id=problem_id):
                yield ev
        return

    async for ev in run_tier3(client, store, problem, domain=r.domain,
                              problem_id=problem_id):
        yield ev
