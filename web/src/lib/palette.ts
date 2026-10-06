// Map encodings. Every ramp below was run through the dataviz validator (--ordinal, dark, surface #10141a):
// one hue, monotone lightness, visible step gaps, darkest step >= 2:1 against the map surface.
// Supply = blue and demand = orange everywhere (map, legend, sparklines): colour follows the entity.

export type Layer = "supply" | "demand" | "imbalance" | "forecast";
export type RGB = [number, number, number];

const hex = (h: string): RGB => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)) as RGB;

export const SUPPLY_RAMP = ["#184f95", "#256abf", "#3987e5", "#6da7ec", "#b7d3f6"].map(hex);
export const DEMAND_RAMP = ["#8a3412", "#b8481a", "#e0602a", "#f2915f", "#f9c8a8"].map(hex);
// Diverging: more drivers than riders (cool) <- neutral gray -> more riders than drivers (warm).
// On a dark map "stronger" means lighter, so each arm brightens away from the neutral midpoint.
const BLUE_ARM = ["#86b6ef", "#3987e5", "#2a6fc4", "#1c4c86"].map(hex);   // index 0 = only drivers
const RED_ARM = ["#873335", "#b84647", "#e66767", "#f2a5a5"].map(hex);    // index 8 = only riders
export const NEUTRAL: RGB = hex("#383835");
export const IMBALANCE_RAMP: RGB[] = [...BLUE_ARM, NEUTRAL, ...RED_ARM];
export const IMBALANCE_CUTS = [-0.75, -0.5, -0.3, -0.1, 0.1, 0.3, 0.5, 0.75];   // 8 cuts -> 9 symmetric bins

export const SERIES = { supply: "#3987e5", demand: "#e0602a" } as const;

/** Round up to 1, 2 or 5 x 10^n so legend ticks are readable and don't jitter frame to frame. */
export function niceCeil(v: number): number {
  if (v <= 5) return 5;
  const p = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 5, 10].map((m) => m * p).find((x) => x >= v)!;
}

/** Upper bound of a sequential legend: the 95th percentile, rounded to a nice number. */
export function domainMax(values: number[]): number {
  const v = values.filter((x) => x > 0).sort((a, b) => a - b);
  if (!v.length) return 5;
  return niceCeil(v[Math.min(v.length - 1, Math.floor(v.length * 0.95))]);
}

export function seqColor(ramp: RGB[], value: number, max: number): RGB {
  const i = Math.min(ramp.length - 1, Math.floor((value / max) * ramp.length));
  return ramp[Math.max(0, i)];
}

/** -1 = only drivers, +1 = only riders, 0 = balanced. Unitless, so it works at every resolution. */
export function imbalance(supply: number, demand: number): number {
  return supply + demand === 0 ? 0 : (demand - supply) / (demand + supply);
}

export function divColor(v: number): RGB {
  return IMBALANCE_RAMP[IMBALANCE_CUTS.filter((c) => v > c).length];
}

export const rgbCss = ([r, g, b]: RGB) => `rgb(${r} ${g} ${b})`;
