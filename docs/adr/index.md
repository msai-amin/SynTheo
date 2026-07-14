# Architecture Decision Records — SynTheo

Decisions made while building SynTheo, a local verified-reasoning engine on a
single NVIDIA DGX Spark. Each document is written to be readable without prior
knowledge of the tools involved.

| ADR | Decision in one sentence | Status |
|---|---|---|
| [ADR-001](adr-001-vllm-containers-per-model.md) | Run the three AI models as three separate vLLM Docker containers on their own ports, using NVIDIA's prebuilt ARM64 image. | Accepted |
| [ADR-002](adr-002-memory-budget-4b-fast-model.md) | Downsize the "fast" model from 30B to 4B, start models sequentially, and set the free-memory gate to a measured 8 GB — the original plan didn't fit and crashed the machine. | Accepted (approved spec deviation) |
| [ADR-003](adr-003-docker-sandbox.md) | Execute model-written code in a locked-down Docker container, because this kernel forbids the lighter bwrap/unshare sandboxes. | Accepted |
| [ADR-004](adr-004-anti-gaming-verification.md) | Only count model checking code as "verification" if it genuinely recomputes the answer — echo blocks, answer-seeded code, and self-checks of open-ended essays are rejected. | Accepted |
| [ADR-005](adr-005-sqlite-episode-store.md) | Record every run, sample, verification, and judgment in SQLite (one local file) rather than PostgreSQL or JSON logs. | Accepted |
