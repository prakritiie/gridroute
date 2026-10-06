"use client";

import { useCallback, useEffect, useState } from "react";
import { DEMO } from "./playback";
import { GATEWAY_HTTP } from "./useOps";

/** eval/report.py run_metrics(), as stored by workers/runner.py */
export interface RunMetrics {
  requests?: number;
  picked_up?: number;
  pickup_mean_s?: number | null;
  pickup_p90_s?: number | null;
  wait_mean_s?: number | null;
  unmatched_pct?: number | null;
  empty_km_per_driver_h?: number | null;
  repos_km_per_driver_h?: number | null;
  decision_p99_ms?: number | null;
  eta_mae_s?: number | null;
  error?: string;
}

/** GET /runs item (gateway/app.py _run_view) */
export interface Run {
  id: string;
  status: "running" | "done" | "cancelled" | "failed" | null;
  source: "protocol" | "lab" | null;
  label: string;
  policy: string | null;
  day: string | null;
  start: string | null;
  hours: number | null;
  speed: number | null;
  seed: number | null;
  reposition: boolean;
  progress: number | null;
  sim_time: string | null;
  requested: number | null;
  requests: number | null;
  metrics: RunMetrics | null;
}

export interface RunRequest {
  day: string;
  start: string;
  hours: number;
  speed: 10 | 20;
  matching: "greedy" | "batch";
  cost: "distance" | "eta";
  reposition: boolean;
  seed: number;
  label: string;
}

export interface Pending {
  request_id: string;
  status: "queued" | "running";
  request: RunRequest & { req: string };
  error: string | null;
  run: Run | null;
}

/** The report's row names, so a lab run reads like the results table. */
const ROWS: Record<string, string> = {
  "greedy-distance": "R0", "greedy-eta": "R1", "batch-distance": "R2", "batch-eta": "R3", "batch-eta-repos": "R4",
};

export function policyParts(policy: string | null): { row: string | null; text: string } {
  if (!policy) return { row: null, text: "Unknown policy" };
  const [matching, cost, repos] = policy.split("-");
  const text = [matching === "batch" ? "Batch" : "Greedy", cost === "eta" ? "ETA model" : "Distance",
    repos ? "Repositioning" : null].filter(Boolean).join(" · ");
  return { row: ROWS[policy] ?? null, text };
}

export function dayLabel(day: string | null, start: string | null): string {
  if (!day) return "—";
  const d = new Date(`${day}T12:00:00`);
  const wd = d.toLocaleDateString("en-US", { weekday: "short" });
  return `${wd} ${d.getDate()} Jun${start ? ` · ${start}` : ""}`;
}

/** Wall-clock minutes a replay takes: the window plus 20 sim-minutes of drain, compressed by speed. */
export const wallMinutes = (hours: number, speed: number) => Math.ceil((hours * 60 + 20) / speed);

/** Polls GET /runs: every 2 s while something is queued or running, every 15 s otherwise. */
export function useRuns(enabled: boolean) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [pending, setPending] = useState<Pending[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      let busy = false;
      try {
        const r = await fetch(DEMO ? "demo/runs.json" : `${GATEWAY_HTTP}/runs?limit=200`);
        if (!r.ok) throw new Error(`Gateway error ${r.status}`);
        const data = (await r.json()) as { runs: Run[]; pending: Pending[] };
        if (!alive) return;
        setRuns(data.runs);
        setPending(data.pending);
        setError(null);
        busy = data.pending.length > 0;
      } catch {
        if (alive) setError("Gateway unreachable");
      }
      if (alive && !DEMO) timer = setTimeout(load, busy ? 2000 : 15000);
    };
    load();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [enabled, tick]);

  const start = useCallback(async (req: RunRequest): Promise<string | null> => {
    if (DEMO) return "This is the static demo: experiments need the full stack (clone the repo, docker compose up).";
    const r = await fetch(`${GATEWAY_HTTP}/runs`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(req),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => null);
      const detail = body?.detail;
      return typeof detail === "string" ? detail
        : Array.isArray(detail) ? detail.map((d: { msg: string }) => d.msg.replace(/^Value error, /, "")).join("; ")
          : `Gateway error ${r.status}`;
    }
    refresh();
    return null;
  }, [refresh]);

  const cancel = useCallback(async (id: string) => {
    await fetch(`${GATEWAY_HTTP}/runs/${id}/cancel`, { method: "POST" });
    refresh();
  }, [refresh]);

  return { runs, pending, error, start, cancel, refresh };
}

/** Rows of the comparison table. better: which direction is an improvement. */
export interface MetricDef {
  key: keyof RunMetrics;
  label: string;
  fmt: "dur" | "pct" | "km" | "ms" | "int";
  better: "lower" | "higher";
}

export const METRICS: MetricDef[] = [
  { key: "pickup_mean_s", label: "Pickup time, mean", fmt: "dur", better: "lower" },
  { key: "pickup_p90_s", label: "Pickup time, p90", fmt: "dur", better: "lower" },
  { key: "wait_mean_s", label: "Rider wait, mean", fmt: "dur", better: "lower" },
  { key: "unmatched_pct", label: "Riders unserved", fmt: "pct", better: "lower" },
  { key: "empty_km_per_driver_h", label: "Empty km / driver-h", fmt: "km", better: "lower" },
  { key: "repos_km_per_driver_h", label: "of which repositioning", fmt: "km", better: "lower" },
  { key: "decision_p99_ms", label: "Decision time, p99", fmt: "ms", better: "lower" },
  { key: "eta_mae_s", label: "Pickup-estimate error", fmt: "dur", better: "lower" },
];

export function fmtMetric(v: number | null | undefined, fmt: MetricDef["fmt"]): string {
  if (v == null || Number.isNaN(v)) return "—";
  const a = Math.abs(v);
  switch (fmt) {
    case "dur": return `${v < 0 ? "−" : ""}${Math.floor(a / 60)}:${String(Math.round(a % 60)).padStart(2, "0")}`;
    case "pct": return `${v.toFixed(1)}%`;
    case "km": return v.toFixed(2);
    case "ms": return `${Math.round(v)} ms`;
    case "int": return Math.round(v).toLocaleString("en-US");
  }
}

/** CSV of the compared runs (one row per run, one column per metric). */
export function toCsv(runs: Run[]): string {
  const head = ["run_id", "label", "policy", "day", "start", "hours", "seed", ...METRICS.map((m) => m.key)];
  const rows = runs.map((r) => [r.id, r.label, r.policy, r.day, r.start, r.hours, r.seed,
    ...METRICS.map((m) => r.metrics?.[m.key] ?? "")]);
  return [head, ...rows].map((row) => row.map((c) => `"${String(c ?? "").replace(/"/g, '""')}"`).join(",")).join("\n");
}
