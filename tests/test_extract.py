"""Goldens for extraction + canonicalization (>=30 math equivalence cases)."""
import pytest

from core.extract import answers_equivalent, canonicalize, extract_answer

# --- extraction ---

def test_extract_fenced_answer_block():
    text = "reasoning...\n```answer\n42\n```\n"
    assert extract_answer(text) == "42"

def test_extract_last_answer_block_wins():
    text = "```answer\n1\n```\nwait no\n```answer\n2\n```"
    assert extract_answer(text) == "2"

def test_extract_boxed_fallback():
    assert extract_answer(r"so the result is \boxed{\frac{1}{2}}") == r"\frac{1}{2}"

def test_extract_none_when_absent():
    assert extract_answer("I ramble and never answer") is None

def test_extract_case_insensitive_fence():
    assert extract_answer("```ANSWER\n7\n```") == "7"


# --- equivalence goldens: (a, b, equivalent?) ---

EQUIV_CASES = [
    # fractions / decimals
    ("0.5", "1/2", True),
    ("1/2", r"\frac{1}{2}", True),
    ("0.50", "0.5", True),
    ("2/4", "1/2", True),
    ("-3/6", "-0.5", True),
    ("0.333", "1/3", False),          # close is not equal
    ("1/3", "2/6", True),
    ("7", "7.0", True),
    ("7", "8", False),
    ("1,234", "1234", True),
    # radicals / symbolic
    (r"\sqrt{2}", "sqrt(2)", True),
    (r"2\sqrt{3}", "sqrt(12)", True),
    (r"\sqrt{4}", "2", True),
    ("sqrt(2)/2", r"\frac{1}{\sqrt{2}}", True),
    ("sqrt(2)", "1.414", False),
    (r"\pi/2", "pi/2", True),
    ("2*pi", r"2\pi", True),
    # assignments and units
    ("x = 3", "3", True),
    ("y=1/2", "0.5", True),
    ("42 cm", "42", True),
    ("3.5 meters", "7/2", True),
    ("x = 3", "x = 4", False),
    # multiple choice
    ("(B)", "b", True),
    ("C.", "C", True),
    ("(A)", "B", False),
    # sets in any order
    ("{1, 2, 3}", "{3, 1, 2}", True),
    ("{1, 2}", "{1, 2, 3}", False),
    ("{1/2, 0.25}", "{0.5, 1/4}", True),
    # tuples are ordered
    ("(1, 2)", "(1, 2)", True),
    ("(1, 2)", "(2, 1)", False),
    # expressions
    ("x**2 - 1", "(x-1)*(x+1)", True),
    ("x**2 + 1", "(x-1)*(x+1)", False),
    ("(3+4)*2", "14", True),
    ("2**10", "1024", True),
    # text answers
    ("Yes", "  yes ", True),
    ("yes", "no", False),
]

@pytest.mark.parametrize("a,b,expected", EQUIV_CASES)
def test_equivalence(a, b, expected):
    assert answers_equivalent(a, b) is expected, (
        f"{a!r} vs {b!r}: canonical {canonicalize(a)!r} vs {canonicalize(b)!r}"
    )


def test_case_count_meets_spec():
    assert len(EQUIV_CASES) >= 30
