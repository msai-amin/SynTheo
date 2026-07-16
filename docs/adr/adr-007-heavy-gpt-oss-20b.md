# ADR-007: Replace the `heavy` model with gpt-oss-20b to reclaim unified memory

**Status:** Accepted
**Date:** 2026-07-16
**Deciders:** Amin Amouhadi (approved after the A/B), Claude (research + validation)

## Decision in one sentence

The `heavy` role switches from `openai/gpt-oss-120b` (66 GiB) to `openai/gpt-oss-20b`
(13.7 GiB) — the same family and MXFP4 quant — freeing ~48 GiB of the unified pool after an
eval A/B showed no loss on the core workload and only run-to-run-variance-level difference on
formal proving.

## The problem we're solving

SynTheo's three resident vLLM engines occupied ~110 of the 121 GiB unified pool, leaving only
~11 GiB headroom (ADR-002's 8 GiB gate). We first tried to reclaim RAM by right-sizing KV
caches / `gpu_memory_utilization`; that failed — on this GB10 unified pool `gpu_memory_utilization`
is not a strict fraction-of-total cap (engines re-grab freed memory as KV, and recreate order
changes allocations), and each engine needs KV ≥ its `max_model_len` (floored at prompt +
the code's `max_tokens=8192`), so lowering util until it mattered made `mid` start unhealthy.
The weights (~89 GiB) dominate the footprint, so **the only reliable RAM lever is smaller weights.**

## The platforms involved

### gpt-oss-20b vs gpt-oss-120b (OpenAI open-weight, MXFP4)

Same architecture family and native MXFP4 quantization as the incumbent `heavy`, so it serves on
the exact validated path (sm_121 / vLLM 25.12, no `--quantization` flag) with zero new serving
risk — unlike the super-swap MoE (ADR-006 note: MIXED_PRECISION ModelOpt, unserveable here) or
AWQ/NVFP4 alternatives (unverified on this box). Staying in the `gpt-oss` family also leaves the
cross-family judge design (`core/verify/judge.py` FAMILIES: heavy=gpt-oss, mid=qwen, fast=nemotron)
untouched — a DeepSeek-R1-Distill-Qwen or Qwen3 would have collapsed `heavy` into `mid`'s family.

## Options considered

- **gpt-oss-20b (chosen)** — 13.7 GiB, same path/family, math ≈ 120B (published AIME'25 98.7% vs
  97.9%). Frees ~48 GiB. Risk: weaker on the hardest broad-knowledge/proof-search tasks.
- **Keep gpt-oss-120b** — max capability, but leaves headroom at ~11 GiB (the status quo we set
  out to improve).
- **Distinct-family reasoning model** (Magistral-Small-24B FP8, Phi-4-reasoning-plus NVFP4) — the
  designated fallbacks if 20B underperformed; not needed.

## Why we chose gpt-oss-20b — the measured A/B (2026-07-16)

Swapped 20B in as `heavy` (mid/fast unchanged) and ran the SynTheo suites against the 120B
baselines captured the same session:

| | 120B (baseline) | 20B |
|---|---|---|
| core tier2 (math/logic/philosophy) | 100% (6/6) | **100% (6/6)** — tie |
| formal metaphysics (18 items, ×2) | 78% / 83% | **72% / 83%** — within variance |
| headroom | ~11 GiB | **~65 GiB** |
| heavy weights | 66 GiB | 13.7 GiB |

The core generation/BoN path is a dead tie at 100%. Formal proving matches within the same
run-to-run variance both models exhibit (the persistent miss is modal collapse — the deliberate
hard ceiling); 20B is slightly less reliable at the hardest proof-*search* tactics (one run
missed three that both the 120B and 20B's own second run proved). We judged that a good trade for
~48 GiB and accepted it.

## Trade-offs we accepted

`heavy` is a meaningfully smaller model, so the hardest problems (grad-science knowledge, the
trickiest Isabelle tactic search) lose some reliability — the eval gate bounded this, not
eliminated it. Served at `--gpu-memory-utilization 0.20` (~24 GiB: 13.7 weights + a large 155k-token
KV); RAM is now abundant so there is no reason to run it lean. The 120B weights remain cached and
the swap is reversible (point the repo back, recreate). The unserveable ~75 GiB super-swap MoE
download can be deleted to reclaim disk.

## Follow-up enabled

With ~65 GiB now free, a future option is to run gpt-oss-120b AND gpt-oss-20b simultaneously
(e.g. 20B as a fast Tier-1/router path, 120B retained for the hardest tier) — out of scope here.
