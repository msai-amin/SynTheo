"""Goldens for the math-verify + SymPy verification cascade."""
import pytest

from core.verify.mathcheck import check_math

CASES = [
    ("391", "391", True),
    ("1/2", "0.5", True),
    (r"\frac{3}{4}", "0.75", True),
    ("sqrt(8)", r"2\sqrt{2}", True),
    ("x**2-1", "(x-1)*(x+1)", True),
    ("sin(x)**2 + cos(x)**2", "1", True),
    ("e**(i*pi)", "-1", False),  # sympy parse of e/i is ambiguous -> must NOT falsely verify
    ("17*23", "391", True),
    ("392", "391", False),
    ("0.1+0.2", "0.3", True),   # symbolic, not float, arithmetic
    ("x+y", "y+x", True),
    ("x-y", "y-x", False),
    ("log(exp(2))", "2", True),
    ("garbage $$%», ", "391", False),
]

@pytest.mark.parametrize("candidate,target,expected", CASES)
def test_check_math(candidate, target, expected):
    result = check_math(candidate, target)
    assert result["verified"] is expected, result

def test_result_contract():
    r = check_math("1/2", "0.5")
    assert set(r) == {"verified", "method", "detail"}
