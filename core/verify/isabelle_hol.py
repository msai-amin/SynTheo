"""Isabelle/HOL verification of formal-metaphysics claims. [ADR-006]

The fourth verification backend. Where execute.py runs model-written Python and
logic_z3.py solves model-written SMT, this checks a model-written Isabelle/Isar
theory with the LCF kernel — the strongest "verified" SynTheo can emit. Formal
metaphysics (Gödel's ontological argument, Abstract Object Theory, modal collapse)
lives in classical HOL via shallow semantic embedding, so an off-the-shelf HOL
prover reasons in the embedded modal logic.

Runtime model mirrors ADR-003, NOT a warm server: each proof runs in an on-demand,
memory-capped, network-less Docker container with heap images baked in, serialized
to one at a time (ADR-002 forbids a permanent multi-GB resident). Memory is used
transiently and returns when the proof ends.

Anti-gaming mirrors ADR-004: the kernel can't be fooled into accepting a bad proof,
but a model can game *around* it — `sorry` stubs, smuggled axioms (inconsistent
axioms make anything provable — the exact defect the field found in Gödel's own 1970
axioms), or proving an easier neighbor of the target. Those are rejected by cheap
static checks before the container ever runs.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

# Runtime constants (hardcoded like execute.py:19-23 / logic_z3.py:16-17 — the
# `verify:` YAML block is documentary only, nothing reads it).
ISABELLE_IMAGE = "syntheo-isabelle:latest"
# Cap from measurement (serve/isabelle_gate.py, 2026-07-15): a proof/refutation's real
# RSS is ~1.7 GiB, but the cgroup also charges the mmap'd heap files' page-cache against
# the limit, so a 3 GiB cap deterministically OOM-kills Nitpick while 4-5 GiB is reliable.
# 5 GiB gives margin. The page-cache is reclaimable, so the box's true headroom dip stays
# ~1.7 GiB (comparable to the Python sandbox); the hard cap OOM-kills the container, never
# the box, and proofs are serialized. See ADR-006.
MEM_MB = 5120
WALL_SECONDS = 300  # proofs are seconds-to-minutes; far longer than the 20s Python cap
PIDS_LIMIT = 512    # Isabelle/Poly-ML + the JVM PIDE layer spawn many threads
CPUS = "4"

# Only one proof in flight at a time: two capped containers would stack memory and
# could breach the box, and there is no benefit to concurrency for a rare workload.
_PROOF_SEMAPHORE = asyncio.Semaphore(1)

_THEORY_BLOCK = re.compile(r"```(?:isabelle|isar|theory|thy)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_THEORY_NAME = re.compile(r"^\s*theory\s+([A-Za-z][A-Za-z0-9_']*)", re.MULTILINE)
# `sorry` accepts a goal unproved — always gaming. `oops` abandons a goal — gaming on
# the VERIFY path (build passes, nothing proved) but LEGITIMATE in a refutation
# (`nitpick ... oops`), so it is handled separately and only downgrades a `verified`.
_SORRY = re.compile(r"(?<![A-Za-z0-9_'])sorry(?![A-Za-z0-9_'])")
_OOPS = re.compile(r"(?<![A-Za-z0-9_'])oops(?![A-Za-z0-9_'])")
# New axioms the model tries to introduce. `axiomatization`/`axioms` open an axiom
# block; a bare `axiom` name is the older syntax. All make anything provable.
_NEW_AXIOM = re.compile(r"(?<![A-Za-z0-9_'])(axiomatization|axioms|axiom)(?![A-Za-z0-9_'])")
# The proposition of a theorem/lemma goal: the first double-quoted string after the
# keyword (skipping an optional `name:`). Handles both `theorem t: "P"` and `lemma "P"`.
_GOAL = re.compile(
    r"(?:theorem|lemma|corollary|proposition)\b[^\"]*\"((?:[^\"\\]|\\.)*)\"",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class IsabelleResult:
    """Raw outcome of running one theory in the sandbox container."""
    status: str          # "verified" | "refuted" | "unverifiable"
    detail: str
    raw: str             # tail of the build log, for the trace


def extract_theory(sample_text: str) -> str | None:
    """Pull the last fenced Isabelle/Isar theory block from a sample (like
    extract_smt). A theory block must contain a `theory ... begin` header."""
    blocks = _THEORY_BLOCK.findall(sample_text)
    for block in reversed(blocks):
        if "theory" in block and "begin" in block:
            return block.strip()
    return None


def theory_name(theory_text: str) -> str | None:
    m = _THEORY_NAME.search(theory_text)
    return m.group(1) if m else None


def has_sorry(theory_text: str) -> bool:
    """`sorry` accepts a goal without proving it — the proof equivalent of an echo
    block (ADR-004). Never legitimate; blocked before the kernel even runs."""
    return bool(_SORRY.search(_strip_comments_and_strings(theory_text)))


def has_oops(theory_text: str) -> bool:
    """`oops` abandons the current goal. In a VERIFY submission the build then passes
    without the target being proved (a false 'verified'); in a REFUTATION it is the
    normal shape (`nitpick ... oops`). So it only downgrades a would-be `verified`."""
    return bool(_OOPS.search(_strip_comments_and_strings(theory_text)))


def introduces_axiom(theory_text: str) -> bool:
    """True if the submission declares its own axiom. This guards the VERIFIED path
    only: a new axiom lets a model 'prove' anything (inconsistent axioms → ⊥ →
    everything), so a proof resting on one is not trustworthy. It is NOT a blanket
    pre-reject — feeding an axiom set to Nitpick to discover it is inconsistent (the
    flagship Gödel-1970 result) is a legitimate REFUTED outcome, not gaming. So this
    predicate only downgrades a would-be `verified` in check_isabelle, never a
    `refuted`."""
    return bool(_NEW_AXIOM.search(_strip_comments_and_strings(theory_text)))


def _strip_comments_and_strings(theory_text: str) -> str:
    """Drop (* ... *) comments and "..." string literals so a `sorry`/`axiom` keyword
    mentioned in prose or inside a string doesn't trip the guards. Used for keyword
    detection only — NOT for goal extraction, where the goal itself is a string."""
    no_block = re.sub(r"\(\*.*?\*\)", " ", theory_text, flags=re.DOTALL)
    return re.sub(r'"(?:[^"\\]|\\.)*"', ' "" ', no_block)


def _strip_comments(theory_text: str) -> str:
    """Drop only (* ... *) comments, preserving string literals (goal propositions)."""
    return re.sub(r"\(\*.*?\*\)", " ", theory_text, flags=re.DOTALL)


def _normalize(prop: str) -> str:
    """Collapse whitespace and drop surrounding quotes so goal/target comparison is
    layout-insensitive."""
    return re.sub(r"\s+", " ", prop).strip().strip('"').strip().rstrip(".")


def goal_matches_target(theory_text: str, target_statement: str) -> bool:
    """True iff the theory's proved goal is syntactically the target. Models love
    proving an easier neighbor of the asked question; the harness must pin the goal.
    `target_statement` is the proposition text (quotes optional)."""
    m = _GOAL.search(_strip_comments(theory_text))
    if not m:
        return False
    return _normalize(m.group(1)) == _normalize(target_statement)


def detect_embedding(theory_text: str) -> str:
    """Best-effort label of the modal logic / SSE the result rests on — in
    metaphysics the choice of logic (K, KB, S5; constant vs varying domains) is part
    of the claim, so it is recorded in `detail`, not used as a gate."""
    t = theory_text
    logics = [name for name in ("S5", "KB", "S4", "K") if re.search(rf"\b{name}\b", t)]
    tags = []
    if logics:
        tags.append("/".join(logics))
    if re.search(r"\bAOT\b|AbstractObject", t):
        tags.append("AOT")
    if re.search(r"varying[_ ]?domain", t, re.IGNORECASE):
        tags.append("varying-domain")
    elif re.search(r"constant[_ ]?domain", t, re.IGNORECASE):
        tags.append("constant-domain")
    return ", ".join(tags) if tags else "unspecified-embedding"


def static_rejection(theory_text: str, target_statement: str | None) -> str | None:
    """Cheap pre-container anti-gaming checks (ADR-004 sequel) that hold in EVERY mode.
    Returns a rejection reason, or None if the theory may be handed to the kernel.
    Deliberately excludes `oops` and new-axiom checks — those are legitimate in a
    refutation, so they only downgrade a would-be `verified` post-run (check_isabelle)."""
    if has_sorry(theory_text):
        return "contains sorry (goal accepted without proof)"
    if target_statement is not None and not goal_matches_target(theory_text, target_statement):
        return "proved goal does not match the target statement"
    return None


def _image_present() -> bool:
    try:
        r = subprocess.run(["docker", "image", "inspect", ISABELLE_IMAGE],
                           capture_output=True)
        return r.returncode == 0
    except FileNotFoundError:
        return False


def run_isabelle_sandboxed(theory_text: str, parent_session: str = "GoedelGod") -> IsabelleResult:
    """Build the theory inside the locked-down container. Mirrors run_sandboxed
    (execute.py:72-116): no network, read-only rootfs, capped memory/pids, non-root,
    wall-clock kill. The baked /usr/local/bin/check-theory wrapper prints a
    `RESULT: verified|refuted|unverifiable` line the kernel/Nitpick outcome maps to."""
    name = theory_name(theory_text) or "Submission"
    if not _image_present():
        # ADR-003 degradation contract: no image/daemon → unverifiable, never a crash.
        return IsabelleResult("unverifiable", "isabelle image unavailable", "")

    container = f"syntheo-isa-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="syntheo-isa-") as td:
        thy = Path(td) / f"{name}.thy"
        thy.write_text(theory_text)
        cmd = [
            "docker", "run", "--rm",
            "--name", container,
            "--network=none",
            "--read-only",
            "--tmpfs", "/work:rw,size=2g,uid=10001,gid=10001",
            "--tmpfs", "/tmp:rw,size=1g,uid=10001,gid=10001",
            f"--memory={MEM_MB}m", f"--memory-swap={MEM_MB}m",
            f"--pids-limit={PIDS_LIMIT}",
            f"--cpus={CPUS}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user", "10001:10001",
            "--env-file", "/dev/null",
            # Isabelle's user dir (heap output, logs, locks) must be writable; the baked
            # heaps/components are READ from the system location on the read-only rootfs.
            "-e", "HOME=/work",
            "-e", "ISABELLE_HOME_USER=/work/.isabelle",
            "-v", f"{thy}:/input/{name}.thy:ro",
            ISABELLE_IMAGE,
            "check-theory", f"/input/{name}.thy", parent_session,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=WALL_SECONDS)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", container], capture_output=True)
            return IsabelleResult("unverifiable", "wall-clock timeout", "")

        out = (proc.stdout or "") + (proc.stderr or "")
        tail = out[-8000:]
        if proc.returncode == 125:  # docker could not start the container
            return IsabelleResult("unverifiable", "container failed to start", tail)
        status = _parse_result_line(out)
        return IsabelleResult(status, _status_detail(status, out), tail)


def _parse_result_line(out: str) -> str:
    for line in reversed(out.splitlines()):
        m = re.match(r"\s*RESULT:\s*(verified|refuted|unverifiable)\s*$", line, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    return "unverifiable"  # wrapper produced no verdict → treat as unverifiable


def _status_detail(status: str, out: str) -> str:
    if status == "refuted":
        return "Nitpick countermodel confirmed (nitpick expect=genuine satisfied)"
    if status == "verified":
        return "kernel-checked proof of target"
    return "no proof and no countermodel (timeout or open goal)"


def check_isabelle(theory_text: str, target_statement: str | None = None) -> dict:
    """The verifier entry point. Returns the flat backend contract
    {"verified": bool, "method": str, "detail": str}, with the three-way outcome
    carried in `method`:
      - kernel proves the target          -> method "isabelle"            (verified)
      - Nitpick countermodel / inconsistent-> method "isabelle-refuted"    (a POSITIVE
                                              result in metaphysics: the claim is false)
      - static anti-gaming rejection       -> method "isabelle-rejected"   (not verified)
      - neither (timeout / open goal)      -> method "isabelle-unverifiable" (-> judge)
    The relied-on embedding/logic is appended to `detail`."""
    embedding = detect_embedding(theory_text)

    reason = static_rejection(theory_text, target_statement)
    if reason is not None:
        return {"verified": False, "method": "isabelle-rejected",
                "detail": f"{reason} [logic: {embedding}]"}

    result = run_isabelle_sandboxed(theory_text)

    # A would-be `verified` that only 'closed' via a cancelled goal (`oops`) or a
    # model-introduced axiom is not trustworthy (ADR-004: the answer smuggled in where
    # the work should be). Downgrade it — but only `verified`; a `refuted` result rests
    # on a countermodel, not on the submission's proof, so these don't touch it.
    if result.status == "verified":
        if has_oops(theory_text):
            return {"verified": False, "method": "isabelle-rejected",
                    "detail": f"goal cancelled with oops, not proved [logic: {embedding}]"}
        if introduces_axiom(theory_text):
            return {"verified": False, "method": "isabelle-rejected",
                    "detail": f"proof rests on a model-introduced axiom [logic: {embedding}]"}

    method = {"verified": "isabelle",
              "refuted": "isabelle-refuted",
              "unverifiable": "isabelle-unverifiable"}[result.status]
    return {"verified": result.status == "verified", "method": method,
            "detail": f"{result.detail} [logic: {embedding}]"}
