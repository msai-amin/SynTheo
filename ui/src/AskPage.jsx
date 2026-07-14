import React, { useRef, useState } from "react";
import { askStream, postJSON } from "./api.js";

const STEPS = ["routing", "sampling", "verifying", "judging", "synthesizing", "done"];
const BUDGETS = [
  ["auto", "Auto"], ["reflex", "Reflex"], ["deliberate", "Deliberate"], ["max", "Max"],
];

function VerifyBadge({ m }) {
  const label = { execute: "exec", z3: "Z3", "z3-unverifiable": "Z3", sympy: "SymPy",
                  extract: "extract" }[m.method] ?? m.method;
  return (
    <span className={`badge ${m.verified ? "badge-verified" : "badge-fail"}`}
          title={m.detail}>
      {m.verified ? "✓" : "✗"} {label}
    </span>
  );
}

function CandidateCard({ c }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="card">
      <div className="flex items-center gap-2 mb-1">
        <span className="badge badge-neutral">{c.model}</span>
        <span className="text-zinc-500 text-xs">t={c.temp}</span>
        {c.error && <span className="badge badge-fail">error</span>}
        {c.judgment != null && (
          <span className="badge badge-judge">judge {c.judgment.toFixed(1)}</span>
        )}
      </div>
      {c.extracted != null && (
        <div className="mb-1">
          <span className="badge badge-neutral text-teal-300">{String(c.extracted)}</span>
        </div>
      )}
      <div className="flex flex-wrap gap-1 mb-1">
        {(c.verifications ?? []).map((m, i) => <VerifyBadge key={i} m={m} />)}
      </div>
      {c.error
        ? <p className="text-red-400 text-xs">{c.error}</p>
        : (
          <button className="text-xs text-zinc-500 hover:text-zinc-300"
                  onClick={() => setOpen((v) => !v)}>
            {open ? "collapse" : "expand reasoning"}
          </button>
        )}
      {open && <pre className="text-xs text-zinc-400 whitespace-pre-wrap mt-1 max-h-64 overflow-auto">{c.text ?? "(reasoning not streamed at sample granularity)"}</pre>}
    </div>
  );
}

function Stepper({ active, done }) {
  return (
    <div className="flex gap-1 items-center mb-3">
      {STEPS.map((s) => {
        const state = done.includes(s) ? "done" : s === active ? "active" : "idle";
        return (
          <span key={s} className={`px-2 py-0.5 rounded text-xs uppercase tracking-wide ${
            state === "done" ? "bg-teal-900 text-teal-300"
            : state === "active" ? "bg-teal-600 text-white"
            : "bg-zinc-800 text-zinc-500"}`}>
            {s}
          </span>
        );
      })}
    </div>
  );
}

