"""Episode store tests, including the contamination firewall."""
import pytest

from core.store import NON_EVAL_FILTER, Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.sqlite3") as s:
        yield s


def _full_run(store: Store, is_eval: bool = False) -> int:
    pid = store.add_problem("what is 2+2", "math", is_eval=is_eval)
    rid = store.start_run(pid, tier=2, strategy="tier2", config={"n": 8})
    sid = store.add_sample(rid, "heavy", 0.9, "text...", "4", tokens=100,
                           latency_ms=1234.5)
    store.add_verification(sid, "execute", True, "stdout matched")
    store.add_judgment(sid, "mid", 8.0, "none found in step 2", "{...}")
    store.finish_run(rid, verdict="verified", answer="4",
                     confidence_type="verified", tokens_in=50, tokens_out=100,
                     wall_ms=5000)
    return rid


def test_full_trace_roundtrip(store):
    rid = _full_run(store)
    trace = store.get_run(rid)
    assert trace["run"]["verdict"] == "verified"
    assert trace["problem"]["text"] == "what is 2+2"
    assert len(trace["samples"]) == 1
    s = trace["samples"][0]
    assert s["extracted_answer"] == "4"
    assert s["verifications"][0]["method"] == "execute"
    assert s["judgments"][0]["score"] == 8.0


def test_run_reproducible_from_config(store):
    rid = _full_run(store)
    cfg = store.get_run(rid)["run"]["config_json"]
    assert '"n": 8' in cfg


def test_missing_run_raises(store):
    with pytest.raises(KeyError):
        store.get_run(9999)


def test_list_runs_includes_domain(store):
    _full_run(store)
    runs = store.list_runs()
    assert runs[0]["domain"] == "math"


# --- contamination firewall [LOCKED] ---

def test_firewall_excludes_eval_rows(store):
    rid_ok = _full_run(store, is_eval=False)
    rid_eval = _full_run(store, is_eval=True)
    exportable = store.exportable_run_ids()
    assert rid_ok in exportable
    assert rid_eval not in exportable


def test_firewall_fragment_is_the_shared_one(store):
    # exporters must compose NON_EVAL_FILTER, and it must actually filter
    assert NON_EVAL_FILTER == "problems.is_eval = 0"
    import inspect

    from core import store as store_mod
    src = inspect.getsource(store_mod.Store.exportable_run_ids)
    assert "NON_EVAL_FILTER" in src


def test_wal_mode_active(store):
    mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
