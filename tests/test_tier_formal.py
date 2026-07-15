"""Formal-metaphysics flow (run_tier_formal) + metaphysics routing. [ADR-006 Phase 1]

Uses a fake client and a monkeypatched check_isabelle, so no models or Docker are
needed — the Isabelle backend itself is covered by tests/test_isabelle_hol.py.
"""
from core.router import decide_tier
from core.tiers import _formal_answer, run_tier_formal

CFG = {"tiers": {"formal": {"prover_model": "heavy", "parent_session": "GoedelGod",
                            "max_repair_rounds": 1}}}
ROUTING_CFG = {"routing": {"metaphysics_tier": 3, "philosophy_or_mixed_tier": 3,
                           "reflex_max_difficulty": 2}}

THEORY = ('```isabelle\ntheory Submission\n  imports "GoedelGod.GoedelGod"\n'
          'begin\ntheorem target: "[\\<box> (\\<exists> G)]" using T3 by simp\nend\n```')


class FakeSample:
    def __init__(self, text):
        self.text, self.reasoning = text, ""
        self.tokens_in, self.tokens_out, self.latency_ms = 5, 5, 1.0


class FakeClient:
    def __init__(self, responses):
        self.config = CFG
        self.calls = []
        self._r = list(responses)

    async def complete(self, alias, messages, **kw):
        self.calls.append(alias)
        return [FakeSample(self._r.pop(0))]


async def _drain(gen):
    events = []
    async for ev in gen:
        events.append(ev)
    return events


def _patch_check(monkeypatch, verdicts):
    """verdicts: (dict, raw) tuples returned in order for successive check_isabelle calls."""
    seq = list(verdicts)

    def fake(theory, target=None, *, return_raw=False):
        v = seq.pop(0)
        return v if return_raw else v[0]

    monkeypatch.setattr("core.tiers.check_isabelle", fake)


# --- verdict mapping ---

async def test_formal_verified(monkeypatch):
    _patch_check(monkeypatch, [({"verified": True, "method": "isabelle", "detail": "ok"}, "raw")])
    ev = await _drain(run_tier_formal(FakeClient([THEORY]), None,
                                      "necessarily God exists?", target=r"[\<box> (\<exists> G)]"))
    r = ev[-1]
    assert r["type"] == "result"
    assert r["confidence_type"] == "verified"
    assert r["method"] == "isabelle"
    assert "Proved" in r["answer"]


async def test_formal_refuted(monkeypatch):
    _patch_check(monkeypatch, [({"verified": False, "method": "isabelle-refuted",
                                 "detail": "countermodel"}, "raw")])
    ev = await _drain(run_tier_formal(FakeClient([THEORY]), None,
                                      "necessarily God does not exist?", target=r"[m\<not> (\<exists> G)]"))
    assert ev[-1]["confidence_type"] == "refuted"


async def test_formal_repair_then_verified(monkeypatch):
    # first build fails (unverifiable), repair round succeeds
    _patch_check(monkeypatch, [
        ({"verified": False, "method": "isabelle-unverifiable", "detail": "open goal"}, "*** error"),
        ({"verified": True, "method": "isabelle", "detail": "ok"}, "raw"),
    ])
    client = FakeClient([THEORY, THEORY])
    ev = await _drain(run_tier_formal(client, None, "q", target="[X]"))
    assert ev[-1]["confidence_type"] == "verified"
    assert client.calls.count("heavy") == 2  # initial attempt + one repair
    assert any(e["type"] == "repairing" for e in ev)


async def test_formal_no_theory_block_never_calls_kernel(monkeypatch):
    called = {"n": 0}

    def fake(*a, **k):
        called["n"] += 1
        return ({}, "") if k.get("return_raw") else {}

    monkeypatch.setattr("core.tiers.check_isabelle", fake)
    ev = await _drain(run_tier_formal(FakeClient(["no code here", "still nothing"]),
                                      None, "q", target="[X]"))
    assert ev[-1]["confidence_type"] == "unverified"
    assert called["n"] == 0  # kernel never invoked when the model emits no theory


# --- routing ---

def test_router_metaphysics_routes_to_formal_marker():
    # metaphysics ignores difficulty/votable and takes the formal marker tier
    assert decide_tier("metaphysics", 5, False, ROUTING_CFG) == 3
    assert decide_tier("metaphysics", 1, True, ROUTING_CFG) == 3


def test_formal_answer_strings():
    assert "Proved" in _formal_answer("verified", "[X]")
    assert "Refuted" in _formal_answer("refuted", None)
    assert "Unresolved" in _formal_answer("unverified", "[X]")
