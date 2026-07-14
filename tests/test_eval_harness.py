"""Eval harness tests: frozen suite validity + the contamination firewall on
the import path (spec §3.5/§3.6: is_eval rows never reach exports)."""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals"))

from run_eval import import_suite, load_suite  # noqa: E402

from core.store import Store  # noqa: E402


@pytest.fixture(scope="module")
def suite_rows():
    return load_suite("core")


def test_suite_counts(suite_rows):
    by_domain = {}
    for r in suite_rows:
        by_domain.setdefault(r["domain"], []).append(r)
    assert len(by_domain["math"]) == 40
    assert len(by_domain["logic"]) == 30
    assert len(by_domain["philosophy"]) == 15


def test_suite_row_contract(suite_rows):
    for r in suite_rows:
        assert r["id"] and r["domain"] in ("math", "logic", "philosophy")
        assert r["problem"].strip()
        assert 1 <= r["difficulty"] <= 5
        # every problem has exactly one of: gold answer | rubric
        assert ("answer" in r) != ("rubric" in r), r["id"]


def test_math_has_10_perturbed_variants(suite_rows):
    perturbed = [r for r in suite_rows if r.get("perturbed")]
    assert len(perturbed) == 10
    # each perturbed variant shares a template with some base problem
    base_templates = {r["template"] for r in suite_rows
                      if r["domain"] == "math" and not r.get("perturbed")}
    assert all(r["template"] in base_templates for r in perturbed)


def test_perturbed_differ_from_bases(suite_rows):
    base_texts = {r["problem"] for r in suite_rows if not r.get("perturbed")}
    for r in suite_rows:
        if r.get("perturbed"):
            assert r["problem"] not in base_texts


def test_philosophy_rubrics_are_substantive(suite_rows):
    for r in suite_rows:
        if r["domain"] == "philosophy":
            assert len(r["rubric"]) > 60, r["id"]


def test_unique_ids(suite_rows):
    ids = [r["id"] for r in suite_rows]
    assert len(ids) == len(set(ids))


def test_suites_are_valid_jsonl():
    for path in (REPO / "evals" / "suites").glob("*.jsonl"):
        with open(path) as f:
            for line in f:
                json.loads(line)


# --- contamination firewall [LOCKED] ---

def test_import_marks_all_as_eval(tmp_path, suite_rows):
    with Store(tmp_path / "t.sqlite3") as store:
        ids = import_suite(store, suite_rows, "core")
        assert len(ids) == 85
        n_eval = store.conn.execute(
            "SELECT COUNT(*) FROM problems WHERE is_eval = 1").fetchone()[0]
        assert n_eval == 85


def test_eval_runs_never_exportable(tmp_path, suite_rows):
    with Store(tmp_path / "t.sqlite3") as store:
        ids = import_suite(store, suite_rows, "core")
        # a run on an eval problem and a run on a user problem
        eval_run = store.start_run(next(iter(ids.values())), tier=2,
                                   strategy="tier2", config={})
        user_pid = store.add_problem("what is 1+1", "math")
        user_run = store.start_run(user_pid, tier=2, strategy="tier2", config={})
        exportable = store.exportable_run_ids()
        assert user_run in exportable
        assert eval_run not in exportable


def test_import_is_idempotent(tmp_path, suite_rows):
    with Store(tmp_path / "t.sqlite3") as store:
        a = import_suite(store, suite_rows, "core")
        b = import_suite(store, suite_rows, "core")
        assert a == b
        n = store.conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
        assert n == 85


def test_import_refuses_uneval_duplicate(tmp_path, suite_rows):
    """If an eval problem somehow exists un-flagged, the import must fail
    loudly rather than run evals against a contaminated store."""
    with Store(tmp_path / "t.sqlite3") as store:
        store.add_problem(suite_rows[0]["problem"], "math", is_eval=False)
        with pytest.raises(AssertionError, match="firewall"):
            import_suite(store, suite_rows, "core")
