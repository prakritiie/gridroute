"use client";

import { useEffect, useState } from "react";

import { Hexi } from "./Hexi";
import { Sparkline } from "./Sparkline";
import { ago, clock, dur, num, pct, shortId } from "@/lib/format";
import {
  DEMAND_RAMP, IMBALANCE_RAMP, SERIES, SUPPLY_RAMP, imbalance, rgbCss, type Layer,
} from "@/lib/palette";
import type { Cell, Kpis, Link, Match, Resolution } from "@/lib/types";

function useNow(every = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), every);
    return () => clearInterval(t);
  }, [every]);
  return now;
}

/* ---------------------------------------------------------------- brand + link */
export function BrandPlate({ link, t }: { link: Link; t: number | null }) {
  const status = {
    live: { cls: "good", icon: "●", text: "Live" },
    connecting: { cls: "warn", icon: "◌", text: "Tuning in" },
    reconnecting: { cls: "crit", icon: "▲", text: "Signal lost" },
  }[link];
  return (
    <header className="plate rise" style={{ animationDelay: "0ms" }}>
      <Hexi pose={link === "live" ? "duty" : "sleep"} size={46} />
      <div>
        <div className="plate__roof">Dispatch · NYC</div>
        <h1 className="plate__name">GridRoute</h1>
      </div>
      <div className="plate__meta">
        <span className={`status status--${status.cls}`}>
          <i aria-hidden>{status.icon}</i> {status.text}
        </span>
        <span className="plate__clock">{t ? clock(t) : "--:--:--"}</span>
      </div>
    </header>
  );
}

/* ---------------------------------------------------------------- layer + legend */
const LAYERS: { id: Layer | "forecast"; label: string; locked?: string }[] = [
  { id: "supply", label: "Supply" },
  { id: "demand", label: "Demand" },
  { id: "imbalance", label: "Imbalance" },
  { id: "forecast", label: "Forecast", locked: "M3" },
];

export function LayerControl({
  layer, setLayer, res, setRes, max, windowS,
}: {
  layer: Layer; setLayer: (l: Layer) => void; res: Resolution; setRes: (r: Resolution) => void;
  max: number; windowS: number | null;
}) {
  return (
    <section className="card controls rise" style={{ animationDelay: "80ms" }} aria-label="Map layer">
      <div className="seg" role="radiogroup" aria-label="Layer">
        {LAYERS.map((l) => (
          <button
            key={l.id}
            role="radio"
            aria-checked={layer === l.id}
            disabled={!!l.locked}
            className={layer === l.id ? "on" : ""}
            onClick={() => !l.locked && setLayer(l.id as Layer)}
            title={l.locked ? "Demand forecasting arrives in milestone M3" : undefined}
          >
            {l.label}
            {l.locked && <small>{l.locked}</small>}
          </button>
        ))}
      </div>
      <div className="dial" role="radiogroup" aria-label="H3 resolution">
        <span className="dial__label">H3 res</span>
        {([9, 8, 7] as Resolution[]).map((r) => (
          <button key={r} role="radio" aria-checked={res === r} className={res === r ? "on" : ""} onClick={() => setRes(r)}>
            {r}
          </button>
        ))}
        <span className="dial__hint">{{ 9: "0.1 km²", 8: "0.7 km²", 7: "5 km²" }[res]}</span>
      </div>
      <Legend layer={layer} max={max} windowS={windowS} />
    </section>
  );
}

function Legend({ layer, max, windowS }: { layer: Layer; max: number; windowS: number | null }) {
  if (layer === "imbalance") {
    return (
      <figure className="legend">
        <figcaption>Riders vs drivers per hex</figcaption>
        <div className="legend__bar">
          {IMBALANCE_RAMP.map((c, i) => <span key={i} style={{ background: rgbCss(c) }} />)}
        </div>
        <div className="legend__ticks legend__ticks--3">
          <span>more drivers</span><span>balanced</span><span>more riders</span>
        </div>
      </figure>
    );
  }
  const ramp = layer === "supply" ? SUPPLY_RAMP : DEMAND_RAMP;
  const step = max / ramp.length;
  return (
    <figure className="legend">
      <figcaption>
        {layer === "supply" ? "Available drivers per hex" : `Ride requests per hex, last ${Math.round((windowS ?? 300) / 60)} min`}
      </figcaption>
      <div className="legend__bar">
        {ramp.map((c, i) => <span key={i} style={{ background: rgbCss(c) }} />)}
      </div>
      <div className="legend__ticks" style={{ gridTemplateColumns: `repeat(${ramp.length}, 1fr)` }}>
        {ramp.map((_, i) => <span key={i}>{i === ramp.length - 1 ? `${num(step * i)}+` : num(step * i)}</span>)}
      </div>
    </figure>
  );
}

