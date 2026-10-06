"use client";

import { cellToParent } from "h3-js";
import { useEffect, useMemo, useRef, useState } from "react";

import type { Cell, Forecast, HexSnapshot, Kpis, Match, Reposition } from "./types";
import type { OpsState } from "./useOps";

/** Static demo build (Vercel): the dashboard plays a recorded replay instead of a live socket. */
export const DEMO = process.env.NEXT_PUBLIC_DEMO === "1";

const MATCH_LOG = 40;

/** tools/export_demo.py index.json */
export interface Scenario {
  key: string;
  label: string;
  file: string;
  day: string;
  start: string;
  hours: number;
  sample: number;
  drivers: number;
  seed: number;
  policy: string;
  metrics: Record<string, number | null>;
}

/** sim/playback.py */
interface Recording {
  meta: { day: string; start: string; window_s: number };
  frame_sim_s: number;
  cells: string[];
  frames: { s: number; c: number[]; k: Omit<Kpis, "t" | "audited_at"> }[];
  matches: [number, number, number, number, string, string][];
  forecasts: ({ s: number } & Omit<Forecast, "slot">)[];
  repos: { s: number; flows: [string, string][] }[];
}

export interface PlaybackControls {
  scenarios: Scenario[];
  scenario: string;
  setScenario: (k: string) => void;
  playing: boolean;
  setPlaying: (p: boolean) => void;
  speed: number;
  setSpeed: (s: number) => void;
  /** sim seconds since the window start */
  pos: number;
  seek: (s: number) => void;
  duration: number;
  /** "07:42" NYC */
  simClock: string;
  loading: boolean;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** Wall-clock ms the dashboard formats as the sim's NYC time (it renders times in the browser's zone). */
function simMs(rec: Recording, s: number): number {
  const [h, m] = rec.meta.start.split(":").map(Number);
  const [y, mo, d] = rec.meta.day.split("-").map(Number);
  return new Date(y, mo - 1, d, h, m, 0).getTime() + s * 1000;
}

function lastAtOrBefore<T extends { s: number }>(xs: T[], s: number): T | undefined {
  let lo = 0, hi = xs.length - 1, best: T | undefined;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (xs[mid].s <= s) { best = xs[mid]; lo = mid + 1; } else hi = mid - 1;
  }
  return best;
}

