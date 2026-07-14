"""Z3 goldens (>=10 cases): validation guard, sat/unsat verdicts, answer matching."""
from core.verify.logic_z3 import check_logic, extract_smt, solve_smt, validate_smt

ENC_XY = """
(declare-const x Int)
(declare-const y Int)
(declare-const answer Int)
(assert (= (+ x y) 10))
(assert (= (- x y) 4))
(assert (= answer x))
"""

ENC_UNSAT = """
(declare-const p Bool)
(assert p)
(assert (not p))
"""

ENC_INVALID = "(declare-const x Int) (assert (= x undefined_function))"

ENC_KNIGHTS = """
(declare-const a Bool)
(declare-const b Bool)
(declare-const answer Bool)
(assert (= a (not b)))
(assert (= b (not a)))
(assert (= answer a))
"""

ENC_AGES = """
(declare-const alice Int)
(declare-const bob Int)
(declare-const answer Int)
(assert (= alice (* 2 bob)))
(assert (= (+ alice bob) 36))
(assert (> bob 0))
(assert (= answer alice))
"""


def test_validate_good_encoding():          # 1
    assert validate_smt(ENC_XY).valid

def test_validate_bad_encoding():           # 2
    v = validate_smt(ENC_INVALID)
    assert not v.valid and v.error

def test_solve_sat_with_answer():           # 3
    r = solve_smt(ENC_XY)
    assert r["status"] == "sat" and r["answer"] == "7"

def test_solve_unsat_is_verdict():          # 4
    assert solve_smt(ENC_UNSAT)["status"] == "unsat"

def test_check_logic_verified():            # 5
    r = check_logic([ENC_XY], "7")
    assert r["verified"] is True and r["method"] == "z3"

def test_check_logic_wrong_claim():         # 6
    r = check_logic([ENC_XY], "6")
    assert r["verified"] is False and "z3 says" in r["detail"]

def test_check_logic_unsat_encoding():      # 7
    r = check_logic([ENC_UNSAT], "true")
    assert r["verified"] is False and "unsat" in r["detail"]

def test_invalid_twice_is_unverifiable():   # 8 — the two-strikes guard
    r = check_logic([ENC_INVALID, ENC_INVALID], "7")
    assert r["verified"] is False and r["method"] == "z3-unverifiable"

def test_second_encoding_can_rescue():      # 9
    r = check_logic([ENC_INVALID, ENC_XY], "7")
    assert r["verified"] is True

def test_third_encoding_never_consulted():  # 10 — hard cap at 2 attempts
    r = check_logic([ENC_INVALID, ENC_INVALID, ENC_XY], "7")
    assert r["verified"] is False and r["method"] == "z3-unverifiable"

def test_ages_word_problem():               # 11
    r = check_logic([ENC_AGES], "24")
    assert r["verified"] is True

def test_knights_bool_answer():             # 12
    r = solve_smt(ENC_KNIGHTS)
    assert r["status"] == "sat" and r["answer"] in {"true", "false"}

def test_extract_smt_block():               # 13
    text = f"here:\n```smtlib\n{ENC_XY}\n```"
    assert "declare-const x" in extract_smt(text)

def test_extract_smt_none():                # 14
    assert extract_smt("no code here") is None
