"""Answer extraction + canonicalization.

Extraction is where systems silently die (spec §6): two samples that both mean
one-half must canonicalize to the same string or voting is broken. Canonical
forms are compared as strings; `answers_equivalent` adds SymPy-backed numeric
equivalence on top for forms that canonicalize differently (e.g. `1/2` vs `0.5`).
"""
from __future__ import annotations

import re
from fractions import Fraction

import sympy
from sympy.parsing.sympy_parser import (
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

_ANSWER_BLOCK = re.compile(r"```answer\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_BOXED = re.compile(r"\\boxed\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")
_MC_LETTER = re.compile(r"^\(?([A-Ea-e])\)?[.):]?$")
_TRANSFORMS = standard_transformations + (implicit_multiplication_application,)


_PLACEHOLDER_LINE = re.compile(r"^\s*<[^>]{1,40}>\s*$")  # e.g. "<final answer>"


def extract_answer(sample_text: str) -> str | None:
    """Pull the model's final answer from a sample.

    Priority: last fenced ```answer block (prompt-enforced format), else last
    \\boxed{}, else None — no heuristic guessing beyond these two forms.
    Small models sometimes copy the prompt's literal "<final answer>"
    placeholder into the block above the real answer; such lines are dropped.
    """
    blocks = _ANSWER_BLOCK.findall(sample_text)
    if blocks:
        lines = [ln for ln in blocks[-1].splitlines()
                 if ln.strip() and not _PLACEHOLDER_LINE.match(ln)]
        return "\n".join(lines).strip() or None
    boxed = _BOXED.findall(sample_text)
    if boxed:
        return boxed[-1].strip()
    return None


def _strip_latex(s: str) -> str:
    s = s.replace(r"\left", "").replace(r"\right", "")
    s = re.sub(r"\\sqrt\{([^{}]+)\}", r"sqrt(\1)", s)
    s = re.sub(r"\\sqrt\s*(\w)", r"sqrt(\1)", s)
    # frac arguments may contain one level of nesting (e.g. \frac{1}{sqrt(2)})
    _arg = r"\{((?:[^{}]|\{[^{}]*\})+)\}"
    s = re.sub(r"\\d?frac" + _arg + _arg, r"(\1)/(\2)", s)
    # keep a space so implicit multiplication sees "2 pi", not the token "2pi"
    s = s.replace(r"\pi", " pi").replace(r"\cdot", "*").replace(r"\times", "*")
    s = s.replace("^", "**")
    s = re.sub(r"\\text\{[^}]*\}", "", s)  # units like \text{ cm} are dropped
    s = s.replace("$", "").replace(r"\,", "").replace(r"\!", "")
    return s.strip()


def _strip_units_and_prefix(s: str) -> str:
    # "x = 3" -> "3"; keep RHS of a simple assignment/equation
    m = re.match(r"^\s*[a-zA-Z]\w*\s*=\s*(.+)$", s)
    if m:
        s = m.group(1)
    # trailing unit words after a number: "42 cm", "3.5 meters" — but never
    # math constants ("2 pi" is a product, not a quantity of pis)
    m = re.match(r"^([-+]?[\d.,/]+(?:\s*\*\*?\s*\d+)?)\s*([a-zA-Z°%]+)\.?$", s.strip())
    if m and m.group(2).lower() not in {"pi", "e", "i", "oo"}:
        s = m.group(1)
    return s.strip()


def _try_number(s: str) -> str | None:
    """Canonicalize plain numerics: 0.50 -> 1/2, 1,234 -> 1234, 4/8 -> 1/2."""
    t = s.replace(",", "").replace(" ", "")
    try:
        f = Fraction(t)
    except (ValueError, ZeroDivisionError):
        return None
    if f.denominator == 1:
        return str(f.numerator)
    return f"{f.numerator}/{f.denominator}"


_CHAINED_POW = re.compile(r"\*\*\s*\(?\d+(\.\d+)?\)?\s*\*\*")
_BIG_EXPONENT = re.compile(r"\*\*\s*\(?(\d{5,})")


def _pathological(s: str) -> bool:
    """Expressions whose EVALUATION would hang or exhaust memory (10**10**10
    is a 10-billion-digit integer). Cheap syntactic screen before parse."""
    if len(s) > 500:
        return True
    if _CHAINED_POW.search(s) or _BIG_EXPONENT.search(s):
        return True
    return False


def _try_sympy(s: str) -> sympy.Expr | None:
    if _pathological(s):
        return None
    try:
        expr = parse_expr(s, transformations=_TRANSFORMS, evaluate=True)
    except Exception:
        # parse_expr raises a zoo on arbitrary model text (SympifyError,
        # SyntaxError, tokenize.TokenError, MemoryError on huge exponents, ...);
        # any failure just means "not a math expression"
        return None
    return expr if isinstance(expr, sympy.Expr) else None


def canonicalize(raw: str) -> str:
    """Map equivalent surface forms to one canonical string.

    Handles: numeric normalization (0.5 == 1/2 == \\frac{1}{2}), `x = 3` vs `3`,
    simple units, multiple-choice letters, set order, case/whitespace folding.
    Non-mathematical answers fold to lowercase collapsed-whitespace text.
    """
    s = raw.strip()

    m = _MC_LETTER.match(s)
    if m:
        return m.group(1).upper()

    s = _strip_latex(s)
    s = _strip_units_and_prefix(s)

    # thousands separators before tuple logic: "1,234" is a number, not a pair
    if re.match(r"^[-+]?\d{1,3}(,\d{3})+(\.\d+)?$", s):
        s = s.replace(",", "")

    # Sets / tuples: canonicalize element order for {..} and comma lists
    m = re.match(r"^\{(.+)\}$", s) or re.match(r"^\((.+)\)$", s)
    body = m.group(1) if m else (s if "," in s else None)
    if body and "," in body:
        parts = [canonicalize(p) for p in body.split(",") if p.strip()]
        if m and s.startswith("{"):
            return "{" + ", ".join(sorted(parts)) + "}"
        return "(" + ", ".join(parts) + ")"

    num = _try_number(s)
    if num is not None:
        return num

    # plain words ("yes", "impossible") are text, not sympy Symbols — but keep
    # known mathematical constants on the math path
    if re.fullmatch(r"[a-zA-Z]+", s) and s.lower() not in {"pi", "e", "oo", "i"}:
        return s.lower()

    expr = _try_sympy(s)
    if expr is not None and not expr.free_symbols:
        simplified = sympy.nsimplify(sympy.simplify(expr), rational=False)
        # exact rational -> fraction form; else exact symbolic form
        if simplified.is_Rational:
            return _try_number(str(simplified)) or str(simplified)
        return str(simplified)
    if expr is not None:
        return str(sympy.simplify(expr))

    return re.sub(r"\s+", " ", s).lower()


def answers_equivalent(a: str, b: str) -> bool:
    """True if two raw answers mean the same thing."""
    ca, cb = canonicalize(a), canonicalize(b)
    if ca == cb:
        return True
    ea, eb = _try_sympy(ca), _try_sympy(cb)
    if ea is None or eb is None:
        return False
    try:
        diff = sympy.simplify(ea - eb)
        if diff == 0:
            return True
        if not (ea.free_symbols | eb.free_symbols):
            return abs(complex(ea.evalf()) - complex(eb.evalf())) < 1e-9
    except (TypeError, ValueError):
        pass
    return False
