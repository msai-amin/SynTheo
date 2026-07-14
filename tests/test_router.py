"""Router tests: the LOCKED decision rule (pure) + JSON parsing robustness.

Live classification accuracy (does the fast model itself pick the right
domain/difficulty) is exercised by serve/m3_gate.py against the running stack.
"""
import pytest

from core.router import RouteError, _parse, decide_tier

CFG = {
    "routing": {
        "philosophy_or_mixed_tier": 3,
        "reflex_max_difficulty": 2,
    }
}


# --- decide_tier: the LOCKED rule ---

MATRIX = [
    ("philosophy", 1, True, 3),   # philosophy always tier 3, regardless of ease
    ("philosophy", 5, False, 3),
    ("mixed", 1, True, 3),        # mixed always tier 3
    ("math", 1, True, 1),         # easy + votable -> reflex
    ("math", 2, True, 1),         # boundary: difficulty == reflex_max_difficulty
    ("logic", 2, True, 1),
    ("math", 3, True, 2),         # too hard for reflex -> deliberate
    ("math", 1, False, 2),        # easy but not votable -> deliberate
    ("logic", 5, True, 2),
    ("logic", 1, False, 2),
    ("math", 4, False, 2),
    ("logic", 2, False, 2),       # votable=False overrides the difficulty<=2 reflex path
]

@pytest.mark.parametrize("domain,difficulty,votable,expected_tier", MATRIX)
def test_decide_tier_matrix(domain, difficulty, votable, expected_tier):
    assert decide_tier(domain, difficulty, votable, CFG) == expected_tier

def test_matrix_has_12_cases():
    assert len(MATRIX) == 12


# --- JSON parsing robustness ---

def test_parse_clean_json():
    d = _parse('{"domain": "math", "difficulty": 3, "votable": true, "rationale": "x"}')
    assert d["domain"] == "math" and d["difficulty"] == 3 and d["votable"] is True

def test_parse_json_embedded_in_prose():
    d = _parse('Sure, here: {"domain": "logic", "difficulty": 2, "votable": false, '
              '"rationale": "y"} done')
    assert d["domain"] == "logic"

def test_parse_no_json_raises():
    with pytest.raises(RouteError):
        _parse("I think this is math")

def test_parse_bad_domain_raises():
    with pytest.raises(RouteError):
        _parse('{"domain": "biology", "difficulty": 2, "votable": true}')

def test_parse_bad_difficulty_raises():
    with pytest.raises(RouteError):
        _parse('{"domain": "math", "difficulty": "very hard", "votable": true}')

def test_parse_difficulty_out_of_range_raises():
    with pytest.raises(RouteError):
        _parse('{"domain": "math", "difficulty": 9, "votable": true}')

def test_parse_defaults_votable_false():
    d = _parse('{"domain": "math", "difficulty": 1}')
    assert d["votable"] is False
