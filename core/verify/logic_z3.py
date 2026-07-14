"""Z3 verification of logic problems via model-emitted SMT-LIB2.

The model is prompted (schema-constrained, see PROMPT) to encode the problem's
constraints as SMT-LIB2. The encoding is only trusted after it round-trips
through z3.parse_smt2_string; if encoding fails validation twice, the sample is
UNVERIFIABLE — an unvalidated encoding is never solved (spec §3.3 guard).
A Z3 `unsat` or model mismatch is a verifier VERDICT, not an error.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import z3

MAX_ENCODING_ATTEMPTS = 2
Z3_TIMEOUT_MS = 10_000

PROMPT = """Encode the constraints of this problem as SMT-LIB2.
Rules:
- declare every unknown with declare-const
- emit assert statements only for constraints explicitly stated in the problem
- do NOT emit check-sat or get-model; the harness adds them
- also declare a constant named `answer` constrained to equal the queried quantity
- wrap the encoding in a fenced ```smtlib block
Problem:
{problem}"""

_SMT_BLOCK = re.compile(r"```(?:smtlib|smt|lisp)?\s*\n(.*?)```", re.DOTALL)


@dataclass
class SMTValidation:
    valid: bool
    error: str | None


def extract_smt(sample_text: str) -> str | None:
    blocks = _SMT_BLOCK.findall(sample_text)
    for block in reversed(blocks):
        if "declare-const" in block or "assert" in block:
            return block.strip()
    return None


def validate_smt(encoding: str) -> SMTValidation:
    """Round-trip the encoding through Z3's own parser. No parse, no trust."""
    try:
        z3.parse_smt2_string(encoding)
        return SMTValidation(valid=True, error=None)
    except z3.Z3Exception as exc:
        return SMTValidation(valid=False, error=str(exc)[:500])


def solve_smt(encoding: str) -> dict:
    """Solve a VALIDATED encoding. Returns status + model value of `answer` if sat."""
    solver = z3.Solver()
    solver.set("timeout", Z3_TIMEOUT_MS)
    try:
        constraints = z3.parse_smt2_string(encoding)
    except z3.Z3Exception as exc:
        return {"status": "invalid", "detail": str(exc)[:500]}
    solver.add(constraints)
    status = solver.check()
    if status == z3.unsat:
        return {"status": "unsat", "detail": "constraints are unsatisfiable"}
    if status == z3.unknown:
        return {"status": "unknown", "detail": solver.reason_unknown()}

    model = solver.model()
    answer_val = None
    for decl in model.decls():
        if decl.name() == "answer":
            val = model[decl]
            # bools stringify as Python "True"/"False"; keep SMT-LIB casing
            answer_val = str(val).lower() if z3.is_bool(val) else str(val)
    return {"status": "sat", "answer": answer_val,
            "detail": f"model has {len(model)} assignments"}


def check_logic(encodings: list[str], claimed_answer: str) -> dict:
    """Verify a claimed answer against up to MAX_ENCODING_ATTEMPTS encodings.

    `encodings` are successive model attempts (the caller re-prompts on validation
    failure). Verified iff a validated encoding is sat and its `answer` matches.
    """
    from core.extract import answers_equivalent

    attempts = encodings[:MAX_ENCODING_ATTEMPTS]
    last_error = "no encoding produced"
    for encoding in attempts:
        validation = validate_smt(encoding)
        if not validation.valid:
            last_error = f"encoding failed validation: {validation.error}"
            continue
        result = solve_smt(encoding)
        if result["status"] == "sat":
            if result["answer"] is not None and answers_equivalent(
                result["answer"], claimed_answer
            ):
                return {"verified": True, "method": "z3",
                        "detail": f"z3 model answer={result['answer']}"}
            return {"verified": False, "method": "z3",
                    "detail": f"z3 says {result['answer']}, sample claims {claimed_answer}"}
        if result["status"] == "unsat":
            return {"verified": False, "method": "z3", "detail": "encoding unsat"}
        last_error = f"z3 {result['status']}: {result['detail']}"

    return {"verified": False, "method": "z3-unverifiable", "detail": last_error}