export function usePlayback(): OpsState & { playback: PlaybackControls } {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [scenario, setScenario] = useState("on");
  const [loaded, setLoaded] = useState<{ key: string; rec: Recording | null } | null>(null);
  const rec = loaded?.rec ?? null;
  const loading = loaded?.key !== scenario;
  const [playing, setPlaying] = useState(true);
  const [speed, setSpeed] = useState(60);
  const [pos, setPos] = useState(15 * 60);                 // start after the cold-start quarter hour
  const [matches, setMatches] = useState<Match[]>([]);
  const lastPos = useRef(pos);

  useEffect(() => {
    fetch("demo/index.json").then((r) => r.json()).then((d) => setScenarios(d.scenarios)).catch(() => setScenarios([]));
  }, []);

  useEffect(() => {
    let alive = true;
    fetch(`demo/${scenario}.json`).then((r) => r.json())
      .then((d: Recording) => alive && setLoaded({ key: scenario, rec: d }))
      .catch(() => alive && setLoaded({ key: scenario, rec: null }));
    return () => { alive = false; };
  }, [scenario]);

  // the clock: 10 Hz is plenty (frames are 30 sim-s apart) and keeps re-renders cheap; loops at the end
  useEffect(() => {
    if (!playing || !rec) return;
    let t0 = performance.now();
    const timer = setInterval(() => {
      const t = performance.now();
      const dt = (t - t0) / 1000;
      t0 = t;
      setPos((p) => (p + dt * speed >= rec.meta.window_s ? 15 * 60 : p + dt * speed));
    }, 100);
    return () => clearInterval(timer);
  }, [playing, speed, rec]);

  // matches between the previous and the current position become live pulses / feed rows
  useEffect(() => {
    if (!rec) return;
    const prev = lastPos.current;
    lastPos.current = pos;
    if (pos < prev || pos - prev > 120) {                    // a seek or a loop: start the feed over
      setMatches([]);
      return;
    }
    const now = Date.now();
    const fresh = rec.matches.filter((m) => m[0] > prev && m[0] <= pos);
    if (!fresh.length) return;
    const add: Match[] = fresh.slice(-MATCH_LOG).reverse().map(([s, lat, lng, eta, driver, ride]) => ({
      driver_id: driver, ride_id: ride, pickup: [lat, lng], eta_s: eta, at: now, sim_at: simMs(rec, s),
    }));
    setMatches((ms) => [...add, ...ms].slice(0, MATCH_LOG));
  }, [pos, rec]);

  // per-cell parents, computed once per recording
  const parents = useMemo(() => rec?.cells.map((c) => [cellToParent(c, 8), cellToParent(c, 7)] as const) ?? [], [rec]);

  const frameIdx = rec ? Math.max(0, Math.min(rec.frames.length - 1, Math.floor(pos / rec.frame_sim_s))) : -1;

  const hexes: HexSnapshot | null = useMemo(() => {
    if (!rec || frameIdx < 0) return null;
    const f = rec.frames[frameIdx];
    const r9: Cell[] = [];
    const r8 = new Map<string, [number, number]>();
    const r7 = new Map<string, [number, number]>();
    for (let i = 0; i < f.c.length; i += 3) {
      const [ci, s, d] = [f.c[i], f.c[i + 1], f.c[i + 2]];
      r9.push([rec.cells[ci], s, d]);
      for (const [m, p] of [[r8, parents[ci][0]], [r7, parents[ci][1]]] as const) {
        const v = m.get(p) ?? [0, 0];
        m.set(p, [v[0] + s, v[1] + d]);
      }
    }
    const toCells = (m: Map<string, [number, number]>) => [...m].map(([c, [s, d]]) => [c, s, d] as Cell);
    return { t: simMs(rec, f.s), window_s: 300, res: { "9": r9, "8": toCells(r8), "7": toCells(r7) } };
  }, [rec, frameIdx, parents]);

  const history: Kpis[] = useMemo(() => {
    if (!rec || frameIdx < 0) return [];
    // the last 10 sim-minutes of KPI frames, the sparklines' window
    const from = Math.max(0, frameIdx - Math.round(600 / rec.frame_sim_s));
    return rec.frames.slice(from, frameIdx + 1).map((f) => ({ ...f.k, t: simMs(rec, f.s), audited_at: simMs(rec, f.s) } as Kpis));
  }, [rec, frameIdx]);

  // stable objects between ticks, so map layers rebuild only when the forecast or the moves change
  const fEntry = rec ? lastAtOrBefore(rec.forecasts, pos) : undefined;
  const forecast: Forecast | null = useMemo(() => fEntry
    ? { slot: 0, slot_start: fEntry.slot_start, version: fEntry.version, scale: fEntry.scale, hexes: fEntry.hexes } : null,
  [fEntry]);
  const rEntry = rec ? lastAtOrBefore(rec.repos, pos) : undefined;
  const rFresh = rEntry !== undefined && pos - rEntry.s < 240;
  const reposition: Reposition | null = useMemo(() => rEntry && rFresh && rec
    ? { t: simMs(rec, rEntry.s), n: rEntry.flows.length, flows: rEntry.flows, at: simMs(rec, rEntry.s) } : null,
  [rEntry, rFresh, rec]);

  const simClock = rec ? (() => {
    const d = new Date(simMs(rec, pos));
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  })() : "--:--";

  return {
    link: "live", everConnected: true, hexes, kpis: history[history.length - 1] ?? null, history, matches,
    forecast, reposition, chaos: null,
    playback: {
      scenarios, scenario, setScenario: (k) => { setScenario(k); setMatches([]); }, playing, setPlaying, speed, setSpeed,
      pos, seek: (s) => { setPos(s); setMatches([]); }, duration: rec?.meta.window_s ?? 0, simClock, loading,
    },
  };
}
