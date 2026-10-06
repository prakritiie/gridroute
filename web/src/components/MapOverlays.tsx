"use client";

import { WifiOff } from "lucide-react";

import { num } from "@/lib/format";
import { DEMAND_RAMP, IMBALANCE_RAMP, SUPPLY_RAMP, rgbCss, type Layer } from "@/lib/palette";

export function Legend({ layer, max, windowS, slot }: {
  layer: Layer; max: number; windowS: number | null; slot?: string | null;
}) {
  if (layer === "imbalance") {
    return (
      <figure className="legend">
        <figcaption>Riders vs. drivers</figcaption>
        <div className="legend__bar">
          {IMBALANCE_RAMP.map((c, i) => <span key={i} style={{ background: rgbCss(c) }} />)}
        </div>
        <div className="legend__ends"><span>More drivers</span><span>Balanced</span><span>More riders</span></div>
      </figure>
    );
  }
  const ramp = layer === "supply" ? SUPPLY_RAMP : DEMAND_RAMP;
  const step = max / ramp.length;
  return (
    <figure className="legend">
      <figcaption>
        {layer === "supply" ? "Available drivers per hex"
          : layer === "forecast" ? `Expected requests per hex · ${slot ?? "this 15 min"}`
            : `Requests per hex · last ${Math.round((windowS ?? 300) / 60)} min`}
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

export function Offline({ url }: { url: string }) {
  return (
    <div className="offline" role="status">
      <div className="offline__card">
        <span className="offline__icon"><WifiOff size={20} strokeWidth={2} /></span>
        <h2>Waiting for the gateway</h2>
        <p className="muted">No live feed from <code>{url}</code>.</p>
        <p className="muted">Start the stack with <code>docker compose up -d</code>. This view reconnects automatically.</p>
        <span className="offline__retry"><span className="spinner" aria-hidden /> Retrying</span>
      </div>
    </div>
  );
}
