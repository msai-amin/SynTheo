# ADR-006: Add Isabelle/HOL as an on-demand fourth verification backend

**Status:** Accepted (Phase 0 — acceptance gate PASSED 2026-07-15 with measured numbers below)
**Date:** 2026-07-15
**Deciders:** Claude (implementation), gate-verified on the Spark

## Decision in one sentence

Formal-metaphysics claims (higher-order modal arguments — Gödel's ontological argument,
Abstract Object Theory, modal collapse) are verified by building a model-written
Isabelle/Isar theory in a **locked-down, memory-capped, on-demand Docker container**
(one proof at a time), NOT a persistent warm server — because a warm server is a
permanent multi-GB resident that ADR-002's 8 GB memory gate forbids.

## The problem we're solving

SynTheo's strongest claim is the **verified** label (ADR-004): a program independently
established the answer. Today that covers math (`execute.py` sandbox) and logic
(`logic_z3.py` SMT). It cannot touch formal metaphysics, whose arguments quantify over
properties and run on modality — they live in classical higher-order logic via *shallow
semantic embedding* (possible worlds become a type, `□`/`◇` become quantifiers over
worlds), which an off-the-shelf HOL prover like Isabelle checks with its LCF kernel. An
Isabelle proof is a *stronger* "verified" than anything SynTheo emits now: the kernel
cannot be talked into accepting a bad proof.

Two facts about *this* machine make the naive integration (a warm `isabelle server`, as
an external playbook suggested) unworkable, and shape the decision:

1. **Memory.** ADR-002 leaves ~9.5 GB free after the resident model trio and sets an
   8 GB steady-state gate, explicitly warning "any future resident component must
   displace something else." Isabelle with the `HOL` + `GoedelGod` heaps loaded is a
   multi-GB process — as a *permanent* resident it breaks the gate.
2. **Platform.** The box is aarch64. Isabelle supports arm64-linux; the research note
   warned the bundled prover set would be thin (no Z3/CVC5). **Measured false on this
   Isabelle2025-2 arm64 bundle** — Z3, CVC5, Vampire, E, and SPASS are all present
   (only Leo-III and CVC4 are absent), so Sledgehammer has a full hammer. Recorded here
   because it removes a risk the plan had carried.

## The decision, in detail

