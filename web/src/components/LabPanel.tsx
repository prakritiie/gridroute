"use client";

import { Check, Loader2, Play, X } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { dayLabel, fmtMetric, policyParts, wallMinutes, type Pending, type Run, type RunRequest } from "@/lib/lab";

const DAYS = Array.from({ length: 30 }, (_, i) => `2016-06-${String(i + 1).padStart(2, "0")}`);
const STARTS = Array.from({ length: 38 }, (_, i) => {
  const m = 4 * 60 + i * 30;                                    // 04:00 … 22:30
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${m % 60 ? "30" : "00"}`;
});
export const MAX_COMPARE = 4;

function Seg<T extends string | number | boolean>({ value, options, onChange, label }: {
  value: T; options: { v: T; label: string }[]; onChange: (v: T) => void; label: string;
}) {
  return (
    <div className="seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={String(o.v)} type="button" role="radio" aria-checked={value === o.v}
          className={value === o.v ? "is-active" : ""} onClick={() => onChange(o.v)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <div className="field">
      <span className="field__label">{label}</span>
      {children}
      {hint && <span className="field__hint">{hint}</span>}
    </div>
  );
}

function NewRun({ onStart, busy }: { onStart: (r: RunRequest) => Promise<string | null>; busy: boolean }) {
  const [req, setReq] = useState<RunRequest>({
    day: "2016-06-07", start: "07:00", hours: 2, speed: 10, matching: "batch", cost: "eta",
    reposition: true, seed: 1, label: "",
  });
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const set = <K extends keyof RunRequest>(k: K, v: RunRequest[K]) => setReq((r) => ({ ...r, [k]: v }));
  const startOk = STARTS.filter((s) => Number(s.slice(0, 2)) + Number(s.slice(3)) / 60 + req.hours <= 24);

  const submit = async () => {
    setSending(true);
    setError(await onStart(req));
    setSending(false);
  };

  return (
    <section className="section">
      <div className="section__head">
        <h2>New experiment</h2>
        <span className="muted">Real June 2016 demand</span>
      </div>
      <div className="form">
        <div className="form__row">
          <Field label="Day">
            <select className="select" value={req.day} onChange={(e) => set("day", e.target.value)}>
              {DAYS.map((d) => <option key={d} value={d}>{dayLabel(d, null)}</option>)}
            </select>
          </Field>
          <Field label="Start (NYC)">
            <select className="select" value={req.start} onChange={(e) => set("start", e.target.value)}>
              {startOk.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </Field>
        </div>
        <Field label="Window">
          <Seg label="Window" value={req.hours} onChange={(v) => {
            set("hours", v);
            if (Number(req.start.slice(0, 2)) + v > 24) set("start", "07:00");
          }} options={[{ v: 1, label: "1 h" }, { v: 2, label: "2 h" }, { v: 3, label: "3 h" }]} />
        </Field>
        <Field label="Matching">
          <Seg label="Matching" value={req.matching} onChange={(v) => set("matching", v)}
            options={[{ v: "greedy", label: "Greedy" }, { v: "batch", label: "Batch" }]} />
        </Field>
        <Field label="Cost">
          <Seg label="Cost" value={req.cost} onChange={(v) => set("cost", v)}
            options={[{ v: "distance", label: "Distance" }, { v: "eta", label: "ETA model" }]} />
        </Field>
        <Field label="Repositioning">
          <Seg label="Repositioning" value={req.reposition} onChange={(v) => set("reposition", v)}
            options={[{ v: false, label: "Off" }, { v: true, label: "On" }]} />
        </Field>
        <div className="form__row">
          <Field label="Speed">
            <Seg label="Speed" value={req.speed} onChange={(v) => set("speed", v)}
              options={[{ v: 10, label: "×10" }, { v: 20, label: "×20" }]} />
          </Field>
          <Field label="Seed">
            <input className="select" type="number" min={1} max={99} value={req.seed}
              onChange={(e) => set("seed", Math.max(1, Math.min(99, Number(e.target.value) || 1)))} />
          </Field>
        </div>
        <Field label="Name (optional)">
          <input className="select" maxLength={40} placeholder="e.g. Evening peak, repositioning" value={req.label}
            onChange={(e) => set("label", e.target.value)} />
        </Field>
        <button className="btn btn--primary" type="button" onClick={submit} disabled={sending}>
          {sending ? <Loader2 size={16} className="spin" /> : <Play size={15} />}
          {busy ? "Add to queue" : "Run experiment"}
          <span className="btn__meta">~{wallMinutes(req.hours, req.speed)} min</span>
        </button>
        {error && <p className="form__error" role="alert">{error}</p>}
        <p className="footnote">
          20% of real requests, 1,600 drivers. The live demo pauses while an experiment runs; the map shows the
          replay instead.
        </p>
      </div>
    </section>
  );
}

function Queue({ pending, onCancel }: { pending: Pending[]; onCancel: (id: string) => void }) {
  if (!pending.length) return null;
  return (
    <section className="section">
      <div className="section__head"><h2>Running</h2><span className="muted">{pending.length} in queue</span></div>
      <ul className="queue">
        {pending.map((p) => {
          const run = p.run;
          const { row, text } = policyParts(`${p.request.matching}-${p.request.cost}${p.request.reposition ? "-repos" : ""}`);
          const progress = run?.progress ?? 0;
          return (
            <li key={p.request_id} className="queue__item">
              <div className="queue__head">
                <div>
                  <div className="queue__title">{p.request.label || text}</div>
                  <div className="muted">{row && <b className="rowtag">{row}</b>}{dayLabel(p.request.day, p.request.start)} · seed {p.request.seed}</div>
                </div>
                <button className="icon-btn" onClick={() => onCancel(p.request_id)} aria-label="Cancel experiment"><X size={16} /></button>
              </div>
              {p.status === "running" ? (
                <>
                  <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={100}
                    aria-valuenow={Math.round(progress * 100)}>
                    <span style={{ width: `${Math.max(2, progress * 100)}%` }} />
                  </div>
                  <div className="queue__meta">
                    <span>{run?.sim_time ? `Sim ${run.sim_time.slice(11, 16)}` : "Starting…"}</span>
                    <span>{run?.requested != null && run?.requests ? `${Math.round(run.requested).toLocaleString()} / ${Math.round(run.requests).toLocaleString()} requests` : ""}</span>
                  </div>
                </>
              ) : <div className="queue__meta"><span>Queued</span></div>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function RunRow({ run, selected, disabled, onToggle }: {
  run: Run; selected: boolean; disabled: boolean; onToggle: () => void;
}) {
  const { row, text } = policyParts(run.policy);
  const m = run.metrics;
  const measured = m && !m.error && m.pickup_mean_s != null;
  return (
    <li>
      <button className={`runrow${selected ? " is-selected" : ""}`} onClick={onToggle} disabled={disabled && !selected}
        aria-pressed={selected}>
        <span className="runrow__check" aria-hidden>{selected && <Check size={12} strokeWidth={3} />}</span>
        <span className="runrow__body">
          <span className="runrow__title">{row && <b className="rowtag">{row}</b>}{run.label || text}</span>
          <span className="runrow__sub">
            {run.label ? `${text} · ` : ""}{dayLabel(run.day, run.start)} · seed {run.seed ?? "–"}
            {run.status === "cancelled" && <em className="runrow__flag"> · cancelled</em>}
            {run.status === "failed" && <em className="runrow__flag runrow__flag--bad"> · failed</em>}
          </span>
        </span>
        <span className="runrow__nums">
          <span>{measured ? fmtMetric(m.pickup_mean_s, "dur") : "—"}</span>
          <span className="muted">{measured ? `${fmtMetric(m.unmatched_pct, "pct")} unserved` : "not measured"}</span>
        </span>
      </button>
    </li>
  );
}

export function LabPanel({ runs, pending, error, selected, onToggle, onStart, onCancel }: {
  runs: Run[]; pending: Pending[]; error: string | null; selected: string[];
  onToggle: (id: string) => void; onStart: (r: RunRequest) => Promise<string | null>; onCancel: (id: string) => void;
}) {
  const [lab, protocol] = useMemo(() => {
    const done = runs.filter((r) => r.status !== "running");
    return [done.filter((r) => r.source === "lab"), done.filter((r) => r.source !== "lab")];
  }, [runs]);
  const full = selected.length >= MAX_COMPARE;
  const list = (items: Run[]) => (
    <ul className="runs">
      {items.map((r) => <RunRow key={r.id} run={r} selected={selected.includes(r.id)} disabled={full}
        onToggle={() => onToggle(r.id)} />)}
    </ul>
  );
  return (
    <>
      <NewRun onStart={onStart} busy={pending.length > 0} />
      <Queue pending={pending} onCancel={onCancel} />
      <section className="section">
        <div className="section__head">
          <h2>Runs</h2>
          <span className="muted">{selected.length ? `${selected.length} of ${MAX_COMPARE} selected` : "Select to compare"}</span>
        </div>
        {error && <p className="form__error">{error}</p>}
        {lab.length > 0 && <><h3 className="runs__group">Your experiments</h3>{list(lab)}</>}
        <h3 className="runs__group">Results table · Tue 7 Jun, 07:00–09:00</h3>
        {protocol.length ? list(protocol) : <p className="muted">No recorded runs yet.</p>}
      </section>
    </>
  );
}
