"""Judge bias-guard tests: cross-family selection, contentless rejection, retry."""
import pytest

from core.verify.judge import (
    FAMILIES,
    JudgeError,
    Judgment,
    eligible_judges,
    is_contentless,
    judge_sample,
    parse_verdict,
)


# --- family guard [LOCKED] ---

def test_never_judges_own_family():
    for alias in FAMILIES:
        assert alias not in eligible_judges(alias)

def test_cross_family_pool():
    assert set(eligible_judges("heavy")) == {"mid", "fast"}

def test_respects_availability():
    assert eligible_judges("heavy", available=["heavy", "fast"]) == ["fast"]

def test_no_judge_available():
    assert eligible_judges("heavy", available=["heavy"]) == []


# --- contentless detection ---

@pytest.mark.parametrize("flaw,reason,contentless", [
    ("looks correct", "seems fine", True),
    ("none", "well reasoned", True),
    ("", "detailed reason about step 3 dividing by zero when x=0", True),
    ("N/A", "good", True),
    ("assumes x>0 without justification in step 2",
     "the argument is valid except the sign assumption in step 2, which fails for x=-1", False),
])
def test_is_contentless(flaw, reason, contentless):
    v = {"score": 7, "strongest_flaw": flaw, "verdict_reason": reason}
    assert is_contentless(v) is contentless


# --- verdict parsing ---

def test_parse_valid_json():
    v = parse_verdict('{"score": 8, "strongest_flaw": "f", "verdict_reason": "r"}')
    assert v["score"] == 8.0

def test_parse_json_embedded_in_prose():
    v = parse_verdict('Sure! {"score": 3, "strongest_flaw": "f", "verdict_reason": "r"} done')
    assert v["score"] == 3.0

def test_parse_rejects_out_of_range():
    assert parse_verdict('{"score": 11, "strongest_flaw": "f", "verdict_reason": "r"}') is None

def test_parse_rejects_no_json():
    assert parse_verdict("I give it an eight") is None


# --- retry flow with a fake client ---

class FakeSample:
    def __init__(self, text): self.text = text

class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
    async def complete(self, alias, messages, **kw):
        self.calls.append((alias, messages))
        return [FakeSample(self.responses.pop(0))]

GOOD = ('{"score": 6, "strongest_flaw": "step 4 drops the negative root", '
        '"verdict_reason": "solving x^2=4 yields two roots; only +2 is considered"}')
BAD = '{"score": 9, "strongest_flaw": "none", "verdict_reason": "looks correct"}'


async def test_contentless_triggers_stern_retry():
    client = FakeClient([BAD, GOOD])
    j = await judge_sample(client, "p", "s", generator_alias="heavy")
    assert isinstance(j, Judgment) and j.retried and j.score == 6.0
    # the retry prompt must be sterner, not a repeat
    assert "contentless" in client.calls[1][1][-1]["content"]

async def test_contentful_first_try_no_retry():
    client = FakeClient([GOOD])
    j = await judge_sample(client, "p", "s", generator_alias="heavy")
    assert not j.retried and len(client.calls) == 1

async def test_two_contentless_raises():
    client = FakeClient([BAD, BAD])
    with pytest.raises(JudgeError):
        await judge_sample(client, "p", "s", generator_alias="heavy")

async def test_judge_is_cross_family():
    client = FakeClient([GOOD])
    j = await judge_sample(client, "p", "s", generator_alias="heavy")
    assert FAMILIES[j.judge_alias] != FAMILIES["heavy"]

async def test_blind_judging_no_generator_identity():
    client = FakeClient([GOOD])
    await judge_sample(client, "problem text", "sample text", generator_alias="heavy")
    prompt = client.calls[0][1][0]["content"]
    assert "heavy" not in prompt and "gpt-oss" not in prompt
