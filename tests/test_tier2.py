"""Tier-2 selection cascade tests (pure logic, no live models)."""
from core.tiers import SampleRecord, cluster_answers, select_answer


def rec(i, extracted, verified=False, error=None, model="heavy"):
    r = SampleRecord(index=i, model=model, temp=0.9, text="...",
                     extracted=extracted, tokens_in=10, tokens_out=100,
                     latency_ms=100.0, error=error)
    if verified:
        r.verifications.append({"verified": True, "method": "execute", "detail": ""})
    else:
        r.verifications.append({"verified": False, "method": "execute", "detail": ""})
    return r


# --- stage 1: majority among verified only ---

def test_verified_majority_wins():
    records = [rec(0, "42", True), rec(1, "42", True), rec(2, "41", True),
               rec(3, "13", False), rec(4, "13", False), rec(5, "13", False)]
    s = select_answer(records, votable=True)
    assert s["confidence_type"] == "verified"
    assert s["answer"] == "42"          # 13 has more votes but zero verification

def test_single_verified_beats_unverified_majority():
    records = [rec(0, "7", True)] + [rec(i, "9") for i in range(1, 6)]
    s = select_answer(records, votable=True)
    assert s["confidence_type"] == "verified" and s["answer"] == "7"

def test_equivalent_verified_answers_cluster():
    records = [rec(0, "1/2", True), rec(1, "0.5", True), rec(2, "0.75", True)]
    s = select_answer(records, votable=True)
    assert s["answer"] in ("1/2", "0.5") and "2/3" in s["detail"]


# --- stage 2: self-consistency ---

def test_consensus_when_nothing_verified():
    records = [rec(0, "B"), rec(1, "B"), rec(2, "C")]
    s = select_answer(records, votable=True)
    assert s["confidence_type"] == "consensus" and s["answer"] == "B"
    assert s["detail"] == "2/3"

def test_not_votable_skips_consensus():
    records = [rec(0, "B"), rec(1, "B"), rec(2, "C")]
    assert select_answer(records, votable=False) is None

def test_no_majority_falls_to_judge():
    records = [rec(0, "A"), rec(1, "B"), rec(2, "C")]
    assert select_answer(records, votable=True) is None


# --- robustness ---

def test_errored_samples_excluded():
    records = [rec(0, None, error="timeout"), rec(1, "5", True)]
    s = select_answer(records, votable=True)
    assert s["answer"] == "5"

def test_all_failed_returns_none():
    records = [rec(0, None, error="down"), rec(1, None, error="down")]
    assert select_answer(records, votable=True) is None

def test_none_extractions_dont_cluster():
    assert cluster_answers([rec(0, None), rec(1, None)]) == []

def test_cluster_orders_largest_first():
    clusters = cluster_answers([rec(0, "1"), rec(1, "2"), rec(2, "2")])
    assert [len(c) for c in clusters] == [2, 1]
