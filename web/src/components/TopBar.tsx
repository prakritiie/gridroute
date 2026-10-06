"use client";

import { clock } from "@/lib/format";
import type { Layer } from "@/lib/palette";
import type { Link, Resolution } from "@/lib/types";

const LAYERS: { id: Layer; label: string }[] = [
  { id: "supply", label: "Supply" },
  { id: "demand", label: "Demand" },
  { id: "imbalance", label: "Imbalance" },
  { id: "forecast", label: "Forecast" },
];

const RES_HINT: Record<Resolution, string> = { 9: "0.1 km²", 8: "0.7 km²", 7: "5.2 km²" };

interface Props {
  link: Link;
  t: number | null;
  layer: Layer;
  onLayer: (l: Layer) => void;
  res: Resolution;
  onRes: (r: Resolution) => void;
}

export function TopBar({ link, t, layer, onLayer, res, onRes }: Props) {
  const shown: Resolution = layer === "forecast" ? 7 : res;
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand__mark" aria-hidden />
        <span className="brand__name">GridRoute</span>
        <span className="brand__sep" aria-hidden />
        <span className="brand__product">Dispatch</span>
        <span className="chip">New York</span>
      </div>

      <nav className="tabs" role="tablist" aria-label="Map layer">
        {LAYERS.map((l) => (
          <button
            key={l.id}
            role="tab"
            aria-selected={layer === l.id}
            className={layer === l.id ? "is-active" : ""}
            onClick={() => onLayer(l.id)}
          >
            {l.label}
          </button>
        ))}
      </nav>

      <div className="topbar__right">
        <div className="res" role="radiogroup" aria-label="Hexagon size">
          <span className="res__label">Hex size</span>
          {([9, 8, 7] as Resolution[]).map((r) => (
            <button key={r} role="radio" aria-checked={shown === r} className={shown === r ? "is-active" : ""}
              disabled={layer === "forecast" && r !== 7}
              onClick={() => onRes(r)}
              title={layer === "forecast" ? "The forecast is per 5.2 km² hex (H3 resolution 7)"
                : `H3 resolution ${r} · ${RES_HINT[r]} per cell`}>
              {RES_HINT[r]}
            </button>
          ))}
        </div>
        <div className={`live live--${link}`} aria-live="polite">
          <span className="live__dot" aria-hidden />
          {link === "live" ? "Live" : link === "connecting" ? "Connecting" : "Reconnecting"}
          <span className="live__clock">{t ? clock(t) : "--:--:--"}</span>
        </div>
      </div>
    </header>
  );
}
