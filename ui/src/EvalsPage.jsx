import React, { useEffect, useState } from "react";
import { getJSON } from "./api.js";

function ReportDetail({ name, onClose }) {
  const [report, setReport] = useState(null);
  useEffect(() => { getJSON(`/api/reports/${name}`).then(setReport); }, [name]);
  if (!report) return null;
  return (
    <div className="fixed inset-0 bg-black/70 z-40 flex justify-end" onClick={onClose}>
      <div className="w-2/3 max-w-3xl bg-zinc-900 border-l border-zinc-700 h-full overflow-auto p-4"
           onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 mb-3">
          <h2 className="font-bold text-zinc-100">{name}</h2>
          <span className="text-xs text-zinc-500">{report.n_problems} problems</span>
          <button className="ml-auto text-zinc-400 hover:text-zinc-100" onClick={onClose}>✕</button>
        </div>
        {Object.entries(report.strategies).map(([strategy, data]) => (
          <div key={strategy} className="card mb-3">
            <h3 className="text-teal-300 text-sm font-bold mb-2 uppercase">{strategy}</h3>
            <table className="w-full text-xs mb-2">
              <thead>
                <tr className="text-zinc-500 text-left border-b border-zinc-800">
                  <th className="py-1 pr-2">domain</th><th>n</th><th>accuracy</th>
                  <th className="text-right">mean tokens</th><th className="text-right">mean wall</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(data.aggregate).map(([domain, agg]) => (
                  <tr key={domain} className="border-b border-zinc-900">
                    <td className="py-1 pr-2 text-zinc-300">{domain}</td>
                    <td>{agg.n}</td>
                    <td className={agg.accuracy >= 0.7 ? "text-teal-300" : "text-amber-300"}>
                      {(agg.accuracy * 100).toFixed(0)}%
                    </td>
                    <td className="text-right text-zinc-500">{agg.mean_tokens.toFixed(0)}</td>
                    <td className="text-right text-zinc-500">{(agg.mean_wall_ms / 1000).toFixed(1)}s</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <details>
              <summary className="text-xs text-zinc-500 cursor-pointer">
                {data.problems.length} problem results
              </summary>
              <table className="w-full text-xs mt-2">
                <thead>
                  <tr className="text-zinc-500 text-left border-b border-zinc-800">
                    <th className="py-1 pr-2">id</th><th>domain</th><th>correct</th>
                    <th>answer</th><th className="text-right">tokens</th>
                  </tr>
                </thead>
                <tbody>
                  {data.problems.map((p) => (
                    <tr key={p.id} className="border-b border-zinc-900">
                      <td className="py-1 pr-2 text-zinc-400">{p.id}</td>
                      <td>{p.domain}</td>
                      <td className={p.score.correct ? "text-teal-300" : "text-red-400"}
                          title={p.score.detail}>
                        {p.score.correct ? "✓" : "✗"}
                      </td>
                      <td className="max-w-48 truncate text-zinc-300">{p.result.answer}</td>
                      <td className="text-right text-zinc-500">
                        {(p.result.tokens_in ?? 0) + (p.result.tokens_out ?? 0)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function EvalsPage() {
  const [reports, setReports] = useState([]);
  const [sel, setSel] = useState(null);

  useEffect(() => {
    getJSON("/api/reports").then(setReports).catch(() => setReports([]));
  }, []);

  return (
    <div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-zinc-500 text-left border-b border-zinc-800">
            <th className="py-1 pr-2">report</th><th>suite</th><th>started</th>
            <th>n problems</th><th>strategies</th>
          </tr>
        </thead>
        <tbody>
          {reports.map((r) => (
            <tr key={r.name} onClick={() => setSel(r.name)}
                className="border-b border-zinc-900 hover:bg-zinc-900 cursor-pointer">
              <td className="py-1 pr-2 text-zinc-300">{r.name}</td>
              <td>{r.suite}</td>
              <td className="text-zinc-500">{new Date(r.started * 1000).toLocaleString()}</td>
              <td>{r.n_problems}</td>
              <td className="text-zinc-400">{r.strategies.join(", ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {reports.length === 0 && (
        <p className="text-xs text-zinc-500 mt-2">
          no eval reports yet — run <code className="bg-zinc-900 px-1">python evals/run_eval.py --suite core --strategy all</code>
        </p>
      )}
      {sel && <ReportDetail name={sel} onClose={() => setSel(null)} />}
    </div>
  );
}
