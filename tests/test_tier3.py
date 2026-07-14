"""Tier-3 output contract [LOCKED]: an answer without a surviving objection is
a validation error, not a success. Tested with a fake client (no live models)."""
import pytest

from core.tiers import Tier3ContractError, _extract_fenced, _parse_synthesis, run_tier3

CFG = {
    "tiers": {
        "tier3": {"prover_model": "heavy", "skeptic_model": "mid",
                  "judge_model": "fast", "max_rebuttal_rounds": 1},
    },
}

GOOD_SYNTHESIS = ('{"answer": "The position mostly holds.", '
                  '"key_premises": ["p1", "p2"], '
                  '"strongest_surviving_objection": "the rebuttal never addresses '
                  'the edge case where the premise fails", "judge_confidence": 7}')
NO_OBJECTION = ('{"answer": "The position holds.", "key_premises": ["p1"], '
                '"strongest_surviving_objection": "", "judge_confidence": 9}')
MISSING_FIELD = '{"answer": "x", "key_premises": [], "judge_confidence": 5}'


# --- parsing / contract checks (pure) ---

def test_parse_good_synthesis():
    d = _parse_synthesis(GOOD_SYNTHESIS)
    assert d["judge_confidence"] == 7

def test_parse_rejects_empty_objection():
    with pytest.raises(Tier3ContractError, match="empty"):
        _parse_synthesis(NO_OBJECTION)

def test_parse_rejects_missing_field():
    with pytest.raises(Tier3ContractError, match="strongest_surviving_objection"):
        _parse_synthesis(MISSING_FIELD)

def test_parse_rejects_no_json():
    with pytest.raises(Tier3ContractError):
        _parse_synthesis("I think the position is fine")

def test_none_is_a_valid_surviving_objection_value():
    # "none" is allowed as an honest (rare) claim of an airtight rebuttal;
    # only a genuinely EMPTY field is rejected
    d = _parse_synthesis(GOOD_SYNTHESIS.replace(
        "the rebuttal never addresses the edge case where the premise fails", "none"))
    assert d["strongest_surviving_objection"] == "none"


def test_extract_fenced_block():
    assert _extract_fenced("pre\n```answer\nfoo\n```\npost", "answer") == "foo"

def test_extract_fenced_missing():
    assert _extract_fenced("no fences here", "answer") is None


# --- full run_tier3 with a fake client: retry-then-raise flow ---

class FakeSample:
    def __init__(self, text):
        self.text, self.reasoning = text, ""
        self.tokens_in, self.tokens_out, self.latency_ms = 10, 20, 100.0

class FakeClient:
    """Scripted responses per call, in order: prover, skeptic, rebuttal, then
    judge attempts."""
    def __init__(self, judge_responses):
        self.config = CFG
        self.calls = []
        self._scripted = [
            '```premises\n["p1", "p2"]\n```\n```answer\nThe position holds.\n```',
            '```objection\nThe premise fails when x<0.\n```',
            '```answer\nRefined: still holds for x>=0.\n```',
        ] + list(judge_responses)

    async def complete(self, alias, messages, **kw):
        self.calls.append(alias)
        return [FakeSample(self._scripted.pop(0))]


async def _drain(gen):
    events = []
    async for ev in gen:
        events.append(ev)
    return events

async def test_run_tier3_happy_path():
    client = FakeClient([GOOD_SYNTHESIS])
    events = await _drain(run_tier3(client, None, "Is X good?", domain="philosophy"))
    result = events[-1]
    assert result["type"] == "result"
    assert result["strongest_surviving_objection"]
    assert result["confidence_type"] == "judged"

async def test_run_tier3_retries_once_on_bad_contract():
    client = FakeClient([NO_OBJECTION, GOOD_SYNTHESIS])
    events = await _drain(run_tier3(client, None, "Is X good?", domain="philosophy"))
    result = events[-1]
    assert result["type"] == "result" and result["strongest_surviving_objection"]
    # 3 (prover/skeptic/rebuttal) + 2 judge attempts
    assert client.calls.count("fast") == 2

async def test_run_tier3_raises_after_two_bad_contracts():
    client = FakeClient([NO_OBJECTION, NO_OBJECTION])
    with pytest.raises(Tier3ContractError):
        await _drain(run_tier3(client, None, "Is X good?", domain="philosophy"))

async def test_judge_is_third_family():
    from core.verify.judge import FAMILIES
    assert FAMILIES["fast"] != FAMILIES["heavy"] and FAMILIES["fast"] != FAMILIES["mid"]

async def test_exactly_one_rebuttal_round():
    # the fake client's scripted list has exactly one rebuttal entry; a second
    # rebuttal round would starve the judge call and raise IndexError via pop
    client = FakeClient([GOOD_SYNTHESIS])
    await _drain(run_tier3(client, None, "Is X good?", domain="philosophy"))
    assert client.calls == ["heavy", "mid", "heavy", "fast"]
