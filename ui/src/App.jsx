import React, { useEffect, useState } from "react";
import AskPage from "./AskPage.jsx";
import TracesPage from "./TracesPage.jsx";
import EvalsPage from "./EvalsPage.jsx";
import { getJSON } from "./api.js";

const PAGES = ["ask", "traces", "evals"];
const DOT = { green: "bg-emerald-500", amber: "bg-amber-500", red: "bg-red-500" };

export default function App() {
  const [page, setPage] = useState("ask");
  const [health, setHealth] = useState(null);
  const [tokRate, setTokRate] = useState(0); // updated by AskPage during runs
  const [showKeys, setShowKeys] = useState(false);

  useEffect(() => {
    const poll = () => getJSON("/api/health").then(setHealth).catch(() => setHealth(null));
    poll();
    const t = setInterval(poll, 5000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    const onKey = (e) => {
      if (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT") return;
      if (e.key >= "1" && e.key <= "3") setPage(PAGES[+e.key - 1]);
      if (e.key === "?") setShowKeys((v) => !v);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const downModels = health
    ? Object.entries(health.models).filter(([, s]) => s !== "green")
    : [];

  return (
    <div className="min-h-screen flex flex-col">
      <header className="flex items-center gap-4 px-4 py-2 border-b border-zinc-800 bg-zinc-900 sticky top-0 z-10">
        <span className="text-teal-400 font-bold tracking-widest">SYNTHEO</span>
        <nav className="flex gap-1">
          {PAGES.map((p, i) => (
            <button key={p} onClick={() => setPage(p)}
              className={`px-2 py-0.5 rounded uppercase text-xs tracking-wide ${
                page === p ? "bg-zinc-700 text-white" : "text-zinc-400 hover:text-zinc-200"}`}>
              {i + 1} {p}
            </button>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-4">
          <span className="text-xs text-zinc-500">{tokRate > 0 ? `${tokRate.toFixed(0)} tok/s` : "idle"}</span>
          <span className="text-xs text-zinc-500">{health?.profile ?? "–"}</span>
          <div className="flex gap-2">
            {["heavy", "mid", "fast"].map((m) => (
              <span key={m} className="flex items-center gap-1 text-xs text-zinc-400">
                <span className={`w-2 h-2 rounded-full ${DOT[health?.models?.[m]] ?? "bg-zinc-600"}`} />
                {m}
              </span>
            ))}
          </div>
        </div>
      </header>

      {downModels.length > 0 && (
        <div className="bg-red-950 border-b border-red-800 text-red-200 text-xs px-4 py-1.5">
          {downModels.map(([m]) => (
            <span key={m}>
              model <b>{m}</b> is down — restart with:{" "}
              <code className="bg-red-900 px-1">docker compose -f serve/docker-compose.yaml up -d {m}</code>{" "}
              (routing degrades around it meanwhile).{" "}
            </span>
          ))}
        </div>
      )}

      <main className="flex-1 p-4 max-w-7xl mx-auto w-full">
        {page === "ask" && <AskPage onTokRate={setTokRate} />}
        {page === "traces" && <TracesPage />}
        {page === "evals" && <EvalsPage />}
      </main>

      {showKeys && (
        <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50"
             onClick={() => setShowKeys(false)}>
          <div className="card w-80">
            <h2 className="font-bold mb-2 text-zinc-100">Keyboard</h2>
            <table className="w-full text-xs">
              <tbody>
                <tr><td className="py-1"><kbd>1/2/3</kbd></td><td>switch page</td></tr>
                <tr><td className="py-1"><kbd>⌘/Ctrl+Enter</kbd></td><td>submit problem</td></tr>
                <tr><td className="py-1"><kbd>?</kbd></td><td>this overlay</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
