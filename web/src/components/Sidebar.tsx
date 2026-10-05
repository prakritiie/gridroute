"use client";

import { AlertTriangle, CheckCircle2, X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { Sparkline } from "./Sparkline";
import { ago, dur, num, pct } from "@/lib/format";
import { SERIES, divColor, imbalance, rgbCss, type Layer } from "@/lib/palette";
import type { Cell, Kpis, Resolution } from "@/lib/types";

function useNow(every = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), every);
    return () => clearInterval(t);
  }, [every]);
  return now;
}

/* ─────────────────────────────── overview ─────────────────────────────── */
interface Metric {
  key: string;
  label: string;
  value: (k: Kpis) => string;
  unit?: string;
  sub: (k: Kpis) => string;
  series: (k: Kpis) => number | null;
  fmt: (v: number) => string;
  color: string;
}

const METRICS: Metric[] = [
  { key: "avail", label: "Available drivers", value: (k) => num(k.drivers_available),
    sub: (k) => `${num(k.drivers_online)} online · ${num(k.drivers_busy)} on trips`,
    series: (k) => k.drivers_available, fmt: num, color: SERIES.supply },
  { key: "req", label: "Ride requests", value: (k) => num(k.requests_per_min), unit: "/min",
    sub: (k) => `${num(k.matched_per_min)} matched · ${num(k.completed_per_min)} completed`,
    series: (k) => k.requests_per_min, fmt: num, color: SERIES.demand },
  { key: "rate", label: "Match rate", value: (k) => pct(k.match_rate_5m),
    sub: (k) => `${num(k.unmatched_per_min)} unmatched / min`,
    series: (k) => k.match_rate_5m, fmt: pct, color: "var(--text-2)" },
  { key: "eta", label: "Avg. pickup time", value: (k) => dur(k.eta_mean_s),
    sub: (k) => `p90 ${dur(k.eta_p90_s)} · distance estimate`,
    series: (k) => k.eta_mean_s, fmt: dur, color: "var(--text-2)" },
  { key: "queue", label: "Ride queue", value: (k) => num(k.queue_rides),
    sub: (k) => `${num(k.queue_pings)} pings in flight`,
    series: (k) => k.queue_rides, fmt: num, color: "var(--text-2)" },
];

function Overview({ kpis, history }: { kpis: Kpis | null; history: Kpis[] }) {
  const times = history.map((h) => h.t);
  return (
    <section className="section">
      <div className="section__head">
        <h2>Overview</h2>
        <span className="muted">Last 5 min</span>
      </div>
      <div className="metrics">
        {METRICS.map((m) => (
          <article key={m.key} className="metric">
            <div className="metric__text">
              <div className="metric__label">{m.label}</div>
              <div className="metric__value">
                {kpis ? m.value(kpis) : <span className="skeleton" />}
                {kpis && m.unit && <span className="metric__unit">{m.unit}</span>}
              </div>
              <div className="metric__sub">{kpis ? m.sub(kpis) : "Waiting for data"}</div>
            </div>
            <Sparkline values={history.map(m.series)} times={times} color={m.color} label={m.label} format={m.fmt} />
          </article>
        ))}
      </div>
    </section>
  );
}

/* ─────────────────────────────── integrity ─────────────────────────────── */
function Integrity({ kpis }: { kpis: Kpis | null }) {
  const now = useNow();
  const db = kpis?.double_bookings ?? 0;
  const broken = db > 0 || (kpis?.invariant_violations ?? 0) > 0;
  return (
    <section className="section">
      <div className="section__head">
        <h2>Integrity</h2>
        {kpis && (
          <span className={`badge ${broken ? "badge--bad" : "badge--good"}`}>
            {broken ? <AlertTriangle size={13} strokeWidth={2.25} /> : <CheckCircle2 size={13} strokeWidth={2.25} />}
            {broken ? "Violation" : "Healthy"}
          </span>
        )}
      </div>
      <div className={`integrity ${broken ? "integrity--bad" : ""}`}>
        <div className="integrity__num">{kpis ? db : "—"}</div>
        <div>
          <div className="integrity__label">Double-bookings</div>
          <div className="metric__sub">
            {broken ? `${kpis?.invariant_violations} invariant violations` : "Every driver holds at most one ride"}
          </div>
        </div>
      </div>
      <p className="footnote">Live audit of C1 + C2 every 10 s · last run {ago(kpis?.audited_at, now)}</p>
    </section>
  );
}

