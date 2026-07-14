#!/usr/bin/env python3
"""Generate the frozen eval suites (dev tool — run once, commit the JSONL).

Every math/logic answer is COMPUTED at generation time (sympy / exhaustive
search / z3-style enumeration), so gold answers are correct by construction.
10 of the 40 math problems are perturbed variants of base problems (same
template, different constants) — the contamination defense from spec §3.6:
a model that memorized the base form still has to actually solve the variant.

The app never writes to evals/suites/ [LOCKED]. Deterministic: fixed seed.
"""
from __future__ import annotations

import json
import random
import sys
from itertools import permutations
from math import gcd
from pathlib import Path

import sympy

SUITES_DIR = Path(__file__).resolve().parent / "suites"
rng = random.Random(20260714)


# ============================================================================
# Math templates — each returns (problem_text, integer_answer)
# ============================================================================

def t_crt(m1, r1, m2, r2, m3, r3):
    x = 0
    while not (x % m1 == r1 and x % m2 == r2 and x % m3 == r3):
        x += 1
        if x > 10**6:
            raise ValueError("no CRT solution")
    return (f"Find the smallest positive integer that leaves remainder {r1} when "
            f"divided by {m1}, remainder {r2} when divided by {m2}, and remainder "
            f"{r3} when divided by {m3}.", x)

def t_units_digit(a, b):
    return (f"What is the units digit of {a}^{b}?", pow(a, b, 10))

def t_divisors(n):
    return (f"How many positive divisors does {n} have?",
            sympy.divisor_count(n))

def t_digit_sum_power(a, b):
    total = sum(int(d) for d in str(a**b))
    return (f"What is the sum of the digits of {a}^{b}?", total)

def t_arith_series(a1, d, n):
    s = n * (2 * a1 + (n - 1) * d) // 2
    return (f"An arithmetic sequence starts at {a1} with common difference {d}. "
            f"What is the sum of its first {n} terms?", s)

def t_committee(n, k, forbidden_pair=True):
    total = sympy.binomial(n, k)
    if forbidden_pair:
        bad = sympy.binomial(n - 2, k - 2)
        return (f"A club has {n} members. How many ways can a committee of {k} "
                f"be chosen if two specific members refuse to serve together?",
                int(total - bad))
    return (f"A club has {n} members. How many committees of {k} can be formed?",
            int(total))

def t_quadratic_roots(p, q):
    # roots p and q -> x^2 - (p+q)x + pq; ask for sum of squares of roots
    return (f"The roots of x^2 - {p + q}x + {p * q} = 0 are r and s. "
            f"What is r^2 + s^2?", p * p + q * q)

