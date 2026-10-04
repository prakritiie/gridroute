export const num = (v: number | null | undefined) => (v == null ? "—" : Math.round(v).toLocaleString("en-US"));

export const pct = (v: number | null | undefined) => (v == null ? "—" : `${(v * 100).toFixed(v >= 0.995 ? 0 : 1)}%`);

export function dur(s: number | null | undefined): string {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.round(s % 60)).padStart(2, "0")}`;
}

export const clock = (ms: number) =>
  new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

export function ago(ms: number | null | undefined, now: number): string {
  if (!ms) return "not yet";
  const s = Math.max(0, Math.round((now - ms) / 1000));
  return s < 60 ? `${s}s ago` : `${Math.floor(s / 60)}m ago`;
}

export const shortId = (id: string) => (id.length > 8 ? id.slice(0, 6) : id);
