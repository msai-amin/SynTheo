"""Math verification: math-verify primary, SymPy equivalence fallback.

Returns {"verified": bool, "method": str, "detail": str} — never raises on model
garbage; an unparseable answer is a False verdict with the parse error in detail.
"""
from __future__ import annotations

import random

import sympy

from core.extract import _try_sympy, canonicalize  # shared parsing, one behavior


def _math_verify_check(candidate: str, target: str) -> bool | None:
    """math-verify verdict, or None if it can't parse either side."""
    try:
        from math_verify import parse, verify
    except ImportError:
        return None
    try:
        gold = parse(target)
        answer = parse(candidate)
        if not gold or not answer:
            return None
        return bool(verify(gold, answer))
    except Exception:  # math-verify raises a zoo of internal errors on junk input
        return None


def _sympy_equivalent(candidate: str, target: str) -> tuple[bool | None, str]:
    ea = _try_sympy(canonicalize(candidate))
    eb = _try_sympy(canonicalize(target))
    if ea is None or eb is None:
        return None, "sympy parse failed"
    try:
        if sympy.simplify(ea - eb) == 0:
            return True, "symbolic equivalence"
    except (TypeError, ValueError) as exc:
        return None, f"simplify error: {exc}"

    # Random numeric evaluation over shared free symbols (20 points per spec).
    symbols = sorted(ea.free_symbols | eb.free_symbols, key=str)
    rng = random.Random(0)  # deterministic: same verdict for same inputs
    agreements = 0
    for _ in range(20):
        subs = {s: rng.uniform(-10, 10) for s in symbols}
        try:
            va = complex(ea.evalf(subs=subs))
            vb = complex(eb.evalf(subs=subs))
        except (TypeError, ValueError):
            return False, "numeric evaluation failed"
        if abs(va - vb) > 1e-8 * max(1.0, abs(va), abs(vb)):
            return False, f"numeric mismatch at {subs}"
        agreements += 1
    return True, f"numeric agreement at {agreements} random points"


def check_math(candidate: str, target: str) -> dict:
    """Is `candidate` mathematically equivalent to `target`?"""
    mv = _math_verify_check(candidate, target)
    if mv is True:
        return {"verified": True, "method": "math-verify", "detail": "math-verify accepted"}

    sym, detail = _sympy_equivalent(candidate, target)
    if sym is not None:
        return {"verified": sym, "method": "sympy", "detail": detail}

    if mv is False:
        return {"verified": False, "method": "math-verify", "detail": "math-verify rejected"}

    # Neither engine could parse: last resort is canonical string equality.
    ca, cb = canonicalize(candidate), canonicalize(target)
    return {
        "verified": ca == cb,
        "method": "canonical-string",
        "detail": f"{ca!r} vs {cb!r}",
    }
