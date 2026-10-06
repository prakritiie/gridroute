/** [h3 index, supply (fresh drivers), demand (requests in the window)] — see workers/aggregator.py */
export type Cell = [string, number, number];

export type Resolution = 9 | 8 | 7;

export interface HexSnapshot {
  t: number;
  window_s: number;
  res: Record<"9" | "8" | "7", Cell[]>;
}

export interface Kpis {
  t: number;
  drivers_available: number;
  drivers_busy: number;
  drivers_online: number;
  requests_per_min: number;
  matched_per_min: number;
  completed_per_min: number;
  unmatched_per_min: number;
  match_rate_5m: number | null;
  eta_mean_s: number | null;
  eta_p90_s: number | null;
  queue_rides: number;
  queue_pings: number;
  /** out-of-order pings ingest dropped since the stack started (chaos: reorder) */
  pings_stale_total?: number;
  double_bookings: number;
  invariant_violations: number;
  audited_at: number | null;
}

export interface Match {
  driver_id: string;
  ride_id: string;
  pickup: [number, number];
  dropoff?: [number, number];
  eta_s: number;
  /** client receive time, ms */
  at: number;
}

export type Link = "connecting" | "live" | "reconnecting";

/** A chaos command as the gateway stored it (core/chaos.py). area: res-7 cells, null = whole city. */
export interface ChaosCommand {
  type: "kill" | "burst" | "reorder" | "heal";
  area: string[] | null;
  pct: number;
  duration_s: number;
  t: number;
}

/** forecast:snapshot — workers/forecaster.py. hexes are full-city units; multiply by scale for this run. */
export interface Forecast {
  slot: number;
  slot_start: string;
  version: string;
  scale: number;
  hexes: Record<string, number>;
}

/** One repositioner cycle (gateway summary of ch:reposition): [from res-7, to res-7] per driver sent. */
export interface Reposition {
  t: number;
  n: number;
  flows: [string, string][];
  /** client receive time, ms */
  at: number;
}

export interface ExplainCandidate {
  driver: string;
  dist_m: number;
  cost: number;
  osrm_s?: number | null;
  osrm_m?: number | null;
  model_s?: number;
}

/** GET /rides/{id}/explain — written by the matcher for every match (workers/matcher.py). */
export interface Explain {
  ride: string;
  policy: string;
  t: number;
  chosen: string;
  cost_unit: "s" | "m";
  model: string | null;
  fallback: boolean;
  sim_time?: string;
  pickup: [number, number];
  candidates: ExplainCandidate[];
}
