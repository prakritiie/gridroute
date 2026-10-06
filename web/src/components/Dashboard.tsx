"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";

import type { HoverInfo } from "./HexMap";
import { Compare } from "./Compare";
import { ExplainPanel } from "./ExplainPanel";
import { LabPanel, MAX_COMPARE } from "./LabPanel";
import { Legend, Offline } from "./MapOverlays";
import { MatchFeed } from "./MatchFeed";
import { Sidebar } from "./Sidebar";
import { TopBar, type View } from "./TopBar";
import { useRuns } from "@/lib/lab";
import { num } from "@/lib/format";
import { domainMax, imbalance, type Layer } from "@/lib/palette";
import type { Cell, Explain, Resolution } from "@/lib/types";
import { GATEWAY_HTTP, GATEWAY_WS, useOps } from "@/lib/useOps";

// deck.gl + MapLibre need the browser (WebGL), so the map never renders on the server
const HexMap = dynamic(() => import("./HexMap"), { ssr: false, loading: () => <div className="map-loading" /> });

export default function Dashboard() {
  const ops = useOps();
  const [view, setView] = useState<View>("live");
  const lab = useRuns(view === "lab");
  const [compare, setCompare] = useState<string[]>([]);
  const toggleCompare = (id: string) => setCompare((c) =>
    c.includes(id) ? c.filter((x) => x !== id) : c.length >= MAX_COMPARE ? c : [...c, id]);
  const compared = compare.map((id) => lab.runs.find((r) => r.id === id)).filter((r) => r !== undefined);
  const [layer, setLayer] = useState<Layer>("supply");
  const [res, setRes] = useState<Resolution>(9);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hover, setHover] = useState<HoverInfo | null>(null);
  const [explainId, setExplainId] = useState<string | null>(null);
  const [explain, setExplain] = useState<{ id: string; data: Explain | null; error: string | null } | null>(null);

  useEffect(() => {
    if (!explainId) return;
    let alive = true;
    fetch(`${GATEWAY_HTTP}/rides/${explainId}/explain`)
      .then(async (r) => (r.ok ? r.json() : Promise.reject(r.status === 404
        ? "No explanation stored for this ride (older than an hour?)." : `Gateway error ${r.status}.`)))
      .then((data: Explain) => alive && setExplain({ id: explainId, data, error: null }))
      .catch((e) => alive && setExplain({ id: explainId, data: null, error: typeof e === "string" ? e : "Gateway unreachable." }));
    return () => { alive = false; };
  }, [explainId]);
  const shown = explain && explain.id === explainId ? explain : null;

  // Forecast layer: res-7 hexes, [id, drivers now, requests expected this 15-min slot] in this run's units.
  const forecastCells: Cell[] = useMemo(() => {
    const f = ops.forecast;
    if (!f) return [];
    const supply = new Map((ops.hexes?.res["7"] ?? []).map((c) => [c[0], c[1]]));
    return Object.entries(f.hexes).map(([h, v]) => [h, supply.get(h) ?? 0, v * f.scale] as Cell);
  }, [ops.forecast, ops.hexes]);
  const forecastByHex = useMemo(() => new Map(forecastCells.map((c) => [c[0], c[2]])), [forecastCells]);
  const forecasting = layer === "forecast";
  const cells: Cell[] = useMemo(
    () => (forecasting ? forecastCells : ops.hexes?.res[String(res) as "9" | "8" | "7"] ?? []),
    [forecasting, forecastCells, ops.hexes, res]);
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
      <TopBar view={view} onView={setView} link={ops.link} t={ops.hexes?.t ?? null} layer={layer} res={res} onRes={changeRes}
        onLayer={(l) => { setLayer(l); setSelectedId(null); setHover(null); }} />

      {view === "lab" ? (
        <aside className="sidebar" aria-label="Policy lab">
          <LabPanel runs={lab.runs} pending={lab.pending} error={lab.error} selected={compare}
            onToggle={toggleCompare} onStart={lab.start} onCancel={lab.cancel} />
        </aside>
      ) : <Sidebar kpis={ops.kpis} history={ops.history} cell={selected} cells={cells} layer={layer}
        res={forecasting ? 7 : res} forecast={ops.forecast} forecastByHex={forecastByHex} reposition={ops.reposition}
        onPick={(c) => setSelectedId(c[0])} onClose={() => setSelectedId(null)}
        top={explainId ? <ExplainPanel explain={shown?.data ?? null} error={shown?.error ?? null}
          onClose={() => setExplainId(null)} /> : null} />}

      <main className="map" aria-label="Live map">
        <HexMap
          cells={cells}
          layer={layer}
          max={max.value}
          matches={ops.matches}
          selected={selectedId}
          onSelect={(c) => setSelectedId(c?.[0] ?? null)}
          onHover={setHover}
          focus={shown?.data?.pickup ?? null}
          reposition={ops.reposition}
        />
        {hover && hovered && (
          <div className="tooltip" style={{ left: hover.x, top: hover.y }}>
            <div className="tooltip__row"><span>Drivers</span><b>{num(hovered[1])}</b></div>
            {forecasting
              ? <div className="tooltip__row"><span>Expected · 15 min</span><b>{num(Math.round(hovered[2]))}</b></div>
              : <div className="tooltip__row"><span>Requests · 5 min</span><b>{num(hovered[2])}</b></div>}
            <div className="tooltip__foot">{forecasting ? shortfall(hovered) : balance(hovered)}</div>
          </div>
        )}
        <div className="map__legend">
          <Legend layer={layer} max={max.value} windowS={ops.hexes?.window_s ?? null}
            slot={ops.forecast ? slotLabel(ops.forecast.slot_start) : null} />
        </div>
        {forecasting && !ops.forecast && ops.hexes && (
          <div className="map__notice" role="status">
            No forecast yet. It appears once a demand model is published and the history is seeded
            (<code>python -m tools.seed_demand</code>), and during every replay.
          </div>
        )}
        {view === "lab" && lab.pending.some((p) => p.status === "running") && (
          <div className="map__notice map__notice--live" role="status">
            <span className="live__dot" aria-hidden />
            Replaying {lab.pending.find((p) => p.status === "running")?.run?.sim_time?.slice(11, 16) ?? ""} on
            {" "}{lab.pending.find((p) => p.status === "running")?.request.day.slice(8)} June 2016. The map shows the experiment.
          </div>
        )}
        {view === "lab" && compared.length > 0 && (
          <Compare runs={compared} onRemove={(id) => setCompare((c) => c.filter((x) => x !== id))}
            onClear={() => setCompare([])} />
        )}
        {ops.link !== "live" && !ops.hexes && <Offline url={GATEWAY_WS} />}
      </main>

      <MatchFeed matches={ops.matches} kpis={ops.kpis} selected={explainId} onSelect={setExplainId} />
    </div>
  );
}

function shortfall(c: Cell) {
  const gap = c[2] - c[1];
  return gap >= 1 ? `About ${Math.round(gap)} drivers short`
    : gap <= -1 ? `${Math.round(-gap)} more drivers than needed` : "Balanced";
}

/** "07:15–07:30" from the slot's naive NYC start time, shown as NYC time whatever the browser's zone. */
function slotLabel(iso: string) {
  const [h, m] = iso.slice(11, 16).split(":").map(Number);
  const end = h * 60 + m + 15;
  const hm = (x: number) => `${String(Math.floor(x / 60) % 24).padStart(2, "0")}:${String(x % 60).padStart(2, "0")}`;
  return `${hm(h * 60 + m)}–${hm(end)}`;
}

function balance(c: Cell) {
  const v = imbalance(c[1], c[2]);
  return v > 0.1 ? "More riders than drivers" : v < -0.1 ? "More drivers than riders" : "Balanced";
}