/* ─────────────────────────────── hex details ─────────────────────────────── */
function balanceWord(v: number) {
  if (v > 0.5) return "Short of drivers";
  if (v > 0.1) return "Slightly short";
  if (v >= -0.1) return "Balanced";
  if (v >= -0.5) return "Slight surplus";
  return "Driver surplus";
}

function HexDetails({ cell, cells, layer, res, onPick, onClose }: {
  cell: Cell | null; cells: Cell[]; layer: Layer; res: Resolution; onPick: (c: Cell) => void; onClose: () => void;
}) {
  if (cell) {
    const b = imbalance(cell[1], cell[2]);
    return (
      <section className="section">
        <div className="section__head">
          <h2>Hex details</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close hex details"><X size={16} /></button>
        </div>
        <code className="hexid">{cell[0]}</code>
        <dl className="kv">
          <div><dt><i style={{ background: SERIES.supply }} />Drivers</dt><dd>{num(cell[1])}</dd></div>
          <div><dt><i style={{ background: SERIES.demand }} />Requests · 5 min</dt><dd>{num(cell[2])}</dd></div>
          <div><dt><i style={{ background: rgbCss(divColor(b)) }} />Balance</dt><dd>{balanceWord(b)}</dd></div>
          <div><dt>Resolution</dt><dd>H3 · {res}</dd></div>
        </dl>
        <p className="footnote">Forecast and average ETA per hex arrive with M3.</p>
      </section>
    );
  }
  const metric = (c: Cell) => (layer === "supply" ? c[1] : layer === "demand" ? c[2] : imbalance(c[1], c[2]));
  const top = [...cells].filter((c) => (layer === "imbalance" ? c[2] > 0 : metric(c) > 0))
    .sort((a, b) => metric(b) - metric(a) || b[2] - a[2]).slice(0, 6);
  const title = { supply: "Most drivers", demand: "Most requests", imbalance: "Most short of drivers" }[layer];
  return (
    <section className="section">
      <div className="section__head">
        <h2>{title}</h2>
        <span className="muted">Select a hex</span>
      </div>
      <table className="table">
        <thead><tr><th>Hex</th><th>Drivers</th><th>Requests</th></tr></thead>
        <tbody>
          {top.map((c) => (
            <tr key={c[0]} onClick={() => onPick(c)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && onPick(c)}>
              <td><code>{c[0].slice(0, 9)}</code></td><td>{num(c[1])}</td><td>{num(c[2])}</td>
            </tr>
          ))}
          {top.length === 0 && <tr><td colSpan={3} className="muted">No data yet</td></tr>}
        </tbody>
      </table>
    </section>
  );
}

/* ─────────────────────────────── sidebar ─────────────────────────────── */
export function Sidebar(props: {
  kpis: Kpis | null; history: Kpis[]; cell: Cell | null; cells: Cell[]; layer: Layer; res: Resolution;
  onPick: (c: Cell) => void; onClose: () => void; top?: ReactNode;
}) {
  return (
    <aside className="sidebar" aria-label="Metrics">
      {props.top}
      <Overview kpis={props.kpis} history={props.history} />
      <Integrity kpis={props.kpis} />
      <HexDetails cell={props.cell} cells={props.cells} layer={props.layer} res={props.res}
        onPick={props.onPick} onClose={props.onClose} />
    </aside>
  );
}
