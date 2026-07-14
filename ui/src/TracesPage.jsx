import React, { useEffect, useState } from "react";
import { getJSON } from "./api.js";

const VERDICT_BADGE = {
  verified: "badge-verified", consensus: "badge-neutral",
  judged: "badge-judge", unverified: "badge-fail", invalid: "badge-fail",
};

function Detail({ runId, onClose }) {
  const [trace, setTrace] = useState(null);
  const [raw, setRaw] = useState(false);
  useEffect(() => { getJSON(`/api/runs/${runId}`).then(setTrace); }, [runId]);
  if (!trace) return null;
  return (
    <div className="fixed inset-0 bg-black/70 z-40 flex justify-end" onClick={onClose}>
      <div className="w-2/3 max-w-3xl bg-zinc-900 border-l border-zinc-700 h-full overflow-auto p-4"
           onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 mb-3">
          <h2 className="font-bold text-zinc-100">run #{trace.run.id}</h2>
          <span className={`badge ${VERDICT_BADGE[trace.run.verdict] ?? "badge-neutral"}`}>{trace.run.verdict}</span>
          <button className="ml-auto text-xs text-zinc-400 hover:text-zinc-100"
                  onClick={() => setRaw((v) => !v)}>{raw ? "pretty" : "raw JSON"}</button>
          <button className="text-zinc-400 hover:text-zinc-100" onClick={onClose}>✕</button>
        </div>
        {raw ? (
          <pre className="text-xs whitespace-pre-wrap">{JSON.stringify(trace, null, 2)}</pre>
        ) : (
          <>
            <p className="text-sm text-zinc-300 mb-2">{trace.problem.text}</p>
            <p className="text-xs text-zinc-500 mb-3">
              tier {trace.run.tier} · {trace.run.strategy} · answer: <b className="text-zinc-200">{trace.run.answer}</b>
              {" "}· tokens {trace.run.tokens_in}/{trace.run.tokens_out} · {(trace.run.wall_ms / 1000).toFixed(1)}s
            </p>
            {trace.samples.map((s) => (
              <div key={s.id} className="card mb-2">
                <div className="flex gap-2 items-center mb-1">
                  <span className="badge badge-neutral">{s.model}</span>
                  <span className="text-xs text-zinc-500">t={s.temp} · {s.tokens} tok · {(s.latency_ms / 1000).toFixed(1)}s</span>
                  {s.extracted_answer && <span className="badge badge-neutral text-teal-300">{s.extracted_answer}</span>}
                </div>
                <div className="flex flex-wrap gap-1 mb-1">
                  {s.verifications.map((v) => (
                    <span key={v.id} className={`badge ${v.verified ? "badge-verified" : "badge-fail"}`} title={v.detail}>
                      {v.verified ? "✓" : "✗"} {v.method}
                    </span>
                  ))}
                  {s.judgments.map((j) => (
                    <span key={j.id} className="badge badge-judge" title={j.strongest_flaw}>
                      {j.judge_model} {j.score.toFixed(1)}
                    </span>
                  ))}
                </div>
                <details>
                  <summary className="text-xs text-zinc-500 cursor-pointer">sample text</summary>
                  <pre className="text-xs text-zinc-400 whitespace-pre-wrap max-h-64 overflow-auto">{s.text}</pre>
                </details>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}

export default function TracesPage() {
  const [runs, setRuns] = useState([]);
  const [stats, setStats] = useState(null);
  const [sel, setSel] = useState(null);
  const [fDomain, setFDomain] = useState("");
  const [fTier, setFTier] = useState("");
  const [fVerdict, setFVerdict] = useState("");

  useEffect(() => {
    const q = new URLSearchParams();
    if (fDomain) q.set("domain", fDomain);
    if (fTier) q.set("tier", fTier);
    if (fVerdict) q.set("verdict", fVerdict);
    getJSON(`/api/runs?${q}`).then(setRuns).catch(() => setRuns([]));
    getJSON("/api/runs/stats").then(setStats).catch(() => {});
  }, [fDomain, fTier, fVerdict]);

  return (
    <div>
      {stats && (
        <div className="flex gap-6 mb-3 text-xs text-zinc-400">
          <span>runs today <b className="text-zinc-100">{stats.runs_today}</b></span>
          <span>verified <b className="text-teal-300">{(stats.verified_pct * 100).toFixed(0)}%</b></span>
          <span>escalation <b className="text-amber-300">{(stats.escalation_pct * 100).toFixed(0)}%</b></span>
          <span>router audit disagreements <b className="text-zinc-100">{stats.audit_disagree}/{stats.audit_count}</b></span>
          <span>tokens spent <b className="text-zinc-100">{stats.tokens_total.toLocaleString()}</b></span>
        </div>
      )}
      <div className="flex gap-2 mb-2">
        <select value={fDomain} onChange={(e) => setFDomain(e.target.value)}
                className="bg-zinc-900 border border-zinc-700 rounded p-1 text-xs">
          <option value="">all domains</option>
          {["math", "logic", "philosophy", "mixed", "unrouted"].map((d) => <option key={d}>{d}</option>)}
        </select>
        <select value={fTier} onChange={(e) => setFTier(e.target.value)}
                className="bg-zinc-900 border border-zinc-700 rounded p-1 text-xs">
          <option value="">all tiers</option>
          {[1, 2, 3].map((t) => <option key={t} value={t}>tier {t}</option>)}
        </select>
        <select value={fVerdict} onChange={(e) => setFVerdict(e.target.value)}
                className="bg-zinc-900 border border-zinc-700 rounded p-1 text-xs">
          <option value="">all verdicts</option>
          {["verified", "consensus", "judged", "unverified", "invalid"].map((v) => <option key={v}>{v}</option>)}
        </select>
      </div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-zinc-500 text-left border-b border-zinc-800">
            <th className="py-1 pr-2">time</th><th>domain</th><th>tier</th>
            <th>strategy</th><th>verdict</th><th>answer</th>
            <th className="text-right">tokens</th><th className="text-right">wall</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} onClick={() => setSel(r.id)}
                className="border-b border-zinc-900 hover:bg-zinc-900 cursor-pointer">
              <td className="py-1 pr-2 text-zinc-500">
                {new Date(r.started * 1000).toLocaleTimeString()}
              </td>
              <td>{r.domain}{r.is_eval ? <span className="text-amber-500" title="eval problem — firewalled"> ⚑</span> : null}</td>
              <td>{r.tier}</td>
              <td className="text-zinc-400">{r.strategy}</td>
              <td><span className={`badge ${VERDICT_BADGE[r.verdict] ?? "badge-neutral"}`}>{r.verdict ?? "…"}</span></td>
              <td className="max-w-48 truncate text-zinc-300">{r.answer}</td>
              <td className="text-right text-zinc-500">{(r.tokens_in ?? 0) + (r.tokens_out ?? 0)}</td>
              <td className="text-right text-zinc-500">{r.wall_ms ? `${(r.wall_ms / 1000).toFixed(0)}s` : "–"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {sel && <Detail runId={sel} onClose={() => setSel(null)} />}
    </div>
  );
}
