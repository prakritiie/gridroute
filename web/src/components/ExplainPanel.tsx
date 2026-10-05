"use client";

import { AlertTriangle, Check, X } from "lucide-react";

import { clock, dur, num } from "@/lib/format";
import type { Explain } from "@/lib/types";

const POLICY: Record<string, string> = {
  "greedy-distance": "Greedy · distance",
  "greedy-eta": "Greedy · ETA model",
  "batch-distance": "Batch · distance",
  "batch-eta": "Batch · ETA model",
};

export function ExplainPanel({ explain, error, onClose }: { explain: Explain | null; error: string | null; onClose: () => void }) {
  return (
    <section className="section">
      <div className="section__head">
        <h2>Why this driver</h2>
        <button className="icon-btn" onClick={onClose} aria-label="Close explanation"><X size={16} /></button>
      </div>
      {error && <p className="footnote">{error}</p>}
      {!error && !explain && <div className="skeleton" style={{ width: "100%", height: 120 }} />}
      {explain && <Body e={explain} />}
    </section>
  );
}

function Body({ e }: { e: Explain }) {
  const byTime = e.cost_unit === "s";
  const cands = [...e.candidates].sort((a, b) => a.cost - b.cost);
  const max = Math.max(...cands.map((c) => c.cost), 1);
  const chosen = cands.find((c) => c.driver === e.chosen);
  const nearest = [...e.candidates].sort((a, b) => a.dist_m - b.dist_m)[0];
  const notNearest = byTime && nearest && chosen && nearest.driver !== chosen.driver;
  return (
    <>
      <div className="explain__meta">
        <code>{e.ride.slice(0, 8)}</code>
        <span className="chip">{POLICY[e.policy] ?? e.policy}</span>
        {e.model && <span className="chip">ETA {e.model}</span>}
        {e.fallback && (
          <span className="badge badge--warn"><AlertTriangle size={12} strokeWidth={2.25} /> B3 fallback</span>
        )}
      </div>
      <p className="explain__lede">
        {chosen ? (
          <>
            Driver <b>{e.chosen.toUpperCase()}</b>{" "}
            {byTime ? <>predicted <b>{dur(chosen.cost)}</b> to the pickup</> : <><b>{num(chosen.dist_m)} m</b> away</>}
            {notNearest && (
              <> — not the nearest ({nearest.driver.toUpperCase()}, {num(nearest.dist_m)} m), but faster by road</>
            )}
            .
          </>
        ) : "Chosen driver not among the recorded candidates."}
      </p>
      <table className="table explain__table">
        <thead>
          <tr>
            <th>Driver</th>
            <th>Straight</th>
            {byTime && <th>OSRM</th>}
            {byTime && <th>Model</th>}
            <th aria-label="Cost">{byTime ? "Cost" : "Cost (m)"}</th>
          </tr>
        </thead>
        <tbody>
          {cands.map((c) => (
            <tr key={c.driver} className={c.driver === e.chosen ? "is-chosen" : ""}>
              <td>
                {c.driver === e.chosen && <Check size={13} strokeWidth={2.5} className="explain__check" aria-label="chosen" />}
                {c.driver.toUpperCase()}
              </td>
              <td>{num(c.dist_m)} m</td>
              {byTime && <td>{c.osrm_s != null ? dur(c.osrm_s) : "—"}</td>}
              {byTime && <td>{c.model_s != null ? dur(c.model_s) : "—"}</td>}
              <td className="explain__cost">
                <span className="explain__bar" style={{ width: `${Math.max(4, (100 * c.cost) / max)}%` }} aria-hidden />
                <span>{byTime ? dur(c.cost) : num(c.cost)}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="footnote">
        {byTime
          ? "OSRM = free-flow road time; Model = learned ETA (traffic, area, hour). Lowest cost wins"
          : "Cost = straight-line metres; nearest driver wins"}
        {e.policy.startsWith("batch") ? ", jointly for the whole 2 s batch." : "."}{" "}
        {e.sim_time ? `Sim time ${e.sim_time.replace("T", " ")}.` : `Matched ${clock(e.t)}.`}
      </p>
    </>
  );
}
