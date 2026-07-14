# ADR-001: Serve each model with its own vLLM container

**Status:** Accepted
**Date:** 2026-07-13
**Deciders:** Amin Amouhadi (project owner), Claude (implementation)

## Decision in one sentence

SynTheo runs its three AI models as three separate vLLM servers, each inside its own Docker container on its own network port (8001/8002/8003), using NVIDIA's pre-built ARM64 container image.

## The problem we're solving

SynTheo is a reasoning engine that runs entirely on one machine — an NVIDIA DGX Spark — with no cloud access. It needs three different AI models running at the same time: a large one for hard math ("heavy"), a mid-size one to act as a skeptic and judge ("mid"), and a small fast one for routing ("fast"). Something has to load these models onto the GPU and answer requests from our code, ideally speaking a standard protocol so our code doesn't care which model is behind a port.

Constraints: the machine uses an ARM processor (most software is built for Intel/AMD, so binaries must be ARM builds); all three models share one 121 GB memory pool; the box may be offline, so everything must run locally.

## The platforms involved

### vLLM

A free, open-source **inference server** — a program that loads an AI model and answers requests to generate text, developed originally at UC Berkeley and now by a large open community. Its specialty is serving many requests at once efficiently ("batching"), which SynTheo depends on: we ask for 8 answers in parallel and vLLM computes them nearly as fast as one. Analogy: a restaurant kitchen that cooks every order on one giant stove at once, instead of one dish at a time.

### Docker

Free software that runs programs in **containers** — isolated boxes that carry their own copy of every library the program needs. Maintained by Docker Inc. and a large ecosystem. Analogy: shipping goods in standard sealed containers so they work at any port, regardless of what's inside.

### NVIDIA NGC container images

Pre-built Docker containers published by NVIDIA with vLLM already installed and tuned for NVIDIA hardware, including the ARM64 builds our machine needs. Analogy: buying a pre-assembled toolkit sized for your exact vehicle, instead of assembling tools from parts.

### Ollama / llama.cpp (alternatives)

Two popular free tools for running AI models on personal machines. llama.cpp is a lean engine focused on running one model efficiently, especially on smaller hardware; Ollama wraps it in a friendly one-command experience. Analogy: a camping stove — wonderfully simple, but not built to cook eighty meals at once.

## Options considered

### One vLLM container per model (chosen)

Three containers, three ports, one shared GPU. Each model gets its own explicitly configured slice of memory. If one crashes, the other two keep serving. All three speak the same standard "OpenAI-compatible" protocol, so our single client module (`core/llm.py`) treats them identically.

### One server hosting all three models

Some servers can host several models behind one port and swap between them. Fewer moving parts and one memory pool to manage. But at the time of building, vLLM's multi-model support meant loading/unloading models per request — unusable when all three must answer *simultaneously* (our Tier-2 mode queries heavy and mid in the same burst, and the router must stay responsive throughout).

### Ollama or llama.cpp servers

Genuinely simpler to operate, and excellent on small machines. But our architecture's core bet is that **batched parallel sampling is nearly free** on this hardware — that requires vLLM-class continuous batching. llama.cpp's batching is much weaker, and (measured in our M0 gate) vLLM returned 5 parallel long answers from the 120-billion-parameter model in ~7.7 seconds total. We'd lose the property the whole design leans on.

### vLLM installed directly on the host (no Docker)

One less layer. But vLLM on ARM64 with Blackwell GPUs is exactly the combination NVIDIA's containers exist to solve — building it ourselves means compiling GPU libraries by hand and re-doing it at every update. The NGC image (`nvcr.io/nvidia/vllm:25.12.post1-py3`) was already downloaded on this machine and worked immediately.

## Comparison

| What matters to us | Per-model vLLM containers | One multi-model server | Ollama / llama.cpp | Bare-metal vLLM |
|---|---|---|---|---|
| Three models answering at once | Yes | No (swap per request) | Yes, but slowly | Yes |
| 8-answer parallel burst speed | ~7.7 s (measured) | n/a | Much slower | Same as chosen |
| Crash isolation | One model down, two keep running | All down together | Per-server | Per-process |
| ARM64/Blackwell setup effort | Zero (NVIDIA prebuilt) | Zero | Low | Days, repeated at upgrades |
| Explicit per-model memory budget | Yes, one flag each | Harder | Coarse | Yes |

## Why we chose per-model vLLM containers

First, the architecture requires simultaneous access to all three models: Tier 2 fires a mixed burst at heavy and mid while fast stays ready to route the next question. Only the per-model-server layout gives that without model swapping.

Second, the machine's memory bandwidth (~273 GB/s) makes single-stream generation slow but batched generation nearly free — the entire SynTheo design exploits this. vLLM's continuous batching is the best available implementation of that property, confirmed by our M0 measurement (5 parallel samples from a 120B model, 7.7 s wall time, all correct).

Third, isolation earned its keep immediately: memory budgets are per-container flags mirrored from one config file, and when the fast model's container failed on first launch (a missing `--trust-remote-code` flag), heavy and mid were unaffected while we fixed it.

Fourth, NVIDIA's prebuilt ARM64 image removed an entire category of risk on unusual hardware. We verified vLLM 0.12.0 inside it runs on this GPU before committing.

## Trade-offs we accepted

Three servers cost more fixed overhead than one: roughly 10 GB of per-process CUDA and host overhead across the trio (measured), which mattered painfully in our memory budget (see ADR-002). We also accept Docker as an operational dependency and three healthchecks instead of one. We would revisit this if vLLM ships true concurrent multi-model serving in one process, or if the model roster shrinks to one model.

## Glossary

- **ARM64**: a processor family (used in phones and this DGX) incompatible with programs built for Intel/AMD chips.
- **Batching**: answering many requests in one pass through the model, much cheaper than one at a time.
- **Container**: an isolated environment bundling a program with everything it needs to run.
- **CUDA**: NVIDIA's software layer for running computations on their GPUs.
- **GPU**: the graphics processor; here, the chip that runs the AI models.
- **Inference server**: a program that loads an AI model and answers text-generation requests over the network.
- **OpenAI-compatible protocol**: a de-facto standard request format for talking to AI models; using it means our code works with any conforming server.
- **Port**: a numbered network "door" on a machine; each server listens behind its own.
