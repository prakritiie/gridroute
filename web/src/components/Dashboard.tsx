"use client";

import dynamic from "next/dynamic";
import { useMemo, useState } from "react";

import { BrandPlate, Inspector, KeyRule, KpiStrip, LayerControl, RadioLog, RadioSilence } from "./Panels";
import type { HoverInfo } from "./HexMap";
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

  // Legend domain from the current frame, but only re-chosen when the layer/res changes or it drifts
  // out of range — so colours stay comparable from frame to frame.
  const [max, setMax] = useState({ key: "", value: 5 });
  const key = `${layer}:${res}`;
  const wanted = layer === "imbalance" ? 1 : domainMax(cells.map((c) => (layer === "supply" ? c[1] : c[2])));
  if (max.key !== key || wanted > max.value * 1.5 || wanted < max.value / 3) {
    if (cells.length) setMax({ key, value: wanted });
  }

  const showSilence = ops.link !== "live" && !ops.hexes;

  return (
    <main className="console">
      <div className="map-wrap">
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
          <div className="maptip" style={{ left: hover.x, top: hover.y }}>
            <div><b>{num(hovered[1])}</b> drivers</div>
            <div><b>{num(hovered[2])}</b> requests · 5 min</div>
            <div className="muted">{balance(hovered)}</div>
          </div>
        )}
        <div className="vignette" aria-hidden />
      </div>

      <div className="rail rail--left">
        <BrandPlate link={ops.link} t={ops.hexes?.t ?? null} />
        <KeyRule kpis={ops.kpis} />
        <Inspector
          cell={selected}
          cells={cells}
          layer={layer}
          res={res}
          onPick={(c) => setSelectedId(c[0])}
          onClose={() => setSelectedId(null)}
        />
      </div>

      <div className="rail rail--right">
        <LayerControl layer={layer} setLayer={setLayer} res={res} setRes={(r) => { setRes(r); setSelectedId(null); setHover(null); }}
          max={max.value} windowS={ops.hexes?.window_s ?? null} />
        <RadioLog matches={ops.matches} />
      </div>

      <KpiStrip kpis={ops.kpis} history={ops.history} />

      {showSilence && <RadioSilence url={GATEWAY_WS} />}
    </main>
  );
}

function balance(c: Cell) {
  const v = imbalance(c[1], c[2]);
  return v > 0.1 ? "more riders than drivers" : v < -0.1 ? "more drivers than riders" : "balanced";
}
