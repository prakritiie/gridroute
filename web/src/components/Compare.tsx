"use client";

import { ArrowDownRight, ArrowUpRight, Download, X } from "lucide-react";

import { dayLabel, fmtMetric, METRICS, policyParts, toCsv, type MetricDef, type Run } from "@/lib/lab";

/** Change vs the baseline, judged by the metric's direction. Under 1% reads as no change. */
function Delta({ base, v, def }: { base: number | null | undefined; v: number | null | undefined; def: MetricDef }) {
  if (base == null || v == null || base === 0) return null;
  const rel = (v - base) / Math.abs(base);
  if (Math.abs(rel) < 0.01) return <span className="delta delta--flat">same</span>;
  const better = def.better === "lower" ? v < base : v > base;
  const Icon = v > base ? ArrowUpRight : ArrowDownRight;
  const diff = def.fmt === "pct" ? `${(v - base) > 0 ? "+" : "−"}${Math.abs(v - base).toFixed(1)} pt`
    : `${rel > 0 ? "+" : "−"}${Math.abs(rel * 100).toFixed(rel > -0.1 && rel < 0.1 ? 1 : 0)}%`;
  return (
    <span className={`delta ${better ? "delta--good" : "delta--bad"}`} title={better ? "Better than baseline" : "Worse than baseline"}>
      <Icon size={12} strokeWidth={2.5} aria-hidden />{diff}
      <span className="sr-only">{better ? " better" : " worse"}</span>
    </span>
  );
}

export function Compare({ runs, onRemove, onClear }: { runs: Run[]; onRemove: (id: string) => void; onClear: () => void }) {
  if (!runs.length) return null;
  const base = runs[0];
  const download = () => {
    const url = URL.createObjectURL(new Blob([toCsv(runs)], { type: "text/csv" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: "gridroute-runs.csv" });
    a.click();
    URL.revokeObjectURL(url);
  };
  const rows = METRICS.filter((m) => runs.some((r) => r.metrics?.[m.key] != null));
  return (
    <section className="compare" aria-label="Compare runs">
      <header className="compare__head">
        <div>
          <h2>Compare</h2>
          <span className="muted">{runs.length > 1 ? "Changes are against the first run" : "Select another run to compare"}</span>
        </div>
        <div className="compare__actions">
          <button className="btn btn--ghost" onClick={download}><Download size={14} />Export CSV</button>
          <button className="icon-btn" onClick={onClear} aria-label="Close comparison"><X size={16} /></button>
        </div>
      </header>
      <div className="compare__scroll">
        <table className="ctable">
          <thead>
            <tr>
              <th scope="col"><span className="sr-only">Metric</span></th>
              {runs.map((r, i) => {
                const { row, text } = policyParts(r.policy);
                return (
                  <th key={r.id} scope="col">
                    <div className="ctable__run">
                      <span className="ctable__name">{row && <b className="rowtag">{row}</b>}{r.label || text}</span>
                      <button className="ctable__x" onClick={() => onRemove(r.id)} aria-label={`Remove ${r.label || text}`}><X size={12} /></button>
                    </div>
                    <div className="ctable__sub">{i === 0 ? "Baseline · " : ""}{dayLabel(r.day, r.start)} · seed {r.seed ?? "–"}</div>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => (
              <tr key={m.key}>
                <th scope="row">{m.label}<span className="ctable__dir">{m.better === "lower" ? "lower is better" : "higher is better"}</span></th>
                {runs.map((r, i) => {
                  const v = r.metrics?.[m.key] as number | null | undefined;
                  return (
                    <td key={r.id}>
                      <span className="ctable__val">{fmtMetric(v, m.fmt)}</span>
                      {i > 0 && <Delta base={base.metrics?.[m.key] as number | null | undefined} v={v} def={m} />}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