/* ---------------------------------------------------------------- the key rule */
export function KeyRule({ kpis }: { kpis: Kpis | null }) {
  const now = useNow();
  const db = kpis?.double_bookings ?? 0;
  const broken = db > 0 || (kpis?.invariant_violations ?? 0) > 0;
  return (
    <section className={`card keyrule rise ${broken ? "keyrule--broken" : ""}`} style={{ animationDelay: "160ms" }}>
      <Hexi pose={broken ? "alarm" : "key"} size={84} />
      <div>
        <div className="eyebrow">One key per driver</div>
        <div className="keyrule__num" aria-label={`${db} double-bookings`}>{kpis ? db : "—"}</div>
        <div className="keyrule__label">double-bookings</div>
        <div className={`status status--${broken ? "crit" : "good"}`}>
          <i aria-hidden>{broken ? "▲" : "✓"}</i>
          {broken ? `${kpis?.invariant_violations} invariant violations` : "Invariants hold"}
          <span className="muted"> · audited {ago(kpis?.audited_at, now)}</span>
        </div>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- KPI strip */
interface TileDef {
  key: string;
  label: string;
  value: (k: Kpis) => string;
  sub: (k: Kpis) => string;
  series: (k: Kpis) => number | null;
  fmt: (v: number) => string;
  color: string;
}

const TILES: TileDef[] = [
  { key: "avail", label: "Drivers available", value: (k) => num(k.drivers_available),
    sub: (k) => `${num(k.drivers_online)} online · ${num(k.drivers_busy)} on trips`,
    series: (k) => k.drivers_available, fmt: num, color: SERIES.supply },
  { key: "req", label: "Requests / min", value: (k) => num(k.requests_per_min),
    sub: (k) => `${num(k.matched_per_min)} matched · ${num(k.completed_per_min)} done`,
    series: (k) => k.requests_per_min, fmt: num, color: SERIES.demand },
  { key: "rate", label: "Match rate · 5 min", value: (k) => pct(k.match_rate_5m),
    sub: (k) => `${num(k.unmatched_per_min)} unmatched / min`,
    series: (k) => k.match_rate_5m, fmt: pct, color: "var(--ink-2)" },
  { key: "eta", label: "Pickup ETA · est.", value: (k) => dur(k.eta_mean_s),
    sub: (k) => `p90 ${dur(k.eta_p90_s)} · straight-line`,
    series: (k) => k.eta_mean_s, fmt: dur, color: "var(--ink-2)" },
  { key: "queue", label: "Ride queue", value: (k) => num(k.queue_rides),
    sub: (k) => `${num(k.queue_pings)} pings in flight`,
    series: (k) => k.queue_rides, fmt: num, color: "var(--ink-2)" },
];

export function KpiStrip({ kpis, history }: { kpis: Kpis | null; history: Kpis[] }) {
  const times = history.map((h) => h.t);
  return (
    <section className="kpis" aria-label="Key indicators">
      {TILES.map((t, i) => (
        <article key={t.key} className="tile rise" style={{ animationDelay: `${240 + i * 60}ms` }}>
          <div className="tile__label">{t.label}</div>
          <div className="tile__value">{kpis ? t.value(kpis) : "—"}</div>
          <div className="tile__sub">{kpis ? t.sub(kpis) : "waiting for the aggregator"}</div>
          <Sparkline values={history.map(t.series)} times={times} color={t.color} label={t.label} format={t.fmt} />
        </article>
      ))}
    </section>
  );
}

/* ---------------------------------------------------------------- radio log */
export function RadioLog({ matches }: { matches: Match[] }) {
  return (
    <section className="card radio rise" style={{ animationDelay: "120ms" }} aria-label="Recent matches">
      <div className="radio__head">
        <span className="eyebrow">Radio log</span>
        <span className="radio__live"><i className="dot" aria-hidden /> matches as they happen</span>
      </div>
      {matches.length === 0 ? (
        <p className="radio__empty">Quiet on the radio. Matches appear here the moment a driver is claimed.</p>
      ) : (
        <ol className="radio__list">
          {matches.map((m) => (
            <li key={`${m.ride_id}-${m.at}`} className="radio__line">
              <time>{clock(m.at)}</time>
              <span className="radio__ride">R·{shortId(m.ride_id)}</span>
              <span className="radio__arrow" aria-hidden>→</span>
              <span className="radio__driver">{m.driver_id.toUpperCase()}</span>
              <span className="radio__eta">{dur(m.eta_s)}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/* ---------------------------------------------------------------- inspector + table view */
export function Inspector({
  cell, cells, layer, res, onPick, onClose,
}: {
  cell: Cell | null; cells: Cell[]; layer: Layer; res: Resolution; onPick: (c: Cell) => void; onClose: () => void;
}) {
  const metric = (c: Cell) => (layer === "supply" ? c[1] : layer === "demand" ? c[2] : imbalance(c[1], c[2]));
  const top = [...cells].filter((c) => (layer === "imbalance" ? c[2] > 0 : metric(c) > 0))
    .sort((a, b) => metric(b) - metric(a) || b[2] - a[2]).slice(0, 6);
  return (
    <section className="card inspector rise" style={{ animationDelay: "200ms" }} aria-label="Hex inspector">
      {cell ? (
        <>
          <div className="radio__head">
            <span className="eyebrow">Hex · res {res}</span>
            <button className="x" onClick={onClose} aria-label="Close inspector">×</button>
          </div>
          <code className="inspector__id">{cell[0]}</code>
          <dl className="inspector__stats">
            <div><dt><i style={{ background: SERIES.supply }} />Drivers</dt><dd>{num(cell[1])}</dd></div>
            <div><dt><i style={{ background: SERIES.demand }} />Requests · 5m</dt><dd>{num(cell[2])}</dd></div>
            <div><dt>Balance</dt><dd>{balanceWord(imbalance(cell[1], cell[2]))}</dd></div>
          </dl>
          <p className="muted small">Forecast and average ETA for this hex arrive with M3.</p>
        </>
      ) : (
        <>
          <div className="radio__head">
            <span className="eyebrow">Hottest hexes · {layer}</span>
          </div>
          <table className="toptable">
            <thead><tr><th>Hex</th><th>Drivers</th><th>Req · 5m</th></tr></thead>
            <tbody>
              {top.map((c) => (
                <tr key={c[0]} onClick={() => onPick(c)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && onPick(c)}>
                  <td><code>{c[0].slice(0, 9)}…</code></td><td>{num(c[1])}</td><td>{num(c[2])}</td>
                </tr>
              ))}
              {top.length === 0 && <tr><td colSpan={3} className="muted">No data yet</td></tr>}
            </tbody>
          </table>
          <p className="muted small">Click a hex on the map, or a row, to inspect it.</p>
        </>
      )}
    </section>
  );
}

function balanceWord(v: number) {
  if (v > 0.5) return "Short of drivers";
  if (v > 0.1) return "Leaning short";
  if (v >= -0.1) return "Balanced";
  if (v >= -0.5) return "Leaning surplus";
  return "Driver surplus";
}

/* ---------------------------------------------------------------- signal lost */
export function RadioSilence({ url }: { url: string }) {
  return (
    <div className="silence" role="status">
      <div className="silence__card">
        <Hexi pose="sleep" size={150} />
        <h2>Radio silence</h2>
        <p>Hexi is listening for the gateway on <code>{url}</code>.</p>
        <p className="muted">Start the stack with <code>make stack</code> and this map wakes up on its own.</p>
      </div>
    </div>
  );
}