- **On-demand container, not a warm server** (`core/verify/isabelle_hol.py`,
  `serve/isabelle.Dockerfile`). Each proof runs in a fresh container from a purpose-built
  image with the prover and **prebuilt heap images** baked in, so a run is a heap-*load*
  (seconds) not a rebuild-the-world. Runtime isolation mirrors ADR-003: `--network=none`,
  `--read-only` (+ writable tmpfs for Isabelle's scratch), `--memory` cap, `--pids-limit`,
  `--cap-drop=ALL`, non-root, wall-clock kill. Memory is used **transiently** and returns
  when the proof ends; when idle the box sits at the full ~9.5 GB headroom.
- **Serialized to one proof at a time** (`_PROOF_SEMAPHORE`). Two capped containers would
  stack memory; there is no concurrency benefit for a rare, Tier-3-class workload.
- **Anti-gaming — the ADR-004 sequel.** The kernel can't be fooled, but a model can game
  *around* it. Cheap static checks (pure text/regex, each a unit test in
  `tests/test_isabelle_hol.py`):
  1. reject `sorry`/`oops` (a goal accepted without proof — the theorem-prover echo block);
  2. require the proved goal be **syntactically the target** (models prove an easier
     neighbor) — the harness pins the target statement;
  3. a proof that closes but **rests on a model-introduced axiom** is downgraded from
     verified to rejected (inconsistent axioms prove anything — the exact defect the field
     found in Gödel's *own* 1970 axioms). Crucially this bites only the *verified* path: an
     axiom set that Nitpick *refutes* is a legitimate finding, not gaming.
  4. the relied-on embedding/logic (K/KB/S5, constant vs varying domains) is recorded in
     `detail` — in metaphysics the choice of logic is part of the claim.
- **A third outcome: `refuted`.** A countermodel (Nitpick) or an inconsistency is a
  *positive* result in metaphysics — the claim is false. The verifier returns the flat
  `{verified, method, detail}` contract with the outcome in `method`
  (`isabelle` / `isabelle-refuted` / `isabelle-rejected` / `isabelle-unverifiable`); the
  tier layer maps `isabelle-refuted` to a new `refuted` confidence_type (free-text column,
  no schema change).

## Options considered

### On-demand memory-capped container (chosen)
Fits the memory budget by never being a permanent resident. Weakness: per-proof
container + heap-load overhead (seconds-to-minutes), and no proof concurrency.

### Persistent warm `isabelle server` (the external suggestion)
Fastest per proof (heaps stay hot). Rejected: a permanent multi-GB resident breaks
ADR-002's 8 GB gate unless a model is displaced (super-swap), which forfeits the
resident-trio interactivity guarantee. The note that proposed it did not account for the
measured 9.5 GB margin.

### Lazy warm server with idle teardown
Warm during a metaphysics session, gone otherwise. A reasonable future optimization, but
adds lifecycle complexity and needs a memory guard against a concurrent Tier-2 burst.
Deferred until measured latency proves the on-demand cost actually hurts.

### Lean/Coq (dependent type theory) instead of Isabelle/HOL
Modern LLM-prover ecosystems, but essentially **no metaphysics infrastructure** — Gödel/
AOT/modal-logic-as-metaphysics all live in Isabelle/HOL. Wrong tool for this goal.

## Comparison

| What matters to us | On-demand container | Warm server | Lazy warm | Lean/Coq |
|---|---|---|---|---|
| Respects the 8 GB steady-state gate | Yes (transient only) | **No** (permanent resident) | Yes (idle) | Yes |
| Per-proof latency | Heap-load + proof | Proof only | Proof only (warm) | Proof only |
| Metaphysics corpus available | Yes (AFP: GoedelGod, AOT) | Yes | Yes | **No** |
| Reuses existing patterns (ADR-003/004) | Yes | Partly | Partly | No |
| Lifecycle complexity | Low | Low | Medium | n/a |

## Trade-offs we accepted

**Transient headroom dip — measured small.** A proof/refutation's real RSS is **~1.7 GiB**
(gate-measured). The container is capped at `MEM_MB` = 5 GiB — higher than the RSS because
the cgroup also charges the mmap'd prebuilt-heap page-cache against the limit, so a 3 GiB
cap deterministically OOM-kills Nitpick's SAT solver while 4–5 GiB is reliable. That
page-cache is reclaimable, so the box's true free-headroom dip stays ~1.7 GiB (from ~9.5 to
~7.8 GiB) — the same magnitude as the Python sandbox's existing dip, not the deep dip we
feared, so this needs no special approval. The dip is **bounded** (hard `--memory` cap →
the container is OOM-killed, never the box), **serialized** (one proof at a time), and
**rare** (a Tier-3-class workload). Had it come in near the 8 GiB gate, this would have
been an ADR-002-style deviation requiring sign-off; it did not.

Other trade-offs: the image is a moderate ~3.1 GiB (Isabelle + AFP sources + prebuilt HOL
and GoedelGod heaps — smaller than the "tens of GB" feared because the linux bundle ships
the HOL heap and GoedelGod is a compact session); AFP session theories must be imported
session-qualified (`imports "GoedelGod.GoedelGod"`); refutations use the
`nitpick [expect = genuine]` convention (batch build suppresses Nitpick's text, but
`expect` turns a found countermodel into build success); and, as with ADR-003, if the
image/daemon is absent a metaphysics run degrades to `isabelle-unverifiable` rather than
crashing.

## Measured facts (acceptance gate, 2026-07-15, `serve/isabelle_gate.py` PASSED)

- Image size: **3.09 GiB** (`syntheo-isabelle:latest`).
- Peak RSS of a proof: **1.58 GiB**; of a Nitpick refutation: **~1.7 GiB**. `MEM_MB` cap
  set to **5 GiB** (a 3 GiB cap OOM-kills Nitpick because the cgroup also charges the
  reclaimable heap page-cache; real headroom dip stays ~1.7 GiB).
- Prover roster present on arm64: **E, Z3, CVC5, Vampire, SPASS** (Leo-III and CVC4 absent).
  This contradicts the note's "Z3/CVC5 absent on arm64" — they are present here.
- Scott variant (`[\<box> (\<exists> G)]`, necessarily God exists) → **`verified`** (kernel-checked).
- "Necessarily God does not exist" (`[m\<not> (\<exists> G)]`) → **`refuted`** (Nitpick countermodel).
- Build is fast: a proof reuses the prebuilt GoedelGod heap and finishes in ~3–5 s wall.

## What this ADR does NOT cover (deferred, gated on the gate passing)

- **Phase 1**: `metaphysics` as a real router domain; a `run_tier_formal` flow (Isar
  sketch → sledgehammer/E/Leo-III → kernel); a ~20-argument benchmark suite; a kernel-
  verdict scoring path in the eval harness; the `refuted` label in the UI; a small RAG
  index over theorem signatures.
- **Phase 2**: training (autoformalization flywheel). Never the 120B on this box — the 4B
  fast model or offline QLoRA on the 32B mid, rejection-sampling SFT before DPO. Gated on
  Phase 1 plateauing.

## Glossary

- **Shallow semantic embedding (SSE)**: encoding a non-classical logic (here higher-order
  modal logic) inside plain classical HOL, so a standard HOL prover reasons in it.
- **LCF kernel**: Isabelle's tiny trusted core; every theorem must pass through it, so a
  proof it accepts is trustworthy even if the surrounding tactics are not.
- **Nitpick**: Isabelle's countermodel finder — exhibits a model where a claim fails
  (a *refutation*), which in metaphysics is as valuable as a proof.
- **Sledgehammer**: Isabelle's bridge to external automated provers (E, Leo-III, …); it
  finds a proof, which the kernel then re-checks.
- **Heap image**: a prebuilt, loadable snapshot of a checked Isabelle session, so
  downstream work skips re-checking it.
