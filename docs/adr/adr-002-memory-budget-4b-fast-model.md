# ADR-002: Downsize the "fast" model to 4B and budget memory from measurements

**Status:** Accepted (deviation from the original spec, approved 2026-07-13)
**Date:** 2026-07-13
**Deciders:** Amin Amouhadi (approved both deviations), Claude (proposed)

## Decision in one sentence

The "fast" model was changed from a 30-billion-parameter Nemotron to the 4-billion-parameter Nemotron-3-Nano-4B-FP8, the three models now start strictly one-after-another, and the free-memory safety gate was reset from the planned 20 GB to a measured-and-approved 8 GB — because the original plan physically did not fit and crashed the machine.

## The problem we're solving

SynTheo's design calls for three models permanently resident in the DGX Spark's **unified memory** — one 121 GB pool shared by everything: the operating system, our services, and all three models. The original specification assumed the trio would take ~95 GB, leaving ≥20 GB free as a safety margin.

Reality disagreed. On first launch, loading all three models at once exhausted the pool and **hard-rebooted the machine**. After adding sequential startup and measuring properly, the arithmetic was unambiguous:

| Component | Planned | Measured |
|---|---|---|
| heavy (gpt-oss-120b) loaded size | ~61 GB | **66.0 GB** (from vLLM's own log) |
| Per-server process overhead ×3 | not budgeted | **~10 GB** total |
| fast (Nemotron 30B) loaded size | ~16 GB | 18 GB |
| Free margin achievable | ≥20 GB | **~7 GB** |

Something had to give: the model roster, the safety margin, or both.

## The platforms involved

### NVIDIA Nemotron-3-Nano (30B-A3B and 4B variants)

A family of free, open-weight AI models published by NVIDIA. The 30B "A3B" variant is a **mixture-of-experts** model (large total size, but only a small part activates per word — fast, yet big in memory). The 4B variant is a small conventional model. In SynTheo, this model is the "fast" role: routing questions to the right tier and answering easy ones. Analogy: the receptionist who directs visitors — the job needs speed and decent judgment, not the chief engineer's depth.

### FP8 / NVFP4 / MXFP4 (quantization formats)

Ways of storing a model's numbers with fewer bits so it fits in less memory, at a small accuracy cost — like saving a photo as a smaller JPEG. FP8 uses 8 bits per number; NVFP4/MXFP4 use ~4. The 4B model in FP8 occupies ~5 GB versus the 30B's 18 GB.

### Unified memory (DGX Spark / GB10)

On this machine, the processor and GPU share one physical memory pool. Convenience with a sharp edge: a GPU that over-allocates starves the operating system itself — which is how the first launch *rebooted the whole computer* rather than just failing one program.

## Options considered

### Downsize fast to Nemotron-3-Nano-4B-FP8 (chosen)

Frees ~13 GB. The fast role (routing, easy questions) is the least quality-sensitive of the three. Staying inside the Nemotron family preserves the project's plan to later fine-tune this model with reinforcement learning. Weakness: a 4B model is noticeably less capable — some easy questions it could have answered now escalate to Tier 2, costing time and tokens.

### Downsize mid (Qwen3-32B → Qwen3-14B)

Frees ~10 GB. But mid is the **skeptic and cross-family judge** — the quality backstop that checks the big model's work. Weakening the auditor to save memory undermines the system's core trust mechanism.

### Keep the full trio and accept ~7 GB margin

No capability loss. But 7 GB of slack on a box that has already demonstrated it *reboots* under memory pressure is gambling with the whole machine; the spec's 20 GB margin existed precisely to prevent that.

### Abandon "resident trio": load mid on demand

heavy+fast resident (~75 GB) leaves a luxurious margin. But mid takes ~3 minutes to load, and Tier 2 uses it in *every* deliberate run — turning a 90-second math answer into a 5-minute one. That breaks the product's interactivity promise.

## Comparison

| What matters to us | 4B fast (chosen) | 14B mid | Accept 7 GB | Mid on demand |
|---|---|---|---|---|
| Free margin achieved | **9.5 GB (measured)** | ~17 GB | ~7 GB | ~35 GB |
| Quality cost lands on | Router/easy path (self-correcting: misroutes escalate) | The system's auditor | None | None |
| Risk of another machine crash | Low | Low | High | Lowest |
| Tier-2 answer latency | Unchanged | Unchanged | Unchanged | +~3 min every run |
| Future RL-training story | Kept (same family) | n/a | Kept | Kept |

## Why we chose the 4B fast model (plus sequential startup, plus an 8 GB gate)

The deciding insight is that the three roles are not equally sensitive to model size. The fast model's mistakes are **self-correcting by design**: a misrouted or wrongly-answered Tier-1 question fails verification and automatically escalates to Tier 2, where the big models catch it. The skeptic/judge's mistakes are *not* self-correcting — it is the correction. So the downgrade goes where the architecture already has a safety net.

Sequential startup (heavy → mid → fast, each waiting for the previous to be healthy) fixes the actual crash cause — three engines claiming memory simultaneously, each measuring "free" before the others finished claiming. This is enforced in the Docker configuration with a comment forbidding its removal.

The 8 GB gate is honesty over aspiration. After the downsize and every recoverable trim, the stack stabilizes at 9.5 GB free. A 20 GB (or 15 GB) gate that always fails is worse than an 8 GB gate that actually guards the achieved, stable state — and both deviations were explicitly approved rather than silently absorbed.

## Trade-offs we accepted

Tier 1 is served by a meaningfully weaker model, so more questions will escalate to the expensive tier (we will measure the escalation rate in M4's evaluation harness). The 9.5 GB margin is real but not generous: any future resident component (a bigger KV cache, another service) must displace something else. We would revisit this on different hardware (more memory), or if measured Tier-1 escalation rates turn out high enough that a smarter fast model would pay for itself.

## Glossary

- **Escalation**: SynTheo's rule that an answer failing verification at a cheap tier is automatically retried at a more thorough tier.
- **KV cache**: scratch memory a model uses while generating; grows with the length and number of simultaneous answers.
- **Mixture-of-experts**: a model built from many small specialists where only a few activate per word — computationally fast but memory-hungry.
- **Quantization**: storing model weights in fewer bits to shrink memory use, at slight accuracy cost.
- **Reinforcement learning (RL)**: training method the project plans to later apply to the fast model, using SynTheo's own verified answers as training signal.
- **Resident**: loaded in memory permanently, ready to answer instantly.
- **Unified memory**: one physical memory pool shared by CPU and GPU; an over-greedy GPU can starve the whole machine.
