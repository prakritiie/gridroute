"use client";

import { useState } from "react";
import { clock } from "@/lib/format";

interface Props {
  values: (number | null)[];
  times: number[];
  color: string;
  label: string;
  format: (v: number) => string;
}

const W = 160;
const H = 36;

/** Single-series sparkline: 2px line, area wash, crosshair + readout on hover. One series, so no legend. */
export function Sparkline({ values, times, color, label, format }: Props) {
  const [hover, setHover] = useState<number | null>(null);
  const pts = values.map((v, i) => [i, v] as const).filter((p): p is readonly [number, number] => p[1] != null);
  if (pts.length < 2) return <div className="spark spark--empty" aria-hidden />;

  // A sparkline shows trend, not magnitude: scale to the data's own range (the tile's big number gives
  // the level), with a minimum span so noise on a steady series doesn't look like a cliff.
  const n = values.length;
  const vs = pts.map((p) => p[1]);
  const mid = (Math.min(...vs) + Math.max(...vs)) / 2;
  const span = Math.max(Math.max(...vs) - Math.min(...vs), Math.abs(mid) * 0.04, 1e-6);
  const lo = mid - span / 2;
  const x = (i: number) => (i / (n - 1)) * W;
  const y = (v: number) => H - 4 - ((v - lo) / span) * (H - 10);
  const line = pts.map(([i, v], k) => `${k ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const area = `${line}L${x(pts[pts.length - 1][0])},${H}L${x(pts[0][0])},${H}Z`;
  const hv = hover != null ? values[hover] : null;

  return (
    <div
      className="spark"
      onPointerMove={(e) => {
        const r = e.currentTarget.getBoundingClientRect();
        setHover(Math.max(0, Math.min(n - 1, Math.round(((e.clientX - r.left) / r.width) * (n - 1)))));
      }}
      onPointerLeave={() => setHover(null)}
    >
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={`${label}, last ${Math.round(n / 60)} minutes`}>
        <path d={area} fill={color} opacity="0.14" />
        <path d={line} fill="none" stroke={color} strokeWidth="2" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
        {hover != null && hv != null && (
          <>
            <line x1={x(hover)} x2={x(hover)} y1="0" y2={H} stroke="var(--text-3)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
            <circle cx={x(hover)} cy={y(hv)} r="3" fill={color} stroke="var(--surface)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
          </>
        )}
      </svg>
      {hover != null && hv != null && (
        <div className="spark__tip" style={{ left: `${(hover / (n - 1)) * 100}%` }}>
          <b>{format(hv)}</b> <span>{clock(times[hover])}</span>
        </div>
      )}
    </div>
  );
}
