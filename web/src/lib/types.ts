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
