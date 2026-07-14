"""SynTheo API: FastAPI + SSE on :8080.

Every long operation streams; nothing may present as frozen [spec §4.1].
The tier generators in core/tiers.py already yield event dicts — this module
just frames them as SSE and adds store queries.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.llm import REPO_ROOT, LLMClient, load_config
from core.store import Store
from core.tiers import (
    Tier3ContractError,
    run_auto,
    run_tier1,
    run_tier2,
    run_tier3,
)

app = FastAPI(title="SynTheo")

_cfg = load_config()
_client: LLMClient | None = None
_db_path = REPO_ROOT / _cfg["paths"]["episode_db"]
REPORTS_DIR = REPO_ROOT / "evals" / "reports"
UI_DIST = REPO_ROOT / "ui" / "dist"


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient(config=_cfg)
    return _client


def get_store() -> Store:
    # one connection per request: sqlite objects are thread-bound and cheap
    return Store(_db_path)


class AskRequest(BaseModel):
    problem: str
    budget: str = "auto"          # reflex | deliberate | max | auto
    domain: str | None = None     # override; None = auto-route
    votable: bool | None = None
    n: int | None = None


@app.get("/api/health")
async def health() -> dict:
    client = get_client()
    aliases = [a for a in _cfg["models"] if a != "super_swap"]
    checks = await asyncio.gather(*(client.healthy(a) for a in aliases))
    return {
        "models": {a: ("green" if ok else "red")
                   for a, ok in zip(aliases, checks)},
        "profile": _cfg["profile"],
    }


def _select_generator(client: LLMClient, store: Store, req: AskRequest):
    domain = req.domain or "math"
    votable = req.votable if req.votable is not None else True
    if req.budget == "reflex":
        return run_tier1(client, store, req.problem, domain=domain,
                         votable=votable)
    if req.budget == "deliberate":
        if (req.domain or "") == "philosophy":
            return run_tier2(client, store, req.problem, domain=domain,
                             votable=False, n=req.n)
        return run_tier2(client, store, req.problem, domain=domain,
                         votable=votable, n=req.n)
    if req.budget == "max":
        return run_tier3(client, store, req.problem,
                         domain=req.domain or "philosophy")
    return run_auto(client, store, req.problem)


@app.post("/api/ask")
async def ask(req: AskRequest, request: Request) -> StreamingResponse:
    """SSE stream of tier events. Client disconnect cancels the run."""
    client = get_client()

    async def stream():
        store = get_store()
        try:
            gen = _select_generator(client, store, req)
            async for ev in gen:
                if await request.is_disconnected():
                    break  # cancellation propagates into the generator's close
                yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
        except Tier3ContractError as exc:
            yield ("event: error\ndata: "
                   + json.dumps({"type": "error", "detail": str(exc)}) + "\n\n")
        except Exception as exc:  # noqa: BLE001 — surface, don't hang the stream
            yield ("event: error\ndata: "
                   + json.dumps({"type": "error", "detail": repr(exc)}) + "\n\n")
        finally:
            store.close()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/runs")
def list_runs(limit: int = 200, domain: str | None = None,
              tier: int | None = None, verdict: str | None = None) -> list[dict]:
    store = get_store()
    try:
        runs = store.list_runs(limit=limit)
    finally:
        store.close()
    if domain:
        runs = [r for r in runs if r["domain"] == domain]
    if tier is not None:
        runs = [r for r in runs if r["tier"] == tier]
    if verdict:
        runs = [r for r in runs if r["verdict"] == verdict]
    return runs


@app.get("/api/runs/stats")
def run_stats() -> dict:
    store = get_store()
    try:
        conn = store.conn
        today = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE started > strftime('%s','now','start of day')"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM runs WHERE finished IS NOT NULL"
                             ).fetchone()[0]
        verified = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE verdict = 'verified'").fetchone()[0]
        escalated = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE strategy LIKE '%->%'").fetchone()[0]
        audits = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN note LIKE '%DISAGREE%' THEN 1 ELSE 0 END)"
            " FROM corrections WHERE note LIKE 'shadow-audit%'").fetchone()
        tokens = conn.execute(
            "SELECT COALESCE(SUM(tokens_in + tokens_out), 0) FROM runs").fetchone()[0]
        return {"runs_today": today, "runs_total": total,
                "verified_pct": verified / total if total else 0,
                "escalation_pct": escalated / total if total else 0,
                "audit_count": audits[0] or 0,
                "audit_disagree": audits[1] or 0,
                "tokens_total": tokens}
    finally:
        store.close()


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int) -> dict:
    store = get_store()
    try:
        return store.get_run(run_id)
    except KeyError:
        raise HTTPException(404, f"run {run_id} not found") from None
    finally:
        store.close()


class FlagRequest(BaseModel):
    note: str = "flagged as wrong"


@app.post("/api/runs/{run_id}/flag")
def flag_run(run_id: int, req: FlagRequest) -> dict:
    store = get_store()
    try:
        store.get_run(run_id)  # 404 if missing
        cid = store.add_correction(run_id, req.note)
        return {"correction_id": cid}
    except KeyError:
        raise HTTPException(404, f"run {run_id} not found") from None
    finally:
        store.close()


@app.get("/api/reports")
def list_reports() -> list[dict]:
    if not REPORTS_DIR.exists():
        return []
    out = []
    for path in sorted(REPORTS_DIR.glob("*.json"), reverse=True):
        try:
            with open(path) as f:
                r = json.load(f)
            out.append({"name": path.name, "suite": r.get("suite"),
                        "started": r.get("started"),
                        "n_problems": r.get("n_problems"),
                        "strategies": list(r.get("strategies", {}))})
        except (json.JSONDecodeError, OSError):
            continue
    return out


@app.get("/api/reports/{name}")
def report_detail(name: str) -> dict:
    path = (REPORTS_DIR / name).resolve()
    if not path.is_relative_to(REPORTS_DIR.resolve()) or not path.exists():
        raise HTTPException(404, "report not found")
    with open(path) as f:
        return json.load(f)


# UI static files (built by `npm run build` in ui/) — mounted last so /api wins
if UI_DIST.exists():
    app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        target = (UI_DIST / path).resolve()
        if path and target.is_relative_to(UI_DIST.resolve()) and target.is_file():
            return FileResponse(target)
        return FileResponse(UI_DIST / "index.html")


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