export default function AskPage({ onTokRate }) {
  const [problem, setProblem] = useState("");
  const [budget, setBudget] = useState("auto");
  const [domain, setDomain] = useState("");
  const [nOverride, setNOverride] = useState("");
  const [running, setRunning] = useState(false);
  const [events, setEvents] = useState([]);
  const [candidates, setCandidates] = useState({});
  const [result, setResult] = useState(null);
  const [routeInfo, setRouteInfo] = useState(null);
  const [debate, setDebate] = useState({});
  const [step, setStep] = useState(null);
  const [doneSteps, setDoneSteps] = useState([]);
  const abortRef = useRef(null);
  const startRef = useRef(0);
  const toksRef = useRef(0);

  const submit = async () => {
    if (!problem.trim() || running) return;
    setRunning(true); setEvents([]); setCandidates({}); setResult(null);
    setRouteInfo(null); setDebate({}); setDoneSteps([]); setStep("routing");
    startRef.current = Date.now(); toksRef.current = 0;
    abortRef.current = new AbortController();
    const body = { problem, budget };
    if (domain) body.domain = domain;
    if (nOverride) body.n = +nOverride;
    const advance = (s) => { setStep(s); setDoneSteps((d) => [...new Set([...d, ...STEPS.slice(0, STEPS.indexOf(s))])]); };
    try {
      await askStream(body, (ev) => {
        setEvents((e) => [...e, ev]);
        if (ev.type === "routed") { setRouteInfo(ev); advance("sampling"); }
        if (ev.type === "sampling") advance("sampling");
        if (ev.type === "sample") {
          setCandidates((c) => ({ ...c, [ev.index]: { ...c[ev.index], ...ev } }));
          toksRef.current += 500; // rough: refined when result arrives
          onTokRate(toksRef.current / ((Date.now() - startRef.current) / 1000));
        }
        if (ev.type === "verifying") advance("verifying");
        if (ev.type === "verification") {
          setCandidates((c) => ({ ...c, [ev.index]: { ...c[ev.index], verified: ev.verified, verifications: ev.methods } }));
        }
        if (ev.type === "judging") advance("judging");
        if (ev.type === "judgment") {
          setCandidates((c) => ({ ...c, [ev.index]: { ...c[ev.index], judgment: ev.score } }));
        }
        if (["proving", "skepticizing", "rebutting"].includes(ev.type)) advance(ev.type === "proving" ? "sampling" : ev.type === "skepticizing" ? "verifying" : "judging");
        if (ev.type === "proved") setDebate((d) => ({ ...d, position: ev.position }));
        if (ev.type === "skepticized") setDebate((d) => ({ ...d, objection: ev.objection }));
        if (ev.type === "rebutted") setDebate((d) => ({ ...d, rebuttal: ev.rebuttal }));
        if (ev.type === "escalation") setRouteInfo((r) => ({ ...r, escalated: `tier ${ev.from_tier} → ${ev.to_tier}: ${ev.reason}` }));
        if (ev.type === "result") {
          setResult(ev); advance("done"); setDoneSteps(STEPS);
          const wall = (Date.now() - startRef.current) / 1000;
          onTokRate(((ev.tokens_out ?? 0) / wall) || 0);
          setTimeout(() => onTokRate(0), 3000);
        }
        if (ev.type === "error") setResult({ confidence_type: "error", answer: null, detail: ev.detail });
      }, abortRef.current.signal);
    } catch (e) {
      if (e.name !== "AbortError") setResult({ confidence_type: "error", answer: null, detail: String(e) });
    } finally {
      setRunning(false);
    }
  };

  const onKey = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit();
  };

  const copy = (text) => navigator.clipboard.writeText(text);
  const traceMd = () => {
    const lines = [`# SynTheo trace`, `**Problem:** ${problem}`, ""];
    events.forEach((ev) => lines.push(`- \`${ev.type}\` ${JSON.stringify(ev)}`));
    return lines.join("\n");
  };

  const isDebate = Object.keys(debate).length > 0;
  const confClass = result
    ? { verified: "answer-verified", consensus: "answer-consensus", judged: "answer-judged" }[result.confidence_type] ?? "answer-unverified"
    : "";

  return (
    <div>
      <div className="flex gap-2 mb-3">
        <textarea
          className="flex-1 bg-zinc-900 border border-zinc-700 rounded p-2 h-24 font-mono text-sm focus:border-teal-600 outline-none"
          placeholder="Paste a problem…  (⌘/Ctrl+Enter to submit)"
          value={problem} onChange={(e) => setProblem(e.target.value)} onKeyDown={onKey} />
        <div className="flex flex-col gap-2 w-44">
          <select value={budget} onChange={(e) => setBudget(e.target.value)}
                  className="bg-zinc-900 border border-zinc-700 rounded p-1 text-sm">
            {BUDGETS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          <select value={domain} onChange={(e) => setDomain(e.target.value)}
                  className="bg-zinc-900 border border-zinc-700 rounded p-1 text-sm">
            <option value="">auto-route domain</option>
            {["math", "logic", "philosophy", "mixed"].map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
          <input placeholder="N override" value={nOverride}
                 onChange={(e) => setNOverride(e.target.value.replace(/\D/g, ""))}
                 className="bg-zinc-900 border border-zinc-700 rounded p-1 text-sm" />
          <button onClick={submit} disabled={running}
                  className="bg-teal-700 hover:bg-teal-600 disabled:bg-zinc-800 disabled:text-zinc-500 rounded p-1 font-bold">
            {running ? "running…" : "ask"}
          </button>
        </div>
      </div>

      {(running || result) && <Stepper active={step} done={doneSteps} />}

      {routeInfo && (
        <div className="text-xs text-zinc-400 mb-2">
          routed: domain=<b>{routeInfo.domain}</b> difficulty=<b>{routeInfo.difficulty}</b>{" "}
          votable=<b>{String(routeInfo.votable)}</b> → tier <b>{routeInfo.tier}</b>
          {routeInfo.rationale && <span className="text-zinc-500"> — {routeInfo.rationale}</span>}
          {routeInfo.escalated && <span className="text-amber-400"> · escalated: {routeInfo.escalated}</span>}
        </div>
      )}

      {!isDebate && Object.keys(candidates).length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-3">
          {Object.values(candidates).sort((a, b) => a.index - b.index)
            .map((c) => <CandidateCard key={c.index} c={c} />)}
        </div>
      )}

      {isDebate && (
        <div className="grid grid-cols-2 gap-3 mb-3">
          <div className="card">
            <h3 className="text-teal-400 font-bold text-xs uppercase mb-1">Prover (heavy)</h3>
            <p className="text-sm whitespace-pre-wrap">{debate.position ?? "…"}</p>
            {debate.rebuttal && (
              <div className="mt-2 pt-2 border-t border-zinc-800">
                <h4 className="text-teal-500 text-xs uppercase mb-1">Rebuttal</h4>
                <p className="text-sm whitespace-pre-wrap">{debate.rebuttal}</p>
              </div>
            )}
          </div>
          <div className="card">
            <h3 className="text-red-400 font-bold text-xs uppercase mb-1">Skeptic (mid)</h3>
            <p className="text-sm whitespace-pre-wrap">{debate.objection ?? "…"}</p>
          </div>
        </div>
      )}

      {result && result.confidence_type !== "error" && (
        <div className={`card ${confClass} mb-3`}>
          <div className="flex items-center gap-2 mb-2">
            <span className={`badge ${
              result.confidence_type === "verified" ? "badge-verified"
              : result.confidence_type === "judged" ? "badge-judge" : "badge-neutral"}`}>
              {result.confidence_type}{result.detail ? ` · ${result.detail}` : ""}
            </span>
            {result.judge_confidence != null && (
              <span className="badge badge-judge">confidence {result.judge_confidence}/10</span>
            )}
          </div>
          <p className="text-lg text-zinc-100 whitespace-pre-wrap">{String(result.answer)}</p>

          {result.strongest_surviving_objection && (
            /* equal visual weight with the answer — product principle */
            <div className="mt-3 p-3 bg-red-950/40 border border-red-800 rounded">
              <h4 className="text-red-300 font-bold text-xs uppercase mb-1">strongest surviving objection</h4>
              <p className="text-lg text-red-100 whitespace-pre-wrap">{result.strongest_surviving_objection}</p>
            </div>
          )}
          {result.key_premises && (
            <ol className="mt-2 text-xs text-zinc-400 list-decimal ml-4">
              {result.key_premises.map((p, i) => <li key={i}>{p}</li>)}
            </ol>
          )}

          <div className="flex gap-3 mt-3 pt-2 border-t border-zinc-800 text-xs text-zinc-500">
            <span>tokens {result.tokens_in}/{result.tokens_out}</span>
            <span>wall {(result.wall_ms / 1000).toFixed(1)}s</span>
            {result.run_id && <span>run #{result.run_id}</span>}
            <button className="hover:text-zinc-200" onClick={() => copy(String(result.answer))}>copy answer</button>
            <button className="hover:text-zinc-200" onClick={() => copy(traceMd())}>copy full trace</button>
            {result.run_id && (
              <button className="hover:text-red-300"
                      onClick={() => postJSON(`/api/runs/${result.run_id}/flag`, { note: "flagged as wrong from UI" })}>
                flag as wrong
              </button>
            )}
          </div>
        </div>
      )}

      {result?.confidence_type === "error" && (
        <div className="card border-red-700"><p className="text-red-300">{result.detail}</p></div>
      )}
    </div>
  );
}
