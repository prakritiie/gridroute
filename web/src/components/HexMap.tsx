"use client";

import "maplibre-gl/dist/maplibre-gl.css";

import { MapboxOverlay, type MapboxOverlayProps } from "@deck.gl/mapbox";
import { H3HexagonLayer } from "@deck.gl/geo-layers";
import { LineLayer, ScatterplotLayer } from "@deck.gl/layers";
import type { PickingInfo } from "@deck.gl/core";
import { cellToLatLng } from "h3-js";
import { useEffect, useMemo, useState } from "react";
import { Map, useControl } from "react-map-gl/maplibre";

import { divColor, imbalance, seqColor, DEMAND_RAMP, SUPPLY_RAMP, type Layer, type RGB } from "@/lib/palette";
import type { Cell, Match, Reposition } from "@/lib/types";

const STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";
const PULSE_MS = 5000;
const PULSE: RGB = [255, 255, 255];

/** One repositioner flow between two res-7 hexes, drivers sent along it. Positions are [lng, lat]. */
interface Flow {
  from: [number, number];
  to: [number, number];
  n: number;
}

function flows(rep: Reposition | null): Flow[] {
  if (!rep) return [];
  const byPair = new globalThis.Map<string, number>();
  for (const [a, b] of rep.flows) byPair.set(`${a}>${b}`, (byPair.get(`${a}>${b}`) ?? 0) + 1);
  return [...byPair].map(([k, n]) => {
    const [a, b] = k.split(">");
    const [alat, alng] = cellToLatLng(a);
    const [blat, blng] = cellToLatLng(b);
    return { from: [alng, alat], to: [blng, blat], n };
  });
}

function DeckOverlay(props: MapboxOverlayProps) {
  const overlay = useControl(() => new MapboxOverlay(props));
  overlay.setProps(props);
  return null;
}

export interface HoverInfo {
  id: string;
  x: number;
  y: number;
}

interface Props {
  cells: Cell[];
  layer: Layer;
  max: number;
  matches: Match[];
  selected: string | null;
  onSelect: (cell: Cell | null) => void;
  onHover: (h: HoverInfo | null) => void;
  focus?: [number, number] | null;
  reposition?: Reposition | null;
}

export default function HexMap({ cells, layer, max, matches, selected, onSelect, onHover, focus, reposition }: Props) {
  const [beforeId, setBeforeId] = useState<string | undefined>();
  const [now, setNow] = useState(() => Date.now());

  // Animate only while the newest pulse is alive; each new match restarts the loop
  const latest = matches[0]?.at ?? 0;
  useEffect(() => {
    if (!latest) return;
    let raf = 0;
    const loop = () => {
      const t = Date.now();
      setNow(t);
      if (t - latest < PULSE_MS) raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [latest]);

  const visible = useMemo(
    () => cells.filter(([, s, d]) => (layer === "supply" ? s > 0
      : layer === "demand" ? d > 0 : layer === "forecast" ? d >= 0.5 : s + d > 0)),
    [cells, layer],
  );

  const layers = useMemo(() => {
    const fill = (c: Cell): RGB =>
      layer === "supply" ? seqColor(SUPPLY_RAMP, c[1], max)
        : layer === "demand" || layer === "forecast" ? seqColor(DEMAND_RAMP, c[2], max)
          : divColor(imbalance(c[1], c[2]));
    const repFlows = layer === "forecast" ? flows(reposition ?? null) : [];
    const sel = visible.find((c) => c[0] === selected) ?? (selected ? ([selected, 0, 0] as Cell) : null);
    // Interleaved mode: draw under the basemap's labels. MapboxOverlay reads `beforeId` off each layer,
    // but @deck.gl/mapbox doesn't export that prop type (LayerOverlayProps), so it goes in via a spread.
    const under: object = { beforeId };
    return [
      new H3HexagonLayer<Cell>({
        id: "hexes",
        data: visible,
        getHexagon: (c) => c[0],
        getFillColor: fill,
        coverage: 0.9,                 // the gap between cells is the "surface gap" between fills
        extruded: false,
        stroked: false,
        opacity: 0.88,
        pickable: true,
        autoHighlight: true,
        highlightColor: [255, 255, 255, 60],
        updateTriggers: { getFillColor: [layer, max] },
        transitions: { getFillColor: 350 },
        ...under,
      }),
      new H3HexagonLayer<Cell>({
        id: "selected",
        data: sel ? [sel] : [],
        getHexagon: (c) => c[0],
        filled: false,
        stroked: true,
        getLineColor: [255, 255, 255],
        lineWidthUnits: "pixels",
        getLineWidth: 2,
        ...under,
      }),
      new LineLayer<Flow>({
        id: "reposition",                  // the last repositioner cycle: idle drivers sent to a shortage
        data: repFlows,
        getSourcePosition: (f) => f.from,
        getTargetPosition: (f) => f.to,
        getColor: [255, 255, 255, 170],
        widthUnits: "pixels",
        getWidth: (f) => Math.min(6, 1.5 + Math.sqrt(f.n)),
      }),
      new ScatterplotLayer<Flow>({
        id: "reposition-ends",
        data: repFlows,
        getPosition: (f) => f.to,
        radiusUnits: "pixels",
        getRadius: 3.5,
        getFillColor: [255, 255, 255, 230],
      }),
      new ScatterplotLayer<[number, number]>({
        id: "focus",                       // the pickup of the match being explained
        data: focus ? [focus] : [],
        getPosition: (p) => [p[1], p[0]],
        stroked: true,
        filled: false,
        radiusUnits: "pixels",
        lineWidthUnits: "pixels",
        getRadius: 11,
        getLineWidth: 2.5,
        getLineColor: [255, 255, 255, 255],
      }),
      new ScatterplotLayer<Match>({
        id: "pulses",
        data: matches.filter((m) => now - m.at < PULSE_MS),
        getPosition: (m) => [m.pickup[1], m.pickup[0]],
        stroked: true,
        filled: true,
        radiusUnits: "pixels",
        lineWidthUnits: "pixels",
        getRadius: (m) => 3 + 20 * Math.min(1, (now - m.at) / PULSE_MS) ** 0.6,
        getFillColor: (m) => [...PULSE, Math.round(140 * (1 - (now - m.at) / PULSE_MS) ** 3)] as [number, number, number, number],
        getLineColor: (m) => [...PULSE, Math.round(220 * (1 - (now - m.at) / PULSE_MS))] as [number, number, number, number],
        getLineWidth: 1.5,
        updateTriggers: { getRadius: now, getFillColor: now, getLineColor: now },
      }),
    ];
  }, [visible, layer, max, matches, now, selected, beforeId, focus, reposition]);

  return (
    <Map
      initialViewState={{ latitude: 40.738, longitude: -73.955, zoom: window.innerWidth < 860 ? 10.4 : 11.3 }}
      mapStyle={STYLE}
      style={{ position: "absolute", inset: 0 }}
      attributionControl={{ compact: true }}
      onLoad={(e) => setBeforeId(e.target.getStyle().layers.find((l) => l.type === "symbol")?.id)}
    >
      <DeckOverlay
        interleaved
        layers={layers}
        onClick={(info: PickingInfo<Cell>) => onSelect(info.layer?.id === "hexes" && info.object ? info.object : null)}
        onHover={(info: PickingInfo<Cell>) =>
          onHover(info.layer?.id === "hexes" && info.object ? { id: info.object[0], x: info.x, y: info.y } : null)}
      />
    </Map>
  );
}