def t_gcd_lcm(a, b):
    return (f"What is GCD({a}, {b}) + LCM({a}, {b})?",
            gcd(a, b) + a * b // gcd(a, b))

def t_aime_fraction(n_faces, target):
    # probability two fair dice (1..n) sum to target, as reduced m/n; answer m+n
    count = sum(1 for i in range(1, n_faces + 1) for j in range(1, n_faces + 1)
                if i + j == target)
    m, n = count, n_faces * n_faces
    g = gcd(m, n)
    m, n = m // g, n // g
    return (f"Two fair {n_faces}-sided dice (faces 1 to {n_faces}) are rolled. "
            f"The probability that the faces sum to {target} can be written as "
            f"m/n in lowest terms. What is m + n?", m + n)

def t_last_three(a, b):
    return (f"What are the last three digits of {a}^{b}? Give the three-digit "
            f"number (with leading zeros if needed, e.g. 041 means 41).",
            pow(a, b, 1000))

MATH_BASES = [
    ("crt", t_crt, (3, 2, 5, 3, 7, 2)),
    ("crt", t_crt, (4, 1, 9, 4, 25, 7)),
    ("units", t_units_digit, (7, 2026)),
    ("units", t_units_digit, (3, 405)),
    ("divisors", t_divisors, (720,)),
    ("divisors", t_divisors, (1001,)),
    ("digitsum", t_digit_sum_power, (2, 30)),
    ("digitsum", t_digit_sum_power, (7, 10)),
    ("series", t_arith_series, (5, 7, 40)),
    ("series", t_arith_series, (12, -3, 25)),
    ("committee", t_committee, (12, 4)),
    ("committee", t_committee, (15, 5)),
    ("quadratic", t_quadratic_roots, (3, 11)),
    ("quadratic", t_quadratic_roots, (7, 4)),
    ("gcdlcm", t_gcd_lcm, (84, 36)),
    ("gcdlcm", t_gcd_lcm, (120, 45)),
    ("aimefrac", t_aime_fraction, (6, 7)),
    ("aimefrac", t_aime_fraction, (8, 9)),
    ("last3", t_last_three, (7, 100)),
    ("last3", t_last_three, (3, 200)),
    ("crt", t_crt, (5, 4, 6, 5, 7, 6)),
    ("units", t_units_digit, (13, 500)),
    ("divisors", t_divisors, (5040,)),
    ("digitsum", t_digit_sum_power, (5, 12)),
    ("series", t_arith_series, (100, -7, 30)),
    ("committee", t_committee, (10, 3)),
    ("quadratic", t_quadratic_roots, (5, 9)),
    ("gcdlcm", t_gcd_lcm, (210, 98)),
    ("aimefrac", t_aime_fraction, (12, 13)),
    ("last3", t_last_three, (9, 150)),
]

# perturbed variants: same template as a base, constants nudged — solving is
# required; memorizing the base answer does not help
MATH_PERTURBED = [
    ("crt", t_crt, (3, 1, 5, 2, 7, 4)),
    ("crt", t_crt, (4, 3, 9, 2, 25, 11)),
    ("units", t_units_digit, (7, 2027)),
    ("divisors", t_divisors, (840,)),
    ("digitsum", t_digit_sum_power, (2, 31)),
    ("series", t_arith_series, (5, 7, 41)),
    ("committee", t_committee, (12, 5)),
    ("quadratic", t_quadratic_roots, (3, 12)),
    ("gcdlcm", t_gcd_lcm, (84, 40)),
    ("aimefrac", t_aime_fraction, (6, 8)),
]


# ============================================================================
# Logic templates — all Z3-checkable constraint problems; answers verified by
# exhaustive enumeration at generation time (unique solution enforced)
# ============================================================================

def t_ages(k1, k2, s):
    # alice = k1*bob + k2; alice + bob = s  -> integers, unique
    sols = [(a, b) for a in range(1, s) for b in range(1, s)
            if a == k1 * b + k2 and a + b == s]
    assert len(sols) == 1, (k1, k2, s, sols)
    return (f"Alice's age is {k1} times Bob's age plus {k2}. Their ages sum to "
            f"{s}. How old is Alice?", sols[0][0])

def _knight_statements_space(names):
    """Statement space: (kind, arg). 'role': target is a knight/liar;
    'count': exactly k of us are knights. Pure accusation puzzles are symmetric
    under global negation and never unique — count statements break the tie."""
    space = []
    for target in names:
        space.append(("role", (target, True)))
        space.append(("role", (target, False)))
    for k in range(len(names) + 1):
        space.append(("count", k))
    return space


def _knight_eval(kind, arg, is_knight):
    if kind == "role":
        target, claimed = arg
        return is_knight[target] == claimed
    return sum(is_knight.values()) == arg


def _knight_text(speaker, kind, arg):
    if kind == "role":
        target, claimed = arg
        return f'{speaker} says: "{target} is {"a knight" if claimed else "a liar"}."'
    return f'{speaker} says: "Exactly {arg} of us are knights."'


def gen_knights(seed_rng, count):
    """Search random 3-statement puzzles until `count` unique-solution ones found."""
    names = ("A", "B", "C")
    space = _knight_statements_space(names)
    found, seen = [], set()
    while len(found) < count:
        stmts = {nm: seed_rng.choice(space) for nm in names}
        key = tuple(sorted((nm, k, str(a)) for nm, (k, a) in stmts.items()))
        if key in seen:
            continue
        seen.add(key)
        valid = []
        for bits in range(8):
            is_knight = {nm: bool(bits >> i & 1) for i, nm in enumerate(names)}
            if all(is_knight[nm] == _knight_eval(k, a, is_knight)
                   for nm, (k, a) in stmts.items()):
                valid.append(is_knight)
        if len(valid) != 1:
            continue
        text = " ".join(_knight_text(nm, k, a) for nm, (k, a) in stmts.items())
        found.append((
            "On an island, knights always tell the truth and liars always lie. "
            f"Three islanders A, B, C. {text} How many of the three are knights?",
            sum(valid[0].values())))
    return found

def t_digits(a_mult, b_add, u):
    """Built forward from the units digit u so the constraints are consistent
    by construction; uniqueness then verified by exhaustive search."""
    h, t = a_mult * u, u + b_add
    assert 1 <= h <= 9 and 0 <= t <= 9, (a_mult, b_add, u)
    digit_sum = h + t + u
    sols = [100 * hh + 10 * tt + uu
            for hh in range(1, 10) for tt in range(10) for uu in range(10)
            if hh == a_mult * uu and tt == uu + b_add and hh + tt + uu == digit_sum]
    assert len(sols) == 1, (a_mult, b_add, u, sols)
    return (f"A three-digit number: its hundreds digit is {a_mult} times its "
            f"units digit, its tens digit is the units digit plus {b_add}, and "
            f"its digits sum to {digit_sum}. What is the number?", sols[0])

def t_ordering(constraints_desc, constraints_fn, names, position, question_pos=1):
    orders = [p for p in permutations(names) if constraints_fn(p)]
    assert len(orders) == 1, (constraints_desc, orders)
    return (f"{', '.join(names)} stand in a line, position 1 leftmost. "
            f"{constraints_desc} Who is in position {question_pos}?",
            orders[0][question_pos - 1])

def t_syllogism(all_a_b, no_b_c, question_yes):
    if question_yes:
        return ("All zorks are mibs. Some mibs are teds. Does it follow "
                "logically that some zorks might be teds? Answer yes or no.", "yes")
    return ("All zorks are mibs. No mibs are teds. Can a zork be a ted? "
            "Answer yes or no.", "no")

def _mk_logic() -> list[tuple[str, str | int]]:
    problems = []
    # ages ×7
    for k1, k2, s in [(2, 0, 36), (3, 2, 42), (2, 5, 47), (4, 1, 51),
                      (2, 3, 33), (3, 0, 48), (5, 2, 62)]:
        problems.append(t_ages(k1, k2, s))
    # knights ×7 — searched until unique-solution puzzles are found
    problems.extend(gen_knights(rng, 7))
    # digits ×6 — (a_mult, b_add, units) built forward, consistent by construction
    for a, b, u in [(2, 1, 2), (3, 2, 3), (2, 3, 4), (4, 0, 2),
                    (3, 1, 2), (2, 2, 3)]:
        problems.append(t_digits(a, b, u))
    # ordering ×5
    names = ("Pia", "Quinn", "Rex", "Sam")
    ordering_cases = [
        ("Pia is immediately left of Quinn. Rex is not adjacent to Pia. "
         "Sam is in position 1.",
         lambda p: p.index("Pia") + 1 == p.index("Quinn")
         and abs(p.index("Rex") - p.index("Pia")) > 1 and p[0] == "Sam", 2),
        ("Quinn is somewhere left of Rex. Pia is in position 4. "
         "Sam is immediately right of Quinn.",
         lambda p: p.index("Quinn") < p.index("Rex") and p[3] == "Pia"
         and p.index("Sam") == p.index("Quinn") + 1, 1),
        ("Rex is in position 1 or 2. Pia is immediately left of Sam. "
         "Quinn is in position 4. Rex is left of Pia.",
         lambda p: p.index("Rex") in (0, 1) and p.index("Pia") + 1 == p.index("Sam")
         and p[3] == "Quinn" and p.index("Rex") < p.index("Pia"), 1),
        ("Pia is immediately right of Quinn. Sam is in position 3. "
         "Quinn is in position 1.",
         lambda p: p.index("Pia") == p.index("Quinn") + 1 and p[2] == "Sam"
         and p[0] == "Quinn", 4),
        ("Quinn and Rex occupy the two middle positions, with Quinn left of "
         "Rex. Pia is in position 1.",
         lambda p: p.index("Quinn") == 1 and p.index("Rex") == 2
         and p[0] == "Pia", 4),
    ]
    for desc, fn, qpos in ordering_cases:
        problems.append(t_ordering(desc, fn, names, None, qpos))
    # syllogisms ×5
    problems.append(t_syllogism(True, True, False))
    problems.append(t_syllogism(True, True, True))
    problems.append(("All floops are gleeps. Zed is a floop. Is Zed a gleep? "
                     "Answer yes or no.", "yes"))
    problems.append(("No wubs are yips. Some yips are kels. Can a kel that is "
                     "a yip be a wub? Answer yes or no.", "no"))
    problems.append(("If it rains, the match is cancelled. The match was not "
                     "cancelled. Did it rain? Answer yes or no.", "no"))
    return problems


# ============================================================================
# Philosophy prompts with per-prompt rubrics
# ============================================================================

PHILOSOPHY = [
    ("Is it ever morally permissible to lie to protect someone's feelings?",
     "Takes a clear position; engages both consequentialist and deontological "
     "considerations; addresses at least one strong counterargument; avoids "
     "vague both-sides-ism."),
    ("Does free will exist if the universe is deterministic?",
     "Distinguishes compatibilism from libertarian free will; states which "
     "position is defended and why; addresses the consequence argument or an "
     "equivalent objection."),
    ("Is there a morally relevant difference between killing and letting die?",
     "Uses at least one concrete case pair (e.g. trolley variants); identifies "
     "the doing/allowing distinction; considers whether the distinction "
     "survives the strongest equivalence argument."),
    ("Can a machine genuinely understand language, or only simulate "
     "understanding?",
     "Engages the Chinese Room or an equivalent argument; defines what "
     "'understanding' requires; commits to a position with an argued basis."),
    ("Is punishment justified by desert, deterrence, or neither?",
     "Distinguishes retributive from consequentialist justifications; "
     "addresses the problem the chosen theory faces (e.g. punishing the "
     "innocent for deterrence; free-will skepticism for desert)."),
    ("Do we have stronger moral obligations to nearby strangers than to "
     "distant ones?",
     "Engages Singer's drowning-child analogy or equivalent; takes a position "
     "on distance's moral relevance; addresses demandingness."),
    ("Is the experience machine a good objection to hedonism?",
     "Explains Nozick's thought experiment accurately; identifies what it is "
     "supposed to show; evaluates at least one hedonist reply."),
    ("Could moral facts exist without a divine lawgiver?",
     "Engages the Euthyphro dilemma; distinguishes moral realism from "
     "divine-command theory; defends a position on the source of normativity."),
    ("Is personal identity preserved through teleportation that destroys and "
     "reconstructs the body?",
     "Distinguishes physical from psychological continuity; uses the "
     "duplication problem; commits to what matters in survival."),
    ("Are there truths that science can never discover in principle?",
     "Gives at least one candidate class (qualia, mathematics, normativity); "
     "distinguishes practical from in-principle limits; addresses the "
     "scientistic reply."),
    ("Is it wrong to create art using a style learned from a living artist "
     "without their consent?",
     "Separates legal from moral questions; engages property vs. influence "
     "framings; considers the human-apprentice analogy and whether it holds."),
    ("Does the non-identity problem undermine obligations to future "
     "generations?",
     "States the non-identity problem accurately; explains why it threatens "
     "person-affecting morality; evaluates one proposed solution."),
    ("Is democratic majority rule justified when voters are predictably "
     "irrational?",
     "Engages epistemic vs. procedural justifications of democracy; addresses "
     "jury theorems or epistocracy; defends a position."),
    ("Can an action be morally wrong if it harms no one?",
     "Considers harmless wrongdoing cases (broken promises to the dead, "
     "desecration); engages the harm principle; commits to a position."),
    ("Is suffering necessary for a meaningful life?",
     "Distinguishes necessity from typical contribution; engages at least one "
     "tradition (Stoic, Buddhist, Nietzschean) accurately; avoids platitudes."),
]


def main() -> None:
    SUITES_DIR.mkdir(parents=True, exist_ok=True)

    math_rows = []
    for i, (template, fn, args) in enumerate(MATH_BASES):
        text, answer = fn(*args)
        math_rows.append({"id": f"math-{i:03d}", "domain": "math",
                          "problem": text, "answer": str(answer),
                          "difficulty": 3, "template": template,
                          "perturbed": False})
    for i, (template, fn, args) in enumerate(MATH_PERTURBED):
        text, answer = fn(*args)
        math_rows.append({"id": f"math-p{i:03d}", "domain": "math",
                          "problem": text, "answer": str(answer),
                          "difficulty": 3, "template": template,
                          "perturbed": True})
    assert len(math_rows) == 40, len(math_rows)

    logic_rows = []
    for i, (text, answer) in enumerate(_mk_logic()):
        logic_rows.append({"id": f"logic-{i:03d}", "domain": "logic",
                           "problem": text, "answer": str(answer),
                           "difficulty": 2})
    assert len(logic_rows) == 30, len(logic_rows)

    phil_rows = []
    for i, (prompt, rubric) in enumerate(PHILOSOPHY):
        phil_rows.append({"id": f"phil-{i:03d}", "domain": "philosophy",
                          "problem": prompt, "rubric": rubric,
                          "difficulty": 4})
    assert len(phil_rows) == 15, len(phil_rows)

    for name, rows in [("core_math", math_rows), ("core_logic", logic_rows),
                       ("core_philosophy", phil_rows)]:
        path = SUITES_DIR / f"{name}.jsonl"
        with open(path, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        print(f"wrote {path} ({len(rows)} problems)")


if __name__ == "__main__":
    sys.exit(main())
