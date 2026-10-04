"use client";

import dynamic from "next/dynamic";
import { useMemo, useState } from "react";

import type { HoverInfo } from "./HexMap";
import { Legend, Offline } from "./MapOverlays";
import { MatchFeed } from "./MatchFeed";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";
import { num } from "@/lib/format";
import { domainMax, imbalance, type Layer } from "@/lib/palette";
import type { Cell, Resolution } from "@/lib/types";
import { GATEWAY_WS, useOps } from "@/lib/useOps";

// deck.gl + MapLibre need the browser (WebGL), so the map never renders on the server
const HexMap = dynamic(() => import("./HexMap"), { ssr: false, loading: () => <div className="map-loading" /> });

export default function Dashboard() {
  const ops = useOps();
  const [layer, setLayer] = useState<Layer>("supply");
  const [res, setRes] = useState<Resolution>(9);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hover, setHover] = useState<HoverInfo | null>(null);

  const cells: Cell[] = useMemo(() => ops.hexes?.res[String(res) as "9" | "8" | "7"] ?? [], [ops.hexes, res]);
  const selected = selectedId ? cells.find((c) => c[0] === selectedId) ?? [selectedId, 0, 0] as Cell : null;
  // Live values for the hovered hex; hidden once it's gone from the frame (e.g. after a resolution change)
  const hovered = hover ? cells.find((c) => c[0] === hover.id) : undefined;

  // Legend domain from the current frame, re-chosen only when the layer/res changes or values drift
  // out of range, so colours stay comparable from frame to frame.
  const [max, setMax] = useState({ key: "", value: 5 });
  const key = `${layer}:${res}`;
  const wanted = layer === "imbalance" ? 1 : domainMax(cells.map((c) => (layer === "supply" ? c[1] : c[2])));
  if (max.key !== key || wanted > max.value * 1.5 || wanted < max.value / 3) {
    if (cells.length) setMax({ key, value: wanted });
  }

  const changeRes = (r: Resolution) => {
    setRes(r);
    setSelectedId(null);
    setHover(null);
  };

  return (
    <div className="app">
      <TopBar link={ops.link} t={ops.hexes?.t ?? null} layer={layer} onLayer={setLayer} res={res} onRes={changeRes} />

      <Sidebar kpis={ops.kpis} history={ops.history} cell={selected} cells={cells} layer={layer} res={res}
        onPick={(c) => setSelectedId(c[0])} onClose={() => setSelectedId(null)} />

      <main className="map" aria-label="Live map">
        <HexMap
          cells={cells}
          layer={layer}
          max={max.value}
          matches={ops.matches}
          selected={selectedId}
          onSelect={(c) => setSelectedId(c?.[0] ?? null)}
          onHover={setHover}
        />
        {hover && hovered && (
          <div className="tooltip" style={{ left: hover.x, top: hover.y }}>
            <div className="tooltip__row"><span>Drivers</span><b>{num(hovered[1])}</b></div>
            <div className="tooltip__row"><span>Requests · 5 min</span><b>{num(hovered[2])}</b></div>
            <div className="tooltip__foot">{balance(hovered)}</div>
          </div>
        )}
        <div className="map__legend">
          <Legend layer={layer} max={max.value} windowS={ops.hexes?.window_s ?? null} />
        </div>
        {ops.link !== "live" && !ops.hexes && <Offline url={GATEWAY_WS} />}
      </main>

      <MatchFeed matches={ops.matches} kpis={ops.kpis} />
    </div>
  );
}

function balance(c: Cell) {
  const v = imbalance(c[1], c[2]);
  return v > 0.1 ? "More riders than drivers" : v < -0.1 ? "More drivers than riders" : "Balanced";
}
