# SynTheo (synodus theoriae)

A local verified-reasoning engine for a single DGX Spark: verified Best-of-N for
math/logic, a prover/skeptic pair for philosophy, and an episode store built for a
future GRPO flywheel. Full spec: see the implementation prompt in project history.

**What it's for:** for math and formal logic, SynTheo doesn't trust a model's answer on its
say-so — it samples several candidate solutions and checks them against formal verifiers
(Isabelle/HOL, Z3) before accepting one. For philosophy, where there's no ground-truth
checker, it instead runs a prover model and a skeptic model against each other and has a
judge weigh the exchange. Every run is logged to an episode store so the whole thing can
later be turned into training signal (the "GRPO flywheel").

## Status: M0 — serving + skeleton

## Runbook

```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e ".[dev]"

# Fetch model weights into the project-local HF cache (large; hours on first run).
# Root-owned ~/.cache/huggingface on this box means HF_HOME must stay project-local.
python serve/download_models.py            # all three
python serve/download_models.py heavy      # just one alias

# Bring up the resident-trio stack (heavy/mid/fast on 8001/8002/8003).
./serve/serve.sh
python serve/healthcheck.py            # poll until all green

# Prove headroom and exercise a batched 8-sample call (M0 gate).
python serve/memory_report.py
python serve/m0_gate.py
```

## Repo layout

See `config/syntheo.yaml` for the model roster, ports, memory budget, and tier
thresholds — nothing about model identity or budgets lives in code.
