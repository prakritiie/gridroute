"use client";

import { Activity, Loader2, Shuffle, ShieldCheck, Zap, ZapOff } from "lucide-react";
import { useEffect, useState } from "react";

import { num } from "@/lib/format";
import type { ChaosCommand, Kpis } from "@/lib/types";
import { GATEWAY_HTTP } from "@/lib/useOps";

type Kind = "kill" | "burst" | "reorder";

const ACTIONS: { kind: Kind; title: string; body: string; icon: typeof Zap }[] = [
  { kind: "kill", title: "Silence drivers", body: "They stop pinging and accepting. Search must drop them within 30 s.", icon: ZapOff },
  { kind: "burst", title: "Demand burst ×10", body: "Ten times the requests, from real trips starting in the area.", icon: Zap },
  { kind: "reorder", title: "Reorder pings", body: "Old pings arrive after new ones. None may move a driver back.", icon: Shuffle },
];

const KIND_LABEL: Record<string, string> = {
  kill: "Silenced drivers", burst: "Demand burst", reorder: "Reordered pings", heal: "Healed",
};

function Seg<T extends number>({ value, options, onChange, label }: {
  value: T; options: { v: T; label: string }[]; onChange: (v: T) => void; label: string;
}) {
  return (
    <div className="seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o.v} type="button" role="radio" aria-checked={value === o.v}
          className={value === o.v ? "is-active" : ""} onClick={() => onChange(o.v)}>{o.label}</button>
      ))}
    </div>
  );
}

function useNow(every = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), every);
    return () => clearInterval(t);
  }, [every]);
  return now;
}

/** KPIs at (or just before) a moment, from the 1 Hz history the dashboard keeps. */
function at(history: Kpis[], t: number): Kpis | undefined {
  let best: Kpis | undefined;
  for (const k of history) if (k.t <= t) best = k;
  return best ?? history[0];
}

function Recovery({ last, history, kpis }: { last: ChaosCommand; history: Kpis[]; kpis: Kpis | null }) {
  const now = useNow();
  const before = at(history, last.t - 1000);
  const elapsed = Math.max(0, Math.round((now - last.t) / 1000));
  const ends = last.type === "heal" ? 0 : Math.max(0, Math.round((last.t + last.duration_s * 1000 - now) / 1000));
  // Silenced drivers: settling time, i.e. when 90% of the fall in available drivers had happened.
  // (Matches move the count too, so "first reached the minimum" is noisy; t90 is the standard read.)
  let leftAfter: number | null = null;
  if (last.type === "kill" && before) {
    const after = history.filter((k) => k.t >= last.t);
    if (after.length > 5) {
      const low = Math.min(...after.map((k) => k.drivers_available));
      const drop = before.drivers_available - low;
      const reached = drop > 0 ? after.find((k) => before.drivers_available - k.drivers_available >= 0.9 * drop) : undefined;
      if (reached && now - reached.t > 5000) leftAfter = Math.round((reached.t - last.t) / 1000);
    }
  }
  const stale = kpis && before ? (kpis.pings_stale_total ?? 0) - (before.pings_stale_total ?? 0) : null;
  const rows: [string, string][] = [
    ["Elapsed", `${elapsed}s${ends ? ` · ends in ${ends}s` : last.type === "heal" ? "" : " · ended"}`],
    ["Available drivers", before && kpis ? `${num(before.drivers_available)} → ${num(kpis.drivers_available)}` : "—"],
  ];
  if (last.type === "kill") rows.push(["90% out of search after", leftAfter != null ? `${leftAfter}s` : elapsed < 40 ? "watching…" : "—"]);
  if (last.type === "burst") rows.push(["Requests / min", before && kpis ? `${num(before.requests_per_min)} → ${num(kpis.requests_per_min)}` : "—"],
    ["Ride queue", kpis ? num(kpis.queue_rides) : "—"]);
  if (last.type === "reorder") rows.push(["Out-of-order pings dropped", stale != null ? `+${num(stale)}` : "—"]);
  rows.push(["Ping queue", kpis ? num(kpis.queue_pings) : "—"]);
  const doubles = kpis?.double_bookings ?? 0;
  return (
    <div className="recovery">
      <div className="recovery__head">
        <span className="recovery__kind"><Activity size={14} />{KIND_LABEL[last.type]}{last.area ? " · area" : " · whole city"}</span>
        <span className={`badge ${doubles ? "badge--bad" : "badge--good"}`}><ShieldCheck size={13} />{doubles ? `${doubles} double-booked` : "No double-bookings"}</span>
      </div>
      <dl className="kv">
        {rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
      </dl>
    </div>
  );
}

export function ChaosPanel({ selected, last, history, kpis }: {
  selected: string | null; last: ChaosCommand | null; history: Kpis[]; kpis: Kpis | null;
}) {
  const [pct, setPct] = useState(0.3);
  const [duration, setDuration] = useState(120);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const send = async (type: Kind | "heal") => {
    setBusy(type);
    setError(null);
    try {
      const r = await fetch(`${GATEWAY_HTTP}/chaos`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ type, hex: type === "heal" ? null : selected, pct, duration_s: duration }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => null);
        setError(typeof body?.detail === "string" ? body.detail : `Gateway error ${r.status}`);
      }
    } catch {
      setError("Gateway unreachable");
    }
    setBusy(null);
  };

  return (
    <section className="section">
      <div className="section__head">
        <h2>Chaos</h2>
        <span className="muted">Live demo only</span>
      </div>
      <div className="form">
        <div className="field">
          <span className="field__label">Target</span>
          <div className="target">
            {selected ? <><b>Selected hex + neighbours</b><span className="muted">about 36 km² around it</span></>
              : <><b>Whole city</b><span className="muted">Click a hex on the map to target an area</span></>}
          </div>
        </div>
        <div className="form__row">
          <div className="field">
            <span className="field__label">Drivers hit</span>
            <Seg label="Drivers hit" value={pct} onChange={setPct}
              options={[{ v: 0.1, label: "10%" }, { v: 0.3, label: "30%" }, { v: 0.6, label: "60%" }]} />
          </div>
          <div className="field">
            <span className="field__label">For</span>
            <Seg label="Duration" value={duration} onChange={setDuration}
              options={[{ v: 30, label: "30s" }, { v: 120, label: "2m" }, { v: 300, label: "5m" }]} />
          </div>
        </div>
        <div className="actions">
          {ACTIONS.map(({ kind, title, body, icon: Icon }) => (
            <button key={kind} className="action" onClick={() => send(kind)} disabled={busy !== null}>
              <span className="action__icon">{busy === kind ? <Loader2 size={16} className="spin" /> : <Icon size={16} />}</span>
              <span className="action__text"><b>{title}</b><span>{body}</span></span>
            </button>
          ))}
        </div>
        <button className="btn btn--ghost" onClick={() => send("heal")} disabled={busy !== null}>
          <ShieldCheck size={14} />Heal everything now
        </button>
        {error && <p className="form__error" role="alert">{error}</p>}
        {last && <Recovery last={last} history={history} kpis={kpis} />}
      </div>
    </section>
  );
}
