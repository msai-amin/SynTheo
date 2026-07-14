"""SQLite episode store. Every run is reproducible from its config_json.

Contamination firewall [LOCKED]: any query that feeds training/export MUST include
NON_EVAL_FILTER. It lives here, once; tests/test_store.py fails loudly if an
exporter drops it.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

# The one shared firewall fragment. Exporters compose it into their WHERE clause.
NON_EVAL_FILTER = "problems.is_eval = 0"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS problems (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    domain TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    is_eval INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    problem_id INTEGER NOT NULL REFERENCES problems(id),
    tier INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    config_json TEXT NOT NULL,
    started REAL NOT NULL,
    finished REAL,
    verdict TEXT,
    answer TEXT,
    confidence_type TEXT,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    wall_ms REAL
);
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    model TEXT NOT NULL,
    temp REAL NOT NULL,
    text TEXT NOT NULL,
    extracted_answer TEXT,
    tokens INTEGER DEFAULT 0,
    latency_ms REAL
);
CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY,
    sample_id INTEGER NOT NULL REFERENCES samples(id),
    method TEXT NOT NULL,
    verified INTEGER NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS judgments (
    id INTEGER PRIMARY KEY,
    sample_id INTEGER NOT NULL REFERENCES samples(id),
    judge_model TEXT NOT NULL,
    score REAL NOT NULL,
    strongest_flaw TEXT,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    note TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_problem ON runs(problem_id);
CREATE INDEX IF NOT EXISTS idx_samples_run ON samples(run_id);
CREATE INDEX IF NOT EXISTS idx_verifications_sample ON verifications(sample_id);
CREATE INDEX IF NOT EXISTS idx_judgments_sample ON judgments(sample_id);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- problems ---

    def add_problem(self, text: str, domain: str, source: str = "user",
                    is_eval: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO problems (text, domain, source, is_eval) VALUES (?,?,?,?)",
            (text, domain, source, int(is_eval)),
        )
        self.conn.commit()
        return cur.lastrowid

    # --- runs ---

    def start_run(self, problem_id: int, tier: int, strategy: str,
                  config: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (problem_id, tier, strategy, config_json, started)"
            " VALUES (?,?,?,?,?)",
            (problem_id, tier, strategy, json.dumps(config, sort_keys=True), time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, *, verdict: str, answer: str | None,
                   confidence_type: str, tokens_in: int, tokens_out: int,
                   wall_ms: float) -> None:
        self.conn.execute(
            "UPDATE runs SET finished=?, verdict=?, answer=?, confidence_type=?,"
            " tokens_in=?, tokens_out=?, wall_ms=? WHERE id=?",
            (time.time(), verdict, answer, confidence_type, tokens_in, tokens_out,
             wall_ms, run_id),
        )
        self.conn.commit()

    # --- per-sample records ---

    def add_sample(self, run_id: int, model: str, temp: float, text: str,
                   extracted_answer: str | None, tokens: int,
                   latency_ms: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO samples (run_id, model, temp, text, extracted_answer,"
            " tokens, latency_ms) VALUES (?,?,?,?,?,?,?)",
            (run_id, model, temp, text, extracted_answer, tokens, latency_ms),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_verification(self, sample_id: int, method: str, verified: bool,
                         detail: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO verifications (sample_id, method, verified, detail)"
            " VALUES (?,?,?,?)",
            (sample_id, method, int(verified), detail),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_judgment(self, sample_id: int, judge_model: str, score: float,
                     strongest_flaw: str, raw: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO judgments (sample_id, judge_model, score, strongest_flaw,"
            " raw) VALUES (?,?,?,?,?)",
            (sample_id, judge_model, score, strongest_flaw, raw),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_correction(self, run_id: int, note: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO corrections (run_id, note, created) VALUES (?,?,?)",
            (run_id, note, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    # --- reads ---

    def get_run(self, run_id: int) -> dict:
        """Full trace of one run: run row + problem + samples with their
        verifications and judgments."""
        run = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(f"run {run_id} not found")
        problem = self.conn.execute(
            "SELECT * FROM problems WHERE id=?", (run["problem_id"],)
        ).fetchone()
        samples = []
        for s in self.conn.execute(
            "SELECT * FROM samples WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall():
            samples.append({
                **dict(s),
                "verifications": [dict(v) for v in self.conn.execute(
                    "SELECT * FROM verifications WHERE sample_id=? ORDER BY id",
                    (s["id"],)).fetchall()],
                "judgments": [dict(j) for j in self.conn.execute(
                    "SELECT * FROM judgments WHERE sample_id=? ORDER BY id",
                    (s["id"],)).fetchall()],
            })
        return {"run": dict(run), "problem": dict(problem), "samples": samples}

    def list_runs(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT runs.*, problems.domain, problems.is_eval FROM runs"
            " JOIN problems ON problems.id = runs.problem_id"
            " ORDER BY runs.started DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def exportable_run_ids(self) -> list[int]:
        """Run ids eligible for any training export — firewall enforced HERE."""
        rows = self.conn.execute(
            "SELECT runs.id FROM runs JOIN problems ON problems.id = runs.problem_id"
            f" WHERE {NON_EVAL_FILTER}"
        ).fetchall()
        return [r["id"] for r in rows]
